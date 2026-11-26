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


# Built by concatenation and of a shape no real client has (all zeros, all "f"), so no secret
# scanner reads this file as carrying a Google OAuth client; the patterns in the module still match.
FAKE_ID = "0" * 13 + "-" + "f" * 32 + ".apps." + "googleusercontent.com"
DECOY_ID = "0" * 12 + "-" + "e" * 32 + ".apps." + "googleusercontent.com"
FAKE_SECRET = "GOCS" + "PX-" + "fake" * 7
DECOY_SECRET = "GOCS" + "PX-" + "deco" * 7


@pytest.fixture(autouse=True)
def no_installed_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's own agy out of reach: a test reads only the fake binaries it writes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KEYPROXY_ANTIGRAVITY_CLI", raising=False)
    monkeypatch.setattr(kp_ag.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kp_ag, "_verified_client", None)
    monkeypatch.setattr(kp_ag, "_cli_scans", {})


def _fake_cli(path: Path) -> Path:
    """A stand-in for agy's binary: string literals packed end to end, the way Go lays them out, so a
    secret runs straight into the next string and an id follows a string ending in a digit."""
    blob = (
        b"\x7fELF\x00\x00" + b"\x00" * 64
        + b"https://auth.example/authorize" + DECOY_SECRET.encode() + b"://cloudcode.example"
        + b"/v1beta/models/%s:generate" + FAKE_SECRET.encode() + b".sandbox.example" + b"\x00" * 4096
        + b"max)" + DECOY_ID.encode() + b"error" + FAKE_ID.encode() + b"version2"
    )
    path.write_bytes(blob)
    return path


def _client_checker(right: tuple[str, str], seen: list[tuple[str, str]], refreshed: str = "tok-refreshed"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == kp_ag.ANTIGRAVITY_TOKEN_URL
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["refresh_token"] == "rtok-1"
        pair = (form["client_id"], form["client_secret"])
        seen.append(pair)
        if pair == right:
            return httpx.Response(200, json={"access_token": refreshed, "expires_in": 3600})
        if pair[0] == right[0]:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "Unauthorized"})
        return httpx.Response(400, json={"error": "unauthorized_client", "error_description": "Unauthorized"})

    return handler


async def _refresh(auth: kp_ag.AntigravityAuth, handler) -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await auth.token(client)


async def test_refresh_uses_the_client_stored_in_the_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)
    data = json.loads(cred.read_text(encoding="utf-8"))
    data["client_id"], data["client_secret"] = "fake-client-id", "fake-client-secret"
    cred.write_text(json.dumps(data), encoding="utf-8")

    def no_scan(_binary: Path) -> list:
        raise AssertionError("the binary is read although the login carries the client")

    monkeypatch.setattr(kp_ag, "scan_cli", no_scan)
    monkeypatch.setenv("KEYPROXY_ANTIGRAVITY_CLI", str(_fake_cli(tmp_path / "agy")))
    seen: list[tuple[str, str]] = []
    assert await _refresh(kp_ag.AntigravityAuth(cred), _client_checker(("fake-client-id", "fake-client-secret"), seen)) == "tok-refreshed"
    assert seen == [("fake-client-id", "fake-client-secret")]
    saved = json.loads(cred.read_text(encoding="utf-8"))
    assert saved["token"]["access_token"] == "tok-refreshed"
    assert saved["client_id"] == "fake-client-id"


def test_scan_reads_fixed_length_pairs_out_of_packed_strings(tmp_path: Path) -> None:
    pairs = kp_ag.scan_cli(_fake_cli(tmp_path / "agy"))
    ids = {pair[0] for pair in pairs}
    secrets = {pair[1] for pair in pairs}
    assert {DECOY_ID, FAKE_ID} <= ids
    assert secrets == {DECOY_SECRET, FAKE_SECRET}
    # Same rank first: the layout agy has today puts the right pair in that position.
    assert pairs[:2] == [(DECOY_ID, DECOY_SECRET), (FAKE_ID, FAKE_SECRET)]
    # An id whose project number may have swallowed a neighbour's digit is also tried without it, last.
    assert pairs[-1][0] == FAKE_ID[1:]
    assert "***" in repr(pairs[0]) and FAKE_SECRET not in repr(pairs)


