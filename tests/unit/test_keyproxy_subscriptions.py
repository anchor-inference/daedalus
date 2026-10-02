"""The key proxy's subscription upstreams: Codex chat ⇄ Responses translation, Grok identity headers, usage views."""

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


subs = _load("subscriptions")
claude_mod = _load("claude")
proxy = _load("proxy")


def test_chat_body_becomes_a_responses_body() -> None:
    body = {
        "model": "gpt-5.6-terra",
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": [{"type": "text", "text": "open x"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{\"path\": \"x\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "contents"},
        ],
        "tools": [{"type": "function", "function": {"name": "Read", "description": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
        "tool_choice": {"type": "function", "function": {"name": "Read"}},
        "max_tokens": 500,
        "reasoning_effort": "high",
    }
    out = subs.chat_to_responses(body)
    assert out["instructions"] == "Be terse." and out["store"] is False and out["stream"] is True and "max_output_tokens" not in out
    assert [i["type"] for i in out["input"]] == ["message", "function_call", "function_call_output"]
    assert out["input"][0]["content"][1]["type"] == "input_image"
    assert out["input"][1] == {"type": "function_call", "call_id": "c1", "name": "Read", "arguments": "{\"path\": \"x\"}"}
    assert out["tools"][0]["name"] == "Read" and out["tool_choice"] == {"type": "function", "name": "Read"} and out["reasoning"] == {"effort": "high", "summary": "auto"}


async def _lines(events: list[dict[str, Any]]) -> Any:
    for e in events:
        yield f"event: {e['type']}"
        yield f"data: {json.dumps(e)}"
        yield ""


async def test_responses_events_become_chat_chunks() -> None:
    events = [
        {"type": "response.created", "response": {}},
        {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Read"}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"path\":"},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": " \"x\"}"},
        {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}}}},
    ]
    chunks = [json.loads(c[6:]) async for c in subs.responses_events_to_chunks(_lines(events), model="m") if c.startswith("data: {")]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"reasoning_content": "thinking"} and deltas[1]["content"] == "Hello"
    assert deltas[2]["tool_calls"][0]["id"] == "call_1" and deltas[2]["tool_calls"][0]["function"]["name"] == "Read"
    assert "".join(d["tool_calls"][0]["function"]["arguments"] for d in deltas[2:5]) == "{\"path\": \"x\"}"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls" and chunks[-1]["usage"] == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "prompt_tokens_details": {"cached_tokens": 3}, "completion_tokens_details": {"reasoning_tokens": 2}}
    full = await subs.collect_completion(subs.responses_events_to_chunks(_lines(events), model="m"), model="m")
    assert full["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{\"path\": \"x\"}" and full["choices"][0]["message"]["content"] == "Hello"
    failed = [{"type": "response.failed", "response": {"error": {"message": "nope"}}}]
    assert (await subs.collect_completion(subs.responses_events_to_chunks(_lines(failed), model="m"), model="m"))["error"]["message"] == "nope"


def test_grok_version_is_the_mounted_cli_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "grok"
    blob = home / "downloads" / "grok-1.2.3-linux-x86_64"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"")
    (home / "bin").mkdir()
    (home / "bin" / "grok").symlink_to(blob)
    monkeypatch.setenv("KEYPROXY_GROK_AUTH", str(home / "auth.json"))
    monkeypatch.delenv("KEYPROXY_GROK_CLIENT_VERSION", raising=False)
    assert subs._installed_grok_version() == "1.2.3"
    monkeypatch.setenv("KEYPROXY_GROK_CLIENT_VERSION", "9.9.9")
    assert subs._installed_grok_version() == "9.9.9"


