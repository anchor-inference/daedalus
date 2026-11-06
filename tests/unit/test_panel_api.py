"""The extension's side panel from the host's side: its credential (minted for the connected Chrome,
handed over the relay, hashed, idle for twelve hours at most, revoked with the device and with every
browser session), the one door that accepts it, and who may frame its page.

The door's matrix is the contract: a panel token reads and writes the conversations in the operator's
Chrome and nothing else — every other route answers 401 to it, and a conversation outside the set
answers 404 as if it did not exist. The list is narrower than the door: this Chrome's own
conversations, the ones made for it and any other while it is controlling it now; and what the
extension is told of them is the app's notification, through one channel, not a second notifier."""

from __future__ import annotations

import asyncio
import base64
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
from protocore.contracts.types import Message, MessageRole, TextBlock
from starlette.applications import Starlette
from starlette.routing import Mount

from daedalus.browser.control import ChromeControl
from daedalus.browser.model import USER, Owner, group_id
from daedalus.browser.panel import (
    EXTENSION_ID,
    IDLE,
    NATIVE_DEVICE,
    PANEL_LINE,
    PANEL_SESSION_KEY,
    ChromeChannel,
    PanelGrants,
    PanelHerald,
)
from daedalus.browser.user import CONSENT
from daedalus.config import BrowserConfig, BrowserUserConfig, RuntimeConfig, Settings
from daedalus.extensions import api_panel
from daedalus.extensions import browser as browser_extension
from daedalus.extensions.api import SpaFiles, build_app, frame_policy
from daedalus.extensions.notifications import Draft, NotificationService
from daedalus.host.control_marks import IDLE_AFTER
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.fake_browserd import FakeBrowserd

REPO = Path(__file__).resolve().parents[2]
CONNECTED = {"connected": True, "profile_id": "p1", "profile_name": "Work", "ext_version": "0.1.0", "chrome_version": "141.0.7390.54", "platform": "linux",
             "transport": "server", "since": "2026-10-20T10:00:00Z", "last_seen": "2026-10-20T10:00:05Z"}
PUBLIC = "https://daedalus.example.org"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


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
    # The extension is built in its own repository (daedalus-chrome-extension), whose src/protocol.ts
    # declares this id and whose tests tie it to the manifest key. This test once read that file from
    # a folder of this tree; now the id is held to the extension's value and to the two other places
    # here that spell it, the app's half of the panel bridge and the bridge browserd pins.
    assert EXTENSION_ID == "lkhhklicnjlplokpadpccndipomikoaj"
    app = (REPO / "miniapp" / "src" / "sidepanel" / "bridge.ts").read_text()
    assert re.search(r'export const EXTENSION_ID = "([a-p]{32})"', app).group(1) == EXTENSION_ID  # type: ignore[union-attr]
    daemon = (REPO / "browserd" / "internal" / "bridge" / "where.go").read_text()
    assert re.search(r'PinnedExtensionIDs = \[\]string\{"([a-p]{32})"', daemon).group(1) == EXTENSION_ID  # type: ignore[union-attr]


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

    async def session(self, *, chrome: bool, kind: str | None = None) -> str:
        """A conversation; with ``chrome`` given the operator's Chrome, and by default made for it (a
        Chrome chat). ``kind="chat"`` with ``chrome`` is an ordinary chat moved there later."""
        state = await self.manager.create_session("chat", kind=kind or ("chrome" if chrome else "chat"))
        await self.manager.get_state(state.session.id)
        if chrome:
            await self.service.choices.switch(state.session.id, USER, consent=True)
        return state.session.id

    @property
    def channel(self) -> ChromeChannel:
        return ChromeChannel(self.app.extensions["browser_panel_herald"], manager=self.manager, using=self.service.choices.using_user, offered=self.service.user_offered)


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
        borrowed = await rig.session(chrome=True, kind="chat")
        listing = (await client.get("/api/panel/sessions", headers=panel(token))).json()
        assert [s["id"] for s in listing["sessions"]] == [mine]
        # An ordinary chat given this Chrome is listed only while it controls it; its door stays open,
        # so a panel showing it does not lose it mid-reply when the control lapses.
        assert (await client.get(f"/api/panel/sessions/{borrowed}", headers=panel(token))).status_code == 200
        await rig.manager.control_marks.update("chrome", {borrowed})
        listing = (await client.get("/api/panel/sessions", headers=panel(token))).json()
        assert sorted(s["id"] for s in listing["sessions"]) == sorted([mine, borrowed])
        await rig.manager.control_marks.update("chrome", set())
        listing = (await client.get("/api/panel/sessions", headers=panel(token))).json()
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
        switches = rig.manager.bus.subscribe(EventFilter(types=("browser.user",)), name="test")
        made = await client.post("/api/panel/sessions", headers=panel(token), json={"title": "compare these prices", "consent": True})
        assert made.status_code == 200, made.text
        sid = made.json()["id"]
        # Moved once, by the create: a second move announced the switch twice.
        moved = [await asyncio.wait_for(anext(switches), 2)]
        with pytest.raises(TimeoutError):
            moved.append(await asyncio.wait_for(anext(switches), 0.2))
        assert [(e.session_id, e.payload["kind"], e.payload["reason"]) for e in moved] == [(sid, "switched", USER)]
        assert (await rig.manager.get_state(sid)).kind == "chrome"  # type: ignore[union-attr]
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


