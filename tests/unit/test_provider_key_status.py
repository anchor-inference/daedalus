"""Which endpoints really have a credential, asked of the process that holds them.

An installation reaches most of its endpoints through the key proxy, and its own configuration says
only which address each one is at — never whether anything behind it can authenticate. Reading
readiness out of the configuration is how a machine holding one key came to offer six ready
endpoints, five of which answered the first request with a 404 that mentioned a URL and not a key.

So the proxy answers the question, on the bot's own API token, and these tests pin both halves: what
the proxy reports about itself, and what the app is told as a result.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from daedalus import doctor
from daedalus.config import RuntimeConfig, Settings, is_keyproxy_url, keyproxy_unresolved, keyproxy_upstream
from daedalus.extensions import api as api_module
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
if str(KEYPROXY_DIR) not in sys.path:
    sys.path.insert(0, str(KEYPROXY_DIR))

H = {"X-Daedalus-Token": "tok"}
NATIVE_BASE = "http://127.0.0.1:3201"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


proxy = _load("proxy")


# -- the key proxy's own answer ----------------------------------------------------------------


def test_key_status_names_every_upstream_and_what_its_credential_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One key set, one CLI logged in: the answer says which, and says "no" about the rest."""
    for var in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-not-a-real-key-at-all")
    codex = tmp_path / "codex.json"
    codex.write_text(json.dumps({"tokens": {"access_token": "a"}}), encoding="utf-8")
    monkeypatch.setattr(proxy, "CODEX_AUTH", SimpleNamespace(available=lambda: True, path=codex))
    monkeypatch.setattr(proxy, "GROK_AUTH", SimpleNamespace(available=lambda: False, path=tmp_path / "grok.json"))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", SimpleNamespace(available=lambda: True, path=tmp_path / "claude.json"))

    status = proxy.key_status()
    assert status["openrouter"] == {"configured": True, "kind": "api_key"}
    assert status["deepseek"] == {"configured": False, "kind": "api_key"}
    assert status["codex"] == {"configured": True, "kind": "cli_login"}
    assert status["grok"] == {"configured": False, "kind": "cli_login"}
    # Present per `available()` but not there to read: that is not a login, and saying it is sends
    # the operator to a screen where everything looks fine.
    assert status["claude"] == {"configured": False, "kind": "cli_login"}
    assert "sk-not-a-real-key-at-all" not in json.dumps(status), "the answer carries a key"


def test_an_extra_upstream_with_no_key_is_an_endpoint_not_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYPROXY_UPSTREAM_WORKSHOP", "http://workshop.invalid/v1")
    monkeypatch.delenv("KEYPROXY_KEY_WORKSHOP", raising=False)
    monkeypatch.setenv("KEYPROXY_UPSTREAM_SERPER", "https://google.serper.dev")
    monkeypatch.setenv("KEYPROXY_KEY_SERPER", "s3rp3r")
    status = proxy.key_status()
    assert status["workshop"] == {"configured": True, "kind": "endpoint"}
    assert status["serper"] == {"configured": True, "kind": "api_key"}


async def test_keys_answers_the_agent_and_nobody_else(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "agent_api_token", lambda: "tok")
    async with TestClient(TestServer(proxy.make_app())) as client:
        assert (await client.get("/keys")).status == 403
        assert (await client.get("/keys", headers={"x-daedalus-token": "guessed"})).status == 403
        response = await client.get("/keys", headers={"x-daedalus-token": "tok"})
        assert response.status == 200
        assert "deepseek" in (await response.json())["upstreams"]


