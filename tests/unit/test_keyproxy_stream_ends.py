"""The key proxy finishes an answer only on the upstream's terminal event, and keeps what a failure says.

A stream that ends early is a cut, not a finish: before this the translators wrote a normal finish and
``[DONE]`` after any end of the upstream body, so a half-written tool call ran with its braces repaired.
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

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
if str(KEYPROXY_DIR) not in sys.path:
    sys.path.insert(0, str(KEYPROXY_DIR))


def _load(name: str) -> Any:
    if name in sys.modules and getattr(sys.modules[name], "__file__", "") == str(KEYPROXY_DIR / f"{name}.py"):
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


subs = _load("subscriptions")
claude_mod = _load("claude")
proxy = _load("proxy")


async def _lines(events: list[dict[str, Any]]) -> Any:
    for e in events:
        yield f"event: {e['type']}"
        yield f"data: {json.dumps(e)}"
        yield ""


async def _codex(events: list[dict[str, Any]]) -> list[str]:
    return [c async for c in subs.responses_events_to_chunks(_lines(events), model="m") if c.startswith("data: ")]


async def _claude(events: list[dict[str, Any]]) -> list[str]:
    return [c async for c in claude_mod.messages_events_to_chunks(_lines(events), model="m") if c.startswith("data: ")]


def _payloads(chunks: list[str]) -> list[dict[str, Any]]:
    return [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]


CUT_CALL = [
    {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Write"}},
    {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"path\": \"a.txt\", \"content\": \"half of"},
]


async def test_codex_incomplete_response_finishes_as_length_and_counts_its_usage() -> None:
    events = [*CUT_CALL, {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "usage": {"input_tokens": 7, "output_tokens": 9}}}]
    chunks = await _codex(events)
    last = _payloads(chunks)[-1]
    assert last["choices"][0]["finish_reason"] == "length" and last["usage"]["completion_tokens"] == 9
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_codex_content_filter_is_its_own_finish() -> None:
    events = [{"type": "response.output_text.delta", "delta": "Hi"}, {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}, "usage": {}}}]
    assert _payloads(await _codex(events))[-1]["choices"][0]["finish_reason"] == "content_filter"


async def test_codex_call_item_ended_incomplete_finishes_as_length_even_under_completed() -> None:
    events = [
        *CUT_CALL,
        {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Write", "status": "incomplete"}},
        {"type": "response.completed", "response": {"status": "completed", "usage": {}}},
    ]
    assert _payloads(await _codex(events))[-1]["choices"][0]["finish_reason"] == "length"


async def test_codex_stream_without_a_terminal_event_is_a_cut_not_a_finish() -> None:
    chunks = await _codex(CUT_CALL)
    error = _payloads(chunks)[-1]["error"]
    assert error["code"] == subs.STREAM_TRUNCATED
    assert "data: [DONE]\n\n" not in chunks
    assert not any(p.get("choices", [{}])[0].get("finish_reason") for p in _payloads(chunks) if "choices" in p)


async def test_codex_failure_keeps_its_code_and_the_wait_it_states() -> None:
    message = "Rate limit reached for gpt-6-sol on tokens per min. Please try again in 11.054s. Visit the docs."
    chunks = await _codex([{"type": "response.failed", "response": {"status": "failed", "error": {"code": "rate_limit_exceeded", "message": message}}}])
    error = _payloads(chunks)[-1]["error"]
    assert error["code"] == "rate_limit_exceeded" and error["message"] == message and error["retry_after_seconds"] == 11.054
    assert "data: [DONE]\n\n" not in chunks
    quota = _payloads(await _codex([{"type": "response.failed", "response": {"error": {"code": "insufficient_quota", "message": "spent"}}}]))[-1]["error"]
    assert quota["code"] == "insufficient_quota" and "retry_after_seconds" not in quota
    headed = _payloads(await _codex([{"type": "response.failed", "response": {"error": {"code": "server_is_overloaded", "message": "busy", "headers": {"Retry-After": "30"}}}}]))[-1]["error"]
    assert headed["code"] == "server_is_overloaded" and headed["retry_after_seconds"] == 30
    bare = _payloads(await _codex([{"type": "error", "code": "rate_limit_exceeded", "message": "slow down, try again in 500ms"}]))[-1]["error"]
    assert bare["code"] == "rate_limit_exceeded" and bare["retry_after_seconds"] == 0.5


def test_retry_after_reads_a_reset_time() -> None:
    assert subs.retry_after_seconds({"resets_at": 1_000_120}, now=1_000_000) == 120
    assert subs.retry_after_seconds({"resets_at": 999_000}, now=1_000_000) is None
    assert subs.retry_after_seconds({"message": "nothing to see"}) is None


CLAUDE_HEAD = [
    {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "Write"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"a.txt\", \"con"}},
]


async def test_claude_stream_without_message_stop_is_a_cut() -> None:
    chunks = await _claude([*CLAUDE_HEAD, {"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}])
    assert _payloads(chunks)[-1]["error"]["code"] == subs.STREAM_TRUNCATED
    assert "data: [DONE]\n\n" not in chunks


async def test_claude_tool_use_without_its_block_stop_finishes_as_length() -> None:
    capped = await _claude([*CLAUDE_HEAD, {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 4}}, {"type": "message_stop"}])
    assert _payloads(capped)[-1]["choices"][0]["finish_reason"] == "length" and capped[-1] == "data: [DONE]\n\n"
    unclosed = await _claude([*CLAUDE_HEAD, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}, {"type": "message_stop"}])
    assert _payloads(unclosed)[-1]["choices"][0]["finish_reason"] == "length"


async def test_claude_error_keeps_its_type() -> None:
    chunks = await _claude([{"type": "message_start", "message": {"usage": {}}}, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}])
    error = _payloads(chunks)[-1]["error"]
    assert error == {"message": "Overloaded", "type": "overloaded_error"}
    assert "data: [DONE]\n\n" not in chunks


def test_codex_system_prompt_is_a_developer_message_with_a_stable_id() -> None:
    body = {"model": "gpt-6.1-sol", "prompt_cache_key": "session-1", "messages": [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}]}
    first, second = subs.chat_to_responses(body), subs.chat_to_responses(body)
    assert "instructions" not in first
    head = first["input"][0]
    assert head["role"] == "developer" and head["content"] == [{"type": "input_text", "text": "Be terse."}]
    assert head["id"].startswith("msg_") and head["id"] == second["input"][0]["id"]
    other = subs.chat_to_responses({**body, "prompt_cache_key": "session-2"})
    assert other["input"][0]["id"] != head["id"]
    bare = subs.chat_to_responses({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert [i["role"] for i in bare["input"]] == ["user"]


def _codex_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: Any) -> Any:
    codex_file = tmp_path / "codex.json"
    codex_file.write_text(json.dumps({"tokens": {"access_token": "eyJ.e30.x", "refresh_token": "r", "account_id": "acct"}}))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(codex_file))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(tmp_path / "missing-claude.json"))
    monkeypatch.setattr(subs, "_jwt_claims", lambda token: {"exp": 4102444800, "https://api.openai.com/auth": {"chatgpt_account_id": "acct"}})
    app = proxy.make_app()
    app["upstream"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return app


async def test_codex_refusal_passes_its_retry_after_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "slow"}}, headers={"retry-after": "42"})

    app = _codex_app(monkeypatch, tmp_path, upstream)
    await app["client"].aclose()
    app["client"] = app.pop("upstream")
    async with TestClient(TestServer(app)) as client:
        refused = await client.post("/codex/v1/chat/completions", json={"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        assert refused.status == 429 and refused.headers["Retry-After"] == "42"