async def test_this_chromes_conversations_are_those_made_for_it_and_those_controlling_it_now(rig: Rig, chrome: FakeBrowserd) -> None:
    made = await rig.session(chrome=True)
    borrowed = await rig.session(chrome=True, kind="chat")
    elsewhere = await rig.session(chrome=False)
    channel = rig.channel
    assert channel.available()
    assert await channel.covers(made) and not await channel.covers(borrowed) and not await channel.covers(elsewhere)
    await rig.manager.control_marks.update("chrome", {borrowed, elsewhere})
    # Controlling the operator's Chrome counts only for a conversation given that Chrome.
    assert await channel.covers(borrowed) and not await channel.covers(elsewhere)
    await rig.manager.control_marks.update("chrome", set())
    assert not await channel.covers(borrowed)
    # A turn's end posted after its tabs closed still belongs to the Chrome it was working in.
    assert await channel.covers(borrowed, frozenset({"chrome"}))
    # A Chrome chat moved back to the agent's own browser is no longer this Chrome's.
    await rig.service.choices.switch(made, "agent", consent=True)
    assert not await channel.covers(made)


async def test_the_channel_tells_the_panel_whether_it_waits_and_counts_only_this_chromes(rig: Rig, chrome: FakeBrowserd) -> None:
    mine = await rig.session(chrome=True)
    borrowed = await rig.session(chrome=True, kind="chat")
    channel = rig.channel
    view = {"session_id": mine, "category": "run_finished", "needs_you": False, "title": "Finished: chat"}
    assert await channel.deliver(view, more=0) == "sent"  # type: ignore[arg-type]
    assert chrome.extension_calls[-1] == {"op": "panel.event", "args": {"session": mine, "kind": "done", "title": "chat", "waiting": 0}}
    # A conversation waiting for an approval counts on the badge; an ordinary one waiting does not.
    rig.manager.live_state(mine).metadata["policy_pending"] = {"k": {"tool": "BrowserNavigate"}}  # type: ignore[union-attr]
    rig.manager.live_state(borrowed).metadata["policy_pending"] = {"k": {"tool": "BrowserNavigate"}}  # type: ignore[union-attr]
    await channel.deliver({**view, "category": "permission", "needs_you": True}, more=0)  # type: ignore[arg-type]
    assert chrome.extension_calls[-1]["args"] | {"session": ""} == {"session": "", "kind": "needs_you", "title": "chat", "waiting": 1}


class Pushes:
    name = "push"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def available(self) -> bool:
        return True

    async def deliver(self, view: Any, *, more: int) -> Any:
        self.sent.append(str(view["session_id"]))
        return {"sent": 1}


async def test_a_turn_end_sounds_once_in_chrome_for_its_conversations_and_by_push_for_others(rig: Rig, chrome: FakeBrowserd) -> None:
    mine = await rig.session(chrome=True)
    borrowed = await rig.session(chrome=True, kind="chat")
    service = NotificationService(rig.manager.db, rig.manager.bus)
    pushes = Pushes()
    service.register_channel(pushes)
    service.register_channel(rig.channel)
    before = len(chrome.extension_calls)
    made = await service.post(Draft("run_finished", "Finished", session_id=mine, dedupe_key=f"run:{mine}"))
    assert made is not None and made["delivered"]["chrome"] == "sent" and made["delivered"]["push"] == "skipped: shown in Chrome"
    assert pushes.sent == [] and len(chrome.extension_calls) == before + 1
    # An ordinary chat that once worked in Chrome is announced by the app alone: no second sound.
    plain = await service.post(Draft("run_finished", "Finished", session_id=borrowed, dedupe_key=f"run:{borrowed}"))
    assert plain is not None and "chrome" not in plain["delivered"] and pushes.sent == [borrowed]
    assert len(chrome.extension_calls) == before + 1
    # While it controls the Chrome (or did when its turn ended), it is announced there instead.
    held = await service.post(Draft("run_finished", "Finished", session_id=borrowed, dedupe_key=f"again:{borrowed}", controlling=frozenset({"chrome"})))
    assert held is not None and held["delivered"]["chrome"] == "sent" and pushes.sent == [borrowed]