async def test_a_token_header_that_is_not_ascii_is_refused_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headers arrive decoded as latin-1, so a caller can put anything in one; the answer is still 403."""
    monkeypatch.setattr(proxy, "agent_api_token", lambda: "tok")
    async with TestClient(TestServer(proxy.make_app())) as client:
        response = await client.get("/keys", headers={"x-daedalus-token": "tok\u00ff"})
        assert response.status == 403


async def test_a_known_upstream_with_no_key_says_so_instead_of_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 on ``/deepseek/models`` reads as a moved path; the fact is that nothing can sign the call."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    async with TestClient(TestServer(proxy.make_app())) as client:
        response = await client.get("/deepseek/v1/models")
        assert response.status == 503
        body = await response.json()
        assert body["error"]["type"] == "no_credential" and "deepseek" in body["error"]["message"]
        assert (await client.get("/never-heard-of-it/v1/models")).status == 404


# -- what the app is told ------------------------------------------------------------------------


REAL_CLIENT = httpx.AsyncClient
"""Captured before any patching: the stubs below replace the name the API builds its clients from."""


def _answering(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Every HTTP client the API builds talks to ``handler`` instead of the network."""

    def build(**_: Any) -> httpx.AsyncClient:
        return REAL_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(api_module.httpx, "AsyncClient", build)


def _keyproxy_answering(monkeypatch: pytest.MonkeyPatch, upstreams: dict[str, dict[str, Any]] | None, *, seen: list[httpx.Request]) -> None:
    """Point the app's key-proxy client at a proxy that reports ``upstreams``; None for one that is down."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if upstreams is None:
            raise httpx.ConnectError("nothing is listening", request=request)
        if request.headers.get("x-daedalus-token") != "tok":
            return httpx.Response(403, json={"error": "this endpoint answers the agent only"})
        return httpx.Response(200, json={"upstreams": upstreams})

    _answering(monkeypatch, handler)


def _config() -> RuntimeConfig:
    """The shipped endpoints as a native installation has them: one loopback port, no key in sight."""
    raw = RuntimeConfig().model_dump(mode="json")
    for entry in raw["providers"].values():
        if entry["base_url"].startswith("http://keyproxy:3200"):
            entry["base_url"] = NATIVE_BASE + entry["base_url"][len("http://keyproxy:3200") :]
    return RuntimeConfig.model_validate(raw)


@pytest.fixture
async def client(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYPROXY_BASE_URL", NATIVE_BASE)
    config = _config()
    manager = SessionManager(settings, config, db=db)
    await manager.start()

    async def save_config(cfg: RuntimeConfig) -> None:
        app.config = cfg
        manager.reload_config(cfg)

    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session, save_config=save_config)
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as http:  # type: ignore[arg-type]
        yield http
    await manager.close()


def test_a_loopback_key_proxy_is_recognised_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in ``http://127.0.0.1:3201/deepseek`` says "key proxy"; the address it was given does."""
    monkeypatch.setenv("KEYPROXY_BASE_URL", NATIVE_BASE)
    assert is_keyproxy_url(NATIVE_BASE + "/deepseek") is True
    assert keyproxy_upstream(NATIVE_BASE + "/claude/v1") == "claude"
    assert is_keyproxy_url("http://keyproxy:3200/openrouter") is False, "that is a container's address, not this machine's"
    monkeypatch.delenv("KEYPROXY_BASE_URL", raising=False)
    assert is_keyproxy_url("http://keyproxy:3200/openrouter") is True, "a container still reaches it by name"
    monkeypatch.setenv("KEYPROXY_BASE_URL", NATIVE_BASE)
    assert is_keyproxy_url("http://10.0.0.5:9000/v1") is False
    assert keyproxy_upstream("http://10.0.0.5:9000/v1") == ""


ONE_KEY = {
    "openrouter": {"configured": True, "kind": "api_key"},
    "deepseek": {"configured": False, "kind": "api_key"},
    "opencode": {"configured": False, "kind": "api_key"},
    "codex": {"configured": True, "kind": "cli_login"},
    "grok": {"configured": True, "kind": "cli_login"},
    "claude": {"configured": False, "kind": "cli_login"},
}


