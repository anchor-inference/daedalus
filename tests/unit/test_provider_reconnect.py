"""An endpoint that refuses connections is waited out, for a bounded stretch, before the run hears of it.

A deploy recreated the key proxy, and a run 62 steps deep stopped on ``All connection attempts failed``:
the core's two in-place retries were spent within about three seconds, while the proxy was still
starting. These tests drive the adapter against a transport that refuses for a while and then answers,
on a fake clock, so two minutes of waiting take no time.
"""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import (
    LLMObservabilityContext,
    LLMProviderError,
    LLMRequest,
    ProviderDeltaKind,
    ProviderFailureReason,
    provider_failure_of,
)
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.host.session_runner import HostEventType, SessionManager
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.reconnect import PATIENCE_SECONDS, Patience, proxy_unreachable, unreachable
from tests.unit.test_providers import _chunk, _sse

ANSWER = _sse([_chunk({"content": "Hello"}), _chunk({}, finish="stop"), {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}])
COMPLETION = '{"choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}'
PROXY_DOWN = '{"error": {"message": "upstream unreachable: ConnectError", "type": "proxy_error"}}'


class FakeTime:
    """A clock that moves only when the adapter sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def _refusing_for(seconds: float, clock: FakeTime, *, answer: str = ANSWER, proxy: bool = False) -> tuple[Any, list[bytes]]:
    """A transport that refuses every connection for ``seconds`` of the fake clock, then answers."""
    until = clock.now + seconds
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if clock.now < until:
            if proxy:
                return httpx.Response(502, text=PROXY_DOWN, headers={"content-type": "application/json"})
            raise httpx.ConnectError("All connection attempts failed", request=request) from ConnectionRefusedError(111, "Connection refused")
        sent.append(request.content)
        return httpx.Response(200, text=answer, headers={"content-type": "text/event-stream"})

    return handler, sent


def _provider(handler: Any, clock: FakeTime, notices: list[float | None] | None = None) -> OpenAICompatibleProvider:
    async def notice(endpoint_id: str, request: LLMRequest, retry_in: float | None) -> None:
        if notices is not None:
            notices.append(retry_in)

    endpoint = ProviderEndpoint(id="anthropic", kind="openai_compat", base_url="http://keyproxy.test/anthropic/v1", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), on_unreachable=notice)
    provider._sleep = clock.sleep
    provider._patience = lambda: Patience(clock=clock.time, jitter=lambda: 1.0)
    return provider


def _request() -> LLMRequest:
    return LLMRequest(model="m", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
                      observability=LLMObservabilityContext(tenant_id="t", session_id="s1", run_id="r1"))


async def _drain(provider: OpenAICompatibleProvider) -> list[Any]:
    return [delta async for delta in provider.stream_with_tools(_request())]


async def test_a_refusing_endpoint_is_waited_out_and_the_run_goes_on() -> None:
    clock = FakeTime()
    notices: list[float | None] = []
    handler, sent = _refusing_for(40.0, clock)
    deltas = await _drain(_provider(handler, clock, notices))
    assert deltas[-1].kind is ProviderDeltaKind.finish
    assert "".join(d.content or "" for d in deltas if d.kind is ProviderDeltaKind.text) == "Hello"
    assert len(sent) == 1, "the request went out exactly once, when the endpoint accepted it"
    assert 40.0 <= sum(clock.slept) < PATIENCE_SECONDS
    assert clock.slept == sorted(clock.slept) and clock.slept[0] == 1.0 and max(clock.slept) <= 15.0
    # One progress delta before every pause keeps the core's idle watchdog from cutting the wait.
    progress = [d for d in deltas if d.kind is ProviderDeltaKind.progress]
    assert len(progress) >= len(clock.slept)
    # The screen hears every pause, then that the endpoint is back.
    assert notices[:-1] == clock.slept and notices[-1] is None


async def test_the_key_proxys_own_unreachable_upstream_is_waited_out_too() -> None:
    clock = FakeTime()
    handler, sent = _refusing_for(10.0, clock, proxy=True)
    deltas = await _drain(_provider(handler, clock))
    assert deltas[-1].kind is ProviderDeltaKind.finish and len(sent) == 1
    assert sum(clock.slept) >= 10.0


async def test_an_endpoint_down_for_good_fails_after_about_two_minutes_with_the_connection_verdict() -> None:
    clock = FakeTime()
    notices: list[float | None] = []
    handler, sent = _refusing_for(10_000.0, clock)
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(handler, clock, notices))
    assert not sent
    assert sum(clock.slept) == pytest.approx(PATIENCE_SECONDS)
    verdict = provider_failure_of(caught.value)
    assert verdict is not None and verdict.reason is ProviderFailureReason.connection and verdict.retryable
    assert "All connection attempts failed" in str(caught.value)
    assert notices[-1] is None, "the waiting line is taken down when the wait is given up"


async def test_the_proxy_down_for_good_ends_on_its_own_502() -> None:
    clock = FakeTime()
    handler, _ = _refusing_for(10_000.0, clock, proxy=True)
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(handler, clock))
    assert "upstream unreachable" in str(caught.value)
    assert sum(clock.slept) == pytest.approx(PATIENCE_SECONDS)


@pytest.mark.parametrize(("status", "body"), [
    (402, '{"error": {"type": "billing_error", "message": "no credit"}}'),
    (400, '{"error": {"type": "invalid_request_error", "message": "bad"}}'),
    (502, '{"error": {"message": "upstream unreachable: ReadTimeout", "type": "proxy_error"}}'),
    (500, '{"error": {"message": "boom"}}'),
])
async def test_an_answer_is_never_waited_out(status: int, body: str) -> None:
    clock = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": "application/json"})

    with pytest.raises(LLMProviderError):
        await _drain(_provider(handler, clock))
    assert clock.slept == []


async def test_a_stream_cut_after_it_began_is_not_waited_out() -> None:
    clock = FakeTime()
    cut = 'data: {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": null}]}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=cut, headers={"content-type": "text/event-stream"})

    with pytest.raises(LLMProviderError):
        await _drain(_provider(handler, clock))
    assert clock.slept == []


async def test_a_reset_after_the_request_went_out_is_not_waited_out() -> None:
    clock = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset by peer", request=request) from ConnectionResetError(104, "reset")

    with pytest.raises(LLMProviderError):
        await _drain(_provider(handler, clock))
    assert clock.slept == []


async def test_a_certificate_failure_is_not_waited_out() -> None:
    clock = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed", request=request) from ssl.SSLCertVerificationError("bad cert")

    with pytest.raises(LLMProviderError):
        await _drain(_provider(handler, clock))
    assert clock.slept == []


async def test_a_stop_during_the_wait_ends_it_at_once() -> None:
    """The core's Stop cancels the turn's task; the pause is a plain sleep, so it ends on the spot."""
    attempts = 0
    waiting = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("All connection attempts failed", request=request) from ConnectionRefusedError(111, "refused")

    async def notice(endpoint_id: str, request: LLMRequest, retry_in: float | None) -> None:
        if retry_in is not None:
            waiting.set()

    endpoint = ProviderEndpoint(id="anthropic", kind="openai_compat", base_url="http://keyproxy.test/v1", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), on_unreachable=notice)
    provider._patience = lambda: Patience(first=60.0, longest=60.0)  # real sleeps, far longer than the test

    task = asyncio.create_task(_drain(provider))
    await asyncio.wait_for(waiting.wait(), 5)
    started = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert attempts == 1


async def test_a_completion_without_streaming_waits_the_same_way() -> None:
    clock = FakeTime()
    handler, sent = _refusing_for(20.0, clock, answer=COMPLETION)
    provider = _provider(handler, clock)
    response = await provider.complete_text(_request())
    assert "Hello" in response.model_dump_json() and len(sent) == 1
    assert sum(clock.slept) >= 20.0


def test_the_waits_grow_with_jitter_and_stay_inside_the_budget() -> None:
    now = [0.0]
    draws = iter([0.0, 1.0] * 50)
    patience = Patience(clock=lambda: now[0], jitter=lambda: next(draws))
    waits: list[float] = []
    while (pause := patience.next_wait()) is not None:
        waits.append(pause)
        now[0] += pause
    assert sum(waits) == pytest.approx(PATIENCE_SECONDS)
    assert waits[0] == 0.5 and waits[1] == 2.0, "half the ceiling at the lowest draw, the whole of it at the highest"
    assert max(waits) <= 15.0


def test_only_a_connect_that_never_sent_a_byte_counts() -> None:
    request = httpx.Request("POST", "http://keyproxy.test")
    assert unreachable(httpx.ConnectError("All connection attempts failed", request=request))
    assert unreachable(httpx.ConnectTimeout("timed out", request=request))
    assert not unreachable(httpx.ReadTimeout("slow", request=request))
    assert not unreachable(httpx.RemoteProtocolError("peer closed connection", request=request))
    assert proxy_unreachable(502, PROXY_DOWN)
    assert not proxy_unreachable(502, '{"error": {"message": "upstream unreachable: ReadError", "type": "proxy_error"}}')
    assert not proxy_unreachable(503, PROXY_DOWN)


async def test_the_runner_shows_the_wait_on_the_running_sessions_screen() -> None:
    told: list[tuple[str, Any]] = []

    async def notify(session_id: str, event: Any) -> None:
        told.append((session_id, event))

    state = SimpleNamespace(running=True, run_id="r1", session=SimpleNamespace(id="s1"))
    runner = SimpleNamespace(_states={"s1": state}, _notify_sinks=notify)
    await SessionManager._provider_unreachable(runner, "anthropic", _request(), 4.25)  # type: ignore[arg-type]
    await SessionManager._provider_unreachable(runner, "anthropic", _request(), None)  # type: ignore[arg-type]
    assert [e.type for _, e in told] == [HostEventType.PROVIDER_WAIT] * 2
    assert told[0][1].payload == {"provider": "anthropic", "retry_in": 4.2} and told[0][1].run_id == "r1"
    assert told[1][1].payload["retry_in"] is None
    state.running = False
    await SessionManager._provider_unreachable(runner, "anthropic", _request(), 1.0)  # type: ignore[arg-type]
    assert len(told) == 2, "a call outside a run (a title, a summary) has no screen to tell"
