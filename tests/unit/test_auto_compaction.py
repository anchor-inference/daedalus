"""Whole-history compaction between runs: the cut, the quoted operator messages, chunking, the trigger."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMRequest, LLMResponse, ProviderDelta, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import (
    SessionManager,
    compaction_cut,
    history_tokens,
    identifier_index,
    operator_quotes,
)
from daedalus.host.summaries import split_transcript
from daedalus.host.summary_calls import SummaryStalled, stream_text
from daedalus.providers.openai_compat import UsageRecord
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.support.summariser import streamed
from tests.support.waiting import until

SECTIONED = "## Standing instructions\ns\n## Stable facts\nf\n## Threads\n### t\nx\n## Closed threads\nc\n## Unknowns\nu\n## Identifiers\ni"


def _op(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})


def test_cut_never_splits_a_tool_exchange() -> None:
    history = [
        _op("one"),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Exec", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="r")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a1")]),
        _op("two"),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c2", name="Exec", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c2", content="r")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a2")]),
    ]
    assert compaction_cut(history, 0) == len(history)
    assert compaction_cut(history, 2) == 4  # walks back from 'tool result' to the turn start at "two"
    assert compaction_cut(history, 4) == 4
    assert compaction_cut(history, 100) == 0


def test_operator_messages_are_quoted_by_code() -> None:
    history = [_op("Never push to main."), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ok")]), _op("Use port 8765 for the API."), _op("recent one"), _op("recent two"), _op("recent three")]
    quotes = operator_quotes(history)
    assert "## Operator said (verbatim, oldest first)" in quotes and "- Never push to main." in quotes and "port 8765" in quotes
    assert "recent one" not in quotes  # the last three are printed whole by the tail
    assert operator_quotes([_op("only"), _op("two"), _op("three")]) == ""


def test_transcript_splits_on_lines_by_size() -> None:
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))
    parts = split_transcript(text, chunk_tokens=1000)  # ~4000 chars per part
    assert len(parts) >= 3 and "\n".join(parts) == text and all(len(p) <= 4200 for p in parts)
    assert split_transcript("short", 1000) == ["short"]


async def test_auto_compaction_keeps_the_tail_and_quotes_the_operator(settings: Settings, db: Database) -> None:
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 4
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("long")
    history = [_op("Rule: answer in Russian only."), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="да")]), _op("Second ask about /srv/x."), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="сделано")]), _op("latest ask"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ответ")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)  # the session's own model summarises its history
    calls: list[str] = []

    async def fake_complete(request: Any) -> LLMResponse:
        calls.append(request.messages[0].content_blocks[0].text)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.stream_with_tools = streamed(fake_complete)  # type: ignore[method-assign]
    # below the ratio: nothing happens
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 1000}, cost_usd=0.0, duration_ms=1, run_id="r0", session_id=state.session.id))
    await manager._maybe_auto_compact(state)
    assert not calls
    # above it: the older part becomes one summary, the last turn stays verbatim
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 120_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    seen: list[dict[str, Any]] = []

    async def hook(session_id: str, info: dict[str, Any]) -> None:
        seen.append(info)

    manager.compaction_hooks.append(hook)
    await manager._maybe_auto_compact(state)
    assert len(calls) == 1 and "Rule: answer in Russian only." in calls[0]
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert len(messages) == 3 and messages[0].metadata["daedalus.compaction"]["reason"] == "auto" and messages[0].metadata["daedalus.compaction"]["kept"] == 2
    body = messages[0].content_blocks[0].text  # type: ignore[union-attr]
    assert "Rule: answer in Russian only." in body and "## Recent operator messages (verbatim)" in body  # two operator turns: both fit the verbatim tail
    assert messages[1].content_blocks[0].text == "latest ask" and messages[2].content_blocks[0].text == "ответ"  # type: ignore[union-attr]
    assert seen and seen[0]["before_messages"] == 6 and seen[0]["after_messages"] == 3
    await manager.close()


def test_context_estimate_counts_retained_reasoning() -> None:
    message = Message(role=MessageRole.assistant, content_blocks=[], reasoning_content="thinking " * 100)
    assert history_tokens([message]) == 225


def test_identifiers_are_indexed_by_code() -> None:
    history = [
        _op("Deploy to /srv/state/worktrees/bot and open PR #42 on port 8765; see https://example.org/x?y=1."),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Read", arguments_json='{"path": "/srv/workspaces/abc/notes.md"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="/noise/from/results/only.txt 12345")]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Session 62d62b5f668d done in 2026.")]),
    ]
    index = identifier_index(history)
    for token in ("/srv/state/worktrees/bot", "PR #42", "8765", "https://example.org/x?y=1", "/srv/workspaces/abc/notes.md", "62d62b5f668d"):
        assert f"- {token}" in index, token
    assert "/noise/from/results/only.txt" not in index and "- 2026" not in index


def _writer(text: str, *, every: float, then_silent: bool = False) -> Any:
    """A streamed answer that arrives four characters at a time, ``every`` seconds apart; ``then_silent`` stops half-way and never ends."""

    async def stream(request: Any) -> Any:
        pieces = [text[i : i + 4] for i in range(0, len(text), 4)]
        for index, piece in enumerate(pieces):
            if then_silent and index == len(pieces) // 2:
                await asyncio.sleep(3600)
            await asyncio.sleep(every)
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=piece)
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")

    return stream


async def test_a_summary_still_being_written_is_never_cut(settings: Settings, db: Database) -> None:
    """A final merge on Opus writes for minutes. A 120 s limit on the whole call killed one two minutes
    into its answer and started it again from nothing; only silence ends a call now."""
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    provider = manager.providers.get(manager.providers.available()[0])
    manager.config.compaction.call_timeout_seconds = 0.5
    calls = 0
    steady = _writer(SECTIONED, every=0.05)  # about 1.3 s in all, never 0.5 s quiet

    def counted(request: Any) -> Any:
        nonlocal calls
        calls += 1
        return steady(request)

    provider.stream_with_tools = counted  # type: ignore[method-assign]
    started = time.monotonic()
    summary = await manager._summary_call(provider, "m", "prompt", "body", None)  # type: ignore[arg-type]
    assert time.monotonic() - started > 1.0, "the answer took longer than the silence bound twice over"
    assert summary == SECTIONED and calls == 1
    await manager.close()


async def test_a_silent_summariser_call_is_asked_once_more_then_given_up(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    provider = manager.providers.get(manager.providers.available()[0])
    manager.config.compaction.call_timeout_seconds = 0.05
    calls = 0

    async def silent(request: Any) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(3600)
        yield

    provider.stream_with_tools = silent  # type: ignore[method-assign]
    with pytest.raises(SummaryStalled, match="no byte within"):
        await manager._summary_call(provider, "m", "prompt", "body", None)  # type: ignore[arg-type]
    assert calls == 2
    await manager.close()


async def test_a_call_that_goes_quiet_half_way_is_asked_again_and_its_retry_used(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    provider = manager.providers.get(manager.providers.available()[0])
    manager.config.compaction.call_timeout_seconds = 0.3
    answers = iter([_writer(SECTIONED, every=0.01, then_silent=True), _writer(SECTIONED, every=0.01)])
    provider.stream_with_tools = lambda request: next(answers)(request)  # type: ignore[method-assign]
    assert await manager._summary_call(provider, "m", "prompt", "body", None) == SECTIONED  # type: ignore[arg-type]
    await manager.close()


async def test_a_call_that_trickles_past_its_ceiling_ends() -> None:
    provider = SimpleNamespace(stream_with_tools=_writer("word " * 200, every=0.02))
    request = LLMRequest(model="m", messages=[], max_tokens=100)
    with pytest.raises(SummaryStalled, match="past the ceiling") as stalled:
        await stream_text(provider, request, silence=0.5, ceiling=0.3)
    assert stalled.value.retryable is False  # the same model would only write as slowly again


async def test_a_compaction_does_not_fire_again_on_the_next_turn(settings: Settings, db: Database) -> None:
    """The trigger read the prompt size of the history the compaction had just replaced."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 2
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("repeat")
    # Long turns, so the summary really is smaller than what it replaces — as it is in a real session,
    # where the agent's words far outnumber the operator's: the operator's are quoted whole.
    history = [m for i in range(12) for m in (_op(f"ask {i}: " + "detail " * 40), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"answer {i}: " + "words " * 400)]))]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    calls: list[str] = []

    async def fake_complete(request: Any) -> LLMResponse:
        calls.append(request.messages[0].content_blocks[0].text)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.stream_with_tools = streamed(fake_complete)  # type: ignore[method-assign]
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 300_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    await manager._maybe_auto_compact(state)
    assert len(calls) == 1  # the history was over the ratio: it is compacted
    assert state.usage_floor_seq > 0 and state.observed_prompt_tokens > 0
    await manager._maybe_auto_compact(state)
    assert len(calls) == 1  # and the summariser is not paid a second time to compact nothing
    status = await manager.context_status(state)
    assert status["tokens"] == state.observed_prompt_tokens < 300_000  # what is reported describes the history that is left
    assert status["summaries"] == 1 and status["last_compaction"]["at"] and status["last_compaction"]["reason"]  # the Details tab says when it was
    manager._states.pop(state.session.id)
    restored = await manager.get_state(state.session.id)
    assert restored is not None
    assert (await manager.context_status(restored))["tokens"] == status["tokens"]
    assert (await manager.context_status(restored))["estimated"] is True
    await db.execute("DELETE FROM kv WHERE key = ?", (f"context_measurement:{state.session.id}",))
    manager._states.pop(state.session.id)
    restored = await manager.get_state(state.session.id)
    assert restored is not None
    assert 0 < (await manager.context_status(restored))["tokens"] < 300_000
    assert (await manager.context_status(restored))["estimated"] is True
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 310_000}, cost_usd=0.0, duration_ms=1, run_id="r2", session_id=state.session.id))
    assert (await manager.context_status(state))["tokens"] == 310_000  # a real call after it is read again


