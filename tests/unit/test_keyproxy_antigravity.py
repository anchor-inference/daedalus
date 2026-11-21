"""Unit tests for the Google Antigravity (agy) subscription upstream in keyproxy."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import test_utils

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
if str(KEYPROXY_DIR) not in sys.path:
    sys.path.insert(0, str(KEYPROXY_DIR))

import antigravity as kp_ag  # noqa: E402

import accounts as kp_accounts  # noqa: E402
import proxy as kp_proxy  # noqa: E402


def _make_auth_file(path: Path, *, expired: bool = False) -> None:
    expiry = (datetime.now(UTC) + (timedelta(minutes=-5) if expired else timedelta(hours=1))).isoformat().replace("+00:00", "Z")
    path.write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "tok-initial",
                    "token_type": "Bearer",
                    "refresh_token": "rtok-1",
                    "expiry": expiry,
                    "expires_in": 3599,
                },
                "auth_method": "consumer",
                "id_token_email": "someone@example.com",
            }
        ),
        encoding="utf-8",
    )


def test_identity_reads_email_and_method(tmp_path: Path) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred)
    assert kp_accounts.identity("antigravity", cred) == {"email": "someone@example.com", "plan": "consumer"}


@pytest.mark.asyncio
async def test_auth_refresh_writes_back_token(tmp_path: Path) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == kp_ag.ANTIGRAVITY_TOKEN_URL
        return httpx.Response(200, json={"access_token": "tok-refreshed", "expires_in": 3600})

    auth = kp_ag.AntigravityAuth(cred)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tok = await auth.token(client)
    assert tok == "tok-refreshed"
    saved = json.loads(cred.read_text(encoding="utf-8"))
    assert saved["token"]["access_token"] == "tok-refreshed"


def test_chat_to_antigravity_translates_messages_tools_and_signatures() -> None:
    body: dict[str, Any] = {
        "model": "gemini-3-flash",
        "reasoning_effort": "medium",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Check Paris weather."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}],
                "reasoning_details": [{"type": "reasoning.encrypted", "format": kp_ag.REASONING_FORMAT, "id": "call_1", "data": "sig-abc"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Sunny, 20C"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }
    payload = kp_ag.chat_to_antigravity(body, project="aicode-consumers")
    assert payload["project"] == "aicode-consumers"
    assert payload["model"] == "gemini-3-flash"
    req = payload["request"]
    assert req["systemInstruction"] == {"parts": [{"text": "Be concise."}]}
    assert req["generationConfig"]["thinkingConfig"] == {"includeThoughts": True, "thinkingBudget": 8192}
    assert req["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}
    decl_params = req["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "$schema" not in decl_params
    assert "additionalProperties" not in decl_params
    model_parts = req["contents"][1]["parts"]
    assert model_parts[0]["functionCall"] == {"name": "get_weather", "args": {"city": "Paris"}}
    assert model_parts[0]["thoughtSignature"] == "sig-abc"
    tool_parts = req["contents"][2]["parts"]
    assert tool_parts[0]["functionResponse"] == {"name": "get_weather", "response": {"result": "Sunny, 20C"}}


@pytest.mark.asyncio
async def test_sse_translation_emits_thinking_tool_call_signature_and_usage() -> None:
    events = [
        'data: {"response": {"candidates": [{"content": {"role": "model", "parts": [{"thought": true, "text": "Thinking..."}]}}], "usageMetadata": {"promptTokenCount": 10, "thoughtsTokenCount": 5, "totalTokenCount": 15}}}',
        'data: {"response": {"candidates": [{"content": {"role": "model", "parts": [{"thoughtSignature": "sig-xyz", "functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 8, "thoughtsTokenCount": 5, "totalTokenCount": 23}}}',
    ]

    async def line_iter():
        for line in events:
            yield line

    chunks = [chunk async for chunk in kp_ag.antigravity_sse_to_chunks(line_iter(), "gemini-3-flash", "chatcmpl-1")]
    assert chunks[-1] == "data: [DONE]\n\n"
    parsed = [json.loads(c[6:]) for c in chunks[:-1]]
    assert parsed[1]["choices"][0]["delta"]["reasoning_content"] == "Thinking..."
    tc_delta = parsed[2]["choices"][0]["delta"]
    assert tc_delta["tool_calls"][0]["function"]["name"] == "get_weather"
    assert tc_delta["reasoning_details"][0]["data"] == "sig-xyz"
    assert parsed[3]["choices"][0]["finish_reason"] == "tool_calls"
    assert parsed[3]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 5


@pytest.mark.asyncio
async def test_proxy_antigravity_models_and_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred)
    monkeypatch.setattr(kp_proxy, "ANTIGRAVITY_AUTH", kp_ag.AntigravityAuth(cred))

    def transport_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("v1internal:loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-1", "paidTier": {"name": "Google AI Pro"}})
        if url.endswith("v1internal:fetchAvailableModels"):
            return httpx.Response(
                200,
                json={
                    "models": {
                        "gemini-3-flash": {"displayName": "Gemini 3 Flash", "maxTokens": 1048576, "maxOutputTokens": 65536, "supportsImages": True, "supportsThinking": True, "quotaInfo": {"remainingFraction": 0.9, "resetTime": "2026-11-22T03:00:00Z"}},
                        "tab_flash_lite_preview": {"maxTokens": 16384},
                    }
                },
            )
        if "v1internal:streamGenerateContent" in url:
            sse = (
                'data: {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1, "totalTokenCount": 6}}}\n\n'
            )
            return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    app = kp_proxy.make_app()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        resp_models = await client.get("/antigravity/v1/models")
        assert resp_models.status == 200
        models_data = await resp_models.json()
        ids = [m["id"] for m in models_data["data"]]
        assert ids == ["gemini-3-flash"]

        resp_chat = await client.post(
            "/antigravity/v1/chat/completions",
            json={"model": "gemini-3-flash", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp_chat.status == 200
        chat_data = await resp_chat.json()
        assert chat_data["choices"][0]["message"]["content"] == "ok"

        resp_usage = await client.get("/subscriptions/usage")
        assert resp_usage.status == 200
        usage_data = await resp_usage.json()
        assert usage_data["antigravity"]["logged_in"] is True
        assert usage_data["antigravity"]["windows"][0]["used_percent"] == 10.0
    finally:
        await app["client"].aclose()
        await client.close()
