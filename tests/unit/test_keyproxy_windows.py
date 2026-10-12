"""The key proxy's model listings carry the windows the subscriptions state, so the bot need not guess them."""

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


def _module(name: str) -> Any:
    # One copy per process: the subscription tests load the same files under the same names.
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


subs = _module("subscriptions")
claude_mod = _module("claude")
proxy = _module("proxy")

CACHE = {
    "client_version": "0.160.0",
    "models": [
        {"slug": "gpt-6.1-sol", "context_window": 272_000, "max_context_window": 872_000, "visibility": "list"},
        {"slug": "gpt-reserve", "context_window": 272_000, "visibility": "hide"},
        {"slug": "broken", "context_window": "lots", "visibility": "list"},
        {"display_name": "no slug"},
    ],
}


def test_the_codex_cli_catalogue_is_read_for_its_windows(tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps(CACHE))
    windows = proxy.codex_cached_windows(path)
    assert windows["gpt-6.1-sol"] == {"context_window": 272_000, "max_context_window": 872_000, "listed": True}
    assert windows["gpt-reserve"]["listed"] is False
    assert windows["broken"] == {"listed": True}
    assert proxy.codex_cached_windows(tmp_path / "missing.json") == {}
    (tmp_path / "bad.json").write_text("{")
    assert proxy.codex_cached_windows(tmp_path / "bad.json") == {}


@pytest.mark.asyncio
async def test_the_codex_and_claude_listings_carry_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/wham/usage" in url:
            return httpx.Response(200, json={"plan_type": "plus", "rate_limit": {}, "model_usage": {}})
        if "/v1/models" in url:
            assert request.url.params.get("limit") == "1000"
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5", "max_input_tokens": 1_000_000, "max_tokens": 128_000}, {"id": "claude-old"}]})
        return httpx.Response(404)

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "eyJ.e30.x", "refresh_token": "r", "account_id": "acct"}}))
    (codex_home / "models_cache.json").write_text(json.dumps(CACHE))
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "refreshToken": "r", "expiresAt": 4102444800000}}))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(codex_home / "auth.json"))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(cred))
    monkeypatch.setattr(subs, "_jwt_claims", lambda token: {"exp": 4102444800})
    monkeypatch.setattr(proxy, "_usage_cache", {})
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        codex = {m["id"]: m for m in (await (await client.get("/codex/v1/models")).json())["data"]}
        assert codex["gpt-6.1-sol"]["context_window"] == 272_000 and codex["gpt-6.1-sol"]["max_context_window"] == 872_000
        assert "listed" not in codex["gpt-6.1-sol"]
        assert "gpt-reserve" not in codex  # hidden in the CLI's own picker
        claude = {m["id"]: m for m in (await (await client.get("/claude/v1/models")).json())["data"]}
        assert claude["claude-opus-5-5"]["max_input_tokens"] == 1_000_000 and claude["claude-opus-5-5"]["max_tokens"] == 128_000
        assert "max_input_tokens" not in claude["claude-old"]
