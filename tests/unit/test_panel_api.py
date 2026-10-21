"""The extension's side panel from the host's side: its credential (minted for the connected Chrome,
handed over the relay, hashed, idle for twelve hours at most, revoked with the device and with every
browser session), the one door that accepts it, and who may frame its page.

The door's matrix is the contract: a panel token reads and writes the conversations in the operator's
Chrome and nothing else — every other route answers 401 to it, and a conversation outside the set
answers 404 as if it did not exist."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from daedalus.browser.model import USER
from daedalus.browser.panel import (
    EXTENSION_ID,
    IDLE,
    NATIVE_DEVICE,
    PANEL_LINE,
    PANEL_SESSION_KEY,
    PanelGrants,
    PanelHerald,
)
from daedalus.browser.user import CONSENT
from daedalus.config import BrowserConfig, BrowserUserConfig, RuntimeConfig, Settings
from daedalus.extensions import api_panel
from daedalus.extensions import browser as browser_extension
from daedalus.extensions.api import SpaFiles, build_app, frame_policy
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.fake_browserd import FakeBrowserd

REPO = Path(__file__).resolve().parents[2]
CONNECTED = {"connected": True, "profile_id": "p1", "profile_name": "Work", "ext_version": "0.1.0", "chrome_version": "141.0.7390.54", "platform": "linux",
             "transport": "server", "since": "2026-10-20T10:00:00Z", "last_seen": "2026-10-20T10:00:05Z"}
PUBLIC = "https://daedalus.example.org"


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


# -- the credential ---------------------------------------------------------------------------------


async def test_a_token_is_kept_as_its_hash_lapses_unused_and_slides_with_use(db: Database) -> None:
    clock = Clock()
    grants = PanelGrants(db, now=clock)
    token, expires = await grants.mint("dev1")
    assert token.startswith("pnl_") and expires == clock.at + IDLE
    stored = await db.kv_get(PanelGrants.KEY, [])
    assert token not in repr(stored) and len(stored) == 1
    assert await grants.check(token) == "dev1"
    assert await grants.check(token + "x") is None and await grants.check("") is None
    clock.at += timedelta(hours=11)
    assert await grants.check(token) == "dev1"  # used: twelve more hours from here
    clock.at += timedelta(hours=11)
    assert await grants.check(token) == "dev1"
    clock.at += IDLE + timedelta(seconds=1)
    assert await grants.check(token) is None


async def test_a_device_keeps_two_tokens_and_unpairing_or_ending_every_session_revokes_them(db: Database) -> None:
    grants = PanelGrants(db, now=Clock())
    first, _ = await grants.mint("dev1")
    second, _ = await grants.mint("dev1")
    third, _ = await grants.mint("dev1")
    other, _ = await grants.mint("dev2")
    assert await grants.check(first) is None
    assert await grants.check(second) == "dev1" and await grants.check(third) == "dev1"
    await grants.revoke("dev1")
    assert await grants.check(third) is None and await grants.check(other) == "dev2"
    await grants.revoke_all()
    assert await grants.check(other) is None


async def test_the_herald_renews_only_a_token_that_runs_out_soon_and_survives_a_chrome_that_left(db: Database) -> None:
    clock = Clock()
    grants = PanelGrants(db, now=clock)
    calls: list[tuple[str, dict[str, Any]]] = []
    fail = False

    async def call(op: str, args: dict[str, Any]) -> None:
        if fail:
            raise RuntimeError("chrome went away")
        calls.append((op, args))

    device = "dev1"
    herald = PanelHerald(grants, call=call, device=lambda: device, base=lambda: PUBLIC + "/")
    assert await herald.grant() is True
    op, args = calls[-1]
    assert op == "panel.grant" and args["base"] == PUBLIC and args["expires"].endswith("Z")
    assert await grants.check(args["token"]) == "dev1"
    assert await herald.grant(force=False) is False
    clock.at += IDLE - timedelta(hours=1)
    assert await herald.grant(force=False) is True
    fail = True
    assert await herald.grant() is False
    device = ""
    fail = False
    assert await herald.grant() is False
    await herald.event("s1", "done", "t", waiting=0)
    assert calls[-1][0] == "panel.grant"


def test_the_extension_id_is_the_one_the_extension_declares() -> None:
    protocol = (REPO / "extension" / "src" / "protocol.ts").read_text()
    assert re.search(r'EXTENSION_ID = "([a-p]{32})"', protocol).group(1) == EXTENSION_ID  # type: ignore[union-attr]


# -- framing ----------------------------------------------------------------------------------------


async def test_the_panel_page_is_framed_by_the_extension_alone_and_the_app_by_itself(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>app</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("1")
    site = Starlette(routes=[Mount("/app", SpaFiles(directory=str(tmp_path), html=True))])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site), base_url="http://test") as client:
        panel = await client.get("/app/panel")
        assert panel.status_code == 200 and panel.headers["content-security-policy"] == f"frame-ancestors chrome-extension://{EXTENSION_ID}"
        for path in ("/app/", "/app/agents/s1", "/app/assets/app.js", "/app/panel/other"):
            answer = await client.get(path)
            assert answer.headers["content-security-policy"] == "frame-ancestors 'self' https://web.telegram.org", path
    assert frame_policy("panel/") == frame_policy("panel")


# -- the door ---------------------------------------------------------------------------------------


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="pn-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def chrome(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = FakeBrowserd(base / "u", env="user")
    fake.relay_status = dict(CONNECTED)
    yield await fake.start()
    await fake.stop()


@pytest.fixture
async def agents_browser(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "b").start()
    yield fake
    await fake.stop()


class Rig:
    def __init__(self, manager: SessionManager, app: Any) -> None:
        self.manager = manager
        self.app = app

    @property
    def service(self) -> Any:
        return self.app.extensions["browser"]

    @property
    def grants(self) -> PanelGrants:
        return self.app.extensions["browser_panel"]  # type: ignore[no-any-return]

    async def session(self, *, chrome: bool) -> str:
        state = await self.manager.create_session("chat")
        await self.manager.get_state(state.session.id)
        if chrome:
            await self.service.choices.switch(state.session.id, USER, consent=True)
        return state.session.id


@pytest.fixture
async def rig(settings: Settings, db: Database, base: Path, agents_browser: FakeBrowserd, chrome: FakeBrowserd) -> AsyncIterator[Rig]:
    configured = settings.model_copy(update={"browser_container_dir": base / "b", "browser_user_dir": base / "u", "native": False, "miniapp_public_url": PUBLIC + "/app/"})
    runtime = RuntimeConfig(browser=BrowserConfig(control_wait_seconds=0.2, user=BrowserUserConfig()))
    manager = SessionManager(configured, runtime, db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    assert await app.extensions["browser"].wait_available(USER)
    app.extensions["browser_relays"].connected_device = lambda: "dev1"
    yield Rig(manager, app)
    for task in tasks:
        task.cancel()
    await app.extensions["browser"].close()
    await manager.close()


def panel(token: str) -> dict[str, str]:
    return {"authorization": f"Panel {token}"}


async def test_a_connected_chrome_is_handed_its_token_over_the_relay(rig: Rig, chrome: FakeBrowserd) -> None:
    chrome.emit("relay.connected", {"profile_id": "p1", "profile_name": "Work"})
    async with asyncio.timeout(5):
        while not chrome.extension_calls:
            await asyncio.sleep(0.02)
    call = chrome.extension_calls[0]
    assert call["op"] == "panel.grant" and call["args"]["base"] == PUBLIC
    assert await rig.grants.check(call["args"]["token"]) == "dev1"


def test_the_panel_base_is_where_chrome_reaches_the_app() -> None:
    assert browser_extension.panel_base(SimpleNamespace(native=True, api_port=48765, miniapp_public_url="")) == "http://127.0.0.1:48765"
    assert browser_extension.panel_base(SimpleNamespace(native=False, api_port=8765, miniapp_public_url="https://d.example.org/app/")) == "https://d.example.org"
    assert browser_extension.panel_base(SimpleNamespace(native=False, api_port=8765, miniapp_public_url="")) == ""
    service = SimpleNamespace(links={USER: SimpleNamespace(available=True)})
    assert browser_extension.panel_device(service, None) == NATIVE_DEVICE  # type: ignore[arg-type]


OWNER_ROUTES = [
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/{mine}"),
    ("POST", "/api/sessions/{mine}/messages"),
    ("POST", "/api/sessions/{mine}/stop"),
    ("GET", "/api/settings"),
    ("GET", "/api/browser/user"),
    ("POST", "/api/browser/user/pairing"),
    ("GET", "/api/terminals"),
    ("GET", "/api/projects"),
    ("GET", "/api/auth/me"),
]


async def test_the_door_matrix(rig: Rig, chrome: FakeBrowserd) -> None:
    mine = await rig.session(chrome=True)
    other = await rig.session(chrome=False)
    token, _ = await rig.grants.mint("dev1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        # No credential, a wrong one, and the installation's own token: none opens the door.
        for headers in ({}, panel("pnl_wrong"), {"x-daedalus-token": "tok"}, {"authorization": f"Bearer {token}"}):
            assert (await client.get("/api/panel/sessions", headers=headers)).status_code == 401, headers
        # The operator's routes never read a panel token.
        for method, path in OWNER_ROUTES:
            answer = await client.request(method, path.format(mine=mine), headers=panel(token), json={})
            assert answer.status_code == 401, (method, path, answer.status_code)
        # The conversations in this Chrome, and only those.
        listing = (await client.get("/api/panel/sessions", headers=panel(token))).json()
        assert [s["id"] for s in listing["sessions"]] == [mine]
        assert listing["sessions"][0]["group"].endswith("-u") and listing["consent"] == CONSENT and listing["consented"] is False
        assert listing["chrome"] == {"connected": True, "name": "Work"}
        page = await client.get(f"/api/panel/sessions/{mine}", headers=panel(token))
        assert page.status_code == 200 and page.json()["id"] == mine
        assert (await client.post(f"/api/panel/sessions/{mine}/stop", headers=panel(token))).status_code == 200
        refused = await client.post(f"/api/panel/sessions/{mine}/policy/refuse", headers=panel(token), json={"key": ""})
        assert refused.status_code == 400  # the operator's handler judged it
        # Another conversation does not exist, whatever is asked of it.
        for method, suffix in (("GET", ""), ("POST", "/messages"), ("POST", "/stop"), ("POST", "/policy/grant"), ("POST", "/tab")):
            answer = await client.request(method, f"/api/panel/sessions/{other}{suffix}", headers=panel(token), json={"text": "hi", "key": "k"})
            assert answer.status_code == 404, (method, suffix)
        # Nothing past the list: the conversation's files, terminals, the settings' writes.
        for method, path in (("GET", f"/api/panel/sessions/{mine}/files"), ("DELETE", f"/api/panel/sessions/{mine}"), ("GET", "/api/panel/terminals"),
                             ("PUT", "/api/panel/settings"), ("GET", f"/api/panel/sessions/{mine}/export/download")):
            assert (await client.request(method, path, headers=panel(token))).status_code == 404, path
        # The model list, without an address or a key.
        picked = (await client.get("/api/panel/settings", headers=panel(token))).json()
        assert set(picked) == {"presets", "providers", "model"}
        assert "base_url" not in repr(picked) and "api_key" not in repr(picked)


async def test_a_new_conversation_is_born_in_chrome_after_one_consent_and_takes_the_tab(rig: Rig, chrome: FakeBrowserd) -> None:
    token, _ = await rig.grants.mint("dev1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        asked = await client.post("/api/panel/sessions", headers=panel(token), json={"title": "compare these prices"})
        assert asked.status_code == 409 and asked.json()["code"] == "consent_needed" and asked.json()["consent"] == CONSENT
        made = await client.post("/api/panel/sessions", headers=panel(token), json={"title": "compare these prices", "consent": True})
        assert made.status_code == 200, made.text
        sid = made.json()["id"]
        assert made.json()["group"].endswith("-u")
        assert (await rig.service.choices.get(sid))["env"] == USER
        again = await client.post("/api/panel/sessions", headers=panel(token), json={"title": "and these"})
        assert again.status_code == 200
        assert (await client.post("/api/panel/sessions", headers=panel(token), json={"title": "  "})).status_code == 400
        opened = await client.post(f"/api/panel/sessions/{sid}/tab", headers=panel(token))
        assert opened.status_code == 200 and opened.json()["group"] == made.json()["group"]
        group = chrome.groups[made.json()["group"]]
        assert group.labels["session_id"] == sid and group.labels["title"]
        listing = (await client.get("/api/panel/sessions", headers=panel(token))).json()
        assert {s["id"] for s in listing["sessions"]} == {sid, again.json()["id"]} and listing["consented"] is True


async def test_unpairing_ending_every_session_and_idleness_close_the_door(rig: Rig, chrome: FakeBrowserd) -> None:
    mine = await rig.session(chrome=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        token, _ = await rig.grants.mint("dev1")
        assert (await client.get(f"/api/panel/sessions/{mine}", headers=panel(token))).status_code == 200
        await rig.grants.revoke("dev1")
        assert (await client.get(f"/api/panel/sessions/{mine}", headers=panel(token))).status_code == 401
        token, _ = await rig.grants.mint("dev1")
        assert (await client.post("/api/auth/sessions/revoke", headers={"x-daedalus-token": "tok"})).status_code == 200
        assert (await client.get("/api/panel/sessions", headers=panel(token))).status_code == 401
        clock = Clock()
        rig.grants.now = clock
        token, _ = await rig.grants.mint("dev1")
        clock.at += IDLE + timedelta(minutes=1)
        assert (await client.get("/api/panel/sessions", headers=panel(token))).status_code == 401
        # A conversation switched back to the agent's browser leaves the panel's reach.
        token, _ = await rig.grants.mint("dev1")
        await rig.service.choices.switch(mine, "agent", consent=False)
        assert (await client.get(f"/api/panel/sessions/{mine}", headers=panel(token))).status_code == 404


async def test_a_client_cannot_set_the_doors_mark_itself(rig: Rig) -> None:
    """The mark lives in the ASGI scope, which no header reaches; the door strips any on the way to
    the operator's routes all the same."""
    mine = await rig.session(chrome=True)
    seen: list[Any] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope.get(api_panel.SCOPE_KEY))

    door = api_panel.PanelDoor(inner, grants=lambda: rig.grants, in_scope=lambda sid: asyncio.sleep(0, result=True))
    await door({"type": "http", "path": f"/api/sessions/{mine}", "method": "GET", "headers": [], api_panel.SCOPE_KEY: {"device": "forged"}}, None, None)
    assert seen == [None]