async def test_the_turn_end_leaves_the_tabs_open_by_default(rig: Rig, chrome: FakeBrowserd) -> None:
    # A turn often ends asking the operator to check the page it filled in; closing it lost that page.
    mine = await rig.session(chrome=True)
    owner = Owner("session", mine, session_id=mine)
    await rig.service.open(owner, actor="operator")
    await browser_extension.turn_ended(rig.manager, rig.service, mine, "completed")
    assert (await rig.service.get_row(group_id(owner, env=USER)))["status"] == "open"


async def test_the_turn_end_closes_the_tabs_of_a_turn_that_waits_for_nobody(rig: Rig, chrome: FakeBrowserd) -> None:
    rig.manager.config.browser.user.close_tabs = True
    mine = await rig.session(chrome=True)
    owner = Owner("session", mine, session_id=mine)
    await rig.service.open(owner, actor="operator")
    group = group_id(owner, env=USER)
    rig.manager.live_state(mine).metadata["policy_pending"] = {"k": {"tool": "BrowserNavigate"}}  # type: ignore[union-attr]
    await browser_extension.turn_ended(rig.manager, rig.service, mine, "completed")
    assert (await rig.service.get_row(group))["status"] == "open"
    rig.manager.live_state(mine).metadata.pop("policy_pending")  # type: ignore[union-attr]
    before = len(chrome.extension_calls)
    await browser_extension.turn_ended(rig.manager, rig.service, mine, "completed")
    assert (await rig.service.get_row(group))["status"] == "closed"
    # The end itself is told by the app's notification, not by this hook.
    assert len(chrome.extension_calls) == before


class Clock2:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


async def test_a_chat_controls_the_operators_chrome_while_its_tab_group_is_open_and_used(rig: Rig, chrome: FakeBrowserd) -> None:
    borrowed = await rig.session(chrome=True, kind="chat")
    made = await rig.session(chrome=True)
    clock = Clock2(datetime.now(UTC))
    control = ChromeControl(rig.service, rig.manager, now=clock)
    await control.refresh()
    assert rig.manager.control_marks.of(borrowed) == []
    owner = Owner("session", borrowed, session_id=borrowed)
    await rig.service.open(owner, actor="agent")
    await control.refresh()
    rows = {row["id"]: (row["kind"], row["controlling"]) for row in await rig.manager.list_sessions()}
    assert rows[borrowed] == ("chat", ["chrome"]) and rows[made] == ("chrome", [])
    # Idle: the group still open, unused for the idle time, and no run going.
    clock.at += IDLE_AFTER + timedelta(seconds=1)
    await control.refresh()
    assert rig.manager.control_marks.of(borrowed) == []
    # A hand-over waits for the operator in their Chrome, however long ago it was used.
    await rig.service.db.execute("UPDATE browser_groups SET needs_json = '{}' WHERE id = ?", (group_id(owner, env=USER),))
    await control.refresh()
    assert rig.manager.control_marks.of(borrowed) == ["chrome"]
    await rig.service.db.execute("UPDATE browser_groups SET needs_json = NULL WHERE id = ?", (group_id(owner, env=USER),))
    clock.at = datetime.now(UTC)
    await control.refresh()
    assert rig.manager.control_marks.of(borrowed) == ["chrome"]
    # Released: the group closed, the mark goes, and the chat was a chat all along.
    await rig.service.close_group(group_id(owner, env=USER), actor="operator")
    await control.refresh()
    rows = {row["id"]: (row["kind"], row["controlling"]) for row in await rig.manager.list_sessions()}
    assert rows[borrowed] == ("chat", []) and rows[made] == ("chrome", [])