async def test_refresh_finds_the_client_in_the_cli_and_remembers_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)
    monkeypatch.setenv("KEYPROXY_ANTIGRAVITY_CLI", str(_fake_cli(tmp_path / "agy")))
    scans: list[Path] = []
    real_scan = kp_ag.scan_cli
    monkeypatch.setattr(kp_ag, "scan_cli", lambda binary: scans.append(binary) or real_scan(binary))

    seen: list[tuple[str, str]] = []
    assert await _refresh(kp_ag.AntigravityAuth(cred), _client_checker((FAKE_ID, FAKE_SECRET), seen)) == "tok-refreshed"
    assert seen == [(DECOY_ID, DECOY_SECRET), (FAKE_ID, FAKE_SECRET)]
    assert len(scans) == 1
    stored = json.loads((tmp_path / kp_ag.CLIENT_FILE).read_text(encoding="utf-8"))
    assert (stored["client_id"], stored["client_secret"]) == (FAKE_ID, FAKE_SECRET)
    assert (tmp_path / kp_ag.CLIENT_FILE).stat().st_mode & 0o077 == 0

    # The next refresh goes straight to the client that worked, without reading the binary again.
    _make_auth_file(cred, expired=True)
    seen.clear()
    assert await _refresh(kp_ag.AntigravityAuth(cred), _client_checker((FAKE_ID, FAKE_SECRET), seen, "tok-2")) == "tok-2"
    assert seen == [(FAKE_ID, FAKE_SECRET)]
    assert len(scans) == 1

    # A new process has only the file beside the login, and that is enough.
    monkeypatch.setattr(kp_ag, "_verified_client", None)
    monkeypatch.setattr(kp_ag, "_cli_scans", {})
    monkeypatch.delenv("KEYPROXY_ANTIGRAVITY_CLI")
    _make_auth_file(cred, expired=True)
    seen.clear()
    assert await _refresh(kp_ag.AntigravityAuth(cred), _client_checker((FAKE_ID, FAKE_SECRET), seen, "tok-3")) == "tok-3"
    assert seen == [(FAKE_ID, FAKE_SECRET)]
    assert len(scans) == 1


async def test_a_remembered_client_google_refuses_is_dropped_for_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)
    (tmp_path / kp_ag.CLIENT_FILE).write_text(json.dumps({"client_id": "stale-id", "client_secret": "stale-secret"}), encoding="utf-8")
    monkeypatch.setenv("KEYPROXY_ANTIGRAVITY_CLI", str(_fake_cli(tmp_path / "agy")))
    seen: list[tuple[str, str]] = []
    assert await _refresh(kp_ag.AntigravityAuth(cred), _client_checker((FAKE_ID, FAKE_SECRET), seen)) == "tok-refreshed"
    assert seen[0] == ("stale-id", "stale-secret") and seen[-1] == (FAKE_ID, FAKE_SECRET)
    stored = json.loads((tmp_path / kp_ag.CLIENT_FILE).read_text(encoding="utf-8"))
    assert stored["client_id"] == FAKE_ID


async def test_refresh_without_any_client_says_what_to_do(tmp_path: Path) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing to sign a refresh with, so nothing is sent")

    with pytest.raises(kp_ag.SubscriptionError, match="install or update agy on the host and sign in with it again"):
        await _refresh(kp_ag.AntigravityAuth(cred), handler)


async def test_refresh_when_google_refuses_every_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)
    monkeypatch.setenv("KEYPROXY_ANTIGRAVITY_CLI", str(_fake_cli(tmp_path / "agy")))
    seen: list[tuple[str, str]] = []
    with pytest.raises(kp_ag.SubscriptionError, match="refused every OAuth client.*update agy"):
        await _refresh(kp_ag.AntigravityAuth(cred), _client_checker(("other-id", "other-secret"), seen))
    assert len(seen) == len(set(seen)) == len(kp_ag.scan_cli(tmp_path / "agy"))
    assert not (tmp_path / kp_ag.CLIENT_FILE).exists()


