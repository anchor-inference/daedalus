"""The key proxy serves more than one account per subscription: a call names its account and is signed with it.

Every login here is a file written by the test where the CLI would write it after a sign-in, and every vendor
is a fake that says which credential it was called with. What is proved is the proxy's half: which file or key
signs a call for which account, that the account header never leaves the proxy, and how accounts are made,
listed and forgotten. A real sign-in against a vendor is not something a test can do.
"""

from __future__ import annotations

import base64
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
accounts = sys.modules["accounts"]
H = {"x-daedalus-token": "tok"}


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def _codex_login(path: Path, token: str, email: str, plan: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    path.write_text(json.dumps({"tokens": {"access_token": _jwt({"exp": 4102444800}) + token, "account_id": token, "id_token": _jwt(claims)}}), encoding="utf-8")


def _grok_login(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"operator": {"key": token}}), encoding="utf-8")


@pytest.fixture
def homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The three CLI folders of one home, with the default logins of Codex and Grok in them and no Claude."""
    home = tmp_path / "home"
    _codex_login(home / ".codex" / "auth.json", "codex-default", "me@example.org", "plus")
    _grok_login(home / ".grok" / "auth.json", "grok-default")
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(home / ".codex" / "auth.json"))
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(home / ".grok" / "auth.json"))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(home / ".claude" / ".credentials.json"))
    monkeypatch.setattr(proxy, "_account_auths", {})
    monkeypatch.setattr(proxy, "agent_api_token", lambda: "tok")
    keys = tmp_path / "keyproxy.env"
    keys.write_text("OPENCODE_API_KEY=oc-default\n", encoding="utf-8")
    monkeypatch.setattr(proxy, "KEYS_FILE", str(keys))
    monkeypatch.setattr(proxy, "_keys_file_cache", (-1, -1, {}))
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    return home


def _app(seen: list[httpx.Request]) -> Any:
    def vendor(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    app = proxy.make_app()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(vendor))
    return app


def test_an_account_id_is_a_folder_name_and_nothing_more() -> None:
    assert accounts.account_id("") == "" and accounts.account_id("default") == "" and accounts.account_id(" Work1 ") == "work1"
    for bad in ("../x", "a/b", "a b", "x" * 33, "work-1"):
        with pytest.raises(accounts.AccountRefused):
            accounts.account_id(bad)
    default = Path("/h/.codex/auth.json")
    assert accounts.login_path(default, "") == default
    assert accounts.login_path(default, "w1") == Path("/h/.codex/daedalus-accounts/w1/auth.json")
    assert accounts.key_variable("OPENCODE_API_KEY", "w1") == "OPENCODE_API_KEY__W1"


async def test_a_grok_call_is_signed_with_the_account_it_names(homes: Path) -> None:
    _grok_login(homes / ".grok" / "daedalus-accounts" / "work" / "auth.json", "grok-work")
    seen: list[httpx.Request] = []
    async with TestClient(TestServer(_app(seen))) as client:
        default = await client.post("/grok/v1/chat/completions", json={"model": "grok-4", "messages": []})
        work = await client.post("/grok/v1/chat/completions", json={"model": "grok-4", "messages": []}, headers={"x-daedalus-account": "work"})
        unknown = await client.post("/grok/v1/chat/completions", json={"model": "grok-4", "messages": []}, headers={"x-daedalus-account": "../etc"})
    assert default.status == 200 and default.headers["x-keyproxy-account"] == "default"
    assert work.status == 200 and work.headers["x-keyproxy-account"] == "work"
    assert unknown.status == 400, "a name that is not an account is refused, never served by the default login"
    assert [r.headers["authorization"] for r in seen] == ["Bearer grok-default", "Bearer grok-work"]
    assert all("x-daedalus-account" not in r.headers for r in seen), "the account header stays at the proxy"


async def test_an_account_with_no_login_is_refused_rather_than_signed_by_the_default(homes: Path) -> None:
    seen: list[httpx.Request] = []
    async with TestClient(TestServer(_app(seen))) as client:
        response = await client.post("/grok/v1/chat/completions", json={"model": "grok-4", "messages": []}, headers={"x-daedalus-account": "nobody"})
    assert response.status == 502 and not seen


async def test_a_plan_account_is_a_second_key_for_the_same_upstream(homes: Path) -> None:
    seen: list[httpx.Request] = []
    async with TestClient(TestServer(_app(seen))) as client:
        missing = await client.post("/opencode/chat/completions", json={"model": "m"}, headers={"x-daedalus-account": "spare"})
        stored = await client.post("/accounts/opencode/spare", json={"key": "oc-spare\nEVIL=1"}, headers=H)
        spare = await client.post("/opencode/chat/completions", json={"model": "m"}, headers={"x-daedalus-account": "spare"})
        default = await client.post("/opencode/chat/completions", json={"model": "m"})
        listed = await (await client.get("/accounts", headers=H)).json()
        forgotten = await client.delete("/accounts/opencode/spare", headers=H)
        after = await (await client.get("/accounts", headers=H)).json()
    assert missing.status == 503 and stored.status == 200 and spare.status == 200 and default.status == 200
    assert [r.headers["authorization"] for r in seen] == ["Bearer oc-spareEVIL=1", "Bearer oc-default"]
    assert "EVIL=1\n" not in Path(proxy.KEYS_FILE).read_text(encoding="utf-8").replace("oc-spareEVIL=1", "")
    assert [a["id"] for a in listed["upstreams"]["opencode"]] == ["default", "spare"]
    assert forgotten.status == 200 and [a["id"] for a in after["upstreams"]["opencode"]] == ["default"]


async def test_accounts_are_made_listed_with_who_they_are_and_forgotten(homes: Path) -> None:
    seen: list[httpx.Request] = []
    async with TestClient(TestServer(_app(seen))) as client:
        refused = await client.post("/accounts/codex/work")
        made = await client.post("/accounts/codex/work", headers=H)
        made_body = await made.json()
        # What `CODEX_HOME=<that folder> codex login` leaves behind, written by the test.
        _codex_login(homes / ".codex" / "daedalus-accounts" / "work" / "auth.json", "codex-work", "work@example.org", "pro")
        listed = await (await client.get("/accounts", headers=H)).json()
        no_default = await client.delete("/accounts/codex/default", headers=H)
        forgotten = await client.delete("/accounts/codex/work", headers=H)
    assert refused.status == 403
    assert made.status == 200 and made_body["home"] == "daedalus-accounts/work"
    codex = {row["id"]: row for row in listed["upstreams"]["codex"]}
    assert codex["default"]["email"] == "me@example.org" and codex["default"]["plan"] == "plus" and codex["default"]["configured"] is True
    assert codex["work"]["email"] == "work@example.org" and codex["work"]["plan"] == "pro"
    assert [row["configured"] for row in listed["upstreams"]["claude"]] == [False]
    assert "codex-work" not in json.dumps(listed), "no token is ever listed"
    assert no_default.status == 400 and (homes / ".codex" / "auth.json").exists(), "the CLI's own login is never removed from here"
    assert forgotten.status == 200 and not (homes / ".codex" / "daedalus-accounts" / "work").exists()


async def test_every_account_has_its_own_usage_reading(homes: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _codex_login(homes / ".codex" / "daedalus-accounts" / "work" / "auth.json", "codex-work", "w@example.org", "pro")
    monkeypatch.setattr(proxy, "_usage_cache", {})

    def vendor(request: httpx.Request) -> httpx.Response:
        used = 90 if request.headers.get("chatgpt-account-id") == "codex-work" else 10
        if request.url.path.endswith("/wham/usage"):
            return httpx.Response(200, json={"plan_type": "pro", "rate_limit": {"primary_window": {"used_percent": used, "limit_window_seconds": 18000}}})
        return httpx.Response(404)

    app = proxy.make_app()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(vendor))
    async with TestClient(TestServer(app)) as client:
        usage = await (await client.get("/subscriptions/usage")).json()
    assert usage["codex"]["windows"][0]["used_percent"] == 10
    assert usage["codex"]["accounts"]["work"]["windows"][0]["used_percent"] == 90
