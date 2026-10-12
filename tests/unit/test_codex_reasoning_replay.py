"""Codex reasoning survives a tool step: asked for, carried to the bot, kept, and sent back.

The ChatGPT backend speaks only the Responses API with ``store`` off, so it forgets a response's
reasoning once it has streamed it. The key proxy asks for the encrypted copy, hands it to the bot
as ``reasoning_details`` on the chat stream, and turns the entries back into reasoning items when
the bot sends the turn again. The streams here are shaped like the backend's own; nothing leaves
the process.
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

from daedalus.config import keyproxy_base
from daedalus.providers.openai_compat import REASONING_ENVELOPE, OpenAICompatibleProvider, ProviderEndpoint

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

SUMMARY = [{"type": "summary_text", "text": "**Reading the file** first."}]
ENCRYPTED = "gAAAAABo-recorded-shape-of-an-encrypted-reasoning-item=="
RESPONSES_STREAM: list[dict[str, Any]] = [
    {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
    {"type": "response.output_item.added", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
    {"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "output_index": 0, "summary_index": 0, "part": {"type": "summary_text", "text": ""}},
    {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "output_index": 0, "summary_index": 0, "delta": "**Reading the file** first."},
    {"type": "response.reasoning_summary_text.done", "item_id": "rs_1", "output_index": 0, "summary_index": 0, "text": "**Reading the file** first."},
    {"type": "response.output_item.done", "output_index": 0, "item": {"id": "rs_1", "type": "reasoning", "summary": SUMMARY, "encrypted_content": ENCRYPTED}},
    {"type": "response.output_item.added", "output_index": 1, "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "Read", "arguments": ""}},
    {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1, "delta": "{\"path\":\"a.txt\"}"},
    {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 1, "arguments": "{\"path\":\"a.txt\"}"},
    {"type": "response.output_item.done", "output_index": 1, "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "Read", "arguments": "{\"path\":\"a.txt\"}"}},
    {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "usage": {"input_tokens": 900, "output_tokens": 340, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 321}}}},
]


async def _lines(events: list[dict[str, Any]]) -> Any:
    for event in events:
        yield f"event: {event['type']}"
        yield f"data: {json.dumps(event)}"
        yield ""


async def _proxied_chat_stream(events: list[dict[str, Any]]) -> str:
    """What the key proxy streams to the bot for a backend stream."""
    return "".join([chunk async for chunk in subs.responses_events_to_chunks(_lines(events), model="gpt-6-sol")])


def _provider(answers: list[str], base_url: str | None = None, kind: str = "openai_compat") -> tuple[OpenAICompatibleProvider, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=answers.pop(0), headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="codex", kind=kind, base_url=base_url or keyproxy_base() + "/codex/v1")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))), sent


def _request(messages: list[Message], model: str = "gpt-6-sol") -> LLMRequest:
    return LLMRequest(model=model, messages=messages, extra={"enable_thinking": True, "reasoning_effort": "high"},
                      observability=LLMObservabilityContext(session_id="sess-1234", run_id="run-1"))


def test_the_responses_body_asks_for_encrypted_reasoning_and_carries_the_cache_key() -> None:
    out = subs.chat_to_responses({"model": "gpt-6-sol", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high", "prompt_cache_key": "sess-1234"})
    assert out["include"] == ["reasoning.encrypted_content"] and out["store"] is False
    assert out["prompt_cache_key"] == "sess-1234"
    assert out["reasoning"] == {"effort": "high", "summary": "auto"}


async def test_a_finished_reasoning_item_reaches_the_chat_stream_whole() -> None:
    chunks = [json.loads(line[6:]) for line in (await _proxied_chat_stream(RESPONSES_STREAM)).split("\n\n") if line.startswith("data: {")]
    details = [d for c in chunks for d in c["choices"][0]["delta"].get("reasoning_details", [])]
    assert details == [{"type": "reasoning.encrypted", "format": subs.REASONING_FORMAT, "index": 0, "data": ENCRYPTED, "summary": SUMMARY, "id": "rs_1"}]
    full = await subs.collect_completion(subs.responses_events_to_chunks(_lines(RESPONSES_STREAM), model="m"), model="m")
    assert full["choices"][0]["message"]["reasoning_details"] == details


async def test_a_reasoning_item_without_its_encrypted_copy_is_not_kept() -> None:
    bare = [e if e["type"] != "response.output_item.done" or e["item"]["type"] != "reasoning" else {**e, "item": {"id": "rs_1", "type": "reasoning", "summary": SUMMARY}} for e in RESPONSES_STREAM]
    assert "reasoning_details" not in await _proxied_chat_stream(bare)


async def test_reasoning_goes_from_the_backend_to_the_bot_and_back_in_front_of_its_call() -> None:
    provider, sent = _provider([await _proxied_chat_stream(RESPONSES_STREAM), await _proxied_chat_stream(RESPONSES_STREAM[-1:])])
    user = Message(role=MessageRole.user, content_blocks=[TextBlock(text="read a.txt")])
    deltas = [d async for d in provider.stream_with_tools(_request([user]))]
    finish = deltas[-1]
    assert finish.kind is ProviderDeltaKind.finish and finish.finish_reason == "tool_use"
    assert finish.reasoning_items is not None and len(finish.reasoning_items) == 1
    kept = finish.reasoning_items[0]
    assert kept["format"] == REASONING_ENVELOPE and kept["endpoint"] == "codex" and kept["model"] == "gpt-6-sol" and kept["tokens"] == 321
    assert sent[0]["prompt_cache_key"] == "sess-1234"

    # The message the core builds from that stream, and the next request made with it.
    turn = Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="call_1", name="Read", arguments_json="{\"path\": \"a.txt\"}")],
                   reasoning_content="**Reading the file** first.", reasoning_items=finish.reasoning_items)
    result = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="call_1", content="hello")])
    _ = [d async for d in provider.stream_with_tools(_request([user, turn, result]))]
    assistant = sent[1]["messages"][1]
    assert assistant["reasoning_details"] == kept["details"]
    assert sent[1]["prompt_cache_key"] == "sess-1234"

    responses = subs.chat_to_responses(sent[1])
    assert [item["type"] for item in responses["input"]] == ["message", "reasoning", "function_call", "function_call_output"]
    assert responses["input"][1] == {"type": "reasoning", "summary": SUMMARY, "encrypted_content": ENCRYPTED}


async def test_reasoning_goes_back_only_to_the_model_and_endpoint_that_wrote_it() -> None:
    envelope = {"format": REASONING_ENVELOPE, "endpoint": "codex", "model": "gpt-6-sol", "details": [{"type": "reasoning.encrypted", "format": subs.REASONING_FORMAT, "data": ENCRYPTED}], "tokens": 5}
    turn = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")], reasoning_items=(envelope,))
    user = Message(role=MessageRole.user, content_blocks=[TextBlock(text="next")])
    done = await _proxied_chat_stream(RESPONSES_STREAM[-1:])

    other_model, sent = _provider([done])
    _ = [d async for d in other_model.stream_with_tools(_request([user, turn, user], model="gpt-5.5"))]
    assert "reasoning_details" not in sent[0]["messages"][1]

    elsewhere, sent = _provider([done], base_url="https://api.deepseek.test/v1", kind="deepseek")
    _ = [d async for d in elsewhere.stream_with_tools(_request([user, turn, user]))]
    assert "reasoning_details" not in sent[0]["messages"][1] and "prompt_cache_key" not in sent[0]


async def test_an_endpoint_that_does_not_replay_reasoning_keeps_none() -> None:
    provider, _sent = _provider([await _proxied_chat_stream(RESPONSES_STREAM)], base_url="https://llm.example.test/v1")
    user = Message(role=MessageRole.user, content_blocks=[TextBlock(text="read a.txt")])
    deltas = [d async for d in provider.stream_with_tools(_request([user]))]
    assert deltas[-1].reasoning_items is None


async def test_the_proxy_sends_the_session_header_and_retries_once_without_refused_reasoning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if any(item.get("type") == "reasoning" for item in body["input"]):
            return httpx.Response(400, json={"error": {"message": "The encrypted content for item rs_1 could not be verified.", "type": "invalid_request_error"}})
        sse = "\n".join(["event: response.output_text.delta", "data: " + json.dumps({"type": "response.output_text.delta", "delta": "OK"}), "",
                         "event: response.completed", "data: " + json.dumps({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 1}}}), ""])
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    codex_file = tmp_path / "codex.json"
    codex_file.write_text(json.dumps({"tokens": {"access_token": "eyJ.e30.x", "refresh_token": "r", "account_id": "acct"}}))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(codex_file))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "missing-grok.json"))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(tmp_path / "missing-claude.json"))
    monkeypatch.setattr(subs, "_jwt_claims", lambda token: {"exp": 4102444800})
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    detail = {"type": "reasoning.encrypted", "format": subs.REASONING_FORMAT, "data": ENCRYPTED, "summary": SUMMARY}
    body = {"model": "gpt-6-sol", "prompt_cache_key": "sess-1234", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "earlier", "reasoning_details": [detail]},
        {"role": "user", "content": "again"},
    ]}
    async with TestClient(TestServer(app)) as client:
        reply = await (await client.post("/codex/v1/chat/completions", json=body)).json()
    assert reply["choices"][0]["message"]["content"] == "OK"
    assert len(seen) == 2
    assert all(r.headers["session_id"] == "sess-1234" for r in seen)
    assert [i["type"] for i in json.loads(seen[0].content)["input"]] == ["message", "reasoning", "message", "message"]
    assert [i["type"] for i in json.loads(seen[1].content)["input"]] == ["message", "message", "message"]


def test_any_other_refusal_is_not_mistaken_for_refused_reasoning() -> None:
    assert subs.refused_reasoning(400, '{"error": {"message": "Encrypted content could not be decrypted"}}')
    assert not subs.refused_reasoning(400, '{"error": {"message": "Unsupported parameter: temperature"}}')
    assert not subs.refused_reasoning(500, '{"error": {"message": "reasoning failed"}}')