async def test_the_operators_message_does_not_wait_on_a_summariser(settings: Settings, db: Database) -> None:
    """Compaction runs between runs; only a history that no longer fits at all is compacted on the way in."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.min_messages = 2
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("submit")
    state.context_window = 100_000
    history = [_op("one"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a")]), _op("two"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="b")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    calls: list[str] = []

    async def record(st: Any, instructions: str, **kwargs: Any) -> str:
        calls.append(kwargs.get("reason", ""))
        return "summary"

    manager._compact_locked = record  # type: ignore[method-assign]
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 60_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    await manager._maybe_auto_compact(state, required_only=True)
    assert calls == []  # over the ratio, but it still fits: the run starts, the compaction waits
    await manager._maybe_auto_compact(state)
    assert calls == ["auto"]  # between runs it happens
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 120_000}, cost_usd=0.0, duration_ms=1, run_id="r2", session_id=state.session.id))
    await manager._maybe_auto_compact(state, required_only=True)
    assert calls == ["auto", "auto"]  # a history that no longer fits cannot be sent, so it goes first
    await manager.close()


async def test_a_run_cannot_start_while_the_history_is_being_rewritten(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    state = await manager.create_session("locked")
    started: list[str] = []

    async def fake_start(st: Any, message: Any, *, continue_turn: bool = False) -> str:
        started.append("run")
        return "run-id"

    manager._start_run_locked = fake_start  # type: ignore[method-assign]
    async with state.lock:  # what _compact_locked holds while it rewrites the history
        task = asyncio.create_task(manager._start_run(state, None))
        # Three turns of the loop: enough for the task to be scheduled and to reach the lock, and
        # not a duration — what holds it is the lock, which no amount of elapsed time opens.
        for _ in range(3):
            await asyncio.sleep(0)
        assert started == [] and not task.done()
    assert await task == "run-id" and started == ["run"]
    await manager.close()


async def test_two_callers_over_the_ratio_compact_once(settings: Settings, db: Database) -> None:
    """The ratio is tested before the lock, so two callers can both pass it — a manual compaction
    racing the one after a run, or the check on the way into a run racing it. The second would
    summarise a history the first has already replaced; it asks again behind the lock instead."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 4
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("twice")
    history = [_op("one"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a")]), _op("two"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="b")]), _op("three"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="c")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    calls: list[str] = []
    started = asyncio.Event()

    async def slow_complete(request: Any) -> LLMResponse:
        calls.append("summarised")
        started.set()
        await asyncio.sleep(0.05)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.stream_with_tools = streamed(slow_complete)  # type: ignore[method-assign]
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 120_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))

    await asyncio.gather(manager._maybe_auto_compact(state), manager._maybe_auto_compact(state))
    assert started.is_set()
    assert calls == ["summarised"], "the second caller summarised a history the first had already replaced"
    await manager.close()