async def test_the_installed_watcher_follows_the_groups_by_their_events(rig: Rig, chrome: FakeBrowserd) -> None:
    borrowed = await rig.session(chrome=True, kind="chat")
    owner = Owner("session", borrowed, session_id=borrowed)
    await rig.service.open(owner, actor="agent")
    async with asyncio.timeout(5):
        while rig.manager.control_marks.of(borrowed) != ["chrome"]:
            await asyncio.sleep(0.02)
    await rig.service.close_group(group_id(owner, env=USER), actor="operator")
    async with asyncio.timeout(5):
        while rig.manager.control_marks.of(borrowed):
            await asyncio.sleep(0.02)


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


async def _attached(rig: Rig, session: str, source: Path) -> tuple[str, str]:
    """A file an answer of ``session`` carries: its presentation and its item."""
    staged = await rig.manager.media.stage(session, "run-1", [{"path": str(source), "alt": "Report", "caption": ""}], layout="single")
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=staged["markdown"])], metadata={"daedalus.run_id": "run-1"})
    await rig.manager.sessions.append_transcript(session, [answer])
    view = (await rig.manager.sessions.list_transcript_views(session))[-1]
    return view["media"][0]["id"], view["media"][0]["items"][0]["id"]


async def test_the_panel_reads_the_media_of_its_own_conversations_and_no_others(rig: Rig, chrome: FakeBrowserd, base: Path) -> None:
    mine = await rig.session(chrome=True)
    other = await rig.session(chrome=False)
    second = await rig.session(chrome=True)
    picture = base / "shot.png"
    picture.write_bytes(PNG)
    report = base / "report.md"
    report.write_text("# Report")
    token, _ = await rig.grants.mint("dev1")
    own = await _attached(rig, mine, picture)
    file = await _attached(rig, mine, report)
    outside = await _attached(rig, other, picture)
    beside = await _attached(rig, second, picture)

    def content(session: str, found: tuple[str, str]) -> str:
        return f"/api/panel/sessions/{session}/media/{found[0]}/{found[1]}/content"

    def download(session: str, found: tuple[str, str], name: str = "shot.png") -> str:
        return f"/api/panel/sessions/{session}/media/{found[0]}/download?path={found[1]}/{name}"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        shown = await client.get(content(mine, own), headers=panel(token))
        assert shown.status_code == 200 and shown.content == PNG and shown.headers["content-type"] == "image/png"
        assert "inline" in shown.headers["content-disposition"]
        # A document is handed over, never shown on the app's origin.
        kept = await client.get(content(mine, file), headers=panel(token))
        assert kept.status_code == 200 and "attachment" in kept.headers["content-disposition"] and kept.content == b"# Report"
        assert (await client.get(download(mine, file, "report.md"), headers=panel(token))).content == b"# Report"
        # Without the credential, or with the operator's address and the panel's credential: an
        # element's bare request gets nothing, which is why the app reads these with a fetch.
        assert (await client.get(content(mine, own))).status_code == 401
        assert (await client.get(content(mine, own).replace("/api/panel/", "/api/"), headers=panel(token))).status_code == 401
        # Another chat's media: by its own address the conversation does not exist, whatever the
        # credential; and through a conversation the panel may open, its item is not in that one.
        assert (await client.get(content(other, outside), headers=panel(token))).status_code == 404
        assert (await client.get(download(other, outside), headers=panel(token))).status_code == 404
        assert (await client.get(content(mine, outside), headers=panel(token))).status_code == 404
        assert (await client.get(download(mine, outside), headers=panel(token))).status_code == 404
        assert (await client.get(content(mine, beside), headers=panel(token))).status_code == 404
        assert (await client.get(content(mine, (own[0], beside[1])), headers=panel(token))).status_code == 404
        # The workspace's own download is a file browser, not a message's media, and stays closed.
        assert (await client.get(f"/api/panel/sessions/{mine}/download?path=shot.png", headers=panel(token))).status_code == 404
        assert (await client.get(f"/api/panel/sessions/{mine}/media/{own[0]}/{own[1]}/content/x", headers=panel(token))).status_code == 404
        assert (await client.get(f"/api/panel/sessions/{mine}/media/../../{other}/media/{outside[0]}/{outside[1]}/content", headers=panel(token))).status_code == 404
        # A write of any kind is not a read of it.
        assert (await client.post(content(mine, own), headers=panel(token))).status_code == 404
    # The other things a conversation draws take the same door, each scoped by the conversation.
    for path in (f"/api/panel/sessions/{mine}/attachments/download", f"/api/panel/sessions/{mine}/sent/call_1/download", f"/api/panel/sessions/{mine}/tool-results/call_1/images/download"):
        assert any(m == "GET" and rx.fullmatch(path) for m, rx in api_panel.FORWARDED), path


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
