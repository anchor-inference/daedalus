"""Claude thinking survives a tool step: carried to the bot, kept, and sent back where the model wrote it.

Claude Code sends every earlier ``thinking`` and ``redacted_thinking`` block back verbatim on each
request of a tool loop and across user turns, interleaved with the text and tool calls in their
original order, and drops a turn's blocks only once the conversation has moved to another model. The
key proxy hands each finished block to the bot as a ``reasoning_details`` entry, the bot keeps it on
the assistant message, and the proxy puts it back in place. The streams here are shaped like the
Messages API's own; nothing leaves the process.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock
from protocore.providers.openai_compat import REASONING_ENVELOPE

from daedalus.config import keyproxy_base
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
if str(KEYPROXY_DIR) not in sys.path:
    sys.path.insert(0, str(KEYPROXY_DIR))


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


subs = _load("subscriptions")
claude_mod = _load("claude")
proxy = _load("proxy")

# A turn laid out the way Claude Code's loopback capture showed one: thinking and redacted thinking
# first, a line of text, a progress thinking block, then the call.
TURN: list[dict[str, Any]] = [
    {"type": "thinking", "thinking": "", "signature": "SIG-a"},
    {"type": "redacted_thinking", "data": "REDACTED-1"},
    {"type": "text", "text": "Checking the file."},
    {"type": "thinking", "thinking": "", "signature": "SIG-b"},
    {"type": "tool_use", "id": "toolu_1", "name": "mcp_Read", "input": {"path": "a.txt"}},
]


def _events(content: list[dict[str, Any]], stop: str = "tool_use", thinking_tokens: int = 640) -> list[dict[str, Any]]:
    """The Messages stream that produces ``content``."""
    events: list[dict[str, Any]] = [{"type": "message_start", "message": {"usage": {"input_tokens": 900, "cache_read_input_tokens": 0}}}]
    for index, block in enumerate(content):
        kind = block["type"]
        if kind == "thinking":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            if block["thinking"]:
                events.append({"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": block["thinking"]}})
            if block.get("signature"):
                events.append({"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": block["signature"]}})
        elif kind == "redacted_thinking":
            events.append({"type": "content_block_start", "index": index, "content_block": dict(block)})
        elif kind == "text":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}})
        elif kind == "tool_use":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 700, "output_tokens_details": {"thinking_tokens": thinking_tokens}}})
    events.append({"type": "message_stop"})
    return events


async def _lines(events: list[dict[str, Any]]) -> Any:
    for event in events:
        yield f"event: {event['type']}"
        yield f"data: {json.dumps(event)}"
        yield ""


async def _proxied_chat_stream(events: list[dict[str, Any]]) -> str:
    """What the key proxy streams to the bot for an upstream stream."""
    return "".join([chunk async for chunk in claude_mod.messages_events_to_chunks(_lines(events), model="claude-sonnet-5-5", names={"mcp_Read": "Read"})])


def _details(stream: str) -> list[dict[str, Any]]:
    chunks = [json.loads(line[6:]) for line in stream.split("\n\n") if line.startswith("data: {")]
    return [d for c in chunks for choice in c.get("choices") or [] for d in choice["delta"].get("reasoning_details", [])]


def _provider(answers: list[str]) -> tuple[OpenAICompatibleProvider, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=answers.pop(0), headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url=keyproxy_base() + "/claude/v1")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))), sent


def _request(messages: list[Message], model: str = "claude-sonnet-5-5") -> LLMRequest:
    return LLMRequest(model=model, messages=messages, extra={"enable_thinking": True, "reasoning_effort": "high"},
                      observability=LLMObservabilityContext(session_id="sess-1234", run_id="run-1"))


async def test_each_finished_thinking_block_reaches_the_chat_stream_with_its_place() -> None:
    stream = await _proxied_chat_stream(_events(TURN))
    fmt = claude_mod.REASONING_FORMAT
    assert _details(stream) == [
        {"type": "reasoning.text", "format": fmt, "index": 0, "text_offset": 0, "text": "", "signature": "SIG-a"},
        {"type": "reasoning.encrypted", "format": fmt, "index": 1, "text_offset": 0, "data": "REDACTED-1"},
        {"type": "reasoning.text", "format": fmt, "index": 3, "text_offset": len("Checking the file."), "text": "", "signature": "SIG-b"},
    ]
    full = await subs.collect_completion(claude_mod.messages_events_to_chunks(_lines(_events(TURN)), model="m"), model="m")
    assert full["choices"][0]["message"]["reasoning_details"] == _details(stream)
    # The server's own count of the hidden thinking, which is what replaying it later costs.
    assert full["usage"]["completion_tokens_details"]["reasoning_tokens"] == 640


async def test_an_unsigned_thinking_block_is_not_kept() -> None:
    # The server refuses a thinking block without its signature, so one cut off before it is worth nothing.
    stream = await _proxied_chat_stream(_events([{"type": "thinking", "thinking": "half", "signature": ""}, {"type": "text", "text": "ok"}], stop="end_turn"))
    assert _details(stream) == []


async def test_thinking_goes_from_the_server_to_the_bot_and_back_exactly_where_it_was() -> None:
    provider, sent = _provider([await _proxied_chat_stream(_events(TURN)), await _proxied_chat_stream(_events([{"type": "text", "text": "done"}], stop="end_turn"))])
    user = Message(role=MessageRole.user, content_blocks=[TextBlock(text="read a.txt")])
    deltas = [d async for d in provider.stream_with_tools(_request([user]))]
    finish = deltas[-1]
    assert finish.kind is ProviderDeltaKind.finish and finish.finish_reason == "tool_use"
    assert finish.reasoning_items is not None and len(finish.reasoning_items) == 1
    kept = finish.reasoning_items[0]
    assert kept["format"] == REASONING_ENVELOPE and kept["endpoint"] == "claude" and kept["model"] == "claude-sonnet-5-5" and kept["tokens"] == 640

    # The message the core builds from that stream, and the next request made with it.
    turn = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Checking the file."), ToolUseBlock(tool_call_id="toolu_1", name="Read", arguments_json="{\"path\": \"a.txt\"}")],
                   reasoning_items=finish.reasoning_items)
    result = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="toolu_1", content="hello")])
    _ = [d async for d in provider.stream_with_tools(_request([user, turn, result]))]
    assert sent[1]["messages"][1]["reasoning_details"] == kept["details"]

    messages = claude_mod.chat_to_messages(sent[1])[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # The tail's cache marker rides on the call, as the CLI's does; the blocks are otherwise the server's own.
    assert [{k: v for k, v in block.items() if k != "cache_control"} for block in messages[1]["content"]] == TURN


def _rebuilt(text: str, calls: list[dict[str, Any]], details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    message = {"role": "assistant", "content": text, "tool_calls": calls, "reasoning_details": details}
    sent = claude_mod.chat_to_messages({"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "go"}, message, *[{"role": "tool", "tool_call_id": c["id"], "content": "r"} for c in calls]]})[0]
    return [{k: v for k, v in block.items() if k != "cache_control"} for block in sent["messages"][1]["content"]]


async def test_text_split_by_thinking_and_thinking_between_calls_keep_their_order() -> None:
    # Text on both sides of a thinking block, and a progress thinking block before each of two calls.
    layout = [
        {"type": "text", "text": "First I look. "},
        {"type": "thinking", "thinking": "", "signature": "SIG-1"},
        {"type": "text", "text": "Then I read."},
        {"type": "thinking", "thinking": "", "signature": "SIG-2"},
        {"type": "tool_use", "id": "toolu_a", "name": "mcp_Read", "input": {"path": "a"}},
        {"type": "thinking", "thinking": "", "signature": "SIG-3"},
        {"type": "tool_use", "id": "toolu_b", "name": "mcp_Read", "input": {"path": "b"}},
    ]
    full = await subs.collect_completion(claude_mod.messages_events_to_chunks(_lines(_events(layout)), model="m"), model="m")
    message = full["choices"][0]["message"]
    assert _rebuilt(message["content"], message["tool_calls"], message["reasoning_details"]) == layout


def test_a_turn_without_kept_thinking_is_sent_as_before() -> None:
    calls = [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]
    assert _rebuilt("hi", calls, []) == [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "c1", "name": "mcp_Read", "input": {}}]
    # Reasoning another route wrote is not this route's to read.
    foreign = [{"type": "reasoning.encrypted", "format": subs.REASONING_FORMAT, "data": "x"}]
    assert _rebuilt("hi", [], foreign) == [{"type": "text", "text": "hi"}]


async def test_thinking_goes_back_only_to_the_model_that_wrote_it() -> None:
    # Claude Code strips a turn's thinking once the conversation runs on another model, and keeps it when
    # the conversation comes back: the core's (endpoint, model) record does the same.
    detail = {"type": "reasoning.text", "format": claude_mod.REASONING_FORMAT, "index": 0, "text_offset": 0, "text": "", "signature": "SIG"}
    envelope = {"format": REASONING_ENVELOPE, "endpoint": "claude", "model": "claude-sonnet-5-5", "details": [detail], "tokens": 5}
    turn = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")], reasoning_items=(envelope,))
    user = Message(role=MessageRole.user, content_blocks=[TextBlock(text="next")])
    done = await _proxied_chat_stream(_events([{"type": "text", "text": "ok"}], stop="end_turn"))
    provider, sent = _provider([done, done])
    _ = [d async for d in provider.stream_with_tools(_request([user, turn, user], model="claude-opus-5-5"))]
    assert "reasoning_details" not in sent[0]["messages"][1]
    _ = [d async for d in provider.stream_with_tools(_request([user, turn, user]))]
    assert sent[1]["messages"][1]["reasoning_details"] == [detail]


def test_adaptive_thinking_for_the_models_claude_code_gives_it_and_a_budget_for_the_rest() -> None:
    body = {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high", "max_tokens": 32000}
    adaptive, _ = claude_mod.chat_to_messages({**body, "model": "claude-sonnet-5-5"})
    assert adaptive["thinking"] == {"type": "adaptive", "display": "omitted"} and adaptive["output_config"] == {"effort": "high"}
    budget, _ = claude_mod.chat_to_messages({**body, "model": "claude-haiku-4-5-20251001"})
    assert budget["thinking"]["type"] == "enabled" and budget["thinking"]["budget_tokens"] == 16384


def test_a_prefix_lock_refusal_drops_the_named_block_and_every_later_one() -> None:
    payload = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "S1"}, {"type": "text", "text": "x"}]},
        {"role": "user", "content": [{"type": "text", "text": "b"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "S2"}, {"type": "redacted_thinking", "data": "R"}, {"type": "text", "text": "y"}]},
        {"role": "user", "content": [{"type": "text", "text": "c"}]},
    ]}
    error = '{"type":"error","error":{"type":"invalid_request_error","message":"messages.3.content.1: Invalid `signature` in `thinking` block. The block is bound to a different conversation."}}'
    locked, first = claude_mod.prefix_locked(400, error, {"anthropic-thinking-prefix-mismatch": "block=messages.3.content.1; kind=prefix_binding_mismatch"})
    assert locked and first == (3, 1)
    stripped, removed = claude_mod.without_thinking(payload, first)
    assert removed == 1
    assert [b["type"] for b in stripped["messages"][1]["content"]] == ["thinking", "text"]
    assert [b["type"] for b in stripped["messages"][3]["content"]] == ["thinking", "text"]
    everything, removed = claude_mod.without_thinking(payload)
    assert removed == 3 and all(b["type"] == "text" for m in everything["messages"] for b in m["content"])
    assert claude_mod.prefix_locked(400, error, {}) == (True, None)
    assert claude_mod.prefix_locked(400, '{"error":{"message":"prompt is too long"}}', {}) == (False, None)
    assert claude_mod.prefix_locked(500, error, {}) == (False, None)


async def test_the_proxy_retries_once_without_thinking_the_server_will_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if any(b.get("type") == "thinking" for m in body["messages"] if isinstance(m["content"], list) for b in m["content"]):
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different conversation."}},
                                  headers={"anthropic-thinking-prefix-mismatch": "block=messages.1.content.0"})
        events = _events([{"type": "text", "text": "OK"}], stop="end_turn")
        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "refreshToken": "r", "expiresAt": 4102444800000}}))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(cred))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(tmp_path / "missing-codex.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "missing-grok.json"))
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    detail = {"type": "reasoning.text", "format": claude_mod.REASONING_FORMAT, "index": 0, "text_offset": 0, "text": "", "signature": "SIG"}
    body = {"model": "claude-sonnet-5-5", "reasoning_effort": "high", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "earlier", "reasoning_details": [detail]},
        {"role": "user", "content": "again"},
    ]}
    async with TestClient(TestServer(app)) as client:
        reply = await (await client.post("/claude/v1/chat/completions", json=body)).json()
    assert reply["choices"][0]["message"]["content"] == "OK"
    assert len(seen) == 2
    assert [b["type"] for b in seen[0]["messages"][1]["content"]] == ["thinking", "text"]
    assert [b["type"] for b in seen[1]["messages"][1]["content"]] == ["text"]