def test_usage_views() -> None:
    codex = subs.codex_usage_view({"plan_type": "plus", "rate_limit": {"limit_reached": True, "primary_window": {"used_percent": 0, "reset_at": 1}, "secondary_window": {"used_percent": 100, "reset_at": 2}}, "model_usage": {"gpt-6-astra": {}, "gpt-5.6-terra": {}}})
    assert codex["limit_reached"] and [w["name"] for w in codex["windows"]] == ["5h", "weekly"] and codex["models"] == ["gpt-5.6-terra", "gpt-6-astra"]
    # A single weekly window arrives in primary_window. Naming that slot "5h" told the Usage screen the wrong clock.
    week = subs.codex_usage_view({"plan_type": "prolite", "rate_limit": {"limit_reached": False, "primary_window": {"used_percent": 86, "reset_at": 1, "limit_window_seconds": 604800}, "secondary_window": None}})
    assert [w["name"] for w in week["windows"]] == ["weekly"] and week["windows"][0]["used_percent"] == 86.0
    grok = subs.grok_usage_view({"config": {"currentPeriod": {"end": "2026-09-10T02:13:38+00:00"}, "creditUsagePercent": 59.0, "productUsage": [{"product": "GrokBuild", "usagePercent": 59.0}]}})
    assert grok["windows"][0]["used_percent"] == 59.0 and grok["products"][0]["product"] == "GrokBuild" and not grok["limit_reached"]
    claude = claude_mod.claude_usage_view(
        {"five_hour": {"utilization": 3.0, "resets_at": "2026-09-10T00:20:00+00:00"}, "seven_day": {"utilization": 12.0, "resets_at": "2026-09-14T20:00:00+00:00"}, "extra_usage": {"is_enabled": False}, "limits": [{"kind": "weekly_scoped", "percent": 21, "scope": {"model": {"display_name": "Fable"}}}]},
        {"organization": {"rate_limit_tier": "default_claude_max_20x"}},
    )
    assert claude["plan"] == "default_claude_max_20x" and claude["windows"][0]["name"] == "5h" and claude["windows"][2]["name"] == "Fable" and not claude["extra_usage"]


def _cache_control_count(payload: dict[str, Any]) -> int:
    n = 0
    for block in payload.get("system") or []:
        n += int(isinstance(block, dict) and "cache_control" in block)
    for tool in payload.get("tools") or []:
        n += int(isinstance(tool, dict) and "cache_control" in tool)
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            n += sum(1 for block in content if isinstance(block, dict) and "cache_control" in block)
        elif isinstance(content, dict) and "cache_control" in content:
            n += 1
    return n


def test_claude_chat_body_becomes_messages() -> None:
    body = {
        "model": "claude-opus-5",
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "messages": [
            {"role": "system", "content": "You are Daedalus."},
            {"role": "user", "content": "open x"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{\"path\": \"x\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "contents"},
        ],
        "tools": [{"type": "function", "function": {"name": "Read", "description": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
    }
    out, names = claude_mod.chat_to_messages(body)
    assert out["model"] == "claude-opus-5" and out["stream"] is True and out["thinking"]["type"] == "enabled" and out["thinking"]["budget_tokens"] >= 1024
    assert out["system"][0]["text"].startswith("x-anthropic-billing-header:") and "Claude Agent SDK" in out["system"][1]["text"]
    assert "2.1.282" in out["system"][0]["text"] and claude_mod.attribution_hash("open x") in out["system"][0]["text"]
    # Indexes 4, 7 and 20 of this sentence are o, w, s. The digest is the CLI's stamp for 2.1.282.
    assert claude_mod.attribution_hash("hello world, this is long enough") == "783"
    assert out["system"][2]["text"] == "You are Daedalus." and "cache_control" not in out["system"][2]
    assert out["system"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in out["system"][0]
    assert names["mcp_Read"] == "Read" and out["tools"][0]["name"] == "mcp_Read"
    assert out["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    items = out["messages"]
    assert items[0]["role"] == "user" and items[0]["content"][0]["text"] == "open x"
    assert items[1]["content"][0]["name"] == "mcp_Read"
    assert items[2]["content"][0]["type"] == "tool_result" and items[2]["content"][0]["tool_use_id"] == "c1"
    assert items[-1]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert items[-2]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert "cache_control" not in items[0]["content"][0]
    assert _cache_control_count(out) == 4


def test_claude_prompt_cache_stays_at_four_breakpoints_on_a_long_thread() -> None:
    body = {
        "model": "claude-opus-5",
        "messages": [
            {"role": "system", "content": "You are Daedalus."},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a"},
            {"role": "assistant", "content": "more", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "b"},
            {"role": "user", "content": "two"},
        ],
        "tools": [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {}}}}],
    }
    out, _ = claude_mod.chat_to_messages(body)
    assert _cache_control_count(out) == 4
    assert "cache_control" not in out["messages"][0]["content"][0]
    assert out["messages"][-1]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert out["messages"][-2]["content"][-1]["cache_control"]["type"] == "ephemeral"


async def test_claude_usage_counts_cache_write_inside_prompt_tokens() -> None:
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3, "cache_creation_input_tokens": 4990, "cache_read_input_tokens": 0}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
    ]
    full = await subs.collect_completion(claude_mod.messages_events_to_chunks(_lines(events), model="m"), model="m")
    assert full["usage"]["prompt_tokens"] == 4993 and full["usage"]["cache_creation_input_tokens"] == 4990
    hit = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3, "cache_creation_input_tokens": 16, "cache_read_input_tokens": 4990}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
    ]
    second = await subs.collect_completion(claude_mod.messages_events_to_chunks(_lines(hit), model="m"), model="m")
    assert second["usage"]["prompt_tokens"] == 5009 and second["usage"]["prompt_tokens_details"]["cached_tokens"] == 4990
    already_total = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 67550, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 22078}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
    ]
    third = await subs.collect_completion(claude_mod.messages_events_to_chunks(_lines(already_total), model="m"), model="m")
    assert third["usage"]["prompt_tokens"] == 67550


