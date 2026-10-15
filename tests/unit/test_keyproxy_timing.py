"""The key proxy's per-request timing line: headers, first body byte and total, and nothing secret."""

from __future__ import annotations

import importlib.util
import json
import logging
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


subs = _load("subscriptions")
claude_mod = _load("claude")
proxy = _load("proxy")

SECRET = "sk-timing-secret-0123456789"


def _timing_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "keyproxy.timing"]


async def _serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: Any) -> Any:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setattr(proxy, "KEYS_FILE", "")
    monkeypatch.setattr(proxy, "AGENT_API", "")
    monkeypatch.setattr(proxy, "BUDGET_FLAG", tmp_path / "no-flag")
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(tmp_path / "no-claude.json"))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(tmp_path / "no-codex.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "no-grok.json"))
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return TestClient(TestServer(app))


def test_the_three_moments_are_measured_from_the_send() -> None:
    timing = proxy.UpstreamTiming("grok", "POST", "/chat/completions", "grok-4.7")
    assert timing.view("done")["headers_ms"] is None and timing.view("done")["first_byte_ms"] is None
    timing.answered(200)
    timing.received(0)  # an empty read is not a first byte
    assert timing.first_byte_at is None
    timing.received(12)
    timing.received(30)
    view = timing.view("done")
    assert view["path"] == "chat/completions" and view["status"] == 200 and view["bytes"] == 42
    assert 0 <= view["headers_ms"] <= view["first_byte_ms"] <= view["total_ms"]


async def test_a_streamed_completion_logs_one_line_with_its_timings_and_no_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        sse = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    caplog.set_level(logging.INFO, logger="keyproxy.timing")
    async with await _serve(monkeypatch, tmp_path, upstream) as client:
        response = await client.post("/deepseek/chat/completions", json={"model": "deepseek-flash", "stream": True, "messages": []})
        assert "data: [DONE]" in await response.text()
    assert sent[0].headers["authorization"] == f"Bearer {SECRET}"
    lines = _timing_lines(caplog)
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert lines[0].levelno == logging.INFO
    assert text.startswith("upstream deepseek POST chat/completions model=deepseek-flash status=200 headers_ms=")
    assert "first_byte_ms=" in text and "first_byte_ms=-" not in text and "outcome=done" in text
    assert SECRET not in text and "messages" not in text


async def test_a_request_with_no_first_byte_past_the_threshold_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KEYPROXY_SLOW_FIRST_BYTE_SECONDS", "0")
    caplog.set_level(logging.INFO, logger="keyproxy.timing")
    async with await _serve(monkeypatch, tmp_path, lambda request: httpx.Response(200, json={"ok": True})) as client:
        assert (await client.get("/deepseek/models")).status == 200
    lines = _timing_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert "status=200" in lines[0].getMessage()


async def test_an_unreachable_upstream_is_reported_with_no_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    caplog.set_level(logging.INFO, logger="keyproxy.timing")
    async with await _serve(monkeypatch, tmp_path, upstream) as client:
        assert (await client.post("/deepseek/chat/completions", json={"model": "m"})).status == 502
    text = _timing_lines(caplog)[0].getMessage()
    assert "status=0 headers_ms=- first_byte_ms=-" in text and "outcome=unreachable:ConnectError" in text


async def test_the_claude_route_times_the_upstream_messages_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PONG"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        ]
        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    claude_file = tmp_path / "claude.json"
    claude_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "claude-token", "refreshToken": "r", "expiresAt": 4102444800000}}))
    caplog.set_level(logging.INFO, logger="keyproxy.timing")
    async with await _serve(monkeypatch, tmp_path, upstream) as client:
        monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(claude_file))
        response = await client.post("/claude/v1/chat/completions", json={"model": "claude-sonnet-5", "stream": True, "messages": [{"role": "user", "content": "ping"}]})
        assert "PONG" in await response.text()
    lines = _timing_lines(caplog)
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert text.startswith("upstream claude POST v1/messages model=claude-sonnet-5 status=200")
    assert "first_byte_ms=-" not in text and "claude-token" not in text and "outcome=done" in text