async def test_a_compaction_between_runs_does_not_report_a_run(settings: Settings, db: Database) -> None:
    """It happens on a task of its own now: while it summarises, the session is not running — the
    next message does not wait on it, and the app is told what is really happening."""
    config = model_config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("status")
    assert not state.running
    state.compacting = {"reason": "auto", "stage": "summarising", "messages": 40}
    application = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    api = build_app(application, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        answer = await client.get(f"/api/sessions/{state.session.id}", headers={"X-Daedalus-Token": "tok"})
    assert answer.status_code == 200
    assert answer.json()["status"] == "compacting", "the stream says compacting; the polled state must not say running"
    await manager.close()


async def test_deleting_a_session_stops_the_compaction_its_last_run_left(settings: Settings, db: Database, caplog: Any) -> None:
    """A session deleted while the check after its last run was summarising: the progress event and
    the rewritten history both reference the session row, and the write failed on the foreign key."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 4
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("gone")
    history = [_op("one"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a")]), _op("two"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="b")]), _op("three"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="c")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    summarising = asyncio.Event()

    async def slow_complete(request: Any) -> LLMResponse:
        summarising.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    provider.stream_with_tools = streamed(slow_complete)  # type: ignore[method-assign]
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 120_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    state.auto_compaction = manager._spawn_background(manager._maybe_auto_compact(state), "auto-compact:test")
    await asyncio.wait_for(summarising.wait(), 5)
    assert await manager.delete_session(state.session.id)
    assert state.auto_compaction.done()
    assert not [r for r in caplog.records if "auto-compaction failed" in r.getMessage()]
    assert await db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (state.session.id,)) is None
    await manager.close()


async def test_a_compaction_waiting_on_the_lock_leaves_a_deleted_session_alone(settings: Settings, db: Database) -> None:
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.min_messages = 2
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("gone-before")
    history = [_op("one"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a")]), _op("two"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="b")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    calls: list[str] = []

    async def record(st: Any, instructions: str, **kwargs: Any) -> str:
        calls.append("compacted")
        return "summary"

    manager._compact_locked = record  # type: ignore[method-assign]
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": 120_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    manager._states.pop(state.session.id)  # what delete_session does before the rows go
    await manager._maybe_auto_compact(state)
    assert calls == []
    await manager.close()


def _long_history(turns: int = 12) -> list[Message]:
    return [m for i in range(turns) for m in (_op(f"ask {i}: " + "detail " * 400), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"answer {i}: " + "words " * 400)]))]


async def _measured(manager: SessionManager, state: Any, tokens: int, run_id: str) -> None:
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    await manager.usage.record(UsageRecord(provider_id=provider.endpoint.id, model="m", purpose="stream", raw={}, normalized={"input_tokens": tokens}, cost_usd=0.0, duration_ms=1, run_id=run_id, session_id=state.session.id))


async def test_a_failing_summariser_is_not_tried_again_after_every_turn(settings: Settings, db: Database) -> None:
    """An orchestrator on Grok: every part of the summary ran out of time, the compaction failed, and the
    next settled run tried again — four times in eleven minutes, each holding the session for three,
    while the history went on growing past the trigger. It now waits for real growth, or for time."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 2
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("orchestrator")
    state.context_window = 490_000
    history = _long_history()
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    calls = 0
    failing = True

    async def summarise(request: Any) -> LLMResponse:
        nonlocal calls
        calls += 1
        if failing:
            raise RuntimeError("the summariser gave up")
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.stream_with_tools = streamed(summarise)  # type: ignore[method-assign]
    await _measured(manager, state, 246_702, "r1")  # the prompt that first crossed 0.5 x 490k
    await manager._maybe_auto_compact(state)
    assert calls == 1 and state.compaction_hold is not None and state.compaction_hold["reason"] == "failed"
    for turn, tokens in enumerate((248_968, 250_808, 252_580)):  # the next three turns, ~2k each
        await _measured(manager, state, tokens, f"r{turn + 2}")
        await manager._maybe_auto_compact(state)
    assert calls == 1, "a compaction that failed was tried again on the next turn"
    await _measured(manager, state, 246_702 + 49_000, "r9")  # a tenth of the window more
    await manager._maybe_auto_compact(state)
    assert calls == 2 and state.compaction_hold["failures"] == 2
    await _measured(manager, state, 296_000, "r10")
    await manager._maybe_auto_compact(state)
    assert calls == 2
    state.compaction_hold["until"] = 0.0  # the wait after the second failure has passed
    failing = False
    await manager._maybe_auto_compact(state)
    assert calls == 3 and state.compaction_hold is None  # it worked: the next crossing compacts at once
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert messages[0].metadata["daedalus.compaction"]["reason"] == "auto"
    await manager.close()


async def test_a_history_that_does_not_fit_is_compacted_even_while_the_retry_waits(settings: Settings, db: Database) -> None:
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.min_messages = 2
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("full")
    state.context_window = 100_000
    history = _long_history(2)
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    calls: list[str] = []

    async def record(st: Any, instructions: str, **kwargs: Any) -> str:
        calls.append("compacted")
        return "summary"

    manager._compact_locked = record  # type: ignore[method-assign]
    state.compaction_hold = {"tokens": 60_000, "failures": 1, "until": float("inf"), "reason": "failed"}
    await _measured(manager, state, 101_000, "r1")
    await manager._maybe_auto_compact(state, required_only=True)
    assert calls == ["compacted"]  # without it the provider refuses the prompt outright
    await manager.close()


async def test_a_compaction_that_frees_nothing_is_reported_and_not_repeated(settings: Settings, db: Database, caplog: Any) -> None:
    """When what is left after a compaction is still over the trigger — the kept tail or the fixed part
    of the prompt is that large — the next turn would summarise again and change nothing."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.min_messages = 2
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("fixed")
    state.context_window = 100_000
    history = _long_history(2)
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    calls: list[str] = []

    async def compacts_nothing(st: Any, instructions: str, **kwargs: Any) -> str:
        calls.append("compacted")  # the history is rewritten, the prompt stays as large
        return "summary"

    manager._compact_locked = compacts_nothing  # type: ignore[method-assign]
    await _measured(manager, state, 60_000, "r1")
    await manager._maybe_auto_compact(state)
    assert calls == ["compacted"]
    assert state.compaction_hold is not None and state.compaction_hold["reason"] == "freed_little"
    assert any("freed only 0 of 60000" in r.getMessage() for r in caplog.records)
    await _measured(manager, state, 62_000, "r2")
    await manager._maybe_auto_compact(state)
    assert calls == ["compacted"], "a compaction that freed nothing ran again on the next turn"
    assert state.compaction_hold["until"] == float("inf")  # no clock lets it retry: only growth does
    await _measured(manager, state, 70_000, "r3")
    await manager._maybe_auto_compact(state)
    assert calls == ["compacted", "compacted"]
    await manager.close()


async def _ahead(settings: Settings, db: Database, title: str) -> tuple[SessionManager, Any, Any, list[str], asyncio.Event, list[Any]]:
    """A session on a 100k window at 40k tokens: past 0.7 of its 50k trigger, so the summary starts ahead."""
    config = model_config()
    config.compaction.auto_ratio = 0.5
    config.compaction.background_share = 0.7
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 4
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session(title)
    state.context_window = 100_000
    history = _long_history(3)
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    _, preset = manager.config.preset()
    provider = manager.providers.get(preset.provider)
    calls: list[str] = []
    release = asyncio.Event()
    release.set()

    async def summarise(request: Any) -> LLMResponse:
        calls.append(request.messages[0].content_blocks[0].text)
        await release.wait()
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.stream_with_tools = streamed(summarise)  # type: ignore[method-assign]
    events: list[Any] = []

    async def sink(session_id: str, event: Any) -> None:
        events.append(event)

    manager.add_sink(sink)
    return manager, state, provider, calls, release, events


async def _turn(manager: SessionManager, state: Any, text: str) -> None:
    """One more exchange appended to the history, the way a run's persist leaves it."""
    history = [*await manager.sessions.list_messages(state.session.id, "daedalus", limit=100), _op(text), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"done: {text}")])]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)


