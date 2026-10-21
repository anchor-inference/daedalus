"""An OpenAI-compatible stream is finished only when it says so, and a cut tool call never runs.

A body that ended with neither ``[DONE]`` nor a finish reason used to be read as an ordinary stop: a cut
answer was stored as final, and a half-written call was dispatched with its braces repaired.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from protocore.conformance import assert_adapter_failure
from protocore.contracts.llm import (
    PROVIDER_ADVANCE_REASONS,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    ProviderDeltaKind,
    ProviderFailure,
    ProviderFailureReason,
    provider_failure_of,
)
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint


def _frames(chunks: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _provider(body: str, status: int = 200, headers: dict[str, str] | None = None) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream", **(headers or {})})

    endpoint = ProviderEndpoint(id="direct", kind="openai_compat", base_url="https://x.test", api_key="k")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _request() -> LLMRequest:
    return LLMRequest(model="m", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])])


async def _drain(provider: OpenAICompatibleProvider) -> list[Any]:
    return [delta async for delta in provider.stream_with_tools(_request())]


CUT_WRITE = [
    _chunk({"content": "Writing it now."}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Write", "arguments": '{"path": "a.txt", "content": "half'}}]}),
]


async def test_a_body_that_ends_without_done_or_finish_is_a_retryable_connection_failure() -> None:
    seen: list[Any] = []
    with pytest.raises(LLMProviderError) as caught:
        async for delta in _provider(_frames(CUT_WRITE)).stream_with_tools(_request()):
            seen.append(delta)
    verdict = caught.value.classified  # type: ignore[attr-defined]
    assert verdict.reason == "connection" and verdict.retryable is True
    # The cut call was never closed, so nothing downstream can dispatch it.
    assert not any(d.kind in (ProviderDeltaKind.tool_use_stop, ProviderDeltaKind.finish) for d in seen)


async def test_a_last_frame_cut_inside_its_json_is_a_cut_not_a_skip() -> None:
    body = _frames([_chunk({"content": "Hello"})]) + 'data: {"choices": [{"index": 0, "delta": {"content": " wor'
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(body))
    assert caught.value.classified.reason == "connection"  # type: ignore[attr-defined]


async def test_a_finish_reason_without_done_still_finishes() -> None:
    deltas = await _drain(_provider(_frames([_chunk({"content": "Hello"}), _chunk({}, finish="stop")])))
    assert deltas[-1].kind == ProviderDeltaKind.finish


async def test_the_key_proxys_truncation_error_is_a_connection_failure() -> None:
    body = _frames([*CUT_WRITE, {"error": {"message": "the Codex stream ended before its final event", "type": "stream_truncated", "code": "stream_truncated"}}])
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(body))
    assert caught.value.classified.reason == "connection" and caught.value.classified.retryable  # type: ignore[attr-defined]


@pytest.mark.parametrize("finish", ["tool_calls", "length", "content_filter"])
async def test_a_cut_call_is_never_offered_for_dispatch_whatever_the_finish(finish: str) -> None:
    deltas = await _drain(_provider(_frames([*CUT_WRITE, _chunk({}, finish=finish)]) + "data: [DONE]\n\n"))
    stop = next(d for d in deltas if d.kind == ProviderDeltaKind.tool_use_stop)
    # The core's output-cap recovery takes every call flagged this way under any finish, and never runs it.
    assert stop.args_partial_truncated and stop.truncated_by_output_cap


async def test_a_finished_call_written_slightly_wrong_still_runs_under_a_tool_use_finish() -> None:
    chunks = [_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"path": "a.txt",}'}}]}), _chunk({}, finish="tool_calls")]
    stop = next(d for d in await _drain(_provider(_frames(chunks) + "data: [DONE]\n\n")) if d.kind == ProviderDeltaKind.tool_use_stop)
    assert stop.tool_input_final == {"path": "a.txt"} and not stop.truncated_by_output_cap


async def test_a_cut_call_under_a_plain_stop_is_left_to_the_truncated_call_recovery() -> None:
    deltas = await _drain(_provider(_frames([*CUT_WRITE, _chunk({}, finish="stop")]) + "data: [DONE]\n\n"))
    stop = next(d for d in deltas if d.kind == ProviderDeltaKind.tool_use_stop)
    assert stop.args_partial_truncated and not stop.truncated_by_output_cap


async def test_a_call_with_no_arguments_in_a_capped_answer_is_the_cut_one() -> None:
    chunks = [_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Write", "arguments": ""}}]}), _chunk({}, finish="length")]
    stop = next(d for d in await _drain(_provider(_frames(chunks) + "data: [DONE]\n\n")) if d.kind == ProviderDeltaKind.tool_use_stop)
    assert stop.truncated_by_output_cap
    chunks[-1] = _chunk({}, finish="tool_calls")
    stop = next(d for d in await _drain(_provider(_frames(chunks) + "data: [DONE]\n\n")) if d.kind == ProviderDeltaKind.tool_use_stop)
    assert not stop.truncated_by_output_cap and not stop.args_partial_truncated


async def test_a_rate_limit_in_a_stream_chunk_is_a_rate_limit_with_its_wait() -> None:
    body = _frames([{"error": {"message": "slow down", "type": "upstream_error", "code": "rate_limit_exceeded", "retry_after_seconds": 11.054}}])
    with pytest.raises(LLMRateLimitError) as caught:
        await _drain(_provider(body))
    verdict = caught.value.classified  # type: ignore[attr-defined]
    assert verdict.reason == "rate_limit" and verdict.retry_after_seconds == 11.054


@pytest.mark.parametrize("code", ["insufficient_quota", "usage_not_included", "credit_balance_exhausted"])
async def test_a_spent_quota_in_a_stream_chunk_is_not_retried(code: str) -> None:
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(_frames([{"error": {"message": "spent", "type": "upstream_error", "code": code}}])))
    verdict = caught.value.classified  # type: ignore[attr-defined]
    assert verdict.reason == "billing" and verdict.retryable is False


async def test_a_spent_chatgpt_window_refused_with_429_is_not_retried() -> None:
    body = json.dumps({"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached", "resets_at": 1}})
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(body, status=429))
    assert caught.value.classified.reason == "billing"  # type: ignore[attr-defined]


async def test_a_429_carries_its_retry_after_to_the_core() -> None:
    body = json.dumps({"error": {"type": "rate_limit_error", "message": "slow"}})
    with pytest.raises(LLMRateLimitError) as caught:
        await _drain(_provider(body, status=429, headers={"retry-after": "42"}))
    assert caught.value.classified.retry_after_seconds == 42  # type: ignore[attr-defined]


async def test_an_overloaded_server_in_a_stream_chunk_stays_retryable() -> None:
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(_frames([{"error": {"message": "busy", "code": "server_is_overloaded"}}])))
    verdict = caught.value.classified  # type: ignore[attr-defined]
    assert verdict.reason == "overloaded" and verdict.retryable


async def test_a_context_overflow_in_a_stream_chunk_is_a_context_error() -> None:
    from protocore.contracts.llm import LLMContextWindowExceeded

    with pytest.raises(LLMContextWindowExceeded):
        await _drain(_provider(_frames([{"error": {"message": "Your input exceeds the context window of this model.", "code": "context_length_exceeded"}}])))


async def test_an_endpoint_that_cannot_be_reached_carries_the_cores_connection_verdict() -> None:
    """A refused connection used to be raised bare, and an error without a verdict never moves the
    fallback chain: a dead endpoint with a healthy fallback ended the run instead of stepping down."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request) from ConnectionRefusedError(111, "Connection refused")

    endpoint = ProviderEndpoint(id="direct", kind="openai_compat", base_url="https://x.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(LLMProviderError) as caught:
        await _drain(provider)
    assert_adapter_failure(caught.value)
    verdict = provider_failure_of(caught.value)
    assert verdict is not None and verdict.reason is ProviderFailureReason.connection and verdict.retryable
    assert verdict.reason in PROVIDER_ADVANCE_REASONS


async def test_every_refusal_carries_the_cores_own_verdict_type() -> None:
    with pytest.raises(LLMProviderError) as caught:
        await _drain(_provider(json.dumps({"error": {"type": "billing_error", "message": "no credit"}}), status=402))
    assert_adapter_failure(caught.value)
    assert isinstance(caught.value.classified, ProviderFailure)
