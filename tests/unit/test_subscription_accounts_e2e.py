"""Two accounts of one subscription, end to end: a turn on the first, the first runs out, the turn goes on the second.

Everything between the operator and the vendor is the real code: the app's HTTP API, the session manager and
its engine, the provider adapter, and the key proxy itself, served on a loopback port. Two things are fakes,
and only two, because a test cannot do them for real:

- the vendor (Grok's chat proxy) is a transport that answers by the token it is called with, and refuses the
  first account's token with the 429 a spent plan answers once the test says the plan is spent;
- the sign-in: the terminal service records the sign-in it is asked to open, and the test then writes the
  login file where ``GROK_HOME=<the account's home> grok login`` writes it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestServer

from daedalus.config import ModelConfig, ModelPresetConfig, ProviderConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config

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
REAL_CLIENT = httpx.AsyncClient
PRESET = "grok.grok-4"
SPENT = {"error": {"code": "usage_limit_reached", "type": "usage_limit_reached", "message": "You have reached your plan's limit."}}


class Vendor:
    """Grok's chat proxy, as far as a test can be it: an answer per token, and a plan that can be spent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.spent: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not request.url.path.endswith("/chat/completions"):
            # The plan's usage, which the accounts screen reads for every account: a quiet answer, not a call.
            return httpx.Response(200, json={"config": {"creditUsagePercent": 0}})
        if token in self.spent:
            self.calls.append((token, 429))
            return httpx.Response(429, json=SPENT)
        self.calls.append((token, 200))
        body = json.loads(request.content or b"{}")
        text = f"answered on {token}"
        usage = {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}
        if not body.get("stream"):
            return httpx.Response(200, json={"id": "c", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": usage})
        frames = [
            {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"id": "c", "object": "chat.completion.chunk", "choices": [], "usage": usage},
        ]
        sse = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


class Terminals:
    """The terminal service as far as a sign-in needs it: the machine's home, and the terminals asked for."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.opened: list[Any] = []

    def environments(self) -> list[Any]:
        return [SimpleNamespace(env="host", available=True, home=str(self.home))]

    async def create(self, spec: Any, *, confirm_over_cap: bool = False) -> dict[str, Any]:
        self.opened.append(spec)
        return {"id": f"term-{len(self.opened)}"}


def _write_grok_login(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"operator": {"key": token}}), encoding="utf-8")


async def _turn(manager: SessionManager, session_id: str, text: str) -> str:
    done: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def finished(sid: str, _run_id: str, status: str) -> None:
        if sid == session_id and not done.done():
            done.set_result(status)

    manager.on_finished(finished)
    await manager.submit(session_id, text)
    return await asyncio.wait_for(done, timeout=30)


@pytest.fixture
async def stand(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    home = tmp_path / "home"
    _write_grok_login(home / ".grok", "token-personal")
    monkeypatch.setattr(proxy, "GROK_AUTH", subs.GrokAuth(home / ".grok" / "auth.json"))
    monkeypatch.setattr(proxy, "CODEX_AUTH", subs.CodexAuth(home / ".codex" / "auth.json"))
    monkeypatch.setattr(proxy, "CLAUDE_AUTH", claude_mod.ClaudeAuth(home / ".claude" / ".credentials.json"))
    monkeypatch.setattr(proxy, "_account_auths", {})
    monkeypatch.setattr(proxy, "agent_api_token", lambda: "tok")
    vendor = Vendor()
    keyproxy_app = proxy.make_app()
    keyproxy_app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(vendor))
    server = TestServer(keyproxy_app, host="127.0.0.1")
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    monkeypatch.setenv("KEYPROXY_BASE_URL", base)

    config = model_config()
    raw = config.model_dump(mode="json")
    raw["providers"]["grok"] = ProviderConfig(name="Grok", base_url=f"{base}/grok/v1", billing="subscription").model_dump(mode="json")
    raw["presets"][PRESET] = ModelPresetConfig(provider="grok", model="grok-4", thinking=False).model_dump(mode="json")
    raw["model"] = ModelConfig(preset=PRESET, chain=[], auto_fallback=False).model_dump(mode="json")
    config = type(config).model_validate(raw)
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    terminals = Terminals(home)

    async def save_config(cfg: Any) -> None:
        app.config = cfg
        manager.reload_config(cfg)

    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={"api_token": "tok", "terminals": terminals}, guard=None, create_session=manager.create_session, save_config=save_config)
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test", headers={"X-Daedalus-Token": "tok"}) as http:
        yield SimpleNamespace(http=http, manager=manager, vendor=vendor, terminals=terminals, home=home, db=db)
    await manager.close()
    await server.close()


async def test_a_turn_moves_to_the_second_account_when_the_first_is_spent_and_says_so(stand: Any) -> None:
    http, manager, vendor = stand.http, stand.manager, stand.vendor

    # Two accounts of one subscription: the CLI's own login, named, and a second one added and signed in.
    added = await http.post("/api/subscription-accounts/grok", json={"label": "work"})
    assert added.status_code == 200, added.text
    work = added.json()["id"]
    assert (await http.patch("/api/subscription-accounts/grok/default", json={"label": "personal"})).status_code == 200
    signed = await http.post(f"/api/subscription-accounts/grok/{work}/login")
    assert signed.status_code == 200, signed.text
    spec = stand.terminals.opened[-1]
    account_home = stand.home / ".grok" / "daedalus-accounts" / work
    assert spec.env == "host" and spec.argv == ["grok", "login", "--device-auth"]
    assert spec.env_vars == {"GROK_HOME": str(account_home)}, "the CLI signs in to the account's own home"
    assert account_home.is_dir(), "the key proxy made the home before the terminal opened"
    _write_grok_login(account_home, "token-work")  # what the operator's sign-in leaves there

    listing = (await http.get("/api/subscription-accounts")).json()
    grok = next(u for u in listing["upstreams"] if u["upstream"] == "grok")
    assert [(a["id"], a["label"], a["configured"]) for a in grok["accounts"]] == [("default", "personal", True), (work, "work", True)]
    assert grok["default_account"] == "default" and grok["fallover"] is True and grok["providers"] == ["grok"]

    # A turn on the default account.
    state = await manager.create_session("two accounts")
    session_id = state.session.id
    assert await _turn(manager, session_id, "first") == "completed"
    assert ("token-personal", 200) in vendor.calls and not any(token == "token-work" for token, _ in vendor.calls)

    # The first account's plan is spent; the next turn is refused there and answered on the second.
    events: list[Any] = []

    async def sink(sid: str, event: Any) -> None:
        if sid == session_id:
            events.append(event)

    manager.add_sink(sink)
    vendor.spent.add("token-personal")
    before = len(vendor.calls)
    assert await _turn(manager, session_id, "second") == "completed"
    second = vendor.calls[before:]
    assert second[:2] == [("token-personal", 429), ("token-work", 200)], second

    moved = [e for e in events if str(getattr(e.type, "value", e.type)) == "account_switch"]
    assert moved and moved[0].payload["from"] == "default" and moved[0].payload["to"] == work
    assert moved[0].payload["from_label"] == "personal" and moved[0].payload["to_label"] == "work" and moved[0].payload["reason"] == "quota"

    # The turn says which account answered it, and so do the calls.
    page = (await http.get(f"/api/sessions/{session_id}")).json()
    answers = [m for m in page["messages"] if m["role"] == "assistant" and m.get("text")]
    assert answers[0]["account"] == "default" and answers[0]["account_label"] == "personal"
    assert answers[-1]["account"] == work and answers[-1]["account_label"] == "work"
    recent = (await http.get("/api/usage")).json()["recent"]
    stream_calls = [r for r in recent if r["purpose"] == "stream" and r["session_id"] == session_id]
    assert stream_calls[0]["account"] == work and stream_calls[0]["account_label"] == "work"
    assert stream_calls[-1]["account"] == "default"

    # The spent account rests; the provider does not, since another account answered.
    listing = (await http.get("/api/subscription-accounts")).json()
    rows = {a["id"]: a for a in next(u for u in listing["upstreams"] if u["upstream"] == "grok")["accounts"]}
    assert rows["default"]["resting"] is True and rows["default"]["rest_reason"] == "quota" and rows["default"]["resting_until"]
    assert rows[work]["resting"] is False and rows[work]["calls_24h"] >= 1
    failures = await stand.db.fetchone("SELECT count(*) c FROM provider_failure_observations WHERE provider_id = 'grok'")
    assert failures["c"] == 0, "a refusal another account absorbed is not the provider's failure"

    # The next turn goes straight to the second account: the first is resting, and the session stays where it moved.
    before = len(vendor.calls)
    assert await _turn(manager, session_id, "third") == "completed"
    assert vendor.calls[before] == ("token-work", 200)

    # Removing the second account removes its login at the key proxy, and the endpoint is down to its default.
    removed = await http.delete(f"/api/subscription-accounts/grok/{work}")
    assert removed.status_code == 200, removed.text
    assert not account_home.exists()
    listing = (await http.get("/api/subscription-accounts")).json()
    assert [a["id"] for a in next(u for u in listing["upstreams"] if u["upstream"] == "grok")["accounts"]] == ["default"]
    adapter = manager.providers.get("grok")
    assert manager.providers.accounts is not None and manager.providers.accounts.chain(adapter.endpoint, "grok-4", session_id) == []
    assert (await http.delete("/api/subscription-accounts/grok/default")).status_code == 400


async def test_with_fallover_off_the_turn_stops_on_its_own_account(stand: Any) -> None:
    http, manager, vendor = stand.http, stand.manager, stand.vendor
    work = (await http.post("/api/subscription-accounts/grok", json={"label": "work"})).json()["id"]
    _write_grok_login(stand.home / ".grok" / "daedalus-accounts" / work, "token-work")
    assert (await http.put("/api/subscription-accounts/grok", json={"fallover": False})).json()["fallover"] is False
    vendor.spent.add("token-personal")
    state = await manager.create_session("one account")
    assert await _turn(manager, state.session.id, "go") != "completed"
    assert not any(token == "token-work" for token, _ in vendor.calls), "the operator said no account but the turn's own"
    failure = await stand.db.fetchone("SELECT failure_class, account FROM provider_failure_observations WHERE provider_id = 'grok'")
    assert failure is not None and failure["failure_class"] == "quota" and failure["account"] == "default"


async def test_a_model_can_name_its_own_account(stand: Any) -> None:
    http, manager, vendor = stand.http, stand.manager, stand.vendor
    work = (await http.post("/api/subscription-accounts/grok", json={"label": "work"})).json()["id"]
    _write_grok_login(stand.home / ".grok" / "daedalus-accounts" / work, "token-work")
    saved = await http.put(f"/api/presets/{PRESET}", json={"account": work})
    assert saved.status_code == 200, saved.text
    state = await manager.create_session("named account")
    assert await _turn(manager, state.session.id, "go") == "completed"
    assert [token for token, _ in vendor.calls if token][0] == "token-work"


async def test_a_session_runs_on_the_account_chosen_for_it_and_keeps_it_over_a_restart(stand: Any, settings: Settings) -> None:
    http, manager, vendor = stand.http, stand.manager, stand.vendor
    second = (await http.post("/api/subscription-accounts/grok", json={"label": "second"})).json()["id"]
    _write_grok_login(stand.home / ".grok" / "daedalus-accounts" / second, "token-second")
    state = await manager.create_session("chosen account")
    session_id = state.session.id

    page = (await http.get(f"/api/sessions/{session_id}")).json()
    assert page["account"] == "" and page["account_upstream"] == "grok", "the picker is told whose accounts to list"
    assert page["account_default"] == "default"
    refused = await http.post(f"/api/sessions/{session_id}/model", json={"account": "gone"})
    assert refused.status_code == 400 and "gone" in refused.text
    chosen = await http.post(f"/api/sessions/{session_id}/model", json={"account": second})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["account"] == second and chosen.json()["reasoning_effort"]

    assert await _turn(manager, session_id, "go") == "completed"
    assert [token for token, _ in vendor.calls if token][0] == "token-second"
    other = await manager.create_session("another chat")
    assert (await http.get(f"/api/sessions/{other.session.id}")).json()["account"] == "", "the choice is this session's alone"

    # A restart reads the choice back, both for the page and for the next call's order.
    again = SessionManager(settings, manager.config, db=stand.db)
    await again.start()
    try:
        adapter = again.providers.get("grok")
        assert again.providers.accounts is not None
        assert again.providers.accounts.chain(adapter.endpoint, "grok-4", session_id)[0] == second
        assert again.subscription_accounts.chosen(session_id, "grok") == second
    finally:
        await again.close()

    # Returning the session to the default model returns it to the model's own account as well.
    assert (await http.post(f"/api/sessions/{session_id}/model", json={"clear": True})).status_code == 200
    assert (await http.get(f"/api/sessions/{session_id}")).json()["account"] == ""
    assert (await stand.db.fetchone("SELECT account FROM live_control WHERE session_id = ?", (session_id,)))["account"] is None
    assert (await http.post(f"/api/sessions/{session_id}/model", json={"account": second})).status_code == 200
    assert (await http.post(f"/api/sessions/{session_id}/model", json={"account": ""})).json()["account"] == ""