async def test_the_summary_is_written_ahead_without_the_lock_and_adopted_at_the_trigger(settings: Settings, db: Database) -> None:
    """Eight minutes of 'compacting' on a 980k session: the operator waited on the whole summariser.
    It is written in the background before the trigger now, and reaching the trigger only adopts it."""
    from daedalus.host.session_runner import HostEventType, _status_word

    manager, state, _provider, calls, release, events = await _ahead(settings, db, "ahead")
    release.clear()
    await _measured(manager, state, 40_000, "r1")
    await manager._maybe_auto_compact(state)  # returns at once: the summary is written on its own task
    assert state.prepared_compaction is not None
    await until(lambda: calls)
    assert not state.lock.locked(), "the session lock is held while the summary is written"
    assert state.compacting is None and _status_word(state) == "idle", "a background summary is not a blocking compaction"
    assert state.background_compacting is not None and state.background_compacting["background"] is True
    assert (await manager.list_sessions(limit=5))[0]["status"] == "idle"
    await _turn(manager, state, "a turn while it is written")  # the session goes on meanwhile
    release.set()
    await state.prepared_compaction.task
    assert state.background_compacting is None
    background = [e.payload["compaction"] for e in events if getattr(e, "type", None) == HostEventType.COMPACTION_BACKGROUND]
    assert background[0]["stage"] == "summarising" and background[-1] is None
    assert not [e for e in events if getattr(e, "payload", {}).get("reason") == "compaction_progress"], "the background summary was announced as a blocking compaction"

    await _turn(manager, state, "the turn that crosses the trigger")
    await _measured(manager, state, 60_000, "r2")
    await manager._maybe_auto_compact(state)
    assert len(calls) == 1, "the summary written ahead was not used; the trigger summarised again"
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    record = messages[0].metadata["daedalus.compaction"]
    assert record["messages"] == 4 and record["kept"] == 6  # the prefix it summarised; everything after it kept as it is
    assert [m.content_blocks[0].text for m in messages[-2:]] == ["the turn that crosses the trigger", "done: the turn that crosses the trigger"]  # type: ignore[union-attr]
    assert state.prepared_compaction is None
    await manager.close()


