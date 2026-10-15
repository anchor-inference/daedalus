"""The first-byte watchdog: a request that gets nothing back is sent again unchanged; one that spoke is never."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from protocore.contracts.llm import LLMRateLimitError, LLMTimeoutError, ProviderDeltaKind

from daedalus.config import FIRST_BYTE_TIMEOUT_SECONDS, ProviderConfig, first_byte_timeout_for, keyproxy_base
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from tests.unit.test_providers import RecordingSink, _chunk, _request, _sse

WINDOW = 0.2
"""Short enough to keep the tests quick, long enough that a loaded machine does not miss a reply sent at once."""

ANSWER = _sse([_chunk({"content": "Hello"}), _chunk({}, finish="stop"), {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}])

Handler = Callable[[httpx.Request, int], Awaitable[httpx.Response]]


def _provider(handler: Handler, *, timeout: float = WINDOW, retries: int = 1) -> tuple[OpenAICompatibleProvider, list[bytes]]:
    bodies: list[bytes] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return await handler(request, len(bodies))

    endpoint = ProviderEndpoint(id="grok", kind="openai_compat", base_url="https://x.test", first_byte_timeout_seconds=timeout, first_byte_retries=retries)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return OpenAICompatibleProvider(endpoint, client=client, usage_sink=RecordingSink()), bodies


async def _paced(parts: list[tuple[float, str]]) -> AsyncIterator[bytes]:
    for pause, text in parts:
        await asyncio.sleep(pause)
        yield text.encode()


def _streamed(parts: list[tuple[float, str]]) -> httpx.Response:
    return httpx.Response(200, content=_paced(parts), headers={"content-type": "text/event-stream"})


async def _collect(provider: OpenAICompatibleProvider) -> tuple[list[ProviderDeltaKind], str]:
    kinds: list[ProviderDeltaKind] = []
    text = ""
    async for delta in provider.stream_with_tools(_request()):
        kinds.append(delta.kind)
        if delta.kind is ProviderDeltaKind.text:
            text += delta.content or ""
    return kinds, text


async def test_no_headers_in_time_resends_the_identical_body(caplog: pytest.LogCaptureFixture) -> None:
    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            await asyncio.sleep(30)
        return httpx.Response(200, text=ANSWER, headers={"content-type": "text/event-stream"})

    provider, bodies = _provider(handler)
    with caplog.at_level(logging.WARNING, logger="daedalus.providers.openai_compat"):
        kinds, text = await _collect(provider)
    assert text == "Hello"
    assert len(bodies) == 2 and bodies[0] == bodies[1]  # the same bytes: the cached prefix is the same prefix
    # The heartbeat before the resend keeps the core's idle clock from adding the two windows together.
    assert kinds[0] is ProviderDeltaKind.progress
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "no headers" in said and "sending the identical request again (1 of 1)" in said


async def test_headers_without_a_body_line_in_time_also_resend() -> None:
    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return _streamed([(30, ANSWER)])
        return httpx.Response(200, text=ANSWER, headers={"content-type": "text/event-stream"})

    provider, bodies = _provider(handler)
    _, text = await _collect(provider)
    assert text == "Hello" and len(bodies) == 2 and bodies[0] == bodies[1]


async def test_silence_on_every_attempt_is_a_retryable_timeout() -> None:
    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("never answered")

    provider, bodies = _provider(handler, retries=1)
    with pytest.raises(LLMTimeoutError) as raised:
        await _collect(provider)
    assert len(bodies) == 2
    verdict = raised.value.classified  # type: ignore[attr-defined]
    assert verdict.reason == "timeout" and verdict.retryable is True
    assert "no response byte" in str(raised.value)


async def test_a_ping_comment_is_a_byte_so_a_silent_thinker_is_never_resent() -> None:
    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        return _streamed([(0, ": keepalive\n\n"), (WINDOW * 2, ANSWER)])

    provider, bodies = _provider(handler)
    kinds, text = await _collect(provider)
    assert text == "Hello" and len(bodies) == 1
    assert kinds[0] is ProviderDeltaKind.progress  # the keepalive itself, passed on as liveness


async def test_nothing_is_resent_once_the_first_chunk_arrived() -> None:
    first = 'data: {"choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
    rest = _sse([_chunk({"content": "lo"}), _chunk({}, finish="stop")])

    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        return _streamed([(0, first), (WINDOW * 2, rest)])

    provider, bodies = _provider(handler)
    _, text = await _collect(provider)
    # A stall after output is the core's idle watchdog's to judge: resending here would repeat "Hel".
    assert text == "Hello" and len(bodies) == 1


async def test_a_refusal_is_an_answer_and_is_not_resent() -> None:
    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down", "type": "rate_limit_error"}})

    provider, bodies = _provider(handler)
    with pytest.raises(LLMRateLimitError):
        await _collect(provider)
    assert len(bodies) == 1


async def test_zero_turns_the_watchdog_off() -> None:
    async def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        return _streamed([(WINDOW * 2, ANSWER)])

    provider, bodies = _provider(handler, timeout=0)
    _, text = await _collect(provider)
    assert text == "Hello" and len(bodies) == 1


def test_the_default_watches_hosted_endpoints_and_spares_the_operators_own_hardware() -> None:
    assert first_byte_timeout_for(ProviderConfig(kind="openai_compat", base_url=keyproxy_base() + "/grok/v1")) == FIRST_BYTE_TIMEOUT_SECONDS
    assert first_byte_timeout_for(ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com")) == FIRST_BYTE_TIMEOUT_SECONDS
    assert first_byte_timeout_for(ProviderConfig(kind="llamacpp", base_url="http://127.0.0.1:8080/v1")) == 0
    assert first_byte_timeout_for(ProviderConfig(kind="vllm", base_url="http://127.0.0.1:8000/v1")) == 0
    assert first_byte_timeout_for(ProviderConfig(kind="openai_compat", base_url=keyproxy_base() + "/local")) == 0
    assert first_byte_timeout_for(ProviderConfig(kind="llamacpp", base_url="http://127.0.0.1:8080/v1", first_byte_timeout_seconds=120)) == 120
    # Below the core's 90 s idle baseline, which would otherwise cut a request before this fires.
    assert FIRST_BYTE_TIMEOUT_SECONDS < 90


def test_the_registry_hands_the_setting_to_the_endpoint() -> None:
    from daedalus.config import RuntimeConfig, Settings
    from daedalus.providers.registry import ProviderRegistry

    config = RuntimeConfig(providers={
        "grok": ProviderConfig(base_url=keyproxy_base() + "/grok/v1"),
        "box": ProviderConfig(kind="llamacpp", base_url="http://box/v1", first_byte_retries=2),
    })
    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    grok, box = registry.get("grok").endpoint, registry.get("box").endpoint
    assert (grok.first_byte_timeout_seconds, grok.first_byte_retries) == (FIRST_BYTE_TIMEOUT_SECONDS, 1)
    assert (box.first_byte_timeout_seconds, box.first_byte_retries) == (0, 2)
    assert json.loads(json.dumps(config.providers["grok"].model_dump()))["first_byte_timeout_seconds"] is None