async def test_a_refused_login_is_not_retried_with_other_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "antigravity-oauth-token"
    _make_auth_file(cred, expired=True)
    monkeypatch.setenv("KEYPROXY_ANTIGRAVITY_CLI", str(_fake_cli(tmp_path / "agy")))
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})

    with pytest.raises(kp_ag.SubscriptionError, match="HTTP 400"):
        await _refresh(kp_ag.AntigravityAuth(cred), handler)
    assert len(calls) == 1


def test_cli_binaries_follows_the_shim_agy_writes(tmp_path: Path) -> None:
    binary = _fake_cli(tmp_path / "elsewhere-agy")
    data_dir = tmp_path / "antigravity-cli"
    (data_dir / "bin").mkdir(parents=True)
    (data_dir / "bin" / "agentapi").write_text(f'#!/bin/sh\nexec "{binary}" agentapi "$@"\n', encoding="utf-8")
    assert kp_ag.cli_binaries(data_dir / "antigravity-oauth-token") == [binary]
    # An account's login finds the same shim in the default data folder above it.
    account = data_dir / "daedalus-accounts" / "work"
    account.mkdir(parents=True)
    assert kp_ag.cli_binaries(account / "antigravity-oauth-token") == [binary]


def test_cli_binaries_finds_the_installer_location(tmp_path: Path) -> None:
    installed = Path(kp_ag.os.environ["HOME"]) / ".local" / "bin" / "agy"
    installed.parent.mkdir(parents=True)
    _fake_cli(installed)
    assert kp_ag.cli_binaries(tmp_path / "antigravity-cli" / "antigravity-oauth-token") == [installed]


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
    decl_params = req["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"]
    assert "$schema" not in decl_params
    assert "additionalProperties" not in decl_params
    model_parts = req["contents"][1]["parts"]
    assert model_parts[0]["functionCall"] == {"name": "get_weather", "args": {"city": "Paris"}, "id": "call_1"}
    assert model_parts[0]["thoughtSignature"] == "sig-abc"
    tool_parts = req["contents"][2]["parts"]
    assert tool_parts[0]["functionResponse"] == {"name": "get_weather", "response": {"result": "Sunny, 20C"}, "id": "call_1"}


def test_chat_to_antigravity_adds_skip_thought_signature_for_cross_model_history() -> None:
    body: dict[str, Any] = {
        "model": "gemini-4-argon-medium",
        "messages": [
            {"role": "user", "content": "Run echo"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_from_claude", "type": "function", "function": {"name": "Exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_from_claude", "content": "ok"},
        ],
    }
    payload = kp_ag.chat_to_antigravity(body, project="aicode-consumers")
    model_parts = payload["request"]["contents"][1]["parts"]
    assert model_parts[0]["thoughtSignature"] == "skip_thought_signature_validator"


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
        assert usage_data["antigravity"]["windows"][0]["name"] == "Gemini (5h)"
        assert usage_data["antigravity"]["windows"][0]["used_percent"] == 10.0
    finally:
        await app["client"].aclose()
        await client.close()


def test_a_used_up_window_is_shown_full_rather_than_dropped() -> None:
    """Proto3 JSON leaves a zero out, so an exhausted quota carries a reset time and no fraction."""
    reset = "2026-11-26T22:54:37Z"
    data = {
        "models": {
            "gemini-4-carbon-high": {"quotaInfo": {"resetTime": reset}},
            "gemini-4-argon-high": {"quotaInfo": {"resetTime": reset}},
            "claude-sonnet-5-5-high": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-27T01:49:57Z"}},
            # A quota that says nothing at all is still no window, and a tab model never is one.
            "gemini-unknown": {"quotaInfo": {}},
            "tab_flash_lite_preview": {"quotaInfo": {"remainingFraction": 1}},
        }
    }
    view = kp_ag.antigravity_usage_view(data, tier="Pro")
    windows = {window["name"]: window for window in view["windows"]}
    assert list(windows) == ["Gemini (5h)", "Gemini 4 Argon (weekly)", "Claude / GPT (5h)"]
    assert windows["Gemini (5h)"] == {"name": "Gemini (5h)", "used_percent": 100.0, "resets_at": reset}
    assert windows["Gemini 4 Argon (weekly)"]["used_percent"] == 100.0
    assert windows["Claude / GPT (5h)"]["used_percent"] == 0.0
    assert view["limit_reached"] is True