async def test_a_turn_that_ends_in_chrome_tells_the_panel_whether_it_waits(rig: Rig, chrome: FakeBrowserd) -> None:
    mine = await rig.session(chrome=True)
    other = await rig.session(chrome=False)
    herald = rig.app.extensions["browser_panel_herald"]
    await browser_extension.run_finished(rig.manager, rig.service, herald, other, "completed")
    assert chrome.extension_calls == []
    await browser_extension.run_finished(rig.manager, rig.service, herald, mine, "completed")
    assert chrome.extension_calls[-1] == {"op": "panel.event", "args": {"session": mine, "kind": "done", "title": "chat", "waiting": 0}}
    state = rig.manager.live_state(mine)
    state.metadata["policy_pending"] = {"k": {"tool": "BrowserNavigate"}}
    await browser_extension.run_finished(rig.manager, rig.service, herald, mine, "completed")
    assert chrome.extension_calls[-1]["args"] | {"session": ""} == {"session": "", "kind": "needs_you", "title": "chat", "waiting": 1}


async def test_a_conversation_held_from_the_panel_is_told_to_work_in_the_browser_and_others_are_not(rig: Rig, chrome: FakeBrowserd) -> None:
    from protocore.contracts.types import Message, MessageRole, TextBlock  # noqa: PLC0415

    async def context(sid: str) -> str:
        state = await rig.manager.get_state(sid)
        assert state is not None
        said = Message(role=MessageRole.user, content_blocks=[TextBlock(text="find the cheapest flight here")])
        return (await rig.manager._with_turn_context(state, said)).content_blocks[0].text  # type: ignore[union-attr]

    from_app = await rig.session(chrome=True)
    elsewhere = await rig.session(chrome=False)
    token, _ = await rig.grants.mint("dev1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        made = await client.post("/api/panel/sessions", headers=panel(token), json={"title": "find the cheapest flight here", "consent": True})
        born = made.json()["id"]
        # A conversation in Chrome that the panel only sends into is held from it too. No model is set up
        # here, so the turn itself is refused; the mark is made when the message arrives, before that.
        sent = await client.post(f"/api/panel/sessions/{from_app}/messages", headers=panel(token), json={"text": "hi", "follow_up": False})
        assert sent.status_code in (200, 409), sent.text
        quiet = await rig.session(chrome=True)
        # The app's own messages mark nothing.
        assert (await client.post(f"/api/sessions/{quiet}/messages", headers={"x-daedalus-token": "tok"}, json={"text": "hi"})).status_code in (200, 409)
    for sid in (born, from_app):
        text = await context(sid)
        assert text.count(PANEL_LINE) == 1 and text == await context(sid), sid
        state = await rig.manager.get_state(sid)
        assert "browser" in state.metadata["loaded_tool_groups"] and state.session.metadata[PANEL_SESSION_KEY] is True  # type: ignore[union-attr]
        assert "browser" in rig.manager.loaded_groups_for(state)  # type: ignore[arg-type]
    for sid in (quiet, elsewhere):
        assert PANEL_LINE not in await context(sid)
    # Back in the agent's own browser, the line goes: "their Chrome" is somewhere it cannot work then.
    await rig.service.choices.switch(born, "agent", consent=True)
    assert PANEL_LINE not in await context(born)


async def test_the_panel_sets_the_approval_mode_of_its_own_conversations_only(rig: Rig, chrome: FakeBrowserd) -> None:
    mine = await rig.session(chrome=True)
    other = await rig.session(chrome=False)
    token, _ = await rig.grants.mint("dev1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.get(f"/api/panel/sessions/{mine}/browser-approvals", headers=panel(token))).json()["mode"] == "manual"
        done = await client.put(f"/api/panel/sessions/{mine}/browser-approvals", headers=panel(token), json={"mode": "auto"})
        assert done.status_code == 200 and done.json()["mode"] == "auto"
        assert (await client.put(f"/api/panel/sessions/{other}/browser-approvals", headers=panel(token), json={"mode": "skip", "confirm": True})).status_code == 404
        # The panel token opens only the panel's path; the operator's own path never reads it.
        assert (await client.put(f"/api/sessions/{mine}/browser-approvals", headers=panel(token), json={"mode": "manual"})).status_code == 401
    assert (await rig.service.choices.get(mine))["approvals"] == "auto"
    assert (await rig.service.choices.get(other))["approvals"] == "manual"


async def test_the_panel_uploads_into_its_own_conversations_only_new_ones_included_and_within_its_cap(rig: Rig, chrome: FakeBrowserd, monkeypatch: pytest.MonkeyPatch) -> None:
    other = await rig.session(chrome=False)
    token, _ = await rig.grants.mint("dev1")
    files = {"files": ("bill.png", b"\x89PNG" + b"0" * 2000, "image/png")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        # A conversation made from the panel takes its first message with files, before any other.
        made = await client.post("/api/panel/sessions", headers=panel(token), json={"title": "what is this bill", "consent": True})
        sid = made.json()["id"]
        sent = await client.post(f"/api/panel/sessions/{sid}/upload", headers=panel(token), data={"text": "what is this bill"}, files=files)
        # Past the door to the operator's handler, which stages the file; no model here, so the turn is refused.
        assert sent.status_code in (200, 409), sent.text
        assert "not a side panel route" not in sent.text and "no such conversation" not in sent.text
        assert (await client.post(f"/api/panel/sessions/{other}/upload", headers=panel(token), data={"text": "x"}, files=files)).status_code == 404
        # Dictation goes through the same door, before a conversation and in one (this rig has no
        # speech, so the door's list says it rather than the handler).
        for method, path in (("GET", "/api/panel/asr"), ("POST", "/api/panel/transcribe"), ("POST", f"/api/panel/sessions/{sid}/transcribe"), ("DELETE", "/api/panel/transcribe/ab12cd.webm")):
            assert any(m == method and rx.fullmatch(path) for m, rx in api_panel.FORWARDED), path
        assert (await client.post(f"/api/panel/sessions/{other}/transcribe", headers=panel(token), files={"audio": ("n.webm", b"x", "audio/webm")})).status_code == 404
        # Past the cap, refused at the door before the handler reads a byte.
        monkeypatch.setattr(api_panel, "PANEL_UPLOAD_MAX", 1000)
        big = await client.post(f"/api/panel/sessions/{sid}/upload", headers=panel(token), data={"text": "x"}, files=files)
        assert big.status_code == 413, big.text
    # A body sent without a true length is cut off at the cap all the same.
    chunks = [{"type": "http.request", "body": b"a" * 600, "more_body": True}, {"type": "http.request", "body": b"a" * 600, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return chunks.pop(0)

    capped = api_panel._capped(receive, 1000)
    assert (await capped())["type"] == "http.request"
    assert (await capped())["type"] == "http.disconnect"
