"""Each model call's speed: time to first byte and first token, and the output rate over the generation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from protocore.contracts.llm import LLMProviderError, ProviderDeltaKind

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import CallTiming, OpenAICompatibleProvider, ProviderEndpoint, UsageRecord
from daedalus.stores.database import Database
from daedalus.stores.provider_speed import percentile, provider_speed, run_speeds, turn_speed
from daedalus.stores.sqlite import SqliteUsageSink
from tests.unit.test_providers import RecordingSink, _chunk, _request, _sse


async def _paced(parts: list[tuple[float, str]]) -> AsyncIterator[bytes]:
    for pause, text in parts:
        await asyncio.sleep(pause)
        yield text.encode()


def _streamed(parts: list[tuple[float, str]]) -> httpx.Response:
    return httpx.Response(200, content=_paced(parts), headers={"content-type": "text/event-stream"})


def _frame(chunk: dict) -> str:
    return _sse([chunk]).removesuffix("data: [DONE]\n\n")


def _provider(responses: list[httpx.Response] | None = None, *, handler=None, window: float = 0.0) -> tuple[OpenAICompatibleProvider, RecordingSink]:  # type: ignore[no-untyped-def]
    sink = RecordingSink()
    queue = list(responses or [])

    async def respond(request: httpx.Request) -> httpx.Response:
        return await handler(request) if handler is not None else queue.pop(0)

    endpoint = ProviderEndpoint(id="grok", kind="openai_compat", base_url="https://x.test", first_byte_timeout_seconds=window, first_byte_retries=1)
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), usage_sink=sink), sink


async def _drain(provider: OpenAICompatibleProvider) -> None:
    async for _ in provider.stream_with_tools(_request()):
        pass


def _usage(completion: int, reasoning: int = 0) -> str:
    return _sse([{"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": completion, "completion_tokens_details": {"reasoning_tokens": reasoning}}}])


async def test_a_streamed_answer_times_its_first_byte_its_first_token_and_its_rate() -> None:
    words = [_frame(_chunk({"content": f"w{i} "})) for i in range(4)]
    provider, sink = _provider([_streamed([
        (0, ": keepalive\n\n"),  # the first byte: the proxy is there before the model is
        (0.3, words[0]),         # the first token, after the model's start-up
        (0.15, words[1]), (0.15, words[2]), (0.15, words[3]),
        (0, _frame(_chunk({}, finish="stop")) + _usage(45)),
    ])])
    await _drain(provider)
    [record] = sink.records
    assert record.first_byte_ms is not None and record.first_token_ms is not None
    assert record.first_byte_ms < 250 <= 300 <= record.first_token_ms
    # 45 tokens over at least 0.45 s of generation: at most a hundred a second, and the start-up is not in it.
    assert record.output_tokens_per_second is not None and 20 < record.output_tokens_per_second <= 100


async def test_a_reply_of_only_tool_calls_is_timed_from_its_first_call_chunk() -> None:
    def piece(arguments: str, *, first: bool = False) -> str:
        call = {"index": 0, "function": {"arguments": arguments}}
        if first:
            call.update(id="call_1", function={"name": "t", "arguments": arguments})
        return _frame(_chunk({"tool_calls": [call]}))

    provider, sink = _provider([_streamed([
        (0.2, piece('{"a": "', first=True)), (0.2, piece("x" * 40)), (0.2, piece('"}')),
        (0, _frame(_chunk({}, finish="tool_calls")) + _usage(30)),
    ])])
    await _drain(provider)
    [record] = sink.records
    assert record.first_token_ms is not None and record.first_token_ms >= 200
    assert record.output_tokens_per_second is not None and record.output_tokens_per_second <= 30 / 0.4


async def test_reasoning_never_streamed_is_left_out_of_the_rate_and_a_short_reply_has_none() -> None:
    hidden, sink = _provider([_streamed([
        (0, _frame(_chunk({"content": "a"}))), (0.5, _frame(_chunk({"content": "b"}))),
        (0, _frame(_chunk({}, finish="stop")) + _usage(1_040, reasoning=1_000)),
    ])])
    await _drain(hidden)
    # Forty visible tokens over half a second, not a thousand and forty.
    assert sink.records[0].output_tokens_per_second is not None and sink.records[0].output_tokens_per_second <= 80
    short, sink = _provider([httpx.Response(200, text=_sse([_chunk({"content": "ok"}), _chunk({}, finish="stop"), {"choices": [], "usage": {"completion_tokens": 1}}]),
                                            headers={"content-type": "text/event-stream"})])
    await _drain(short)
    assert sink.records[0].first_token_ms is not None and sink.records[0].output_tokens_per_second is None


async def test_a_resent_request_is_timed_from_the_attempt_that_answered() -> None:
    answer = _sse([_chunk({"content": "Hello"}), _chunk({}, finish="stop"), {"choices": [], "usage": {"completion_tokens": 1}}])
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(30)
        return httpx.Response(200, text=answer, headers={"content-type": "text/event-stream"})

    provider, sink = _provider(handler=handler, window=0.3)
    await _drain(provider)
    [record] = sink.records
    assert attempts == 2
    # The silent window is in the call's duration, not in the provider's time to first token.
    assert record.duration_ms >= 300 and record.first_token_ms is not None and record.first_token_ms < record.duration_ms - 250


async def test_a_failed_attempt_and_a_cancelled_call_are_not_counted() -> None:
    failing, sink = _provider([httpx.Response(503, text='{"error": {"message": "overloaded"}}')])
    with pytest.raises(LLMProviderError):
        await _drain(failing)
    assert sink.records == []
    # The fallback that answered is booked with its own clock.
    fallback, fallback_sink = _provider([httpx.Response(200, text=_sse([_chunk({"content": "hi"}), _chunk({}, finish="stop"), {"choices": [], "usage": {"completion_tokens": 1}}]),
                                                        headers={"content-type": "text/event-stream"})])
    await _drain(fallback)
    assert len(fallback_sink.records) == 1 and fallback_sink.records[0].first_token_ms is not None
    cancelled, sink = _provider([_streamed([(0, _frame(_chunk({"content": "half"}))), (5, _frame(_chunk({}, finish="stop")) + _usage(2))])])
    stream = cancelled.stream_with_tools(_request())
    async for delta in stream:
        if delta.kind is ProviderDeltaKind.text:
            break
    await stream.aclose()  # type: ignore[attr-defined]
    assert sink.records == []


async def test_a_call_answered_whole_has_no_speed() -> None:
    provider, sink = _provider([httpx.Response(200, json={"choices": [{"message": {"content": "summary"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 400}})])
    await provider.complete_text(_request())
    [record] = sink.records
    assert (record.first_byte_ms, record.first_token_ms, record.output_tokens_per_second) == (None, None, None)


def test_the_clock_counts_from_the_attempt_and_rounds_to_milliseconds() -> None:
    timing = CallTiming(began=100.0, first_byte=100.05, first_token=101.0, last_token=103.0, streamed_reasoning=True)
    assert timing.measures({"output_tokens": 200, "reasoning_tokens": 50}) == (50, 1000, 100.0)
    assert CallTiming(began=1.0).measures({"output_tokens": 500}) == (None, None, None)


def test_percentiles_are_ranks_of_real_calls() -> None:
    values = [float(v) for v in range(1, 11)]
    assert percentile(values, 0.5) == 5.0 and percentile(values, 0.9) == 9.0 and percentile([], 0.5) is None


async def test_the_speed_summary_is_per_provider_and_model_over_its_window(settings: Settings, db: Database) -> None:
    sink = SqliteUsageSink(db)

    def record(provider: str, model: str, first_token: int | None, rate: float | None, *, first_byte: int | None = 40) -> UsageRecord:
        return UsageRecord(provider_id=provider, model=model, purpose="stream", raw={}, normalized={"output_tokens": 100}, cost_usd=0.0,
                           duration_ms=5_000, run_id=None, session_id=None, first_byte_ms=first_byte, first_token_ms=first_token, output_tokens_per_second=rate)

    for first_token, rate in ((800, 60.0), (900, 70.0), (1_000, 80.0), (4_000, None)):
        await sink.record(record("grok", "grok-5", first_token, rate))
    await sink.record(record("claude", "opus", 2_000, 50.0))
    await sink.record(record("claude", "opus", None, None, first_byte=None))  # answered whole: not a measured call
    # A call older than the window.
    await db.execute("UPDATE usage_events SET at = ? WHERE seq = 1", ((datetime.now(UTC) - timedelta(days=3)).isoformat(),))
    rows = {(r.provider_id, r.model): r for r in await provider_speed(db, hours=24)}
    grok = rows[("grok", "grok-5")]
    assert (grok.calls, grok.rated, grok.first_token_p50_ms, grok.first_token_p90_ms, grok.tokens_per_second_p50) == (3, 2, 1_000.0, 4_000.0, 70.0)
    assert grok.first_byte_p50_ms == 40.0
    assert rows[("claude", "opus")].calls == 1
    assert len(await provider_speed(db, hours=24 * 7)) == 2 and (await provider_speed(db, hours=24 * 7))[0].calls == 4
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    application = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    api = build_app(application, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        answer = await client.get("/api/usage/speed", params={"hours": 24, "provider_id": "grok", "model": "grok-5"}, headers={"X-Daedalus-Token": "tok"})
        assert answer.status_code == 200
        body = answer.json()
        assert body["hours"] == 24 and [m["model"] for m in body["models"]] == ["grok-5"]
        assert body["models"][0]["first_token_p50_ms"] == 1_000.0 and body["models"][0]["tokens_per_second_p50"] == 70.0
        recent = (await client.get("/api/usage", headers={"X-Daedalus-Token": "tok"})).json()["recent"]
        assert {"first_byte_ms", "first_token_ms", "output_tokens_per_second"} <= set(recent[0])


# A real run's timed calls, as ``usage_events`` holds them: (model, output, reasoning, duration ms, first
# token ms, rate). Short replies between tool calls, three long writes, one call whose reasoning was
# never streamed (its rate is over the visible output), one with a rate far above the rest.
REAL_RUN = [
    ("gpt-6.1-sol", 30, 0, 2493, 1995, 113.08), ("gpt-6.1-sol", 86, 0, 6775, 3683, 31.33), ("gpt-6.1-sol", 952, 0, 20833, 1975, 50.97),
    ("gpt-6.1-sol", 86, 0, 3895, 2349, 61.84), ("gpt-6.1-sol", 98, 0, 61290, 59311, 215.25), ("gpt-6.1-sol", 657, 516, 22701, 11699, 61.35),
    ("gpt-6.1-sol", 676, 92, 78556, 15810, 449.17), ("gpt-6.1-sol", 188, 0, 9180, 5596, 55.56), ("gpt-6.1-sol", 1702, 354, 36265, 10662, 67.09),
    ("gpt-6.1-sol", 1368, 38, 29757, 9683, 68.8), ("gpt-6.1-sol", 803, 0, 21552, 4770, 51.12), ("claude-opus-5-5", 405, 172, 4063, 2571, 156.63),
]


async def test_a_turn_speed_is_what_the_run_rows_on_usage_add_up_to(settings: Settings, db: Database) -> None:
    sink = SqliteUsageSink(db)

    def record(run: str, model: str, output: int, reasoning: int, duration: int, first_token: int | None, rate: float | None, purpose: str = "stream") -> UsageRecord:
        return UsageRecord(provider_id="p", model=model, purpose=purpose, raw={}, normalized={"output_tokens": output, "reasoning_tokens": reasoning}, cost_usd=0.0,
                           duration_ms=duration, run_id=run, session_id="s1", first_byte_ms=40, first_token_ms=first_token, output_tokens_per_second=rate)

    for row in REAL_RUN:
        await sink.record(record("r1", *row))
    # Not the run's generation: a call too short to time, the host's summary streamed in the run, another session's call.
    await sink.record(record("r1", "gpt-6.1-sol", 9, 0, 1500, 1400, None))
    await sink.record(record("r1", "gpt-6.1-sol", 3000, 0, 40000, 900, 75.0, purpose="compaction"))
    await sink.record(UsageRecord(provider_id="p", model="m", purpose="stream", raw={}, normalized={"output_tokens": 500}, cost_usd=0.0,
                                  duration_ms=9000, run_id="r1", session_id="other", first_byte_ms=40, first_token_ms=100, output_tokens_per_second=1.0))
    # A one-line answer: under twenty tokens is noise, and a run with no timed call has no speed at all.
    await sink.record(record("r2", "gpt-6.1-sol", 17, 0, 900, 500, 60.0))
    await sink.record(record("r3", "gpt-6.1-sol", 400, 0, 9000, None, None))
    speeds = await run_speeds(db, "s1", ["r1", "r2", "r3", "missing"])
    assert set(speeds) == {"r1"}
    speed = speeds["r1"]
    assert speed["calls"] == len(REAL_RUN) and speed["tokens"] == sum(row[1] for row in REAL_RUN) and speed["model"] == "gpt-6.1-sol"
    assert speed["rate"] == pytest.approx(speed["tokens"] / (speed["ms"] / 1000), rel=0.001)
    # The same figure from the run's rows as Usage lists them: the timed stream calls' output over that
    # output at each call's own rate, within five per cent.
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    application = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    api = build_app(application, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        recent = (await client.get("/api/usage", headers={"X-Daedalus-Token": "tok"})).json()["recent"]
    rows = [r for r in recent if r["run_id"] == "r1" and r["session_id"] == "s1" and r["purpose"] == "stream" and r["output_tokens_per_second"]]
    usage = sum(r["output_tokens"] for r in rows) / sum(r["output_tokens"] / r["output_tokens_per_second"] for r in rows)
    assert speed["rate"] == pytest.approx(usage, rel=0.05)
    # Not a mean of the rates: one fast short call does not lift a turn of long ordinary ones.
    assert speed["rate"] < sum(row[5] for row in REAL_RUN) / len(REAL_RUN)


def test_a_turn_with_no_timed_call_or_few_tokens_has_no_speed() -> None:
    assert turn_speed([]) is None
    assert turn_speed([{"model": "m", "output_tokens": 19, "output_tokens_per_second": 50.0}]) is None
    assert turn_speed([{"model": "m", "output_tokens": 20, "output_tokens_per_second": 50.0}]) == {"tokens": 20, "ms": 400, "rate": 50.0, "calls": 1, "model": "m"}