async def test_only_the_endpoints_with_a_credential_are_ready(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    _keyproxy_answering(monkeypatch, ONE_KEY, seen=seen)
    body = (await client.get("/api/onboarding", headers=H)).json()
    ready = {p["id"]: p["ready"] for p in body["providers"]}
    assert ready["openrouter"] is True
    assert ready["codex"] is True and ready["grok"] is True, "a CLI logged in on this machine is a credential"
    assert ready["deepseek"] is False and ready["opencode"] is False and ready["claude"] is False
    kinds = {p["id"]: p["key_kind"] for p in body["providers"]}
    assert kinds["codex"] == "cli_login" and kinds["deepseek"] == "api_key"
    assert all(p["via_proxy"] for p in body["providers"] if p["id"] in ONE_KEY)
    assert seen and seen[0].url.path == "/keys", "the proxy was never asked"


async def test_a_key_proxy_that_cannot_be_asked_says_it_does_not_know(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable is not "no key": the app shows the endpoints without claiming either way."""
    seen: list[httpx.Request] = []
    _keyproxy_answering(monkeypatch, None, seen=seen)
    body = (await client.get("/api/onboarding", headers=H)).json()
    held = {p["id"]: p["key_held"] for p in body["providers"] if p["via_proxy"]}
    assert held and set(held.values()) == {None}


async def test_an_endpoint_reached_directly_needs_no_proxy_to_be_ready(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _keyproxy_answering(monkeypatch, ONE_KEY, seen=[])
    assert (await client.put("/api/providers/workshop", json={"kind": "openai_compat", "base_url": "http://10.0.0.5:9000/v1", "api_key": ""}, headers=H)).status_code == 200
    body = (await client.get("/api/onboarding", headers=H)).json()
    workshop = next(p for p in body["providers"] if p["id"] == "workshop")
    assert workshop["via_proxy"] is False
    assert workshop["key_held"] is True and workshop["ready"] is True
    assert workshop["key_kind"] == "endpoint", "a self-hosted endpoint that needs no key is not one whose key is missing"


async def test_listing_the_models_of_an_unkeyed_endpoint_says_what_is_missing(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported symptom: pick DeepSeek, get two URLs and two 404s, and nothing about a key."""
    seen: list[httpx.Request] = []
    _keyproxy_answering(monkeypatch, ONE_KEY, seen=seen)
    response = await client.post("/api/providers/lookup-models", json={"provider": "deepseek"}, headers=H)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "key" in detail and "deepseek" in detail
    assert "404" not in detail and "http://" not in detail
    # The app refreshes its model prices from models.dev in the background from its start, through
    # the same patched transport; on a loaded machine that request landed in this window and failed
    # the test. What must not happen is a probe of the endpoint that has no key.
    probes = [r for r in seen if r.url.path != "/keys" and r.url.host != "models.dev"]
    assert probes == [], "the endpoint was probed anyway"

    claude = await client.post("/api/providers/lookup-models", json={"provider": "claude"}, headers=H)
    assert claude.status_code == 400 and "signed in" in claude.json()["detail"]


async def test_a_keyed_endpoint_is_listed_through_the_proxy(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a credential the lookup goes ahead, and ``/v1`` is found for a base written without it."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/keys":
            return httpx.Response(200, json={"upstreams": ONE_KEY})
        asked.append(str(request.url))
        if not request.url.path.startswith("/openrouter/v1/"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(200, json={"data": [{"id": "z-ai/glm-5.3", "context_length": 200000}]})

    _answering(monkeypatch, handler)
    response = await client.post("/api/providers/lookup-models", json={"provider": "openrouter"}, headers=H)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["models"] == ["z-ai/glm-5.3"]
    assert body["base_url"].endswith("/openrouter/v1")
    assert asked[0].endswith("/openrouter/models") and asked[1].endswith("/openrouter/v1/models")


async def test_an_endpoint_that_lists_nothing_says_so_in_its_own_words(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vendor with no list endpoint is not a failure to hide: the typed-id path is still there."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/keys":
            return httpx.Response(200, json={"upstreams": ONE_KEY})
        return httpx.Response(404, json={"error": {"message": "this endpoint does not list models"}})

    _answering(monkeypatch, handler)
    response = await client.post("/api/providers/lookup-models", json={"provider": "openrouter"}, headers=H)
    assert response.status_code == 502
    assert "this endpoint does not list models" in response.json()["detail"]


# -- where the token goes ------------------------------------------------------------------------


async def test_the_token_is_offered_to_the_proxys_own_address_and_to_nothing_else(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider list is operator-writable, so it may not decide where a credential is sent.

    An endpoint added through Add a model with the word in its URL used to become the address the
    bot asked for keys — with the bot's own API token in the header, which is the token that opens
    every route of this API.
    """
    seen: list[httpx.Request] = []
    _keyproxy_answering(monkeypatch, ONE_KEY, seen=seen)
    hostile = {"kind": "openai_compat", "base_url": "https://evil.example.com/keyproxy/v1", "api_key": ""}
    assert (await client.put("/api/providers/hostile", json=hostile, headers=H)).status_code == 200

    body = (await client.get("/api/onboarding", headers=H)).json()
    entry = next(p for p in body["providers"] if p["id"] == "hostile")
    assert entry["via_proxy"] is False, "a provider URL that merely says keyproxy was believed"
    assert [r.url.host for r in seen if r.headers.get("x-daedalus-token")] == ["127.0.0.1"]
    assert not [r for r in seen if r.url.host == "evil.example.com"], "the bot called a host it was never pointed at"

    lookup = await client.post("/api/providers/lookup-models", json={"provider": "hostile"}, headers=H)
    assert lookup.status_code in (400, 502)
    assert not [r for r in seen if r.url.host == "evil.example.com" and r.headers.get("x-daedalus-token")]


def test_an_address_nobody_gave_this_process_is_not_guessed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no KEYPROXY_BASE_URL the bot cannot recognise its own proxy, and says so instead of assuming.

    The silent version of this reported six ready endpoints on a machine holding one key: a loopback
    provider read as an endpoint that needs no proxy, so nothing was ever asked about it.
    """
    monkeypatch.delenv("KEYPROXY_BASE_URL", raising=False)
    assert is_keyproxy_url("http://127.0.0.1:3200/deepseek") is False, "the token would go to whatever is on that port"
    assert keyproxy_unresolved("http://127.0.0.1:3200/deepseek") is True
    assert keyproxy_unresolved("https://evil.example.com/keyproxy/v1") is False
    monkeypatch.setenv("KEYPROXY_BASE_URL", NATIVE_BASE)
    assert keyproxy_unresolved(NATIVE_BASE + "/deepseek") is False
    assert keyproxy_unresolved("http://127.0.0.1:9999/v1") is False, "the address is known; this is just another endpoint"


async def test_the_doctor_says_when_the_proxy_address_is_missing(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.delenv("KEYPROXY_BASE_URL", raising=False)
    config = _config()  # the loopback addresses a native install holds, with nothing to match them against
    ctx = doctor.DoctorContext(settings=settings, config=config)
    checks = await doctor._keyproxy(ctx)
    assert len(checks) == 1 and checks[0].ok is False and "KEYPROXY_BASE_URL" in checks[0].message
    monkeypatch.setenv("KEYPROXY_BASE_URL", NATIVE_BASE)
    assert (await doctor._keyproxy(ctx))[0].ok is True


def test_a_base_url_without_a_scheme_is_answered_not_raised() -> None:
    """A provider saved with a bare host reached ``/api/onboarding``, which is the first screen."""
    assert keyproxy_upstream("my-keyproxy-host") == ""
    assert is_keyproxy_url("my-keyproxy-host") is False
    assert keyproxy_unresolved("my-keyproxy-host") is False