async def test_a_summary_written_ahead_is_dropped_when_the_history_was_rewritten(settings: Settings, db: Database) -> None:
    manager, state, _provider, calls, _release, _events = await _ahead(settings, db, "reverted")
    await _measured(manager, state, 40_000, "r1")
    await manager._maybe_auto_compact(state)
    assert state.prepared_compaction is not None
    await state.prepared_compaction.task
    # The operator undoes the second exchange, which the summary written ahead stands for.
    rows = await manager.sessions.list_transcript(state.session.id)
    second = [m for m in rows if m.role is MessageRole.user][1]
    await manager.revert(state.session.id, int(second.metadata["daedalus.seq"]))
    for text in ("again one", "again two"):
        await _turn(manager, state, text)
    await _measured(manager, state, 60_000, "r2")
    await manager._maybe_auto_compact(state)
    assert len(calls) == 2, "a summary of turns the operator undid was adopted"
    assert "ask 1:" not in calls[1] and "again one" in calls[1]
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert "ask 1:" not in messages[0].content_blocks[0].text  # type: ignore[union-attr]
    await manager.close()


async def test_a_summary_written_ahead_is_dropped_when_its_prefix_changed_without_a_rewrite_mark(settings: Settings, db: Database) -> None:
    """The core's own compaction inside a run replaces messages without the host's rewrite counter."""
    manager, state, _provider, calls, _release, _events = await _ahead(settings, db, "core-compacted")
    await _measured(manager, state, 40_000, "r1")
    await manager._maybe_auto_compact(state)
    await state.prepared_compaction.task
    history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    rewritten = [_op("[the core's summary of the first turns]"), *history[2:]]
    await manager.sessions.replace_messages(state.session.id, "daedalus", rewritten)
    await _turn(manager, state, "next")
    await _measured(manager, state, 60_000, "r2")
    await manager._maybe_auto_compact(state)
    assert len(calls) == 2 and "the core's summary of the first turns" in calls[1]
    await manager.close()