def test_claude_prompt_cache_without_tools_marks_identity_and_the_user_turn() -> None:
    out, _ = claude_mod.chat_to_messages({"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "hi"}]})
    assert len(out["system"]) == 2
    assert out["system"][1]["cache_control"]["ttl"] == "1h"
    assert out["messages"][0]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert _cache_control_count(out) == 2


async def test_proxy_routes_codex_and_grok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url.endswith("/codex/responses"):
            sse = "\n".join([
                "event: response.output_text.delta", "data: " + json.dumps({"type": "response.output_text.delta", "delta": "PONG"}), "",
                "event: response.completed", "data: " + json.dumps({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 1}}}), "",
            ])
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        if url.endswith("/wham/usage"):
            return httpx.Response(200, json={"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 1}}, "model_usage": {"gpt-5.6-terra": {}}})
        if "cli-chat-proxy.grok.com" in url:
            return httpx.Response(200, json={"echo": dict(request.headers)})
        return httpx.Response(404)

    codex_file = tmp_path / "codex.json"
    codex_file.write_text(json.dumps({"tokens": {"access_token": "eyJ.e30.x", "refresh_token": "r", "account_id": "acct"}}))
    grok_file = tmp_path / "grok.json"
    grok_file.write_text(json.dumps({"https://auth.x.ai::c": {"key": "grok-token", "refresh_token": "r", "expires_at": "2999-01-01T00:00:00Z", "oidc_client_id": "c"}}))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(codex_file))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(grok_file))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(tmp_path / "missing-claude.json"))
    monkeypatch.setattr(subs, "_jwt_claims", lambda token: {"exp": 4102444800, "https://api.openai.com/auth": {"chatgpt_account_id": "acct"}})
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        plain = await client.post("/codex/v1/chat/completions", json={"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "hi"}]})
        data = await plain.json()
        assert data["choices"][0]["message"]["content"] == "PONG" and data["usage"]["prompt_tokens"] == 5
        sent = [r for r in seen if str(r.url).endswith("/codex/responses")][0]
        assert sent.headers["authorization"] == "Bearer eyJ.e30.x" and sent.headers["chatgpt-account-id"] == "acct" and sent.headers["originator"] == "codex_cli_rs"
        assert json.loads(sent.content)["store"] is False
        streamed = await client.post("/codex/v1/chat/completions", json={"model": "gpt-5.6-terra", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        text = await streamed.text()
        assert '"content": "PONG"' in text and text.strip().endswith("data: [DONE]")
        models = await (await client.get("/codex/v1/models")).json()
        assert {m["id"] for m in models["data"]} >= {"gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-terra"}
        grok = await (await client.get("/grok/v1/models")).json()
        echoed = grok["echo"]
        assert echoed["authorization"] == "Bearer grok-token" and echoed["x-grok-client-identifier"] == "grok-shell" and echoed["x-xai-token-auth"] == "xai-grok-cli"
        assert echoed["x-grok-client-version"] == subs.GROK_CLIENT_VERSION and subs.GROK_CLIENT_VERSION != "0.2.101"
        assert str([r for r in seen if "grok.com" in str(r.url)][0].url) == "https://cli-chat-proxy.grok.com/v1/models"
        await client.post("/grok/v1/chat/completions", json={"model": "grok-4.7", "messages": [{"role": "user", "content": "hi"}]})
        posted = [r for r in seen if str(r.url).endswith("/chat/completions")][0]
        assert posted.headers["x-grok-model-override"] == "grok-4.7"
        usage = await (await client.get("/subscriptions/usage")).json()
        assert usage["codex"]["plan"] == "plus" and usage["grok"]["logged_in"] is True and usage["claude"]["logged_in"] is False


async def test_proxy_routes_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url.endswith("/v1/messages") or "/v1/messages?" in url:
            sse = "\n".join([
                "event: content_block_delta", "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "PONG"}}), "",
                "event: message_delta", "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}), "",
                "event: message_start", "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}), "",
            ])
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        if url.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5"}]})
        if url.endswith("/api/oauth/usage"):
            return httpx.Response(200, json={"five_hour": {"utilization": 3}, "seven_day": {"utilization": 12}, "extra_usage": {"is_enabled": False}})
        if url.endswith("/api/oauth/profile"):
            return httpx.Response(200, json={"organization": {"rate_limit_tier": "default_claude_max_20x"}})
        return httpx.Response(404)

    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "refreshToken": "r", "expiresAt": 4102444800000}}))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(cred))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(tmp_path / "no-codex.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "no-grok.json"))
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        plain = await client.post("/claude/v1/chat/completions", json={"model": "claude-opus-5", "messages": [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {}}}}]})
        data = await plain.json()
        assert data["choices"][0]["message"]["content"] == "PONG"
        sent = [r for r in seen if "/v1/messages" in str(r.url)][0]
        payload = json.loads(sent.content)
        assert sent.headers["authorization"] == "Bearer sk-ant-oat-test" and sent.headers["x-app"] == "cli"
        assert payload["system"][0]["text"].startswith("x-anthropic-billing-header:") and payload["tools"][0]["name"] == "mcp_Read"
        assert payload["system"][2]["text"] == "Be terse."
        assert payload["tools"][-1]["cache_control"]["type"] == "ephemeral"
        assert payload["messages"][0]["content"][-1]["cache_control"]["type"] == "ephemeral"
        models = await (await client.get("/claude/v1/models")).json()
        assert "claude-opus-5" in {m["id"] for m in models["data"]}
        usage = await (await client.get("/subscriptions/usage")).json()
        assert usage["claude"]["plan"] == "default_claude_max_20x" and usage["claude"]["windows"][0]["used_percent"] == 3.0


