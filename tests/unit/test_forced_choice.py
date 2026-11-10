"""The host provider asks in words for a forced call the model refuses, on OpenRouter, Zen and the like.

Claude Opus 5.5, Claude Sonnet 5.5 and the 5.1 line refuse a forced ``tool_choice`` with a 400 on every
route that serves them. The fake upstream here refuses exactly that shape, as OpenRouter relays it;
nothing leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import CacheBreakpoint, LLMProviderError, LLMRequest, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema

from daedalus.config import keyproxy_base
from daedalus.providers import forced_choice
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint

REFUSAL = json.dumps({"error": {"code": 400, "message": "Provider returned error", "metadata": {"raw": json.dumps({
    "type": "error", "error": {"type": "invalid_request_error", "message": 'tool_choice: type "tool" and "any" are not supported for this model.'}})}}})


def _sse(chunks: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


CALL = _sse([
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{\"path\": \"x\"}"}}]}, "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
])


@pytest.fixture(autouse=True)
def _nothing_learned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forced_choice, "_learned", set())


def _provider(seen: list[dict[str, Any]], *, kind: str = "openrouter", base_url: str = "https://openrouter.test/api/v1") -> OpenAICompatibleProvider:
    """A provider whose upstream refuses any forced tool_choice, the way these models do."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("tool_choice") not in (None, "auto", "none"):
            return httpx.Response(400, text=REFUSAL, headers={"content-type": "application/json"})
        if body.get("stream"):
            return httpx.Response(200, text=CALL, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    endpoint = ProviderEndpoint(id=kind, kind=kind, base_url=base_url, api_key="k")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _request(model: str, **extra: Any) -> LLMRequest:
    tools = [ToolDefinition(name=name, description="d", parameters=ToolParameterSchema(properties={})) for name in ("Read", "Exec")]
    messages = [Message(role=MessageRole.system, content_blocks=[TextBlock(text="Be terse.")]),
                Message(role=MessageRole.user, content_blocks=[TextBlock(text="read x")])]
    breakpoints = [CacheBreakpoint(message_index=0, cache_control_type="ephemeral", rationale="system_prefix"),
                   CacheBreakpoint(message_index=1, cache_control_type="ephemeral", rationale="last_message")]
    return LLMRequest(model=model, messages=messages, tools=tools, extra={"cache_breakpoints": breakpoints, **extra})


async def _stream(provider: OpenAICompatibleProvider, request: LLMRequest) -> list[Any]:
    return [delta async for delta in provider.stream_with_tools(request)]


@pytest.mark.parametrize("model", ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "claude-fable-5-1"])
async def test_a_forced_tool_goes_out_once_as_a_closing_message(model: str) -> None:
    seen: list[dict[str, Any]] = []
    provider = _provider(seen)
    deltas = await _stream(provider, _request(model, forced_tool_choice="Read"))
    assert len(seen) == 1 and "tool_choice" not in seen[0]
    assert seen[0]["messages"][-1] == {"role": "user", "content": "Call the `Read` tool now. Reply with that tool call only: no other call and no prose."}
    assert any(d.kind is ProviderDeltaKind.tool_use_start and d.tool_name == "Read" for d in deltas)
    # Everything before the closing message, the cache markers included, is the unforced step's request.
    plain: list[dict[str, Any]] = []
    await _stream(_provider(plain), _request(model))
    assert seen[0]["messages"][:-1] == plain[0]["messages"]
    assert {k: v for k, v in seen[0].items() if k != "messages"} == {k: v for k, v in plain[0].items() if k != "messages"}


async def test_a_required_call_asks_for_some_tool_on_zen() -> None:
    seen: list[dict[str, Any]] = []
    await _stream(_provider(seen, kind="opencode", base_url="https://zen.test/v1"), _request("claude-sonnet-5-5", tool_choice_required=True))
    assert len(seen) == 1 and "tool_choice" not in seen[0] and "one of the available tools" in seen[0]["messages"][-1]["content"]


async def test_a_model_that_takes_a_forced_choice_still_gets_it() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=CALL, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(ProviderEndpoint(id="or", kind="openrouter", base_url="https://openrouter.test/api/v1", api_key="k"),
                                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _stream(provider, _request("anthropic/claude-opus-5", forced_tool_choice="Read"))
    assert seen[0]["tool_choice"] == {"type": "function", "function": {"name": "Read"}}


async def test_a_model_not_yet_known_to_refuse_is_learned_from_the_refusal() -> None:
    seen: list[dict[str, Any]] = []
    provider = _provider(seen)
    deltas = await _stream(provider, _request("anthropic/claude-opus-6", forced_tool_choice="Read"))
    assert [body.get("tool_choice") for body in seen] == [{"type": "function", "function": {"name": "Read"}}, None]
    assert seen[1]["messages"][:-1] == seen[0]["messages"] and "`Read`" in seen[1]["messages"][-1]["content"]
    assert any(d.kind is ProviderDeltaKind.tool_use_start for d in deltas)
    assert not forced_choice.forces_tools("claude-opus-6")
    seen.clear()
    await _stream(provider, _request("anthropic/claude-opus-6", tool_choice_required=True))
    assert len(seen) == 1 and "tool_choice" not in seen[0]


async def test_a_non_streamed_call_learns_the_same_way() -> None:
    seen: list[dict[str, Any]] = []
    provider = _provider(seen)
    answer = await provider.complete_text(_request("anthropic/claude-opus-6", forced_tool_choice="Read"))
    assert answer.message.content_blocks[0].text == "ok" and [body.get("tool_choice") is None for body in seen] == [False, True]


async def test_another_400_is_not_read_as_the_forced_choice_refusal() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(400, text=json.dumps({"error": {"message": "invalid tool schema"}}))

    provider = OpenAICompatibleProvider(ProviderEndpoint(id="or", kind="openrouter", base_url="https://openrouter.test/api/v1", api_key="k"),
                                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMProviderError, match="invalid tool schema"):
        await _stream(provider, _request("anthropic/claude-opus-6", forced_tool_choice="Read"))
    assert len(seen) == 1 and forced_choice.forces_tools("claude-opus-6")


async def test_the_key_proxy_claude_routes_get_the_forced_choice_as_it_is() -> None:
    # The proxy's Messages translation steers these itself, and withholds the steered reply's thinking from
    # replay; steering here as well would hide the forced step from it.
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=CALL, headers={"content-type": "text/event-stream"})

    for upstream in ("claude", "anthropic"):
        provider = OpenAICompatibleProvider(ProviderEndpoint(id=upstream, kind="openai_compat", base_url=f"{keyproxy_base()}/{upstream}/v1"),
                                            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await _stream(provider, _request("claude-opus-5-5", forced_tool_choice="Read"))
    assert [body["tool_choice"] for body in seen] == [{"type": "function", "function": {"name": "Read"}}] * 2


def test_model_names_are_read_in_anthropic_spelling() -> None:
    assert forced_choice.canonical_model("anthropic/claude-sonnet-5.5") == "claude-sonnet-5-5"
    assert not forced_choice.forces_tools("Anthropic/Claude-Opus-5.5:thinking")
    assert forced_choice.forces_tools("anthropic/claude-opus-5") and forced_choice.forces_tools("deepseek-v4-flash")
