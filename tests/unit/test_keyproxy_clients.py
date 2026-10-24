"""The imitated clients' profiles: which version is in force, the release check, the update, its revert and its verdict."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
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
clients = sys.modules["clients"]
TOKEN = {"x-daedalus-token": "tok"}
CLAUDE_DEFAULT = clients.SPECS["claude"].defaults["version"]


@pytest.fixture(autouse=True)
def _own_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Every test file loads its own copy of the proxy, and the last one loaded owns the shared hooks: this one takes them back."""
    for spec in clients.SPECS.values():
        monkeypatch.delenv(spec.pin_variable, raising=False)
        monkeypatch.delenv(spec.variable, raising=False)
    monkeypatch.setenv("KEYPROXY_GROK_AUTH", str(tmp_path / "no-grok" / "auth.json"))
    monkeypatch.setattr(proxy, "agent_api_token", lambda: "tok")
    monkeypatch.setattr(proxy, "KEYS_FILE", "")
    clients.configure(read=proxy.settings, write=proxy._write_client_setting, writable=lambda: bool(proxy.KEYS_FILE))
    clients._latest.clear()
    yield
    clients._latest.clear()


def _keys_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str = "DEEPSEEK_API_KEY=ds\n") -> Path:
    path = tmp_path / "keyproxy.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(proxy, "KEYS_FILE", str(path))
    monkeypatch.setattr(proxy, "_keys_file_cache", (-1, -1, {}))
    return path


class Upstream:
    """The registries and Anthropic, faked: what each answers can be changed between calls."""

    def __init__(self) -> None:
        self.versions = {"@anthropic-ai/claude-code": "2.9.1", "@openai/codex": "0.999.0"}
        self.grok = "1.9.9"
        self.registry_down = False
        self.messages_status = 200
        self.messages_body = ""
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = str(request.url)
        if url.startswith("https://registry.npmjs.org/"):
            if self.registry_down:
                return httpx.Response(503)
            package = url.removeprefix("https://registry.npmjs.org/").removesuffix("/latest").replace("%2F", "/")
            return httpx.Response(200, json={"name": package, "version": self.versions[package]})
        if url == "https://x.ai/cli/stable":
            return httpx.Response(200, text=self.grok + "\n")
        if "/v1/messages" in url:
            if self.messages_status >= 400:
                return httpx.Response(self.messages_status, text=self.messages_body)
            sse = "\n".join([
                "event: message_start", "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}), "",
                "event: content_block_delta", "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "PONG"}}), "",
                "event: message_stop", "data: " + json.dumps({"type": "message_stop"}), "",
            ])
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