async def test_claude_usage_429_falls_back_to_profile_and_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hits = {"usage": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/oauth/usage"):
            hits["usage"] += 1
            if hits["usage"] == 1:
                return httpx.Response(200, json={"five_hour": {"utilization": 7}, "seven_day": {"utilization": 12}, "extra_usage": {"is_enabled": False}})
            return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "Rate limited"}}, headers={"Retry-After": "0"})
        if url.endswith("/api/oauth/profile"):
            return httpx.Response(200, json={"organization": {"rate_limit_tier": "default_claude_max_20x"}})
        return httpx.Response(404)

    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "refreshToken": "r", "expiresAt": 4102444800000}}))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(cred))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(tmp_path / "no-codex.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "no-grok.json"))
    monkeypatch.setattr(proxy, "USAGE_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(proxy, "_usage_cache", {})
    monkeypatch.setattr(proxy, "_claude_usage_retry_at", 0.0)
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        first = await (await client.get("/subscriptions/usage")).json()
        assert first["claude"]["windows"][0]["used_percent"] == 7.0 and "error" not in first["claude"]
        second = await (await client.get("/subscriptions/usage")).json()
        assert second["claude"]["windows"][0]["used_percent"] == 7.0 and "error" not in second["claude"]
        assert hits["usage"] == 2
        # backoff: a third call must not hit the usage endpoint again
        third = await (await client.get("/subscriptions/usage")).json()
        assert third["claude"]["windows"][0]["used_percent"] == 7.0 and hits["usage"] == 2


def test_opencode_usage_view_reads_the_three_windows() -> None:
    subs = _load("subscriptions")
    view = subs.opencode_usage_view({"usage": {"rolling": {"status": "ok", "percent": 12.5, "resetsAt": "2026-09-11T01:24:47.169Z"}, "weekly": {"status": "ok", "percent": 3, "resetsAt": "2026-09-14T00:00:00.169Z"}, "monthly": {"status": "exceeded", "percent": 100, "resetsAt": "2026-10-10T20:19:58.169Z"}}})
    assert view["provider"] == "opencode" and view["plan"] == "OpenCode Go"
    assert [w["name"] for w in view["windows"]] == ["5 h", "weekly", "monthly"]
    assert view["windows"][0]["used_percent"] == 12.5 and view["windows"][0]["resets_at"].startswith("2026-09-11")
    assert view["limit_reached"] is True


async def _through_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str, path: str, **kwargs: Any) -> httpx.Request:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(tmp_path / "no-claude.json"))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(tmp_path / "no-codex.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "no-grok.json"))
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        assert (await client.request(method, path, **kwargs)).status == 200
    return seen[0]


async def test_anthropic_key_goes_in_x_api_key_and_the_version_header_is_added(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("KEYPROXY_AUTH_ANTHROPIC", raising=False)
    sent = await _through_proxy(monkeypatch, tmp_path, "GET", "/anthropic/models", headers={"authorization": "Bearer caller-token"})
    assert str(sent.url) == "https://api.anthropic.com/v1/models"
    assert sent.headers["x-api-key"] == "sk-ant-test"
    assert "authorization" not in sent.headers  # the caller's own credential never travels on
    assert sent.headers["anthropic-version"] == "2023-06-01"


async def test_a_caller_chosen_anthropic_version_is_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sent = await _through_proxy(monkeypatch, tmp_path, "GET", "/anthropic/models", headers={"Anthropic-Version": "2099-01-01"})
    assert sent.headers["anthropic-version"] == "2099-01-01"


def test_anthropic_auth_scheme_defaults_to_x_api_key_and_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYPROXY_AUTH_ANTHROPIC", raising=False)
    assert proxy.auth_scheme("anthropic") == "x-api-key"
    assert proxy.auth_scheme("openai") == proxy.BEARER
    monkeypatch.setenv("KEYPROXY_AUTH_ANTHROPIC", "bearer")
    assert proxy.auth_scheme("anthropic") == proxy.BEARER


def test_key_status_lists_anthropic_and_a_keyless_local_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KEYPROXY_KEY_LOCAL", raising=False)
    assert proxy.key_status()["anthropic"] == {"configured": False, "kind": proxy.API_KEY}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_LOCAL", "http://127.0.0.1:11434/v1")
    status = proxy.key_status()
    assert status["anthropic"]["configured"] is True
    assert status["local"] == {"configured": True, "kind": proxy.ENDPOINT}
    monkeypatch.setenv("KEYPROXY_KEY_LOCAL", "k")
    assert proxy.key_status()["local"]["kind"] == proxy.API_KEY
