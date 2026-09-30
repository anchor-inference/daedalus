"""The browser's live view through the host: tickets, the Origin rule, the frames the app may send,
read-only watchers, how a socket ends, the audit, and the browser routes beside it.

A real uvicorn on a loopback port and a real WebSocket client, so the close codes are the ones a
browser sees, with the in-process browser daemon behind the host.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.datastructures import Headers  # noqa: F401 — imported for the type the origin header takes
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from daedalus.browser.agent import Caller
from daedalus.browser.gateway import BROWSER_WS_MAX_BYTES
from daedalus.browser.model import Owner
from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import browser as browser_extension
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.fake_browserd import Element, FakeBrowserd, ViewChannel

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((ROOT / "browserd" / "internal" / "wire" / "testdata" / "frames.json").read_text())
H = {"X-Daedalus-Token": "tok"}


def golden(direction: str) -> list[tuple[str, bytes]]:
    return [(f["name"], bytes.fromhex(f["hex"])) for f in GOLDEN["frames"] if f["direction"] == direction]


def malformed(name: str) -> bytes:
    return next(bytes.fromhex(f["hex"]) for f in GOLDEN["malformed"] if f["name"] == name)


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="bd-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def browser_settings(settings: Settings, base: Path) -> Settings:
    return settings.model_copy(update={"browser_container_dir": base / "b"})


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "b").start()
    yield fake
    await fake.stop()


@pytest.fixture
async def manager(browser_settings: Settings, db: Database) -> AsyncIterator[SessionManager]:
    made = SessionManager(browser_settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def app(browser_settings: Settings, db: Database, manager: SessionManager, daemon: FakeBrowserd) -> AsyncIterator[Any]:
    application = SimpleNamespace(settings=browser_settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await browser_extension.install(application)  # type: ignore[arg-type]
    assert await application.extensions["browser"].wait_available("container")
    yield application
    for task in tasks:
        task.cancel()
    await application.extensions["browser"].close()


class Served:
    def __init__(self, app: Any, api: Any, host: str, daemon: FakeBrowserd) -> None:
        self.app = app
        self.api = api
        self.host = host
        self.daemon = daemon

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://{self.host}")

    async def group(self) -> str:
        state = await self.app.manager.create_session("browsing")
        owner = Owner("session", state.session.id, session_id=state.session.id)
        opened = await self.app.extensions["browser"].open(owner, url="https://shop.test/", actor=f"agent:{state.session.id}")
        return str(opened["group"]["id"])

    async def ticket(self, group: str, *, read_only: bool = False) -> str:
        async with self.http() as http:
            response = await http.post(f"/api/browsers/{group}/ticket", json={"read_only": read_only}, headers=H)
        assert response.status_code == 200, response.text
        return str(response.json()["ticket"])

    def socket(self, group: str, ticket: str, *, origin: str | None = "same") -> Any:
        chosen = f"http://{self.host}" if origin == "same" else origin
        return connect(f"ws://{self.host}/ws/browsers/{group}?ticket={ticket}", origin=Origin(chosen) if chosen else None, max_size=4 << 20, open_timeout=5)


@pytest.fixture
async def served(app: Any, daemon: FakeBrowserd) -> AsyncIterator[Served]:
    api = build_app(app, "tok")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, log_level="warning", access_log=False, lifespan="off", ws_max_size=BROWSER_WS_MAX_BYTES))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    yield Served(app, api, f"127.0.0.1:{port}", daemon)
    server.should_exit = True
    await asyncio.wait_for(task, 10)
    listener.close()


async def closed_with(ws: ClientConnection, timeout: float = 5.0) -> int | None:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd is not None else None


async def attached(daemon: FakeBrowserd) -> ViewChannel:
    return await asyncio.wait_for(daemon.attached.get(), 5)


async def test_tickets_are_for_open_browsers_and_the_signed_in(served: Served) -> None:
    group = await served.group()
    async with served.http() as http:
        assert (await http.post("/api/browsers/nope/ticket", headers=H)).status_code == 404
        assert (await http.post(f"/api/browsers/{group}/ticket")).status_code == 401
        issued = (await http.post(f"/api/browsers/{group}/ticket", headers=H)).json()
    assert issued["expires_in"] == 30 and len(issued["ticket"]) > 20


async def test_the_origin_and_the_ticket_are_judged_on_an_accepted_socket(served: Served) -> None:
    group = await served.group()
    ticket = await served.ticket(group)
    async with served.socket(group, ticket, origin="https://evil.example.org") as ws:
        assert await closed_with(ws) == 4403
    # A refused origin does not spend the ticket.
    async with served.socket(group, ticket) as ws:
        await attached(served.daemon)
    async with served.socket(group, ticket) as ws:
        assert await closed_with(ws) == 4401
    async with served.socket(group, "made-up") as ws:
        assert await closed_with(ws) == 4401
    book = served.api.state.browser_gateway.tickets
    late = await served.ticket(group)
    real_clock = book.clock
    book.clock = lambda: real_clock() + 31
    try:
        async with served.socket(group, late) as ws:
            assert await closed_with(ws) == 4401
    finally:
        book.clock = real_clock


async def test_every_golden_frame_passes_whole_in_its_direction(served: Served) -> None:
    group = await served.group()
    async with served.socket(group, await served.ticket(group)) as ws:
        channel = await attached(served.daemon)
        assert channel.client["kind"] == "human" and channel.client["read_only"] is False
        for _, frame in golden("client"):
            await ws.send(frame)
            assert await channel.frame() == frame
        for _, frame in golden("server"):
            await served.daemon.send_channel(channel.id, frame)
            assert await asyncio.wait_for(ws.recv(), 5) == frame


async def test_a_watcher_sends_no_input_and_the_audit_counts_inputs_never_their_content(served: Served) -> None:
    group = await served.group()
    text = next(f for n, f in golden("client") if n == "input-text-utf8")
    mouse = next(f for n, f in golden("client") if n == "input-mouse")
    ack = next(f for n, f in golden("client") if n == "ack")
    async with served.socket(group, await served.ticket(group, read_only=True)) as ws:
        channel = await attached(served.daemon)
        assert channel.client["kind"] == "viewer" and channel.client["read_only"] is True
        await ws.send(text)
        await ws.send(ack)
        assert await channel.frame() == ack  # the input never reached the daemon; the ack after it did
    async with served.socket(group, await served.ticket(group)) as ws:
        channel = await attached(served.daemon)
        for frame in (mouse, text, text):
            await ws.send(frame)
            await channel.frame()
    await asyncio.sleep(0.2)
    entries = await served.app.extensions["browser"].audit_log(group)
    detaches = [e["detail"] for e in entries if e["action"] == "view.detach"]
    assert {d["input_dropped"] for d in detaches} == {1, 0}
    assert any(d["inputs"] == {"mouse": 1, "text": 2} for d in detaches)
    attaches = [e["detail"] for e in entries if e["action"] == "view.attach"]
    assert len(attaches) == 2 and all(a["via"] == "token" for a in attaches)
    assert "text" not in json.dumps([e["detail"].get("inputs", {}) for e in entries if e["action"] != "view.detach"])
    typed = json.loads(text[1:].decode())["text"]
    assert typed not in json.dumps(entries)


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        ("empty", 1008),
        ("unknown-type", 1008),
        ("terminal-frame-type", 1008),
        ("ack-short", 1008),
        ("ack-long", 1008),
        ("attach-not-object", 1008),
        ("input-no-t", 1008),
        ("event-no-type", 1008),
    ],
)
async def test_a_frame_the_app_must_not_send_ends_the_socket(served: Served, frame: str, code: int) -> None:
    group = await served.group()
    async with served.socket(group, await served.ticket(group)) as ws:
        channel = await attached(served.daemon)
        await ws.send(malformed(frame))
        assert await closed_with(ws) == code
        await asyncio.wait_for(channel.closed_by_host.wait(), 5)


async def test_text_and_oversized_input_are_refused_with_their_codes(served: Served) -> None:
    group = await served.group()
    async with served.socket(group, await served.ticket(group)) as ws:
        await attached(served.daemon)
        await ws.send("hello")
        assert await closed_with(ws) == 1003
    async with served.socket(group, await served.ticket(group)) as ws:
        await attached(served.daemon)
        await ws.send(b"\x33" + json.dumps({"t": "text", "text": "x" * 5000}).encode())
        assert await closed_with(ws) == 1009


async def test_the_daemon_letting_go_and_the_daemon_dying(served: Served) -> None:
    group = await served.group()
    async with served.socket(group, await served.ticket(group)) as ws:
        channel = await attached(served.daemon)
        await served.daemon.close_channel(channel.id)
        assert await closed_with(ws) == 1000
    async with served.socket(group, await served.ticket(group)) as ws:
        await attached(served.daemon)
        await served.daemon.stop()
        assert await closed_with(ws) == 1012


async def test_the_routes_list_hand_over_and_keep_downloads(served: Served, tmp_path: Path) -> None:
    group = await served.group()
    session = group.removeprefix("s-")
    async with served.http() as http:
        listed = (await http.get("/api/browsers", params={"session": session}, headers=H)).json()
        assert listed["available"] and listed["reason"] == "" and [g["id"] for g in listed["groups"]] == [group]
        one = listed["groups"][0]
        # The group as the app reads it (miniapp/src/api.ts, BrowserGroup).
        assert one["status"] == "running" and one["owner"]["kind"] == "session" and one["session_id"] == session
        assert one["tabs"][0]["url"] == "https://shop.test/" and one["active_tab"] == one["tabs"][0]["id"] and one["viewport"] == {"w": 1280, "h": 800}
        assert one["control"] == {"owner": "agent", "holder": None, "until": None, "reason": ""} and one["needs_you"] is None and one["acting"] is False
        assert (await http.get("/api/browsers", params={"session": "someone-else"}, headers=H)).json()["groups"] == []
        assert (await http.get(f"/api/browsers/{group}", headers=H)).json()["id"] == group
        assert (await http.post(f"/api/browsers/{group}/ticket", json={"tier": "thumb", "read_only": True}, headers=H)).status_code == 200
        assert (await http.post(f"/api/browsers/{group}/control", json={"owner": "human"}, headers=H)).status_code == 400
        taken = (await http.post(f"/api/browsers/{group}/control", json={"owner": "human", "client_id": "v1"}, headers=H)).json()
        assert taken["owner"] == "human" and taken["holder"] == "v1"
        given = (await http.post(f"/api/browsers/{group}/control", json={"owner": "agent", "note": "done"}, headers=H)).json()
        assert given["owner"] == "agent"
        served.daemon.tab_of(group).dialog = {"type": "confirm", "message": "Leave?"}
        assert (await http.post(f"/api/browsers/{group}/dialog", json={"accept": True}, headers=H)).status_code == 200
        assert served.daemon.tab_of(group).dialog is None
        served.daemon.add_download(group, "report.pdf", b"%PDF-1.7 fake")
        downloads = (await http.get(f"/api/browsers/{group}/downloads", headers=H)).json()["downloads"]
        saved = (await http.post(f"/api/browsers/{group}/downloads/{downloads[0]['id']}/save", json={}, headers=H)).json()
        assert Path(saved["path"]).read_bytes() == b"%PDF-1.7 fake" and saved["path"].endswith("downloads/report.pdf")
        refused = await http.post(f"/api/browsers/{group}/downloads/{downloads[0]['id']}/save", json={"to": "../../escape.pdf"}, headers=H)
        assert refused.status_code == 400
        actions = (await http.get(f"/api/browsers/{group}/actions", params={"limit": 50}, headers=H)).json()["actions"]
        kinds = [(a["actor"], a["kind"]) for a in actions]
        assert ("operator", "take") in kinds and ("operator", "give") in kinds and ("page", "download") in kinds and ("operator", "dialog") in kinds
        assert (await http.get("/api/browsers/nope/actions", headers=H)).status_code == 404
        refused = await http.post(f"/api/browsers/{group}/viewport", json={"w": 100, "h": 800}, headers=H)
        assert refused.status_code == 422
        resized = (await http.post(f"/api/browsers/{group}/viewport", json={"w": 960, "h": 720}, headers=H)).json()
        assert resized["viewport"] == {"w": 960, "h": 720}
        assert (await http.get(f"/api/browsers/{group}", headers=H)).json()["viewport"] == {"w": 960, "h": 720}
        assert (await http.get(f"/api/browsers/{group}/asks/abcdef012345/thumbnail", headers=H)).status_code == 404
        load = (await http.get("/api/workloads/load", headers=H)).json()
        assert load["browsers"]["running"] == 1 and load["terminals"] is None
        caps = (await http.get("/api/capabilities", headers=H)).json()
        assert caps["browser"] == {"configured": True, "envs": ["container"], "available": True}
        audit = (await http.get(f"/api/browsers/{group}/audit", headers=H)).json()["entries"]
        assert {"open", "take", "give", "download_saved"} <= {e["action"] for e in audit}
        # No image to update from here (no rebuilder, no image binary).
        assert (await http.post("/api/browsers/envs/container/update", json={}, headers=H)).status_code == 501
        closed = await http.post(f"/api/browsers/{group}/close", headers=H)
        assert closed.status_code == 200
        assert (await http.post(f"/api/browsers/{group}/ticket", headers=H)).status_code == 404
        after = (await http.get("/api/browsers", params={"session": session}, headers=H)).json()["groups"]
        assert after[0]["status"] == "closed"  # still listed, so the app keeps offering the tab


async def test_the_doctor_names_the_browser_its_chromium_and_its_walls(browser_settings: Settings, app: Any, daemon: FakeBrowserd) -> None:
    from daedalus.doctor import DoctorContext, _browser  # noqa: PLC0415 — the probe alone, not the whole doctor

    checks = await _browser(DoctorContext(settings=browser_settings, config=app.config, extensions=app.extensions))
    line, walls = checks
    assert line.ok and line.name == "browser (container)" and "Chromium 151.0.0.0, bundled" in line.message
    assert walls.name == "browser walls (container)" and "network of its own" in walls.message
    # Without the running service the doctor dials the daemon itself.
    alone = await _browser(DoctorContext(settings=browser_settings, config=app.config, extensions={}))
    assert alone[0].ok
    await daemon.stop()
    down = await _browser(DoctorContext(settings=browser_settings, config=app.config, extensions={}))
    assert not down[0].ok and down[0].severity == "warn"
    assert await _browser(DoctorContext(settings=browser_settings.model_copy(update={"browser_container_dir": None}), config=app.config)) == []


async def test_needs_you_is_an_urgent_notification_until_the_browser_comes_back(app: Any, db: Database) -> None:
    from daedalus.extensions.notifications import NotificationRouter  # noqa: PLC0415
    from daedalus.host.events import AppEvent  # noqa: PLC0415

    posted: list[Any] = []
    resolved: list[str] = []

    class Service:
        def language(self) -> str:
            return "en"

        def _front(self) -> Any:
            return type("Front", (), {"username": "shop_agent_bot"})()

        async def post(self, draft: Any) -> None:
            posted.append(draft)

        async def resolve(self, request_ref: str, resolution: str, *, via: str) -> int:
            resolved.append(request_ref)
            return 1

    router = NotificationRouter(Service(), app.manager)  # type: ignore[arg-type]
    await router.handle(AppEvent(seq=1, at="2026-09-26T00:00:00Z", type="browser.needs_you", payload={"group_id": "s-1", "reason": "login", "what": "sign in to github.test", "url": "https://github.test/login", "title": "Research"}, session_id="s1"))
    [draft] = posted
    assert draft.level == "urgent" and draft.title == "Research needs you in the browser" and draft.link == "/app/agents/s1?panel=browser"
    assert "sign in to github.test" in draft.body and draft.request_ref == "browser:s-1"
    # From a chat or a lock screen the line opens the Mini App on that session's Browser tab.
    assert draft.body.endswith("Open the browser: https://t.me/shop_agent_bot?startapp=browser_s1")
    await router.handle(AppEvent(seq=2, at="2026-09-26T00:00:01Z", type="browser.returned", payload={"group_id": "s-1", "url": "", "title": "", "tabs": 1, "by": "operator"}, session_id="s1"))
    assert resolved == ["browser:s-1"]


def _ts_fields(name: str) -> tuple[set[str], set[str]]:
    """The fields of a type in the app's ``api.ts``: those it requires, and the optional ones."""
    source = (ROOT / "miniapp" / "src" / "api.ts").read_text(encoding="utf-8")
    body = source[source.index(f"export type {name} = {{") :]
    first = body.split("\n", 1)[0]
    if first.rstrip().endswith("};"):
        # A type on one line: its fields are the top-level names of the object.
        inner = first[first.index("{") + 1 : first.rindex("}")]
        names = re.findall(r"(?:^|;)\s*(\w+)(\??):", inner)
    else:
        names = re.findall(r"^\s{2}(\w+)(\??):", body[: body.index("\n};")], re.M)
    required = {n for n, q in names if not q}
    optional = {n for n, q in names if q}
    return required, optional