async def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: Upstream) -> Any:
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "refreshToken": "r", "expiresAt": 4102444800000}}))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(cred))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(tmp_path / "no-codex.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(tmp_path / "no-grok.json"))
    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return app


def test_the_newest_of_default_stored_and_installed_is_in_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _keys_file(monkeypatch, tmp_path)
    assert clients.profile("claude")["source"] == "default" and clients.version("claude") == CLAUDE_DEFAULT
    # A newer stored update wins, with the fields it learned for that release.
    proxy.write_env_value(proxy.KEYS_FILE, "KEYPROXY_CLIENT_CLAUDE", json.dumps({"fields": {"version": "9.0.0", "stainless_package_version": "0.200.0"}}))
    current = clients.profile("claude")
    assert current["version"] == "9.0.0" and current["source"] == "stored" and current["stainless_package_version"] == "0.200.0"
    # An older one, stored before an image that ships a newer default, holds nothing back — not even its other fields.
    proxy.write_env_value(proxy.KEYS_FILE, "KEYPROXY_CLIENT_CLAUDE", json.dumps({"fields": {"version": "1.0.0", "stainless_package_version": "0.001.0"}}))
    current = clients.profile("claude")
    assert current["version"] == CLAUDE_DEFAULT and current["stainless_package_version"] == clients.SPECS["claude"].defaults["stainless_package_version"]
    # Damage in the stored value is the built-in profile, not a proxy that cannot start.
    proxy.write_env_value(proxy.KEYS_FILE, "KEYPROXY_CLIENT_CLAUDE", "{not json")
    assert clients.profile("claude")["source"] == "default"
    # The pin variable wins over all of it.
    monkeypatch.setenv("KEYPROXY_CLAUDE_CLI_VERSION", "2.0.0")
    assert clients.profile("claude")["version"] == "2.0.0" and clients.profile("claude")["source"] == "environment"


async def test_update_check_verify_and_revert_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _keys_file(monkeypatch, tmp_path)
    upstream = Upstream()
    app = await _app(monkeypatch, tmp_path, upstream)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/clients")).status == 403, "the profiles answer the bot alone"
        listed = await (await client.get("/clients", headers=TOKEN)).json()
        rows = {row["name"]: row for row in listed["clients"]}
        assert set(rows) >= {"claude", "codex", "grok"} and listed["writable"] is True
        assert rows["claude"]["latest"] is None and rows["claude"]["stale"] is False and rows["claude"]["logged_in"] is True
        assert rows["codex"]["logged_in"] is False

        checked = {row["name"]: row for row in (await (await client.post("/clients/check", headers=TOKEN)).json())["clients"]}
        assert checked["claude"]["latest"] == "2.9.1" and checked["claude"]["stale"] is True
        assert checked["grok"]["latest"] == "1.9.9" and checked["codex"]["latest"] == "0.999.0"
        assert checked["claude"]["kept_on_update"] == ["stainless_package_version", "stainless_runtime_version", "betas"]

        done = await client.post("/clients/claude/update", headers=TOKEN)
        result = await done.json()
        assert done.status == 200 and result["changed"] == {"version": {"from": CLAUDE_DEFAULT, "to": "2.9.1"}}
        assert set(result["kept"]) == {"stainless_package_version", "stainless_runtime_version", "betas"}
        assert result["client"]["version"] == "2.9.1" and result["client"]["pending"] is True and result["client"]["previous_version"] == CLAUDE_DEFAULT
        assert "KEYPROXY_CLIENT_CLAUDE=" in path.read_text(encoding="utf-8") and "DEEPSEEK_API_KEY=ds" in path.read_text(encoding="utf-8")

        # The next request carries the new identity, and its success confirms the update.
        await client.post("/claude/v1/chat/completions", json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hello there"}]})
        sent = [r for r in upstream.seen if "/v1/messages" in str(r.url)][-1]
        assert sent.headers["user-agent"] == "claude-cli/2.9.1 (external, sdk-cli)"
        assert "cc_version=2.9.1." in json.loads(sent.content)["system"][0]["text"]
        row = next(r for r in (await (await client.get("/clients", headers=TOKEN)).json())["clients"] if r["name"] == "claude")
        assert row["pending"] is False and row["verified_at"] and row["stale"] is False

        # Revert by hand goes back one step.
        reverted = await (await client.post("/clients/claude/revert", headers=TOKEN)).json()
        assert reverted["client"]["version"] == CLAUDE_DEFAULT and reverted["reverted"]["automatic"] is False
        assert (await client.post("/clients/claude/revert", headers=TOKEN)).status == 409, "one step back, not two"
        assert (await client.post("/clients/nobody/update", headers=TOKEN)).status == 404


async def test_a_refusal_naming_the_version_reverts_on_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _keys_file(monkeypatch, tmp_path)
    upstream = Upstream()
    app = await _app(monkeypatch, tmp_path, upstream)
    async with TestClient(TestServer(app)) as client:
        await client.post("/clients/claude/update", headers=TOKEN)
        # A rate limit says nothing about the identity: the update stays on trial.
        upstream.messages_status, upstream.messages_body = 429, json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
        await client.post("/claude/v1/chat/completions", json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]})
        assert clients.version("claude") == "2.9.1" and clients.stored("claude").get("pending") is True
        upstream.messages_status = 400
        upstream.messages_body = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "claude_code_version_too_old"}})
        await client.post("/claude/v1/chat/completions", json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]})
        row = next(r for r in (await (await client.get("/clients", headers=TOKEN)).json())["clients"] if r["name"] == "claude")
        assert row["version"] == CLAUDE_DEFAULT and row["reverted"]["automatic"] is True and "version_too_old" in row["reverted"]["reason"]


async def test_an_update_needs_a_writable_file_and_an_unpinned_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    upstream = Upstream()
    app = await _app(monkeypatch, tmp_path, upstream)
    async with TestClient(TestServer(app)) as client:
        refused = await client.post("/clients/claude/update", headers=TOKEN)
        assert refused.status == 409 and "environment" in (await refused.json())["error"]
        _keys_file(monkeypatch, tmp_path)
        monkeypatch.setenv("KEYPROXY_CLAUDE_CLI_VERSION", "2.0.0")
        pinned = await client.post("/clients/claude/update", headers=TOKEN)
        assert pinned.status == 409 and "KEYPROXY_CLAUDE_CLI_VERSION" in (await pinned.json())["error"]
        monkeypatch.delenv("KEYPROXY_CLAUDE_CLI_VERSION")
        assert (await client.post("/clients/claude/update", headers=TOKEN, json={"version": "not a version"})).status == 400
        chosen = await client.post("/clients/grok/update", headers=TOKEN, json={"version": "3.1.4"})
        assert chosen.status == 200 and subs.grok_version() == "3.1.4"


async def test_a_failed_check_is_remembered_and_retried_later(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.registry_down = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        await clients.check(http)
        state = clients._latest["claude"]
        assert state["latest"] == "" and "503" in state["error"] and state["failures"] == 1
        asked = len(upstream.seen)
        await clients.check(http)
        assert len(upstream.seen) == asked, "a failed check waits before asking again"
        upstream.registry_down = False
        await clients.check(http, force=True)
        assert clients._latest["claude"]["latest"] == "2.9.1" and clients._latest["claude"]["error"] == ""
    view = clients.view("claude")
    assert view["stale"] is True and view["check_error"] is None


def test_versions_order_by_number_not_by_text() -> None:
    assert clients.newer("2.1.310", "2.1.296") and clients.newer("1.0.70", "1.0.62") and not clients.newer("0.99.0", "0.164.0")
    assert not clients.newer("2.1.310-beta", "2.1.296"), "anything but a plain release is never offered"
