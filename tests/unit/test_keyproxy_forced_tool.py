"""A forced tool call reaches a model that refuses a forced ``tool_choice`` as words, not as a 400.

Claude Opus 5.5, Claude Sonnet 5.5 and the 5.1 line answer ``tool_choice`` ``any`` or a named tool with
``400 tool_choice: type "tool" and "any" are not supported for this model``, on the login and on the key.
The bot's loop forces a tool when a run owes a call, so each such step failed. The fake upstream here
refuses exactly that shape, as the server does; nothing leaves the process.
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
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_load("subscriptions")
claude_mod = _load("claude")
proxy = _load("proxy")

REFUSAL = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": 'tool_choice: type "tool" and "any" are not supported for this model.'}})
TOOLS = [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}} for name in ("Read", "Exec")]


def _body(model: str, tool_choice: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model, "reasoning_effort": "high", "stream": True,
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "read x"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ],
        "tools": TOOLS,
    }
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _named(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "\n".join(line for event in events for line in (f"event: {event['type']}", "data: " + json.dumps(event), "")).encode()


# A reply that thinks (with a signature, so it would be kept and replayed) and then calls Read.
THINK_THEN_READ = [
    {"type": "message_start", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 4000}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"x\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
    {"type": "message_stop"},
]


def _refusing_upstream(seen: list[dict[str, Any]]) -> Any:
    """The server's rule for these models: any forced tool_choice is a 400 before any output."""

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if (payload.get("tool_choice") or {}).get("type") in ("tool", "any"):
            return httpx.Response(400, content=REFUSAL.encode(), headers={"content-type": "application/json"})
        return httpx.Response(200, content=_sse(THINK_THEN_READ), headers={"content-type": "text/event-stream"})

    return upstream


async def _post(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: Any, body: dict[str, Any]) -> tuple[int, str]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("KEYPROXY_AUTH_ANTHROPIC", raising=False)
    monkeypatch.setattr(proxy, "AGENT_API", "")
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(tmp_path / "no-claude.json"))
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        answer = await client.post("/anthropic/chat/completions", json=body, headers={"authorization": "Bearer caller-token"})
        return answer.status, await answer.text()


def _chunks(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data: {")]


@pytest.fixture(autouse=True)
def _nothing_learned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_mod, "_learned_unforceable", set())


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"])
def test_a_forced_tool_becomes_a_closing_line_on_a_model_that_refuses_it(model: str) -> None:
    plain, _ = claude_mod.chat_to_messages(_body(model), login=True)
    out, _ = claude_mod.chat_to_messages(_body(model, _named("Read")), login=True)
    assert "tool_choice" not in out
    closing = out["messages"][-1]["content"][-1]
    assert closing["type"] == "text" and "`mcp_Read`" in closing["text"] and "cache_control" not in closing
    # Everything before the closing line is the unforced step's request, breakpoints included, and the
    # thinking, effort and context edit an unforced step sends are all still there: none of it moves the cache.
    assert out["messages"][-1]["content"][:-1] == plain["messages"][-1]["content"]
    assert out["messages"][:-1] == plain["messages"][:-1]
    assert {key: out[key] for key in ("system", "tools", "thinking", "output_config", "context_management")} == \
        {key: plain[key] for key in ("system", "tools", "thinking", "output_config", "context_management")}


def test_a_required_tool_asks_for_some_call_and_the_keyed_route_names_tools_bare() -> None:
    out, _ = claude_mod.chat_to_messages(_body("claude-opus-5-5", "required"), login=False)
    assert "tool_choice" not in out and "one of the available tools" in out["messages"][-1]["content"][-1]["text"]
    named, _ = claude_mod.chat_to_messages(_body("claude-sonnet-5-5", _named("Read")), login=False)
    assert "`Read`" in named["messages"][-1]["content"][-1]["text"]


def test_a_model_that_takes_a_forced_choice_still_gets_it() -> None:
    out, _ = claude_mod.chat_to_messages(_body("claude-opus-5", _named("Read")), login=True)
    assert out["tool_choice"] == {"type": "tool", "name": "mcp_Read"} and "thinking" not in out
    assert out["messages"][-1]["content"][-1]["type"] == "tool_result"
    some, _ = claude_mod.chat_to_messages(_body("claude-opus-5", "required"), login=True)
    # Forcing "some tool" is as incompatible with thinking as forcing one on the models that take it.
    assert some["tool_choice"] == {"type": "any"} and "thinking" not in some


async def test_a_forced_step_on_opus_5_5_goes_out_once_unforced_and_keeps_no_thinking(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    status, text = await _post(monkeypatch, tmp_path, _refusing_upstream(seen), _body("claude-opus-5-5", _named("Read")))
    assert status == 200 and len(seen) == 1 and "tool_choice" not in seen[0]
    calls = [call for chunk in _chunks(text) for call in chunk["choices"][0]["delta"].get("tool_calls") or []]
    assert calls[0]["function"]["name"] == "Read" and calls[1]["function"]["arguments"] == "{\"path\": \"x\"}"
    # The thinking was signed over a closing line the next request will not carry; replaying it would be an
    # edited history, so the reply hands none back.
    assert "reasoning_details" not in text


async def test_an_unforced_step_on_opus_5_5_keeps_its_thinking(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    status, text = await _post(monkeypatch, tmp_path, _refusing_upstream(seen), _body("claude-opus-5-5"))
    assert status == 200 and len(seen) == 1 and "reasoning_details" in text and "sig-1" in text


async def test_a_model_not_yet_known_to_refuse_is_learned_from_the_refusal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    status, text = await _post(monkeypatch, tmp_path, _refusing_upstream(seen), _body("claude-opus-6", _named("Read")))
    assert status == 200 and [payload.get("tool_choice") for payload in seen] == [{"type": "tool", "name": "Read"}, None]
    assert "`Read`" in seen[1]["messages"][-1]["content"][-1]["text"] and "reasoning_details" not in text
    assert not claude_mod.forces_tools("claude-opus-6")
    seen.clear()
    status, _ = await _post(monkeypatch, tmp_path, _refusing_upstream(seen), _body("claude-opus-6", "required"))
    assert status == 200 and len(seen) == 1 and "tool_choice" not in seen[0]


async def test_another_400_is_not_read_as_the_forced_choice_refusal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(400, content=b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long"}}')

    status, _ = await _post(monkeypatch, tmp_path, upstream, _body("claude-opus-6", _named("Read")))
    assert status == 400 and len(seen) == 1 and claude_mod.forces_tools("claude-opus-6")