async def test_the_host_answers_in_the_shapes_the_app_reads(served: Served) -> None:
    """The app was built against a stub; this holds the real host to the app's own types, so a name
    that drifts on either side fails here rather than as a blank Browser tab."""
    group = await served.group()
    service = served.app.extensions["browser"]
    view = await service.get(group)
    required, _ = _ts_fields("BrowserGroup")
    assert required <= set(view), required - set(view)
    agent = served.app.extensions["browser_agent"]

    async def gate(ask: Any) -> tuple[bool, str]:
        return True, ""

    served.daemon.page("https://shop.test/", title="Shop", elements={"e1": Element("e1", "searchbox", "Search", tag="input")})
    owner = service.owner_of(await service.get_row(group))
    caller = Caller(owner=owner, actor="agent:test", gate=gate, files=None)  # type: ignore[arg-type]
    await agent.run("BrowserNavigate", {"url": "https://shop.test/"}, caller)
    await agent.run("BrowserAct", {"action": "type", "ref": "e1", "element": "the search box", "text": "boots"}, caller)
    rows = await service.actions(group)
    row_required, row_optional = _ts_fields("BrowserActionRow")
    typed = next(r for r in rows if r["kind"] == "type")
    assert row_required <= set(typed) and set(typed) <= row_required | row_optional, set(typed) ^ (row_required | row_optional)
    listing_required, _ = _ts_fields("BrowserList")
    async with served.http() as http:
        listed = (await http.get("/api/browsers", params={"session": group.removeprefix("s-")}, headers=H)).json()
    assert listing_required <= set(listed)


async def test_site_notes_are_reviewed_through_the_routes_in_the_apps_shape(served: Served) -> None:
    notes = served.app.extensions["browser_notes"]
    project = await served.app.manager.projects.create("Bakery", [])
    waiting = await notes.propose(project_id=project.id, host="shop.test", text="Search answers only to Enter", by="agent:s1")
    approved = await notes.propose(project_id="", host="docs.test", text="The API reference is under Guides", by="agent:s2")
    await notes.approve(approved["id"])
    required, optional = _ts_fields("BrowserSiteNote")
    async with served.http() as http:
        listed = (await http.get("/api/browsers/notes", headers=H)).json()["notes"]
        # Waiting ones first: they are what the operator comes for.
        assert [n["id"] for n in listed] == [waiting["id"], approved["id"]] and listed[0]["project"] == "Bakery" and listed[1]["project"] == ""
        assert all(set(n) == required | optional for n in listed), [set(n) ^ required for n in listed]
        assert (await http.post(f"/api/browsers/notes/{waiting['id']}/approve", headers=H)).json()["note"]["status"] == "active"
        assert (await http.delete(f"/api/browsers/notes/{approved['id']}", headers=H)).status_code == 200
        assert (await http.delete(f"/api/browsers/notes/{approved['id']}", headers=H)).status_code == 404
        assert (await http.post("/api/browsers/notes/nope/approve", headers=H)).status_code == 404
    assert [n.text for n in await notes.active_for(project.id, "shop.test")] == ["Search answers only to Enter"]


async def test_recorded_steps_go_through_the_routes_in_the_apps_shape(served: Served) -> None:
    group = await served.group()
    served.daemon.set_control(group, "human", "c1")
    required, optional = _ts_fields("BrowserWorkflow")
    note_fields = set.union(*_ts_fields("BrowserSiteNote"))
    async with served.http() as http:
        for _ in range(200):
            if (await http.get(f"/api/browsers/{group}", headers=H)).json()["control"]["owner"] == "human":
                break
            await asyncio.sleep(0.02)
        started = await http.post(f"/api/browsers/{group}/workflow", json={"values": "literal"}, headers=H)
        assert started.status_code == 200, started.text
        assert set(started.json()["workflow"]) == required | optional
        assert (await http.post(f"/api/browsers/{group}/workflow", json={"values": "everything"}, headers=H)).status_code == 422
        served.daemon.record_step(group, action="click", element={"role": "button", "name": "Export"})
        marked = (await http.post(f"/api/browsers/{group}/workflow/mark", json={}, headers=H)).json()["step"]
        assert marked["action"] == "expect"
        listed = (await http.get(f"/api/browsers/{group}/workflow", headers=H)).json()
        assert set(listed) == {"workflow", "recent"} and set(listed["workflow"]) == required | optional and len(listed["workflow"]["steps"]) == 2
        stopped = (await http.post(f"/api/browsers/{group}/workflow/stop", headers=H)).json()["workflow"]
        assert stopped["state"] == "stopped" and set(stopped) == required | optional
        assert (await http.post(f"/api/browsers/{group}/workflow/stop", headers=H)).status_code == 404
        listed = (await http.get(f"/api/browsers/{group}/workflow", headers=H)).json()
        assert listed["workflow"] is None and [w["id"] for w in listed["recent"]] == [stopped["id"]]
        assert (await http.get(f"/api/browsers/workflows/{stopped['id']}", headers=H)).json()["workflow"]["steps"][0]["element"]["name"] == "Export"
        drafted = (await http.post(f"/api/browsers/workflows/{stopped['id']}/draft", json={"goal": "Export the report"}, headers=H)).json()
        assert set(drafted) == set.union(*_ts_fields("BrowserDraft")) and set(drafted["note"]) == note_fields
        note = drafted["note"]
        assert note["kind"] == "procedure" and note["title"] == "Export the report" and note["status"] == "proposed"
        edited = await http.patch(f"/api/browsers/notes/{note['id']}", json={"text": "1. Click Export.", "title": "Export"}, headers=H)
        assert edited.status_code == 200 and edited.json()["note"]["text"] == "1. Click Export." and edited.json()["note"]["title"] == "Export"
        assert (await http.patch(f"/api/browsers/notes/{note['id']}", json={"text": "  "}, headers=H)).status_code == 400
        assert (await http.patch("/api/browsers/notes/nope", json={"text": "x"}, headers=H)).status_code == 404
        assert (await http.delete(f"/api/browsers/workflows/{stopped['id']}", headers=H)).status_code == 200
        assert (await http.get(f"/api/browsers/workflows/{stopped['id']}", headers=H)).status_code == 404
