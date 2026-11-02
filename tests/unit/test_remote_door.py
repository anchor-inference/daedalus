"""The terminal client from the server's side, through the real app: pairing over the public doors, the
operator's approval and device list, the device token's door and its matrix, the scope of sessions it
may drive, a remote conversation bound at creation, ``/resume``, the guard, presence and the live mark,
the model list, and ``via: cli`` on what the terminal answers. The contract is
``docs/architecture/cli.md``; the matrix below is the one place that says what a device token reaches.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions import remote as remote_extension
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.remote.devices import POLL_INTERVAL, Devices
from daedalus.remote.door import ROUTES, SCOPE_KEY, DeviceDoor, route_of
from daedalus.stores.database import Database
from tests.support.models import model_config

REPO = Path(__file__).resolve().parents[2]
OPERATOR = {"x-daedalus-token": "tok"}


def device(token: str) -> dict[str, str]:
    return {"authorization": f"Device {token}"}


@dataclass
class Rig:
    manager: SessionManager
    app: Any
    client: httpx.AsyncClient

    @property
    def devices(self) -> Devices:
        return self.app.extensions["remote_devices"]

    async def pair(self, name: str = "laptop") -> tuple[str, str]:
        """``(device id, token)`` through the public doors and the operator's approval."""
        started = (await self.client.post("/api/cli/pair/start", json={"name": name, "hostname": name, "os": "linux", "arch": "amd64", "client_version": "0.1.0"})).json()
        approved = await self.client.post(f"/api/cli/pair/{started['user_code']}/approve", headers=OPERATOR)
        assert approved.status_code == 200, approved.text
        await self.wait_interval(started["device_code"])
        polled = (await self.client.post("/api/cli/pair/poll", json={"device_code": started["device_code"]})).json()
        assert polled["status"] == "approved", polled
        return polled["device"]["id"], polled["token"]

    async def wait_interval(self, device_code: str) -> None:
        """Move the request's last poll back past the interval instead of sleeping."""
        await self.app.db.execute("UPDATE cli_pairings SET polled_at = NULL")

    async def remote(self, token: str, cwd: str = "/home/you/project") -> str:
        made = await self.client.post("/api/sessions", headers=device(token), json={"kind": "remote", "cwd": cwd, "title": "work"})
        assert made.status_code == 200, made.text
        return str(made.json()["id"])


@pytest.fixture
async def rig(settings: Settings, db: Database) -> AsyncIterator[Rig]:
    configured = settings.model_copy(update={"native": False, "miniapp_public_url": "https://daedalus.example.org/app/"})
    manager = SessionManager(configured, model_config(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session, extension_failures={})
    tasks = await remote_extension.install(app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        yield Rig(manager, app, client)
    for task in tasks:
        task.cancel()
    await manager.close()


# -- pairing over HTTP --------------------------------------------------------------------------------


async def test_a_terminal_pairs_through_the_public_doors_and_the_operators_approval(rig: Rig) -> None:
    started = await rig.client.post("/api/cli/pair/start", json={"name": "laptop", "hostname": "laptop", "os": "linux", "extra": "ignored"})
    assert started.status_code == 200
    body = started.json()
    code = body["user_code"]
    assert len(code) == 9 and code[4] == "-" and body["interval"] == POLL_INTERVAL and body["expires_in"] == 600
    assert body["verification_uri_complete"] == f"https://daedalus.example.org/app/?pair-device={code}"
    assert (await rig.client.post("/api/cli/pair/poll", json={"device_code": body["device_code"]})).json() == {"status": "pending", "interval": POLL_INTERVAL}
    # The approval is the operator's: a public caller, or one holding nothing, is refused.
    assert (await rig.client.post(f"/api/cli/pair/{code}/approve")).status_code == 401
    pending = (await rig.client.get("/api/cli/pairs", headers=OPERATOR)).json()["requests"]
    assert [p["user_code"] for p in pending] == [code] and pending[0]["state"] == "pending"
    assert (await rig.client.get(f"/api/cli/pair/{code.lower().replace('-', '')}", headers=OPERATOR)).json()["name"] == "laptop"
    approved = await rig.client.post(f"/api/cli/pair/{code}/approve", headers=OPERATOR, json={"name": "Work laptop"})
    assert approved.json()["request"]["state"] == "approved"
    assert (await rig.client.post(f"/api/cli/pair/{code}/deny", headers=OPERATOR)).status_code == 409
    await rig.wait_interval(body["device_code"])
    polled = await rig.client.post("/api/cli/pair/poll", json={"device_code": body["device_code"]})
    assert polled.headers["cache-control"] == "no-store"
    token = polled.json()["token"]
    me = await rig.client.get("/api/cli/me", headers=device(token))
    assert me.status_code == 200 and me.json()["device"]["name"] == "Work laptop" and me.json()["server"]["contract"] == 1
    listed = (await rig.client.get("/api/cli/devices", headers=OPERATOR)).json()["devices"]
    assert [d["name"] for d in listed] == ["Work laptop"] and listed[0]["connected"] is False
    unknown = await rig.client.post("/api/cli/pair/poll", json={"device_code": "nope"})
    assert unknown.status_code == 404 and unknown.json()["status"] == "unknown"
    assert (await rig.client.get("/api/cli/pair/ZZZZ-ZZZZ", headers=OPERATOR)).status_code == 404


async def test_a_denied_terminal_gets_no_token(rig: Rig) -> None:
    body = (await rig.client.post("/api/cli/pair/start", json={})).json()
    assert (await rig.client.post(f"/api/cli/pair/{body['user_code']}/deny", headers=OPERATOR)).status_code == 200
    await rig.wait_interval(body["device_code"])
    assert (await rig.client.post("/api/cli/pair/poll", json={"device_code": body["device_code"]})).json() == {"status": "denied"}


# -- the door -----------------------------------------------------------------------------------------


MATRIX: list[tuple[str, str, bool]] = [
    ("GET", "/api/cli/me", True),
    ("GET", "/api/cli/sessions", True),
    ("POST", "/api/cli/token/rotate", True),
    ("GET", "/api/models", True),
    ("GET", "/api/commands", True),
    ("GET", "/api/skills", True),
    ("GET", "/api/auth/me", True),
    ("POST", "/api/presence", True),
    ("POST", "/api/sessions", True),
    ("GET", "/api/sessions/s1", True),
    ("GET", "/api/sessions/s1/stream", True),
    ("POST", "/api/sessions/s1/messages", True),
    ("POST", "/api/sessions/s1/policy/grant", True),
    ("GET", "/api/sessions/s1/policy/similar/abc", True),
    ("DELETE", "/api/sessions/s1/steer/m1", True),
    ("PATCH", "/api/sessions/s1", True),
    ("GET", "/api/events", True),
    ("POST", "/api/cli/sessions/s1/attach", True),
    ("PUT", "/api/cli/sessions/s1/guard", True),
    ("WS", "/api/cli/relay", True),
    # The operator's alone.
    ("DELETE", "/api/sessions/s1", False),
    ("GET", "/api/sessions", False),
    ("GET", "/api/settings", False),
    ("PUT", "/api/settings", False),
    ("GET", "/api/cli/devices", False),
    ("DELETE", "/api/cli/devices/d1", False),
    ("POST", "/api/cli/pair/ABCD-EFGH/approve", False),
    ("GET", "/api/cli/pairs", False),
    ("GET", "/api/secrets", False),
    ("POST", "/api/sessions/s1/move", False),
    ("PUT", "/api/sessions/s1/mcp", False),
    ("GET", "/api/panel/sessions", False),
    ("POST", "/api/auth/sessions/revoke", False),
    ("GET", "/api/memory", False),
    ("WS", "/api/terminals/t1/ws", False),
]


@pytest.mark.parametrize(("method", "path", "opens"), MATRIX)
def test_the_matrix_says_what_a_device_token_reaches(method: str, path: str, opens: bool) -> None:
    assert (route_of(method, path) is not None) is opens


def test_every_route_the_door_names_is_one_the_app_has() -> None:
    """A route in the door's list that the app does not have is a path a typo opened, or a route that
    was renamed and left the client behind. The relay is the one route of another part."""
    source = "".join((REPO / "daedalus" / "extensions" / name).read_text(encoding="utf-8") for name in ("api.py", "api_remote.py", "api_btw.py"))
    declared = [re.sub(r"\{[a-z_]+\}", "s1", path) for path in re.findall(r'@api\.(?:get|post|put|patch|delete)\("([^"]+)"', source)]
    for method, pattern, _rule in ROUTES:
        if method != "WS":
            assert any(pattern.fullmatch(path) for path in declared), pattern.pattern


async def test_a_bad_token_is_told_to_log_in_again_and_a_closed_route_says_so(rig: Rig) -> None:
    _, token = await rig.pair()
    bad = await rig.client.get("/api/cli/me", headers=device("dcl_nonsense"))
    assert bad.status_code == 401 and bad.json()["code"] == "device_token_invalid"
    assert (await rig.client.get("/api/cli/me", headers={"authorization": "Device "})).status_code == 401
    closed = await rig.client.get("/api/settings", headers=device(token))
    assert closed.status_code == 403 and closed.json()["code"] == "device_route"
    assert (await rig.client.get("/api/cli/devices", headers=device(token))).status_code == 403
    assert (await rig.client.delete("/api/cli/devices/x", headers=device(token))).status_code == 403
    # The device's own routes refuse every other credential: the operator has no device.
    assert (await rig.client.get("/api/cli/me", headers=OPERATOR)).status_code == 401
    # A device token is no operator token, and the static token no device token.
    assert (await rig.client.get("/api/settings", headers={"x-daedalus-token": token})).status_code == 401


async def test_the_scope_is_what_the_device_started_or_resumed(rig: Rig) -> None:
    laptop_id, laptop = await rig.pair("laptop")
    _, desktop = await rig.pair("desktop")
    mine = await rig.remote(laptop)
    chat = (await rig.manager.create_session("chat")).session.id
    assert (await rig.client.get(f"/api/sessions/{mine}", headers=device(laptop))).status_code == 200
    # Outside the scope a session does not exist.
    for token, sid in ((desktop, mine), (laptop, chat), (laptop, "missing")):
        answer = await rig.client.get(f"/api/sessions/{sid}", headers=device(token))
        assert answer.status_code == 404, (sid, answer.text)
    assert (await rig.client.post(f"/api/sessions/{chat}/stop", headers=device(laptop))).status_code == 404
    # /resume puts it in scope; detaching takes it out; a home session cannot be detached.
    attached = await rig.client.post(f"/api/cli/sessions/{chat}/attach", headers=device(laptop))
    assert attached.status_code == 200 and attached.json()["attached"] is True and attached.json()["remote"] is None
    assert (await rig.client.get(f"/api/sessions/{chat}", headers=device(laptop))).status_code == 200
    assert (await rig.client.delete(f"/api/cli/sessions/{chat}/attach", headers=device(laptop))).status_code == 200
    assert (await rig.client.get(f"/api/sessions/{chat}", headers=device(laptop))).status_code == 404
    assert (await rig.client.delete(f"/api/cli/sessions/{mine}/attach", headers=device(laptop))).status_code == 409
    assert (await rig.client.post("/api/cli/sessions/missing/attach", headers=device(laptop))).status_code == 404
    # A subagent of a session in scope is in scope.
    child = (await rig.manager.create_session("helper", metadata={"subagent_of": mine})).session.id
    assert (await rig.client.get(f"/api/sessions/{child}", headers=device(laptop))).status_code == 200
    # The event stream only narrowed to a session in scope.
    assert (await rig.client.get("/api/events", headers=device(laptop))).status_code == 403
    assert (await rig.client.get(f"/api/events?session={chat}", headers=device(laptop))).status_code == 404
    # Revoked, the token opens nothing.
    assert (await rig.client.delete(f"/api/cli/devices/{laptop_id}", headers=OPERATOR)).status_code == 200
    gone = await rig.client.get(f"/api/sessions/{mine}", headers=device(laptop))
    assert gone.status_code == 401 and gone.json()["code"] == "device_token_invalid"
    assert (await rig.client.delete(f"/api/cli/devices/{laptop_id}", headers=OPERATOR)).status_code == 404


# -- remote conversations -----------------------------------------------------------------------------


async def test_a_remote_conversation_is_bound_to_the_device_that_starts_it(rig: Rig) -> None:
    device_id, token = await rig.pair()
    made = await rig.client.post("/api/sessions", headers=device(token), json={"kind": "remote", "cwd": "C:\\Users\\you\\x", "title": "win", "guard": "plan"})
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["kind"] == "remote"
    assert body["remote"] == {"device_id": device_id, "device_name": "laptop", "cwd": "C:\\Users\\you\\x", "connected": False, "disconnected_at": None, "guard": "plan", "waiting": None}
    # The relay going down is kept as a moment every window reads.
    live = rig.app.extensions["remote_control"]
    await live.linked(device_id, [body["id"]], client="p1")
    assert (await rig.client.get(f"/api/sessions/{body['id']}", headers=device(token))).json()["remote"]["connected"] is True
    await live.unlinked(device_id, client="p1")
    dropped = (await rig.client.get(f"/api/sessions/{body['id']}", headers=device(token))).json()["remote"]
    assert dropped["connected"] is False and dropped["disconnected_at"]
    listed = (await rig.client.get("/api/cli/devices", headers=OPERATOR)).json()["devices"][0]
    assert listed["disconnected_at"] == dropped["disconnected_at"] and listed["token_issued_at"] == listed["paired_at"]
    view = (await rig.client.get(f"/api/sessions/{body['id']}", headers=device(token))).json()
    assert view["remote"]["cwd"] == "C:\\Users\\you\\x"
    rows = (await rig.client.get("/api/sessions", headers=OPERATOR)).json()["sessions"]
    row = next(r for r in rows if r["id"] == body["id"])
    assert row["kind"] == "remote" and row["remote"]["device_id"] == device_id and row["remote"]["waiting"] is False
    # What a device may not start, and what a remote conversation may not name.
    refused = [
        ({"kind": "chat", "title": "x"}, 403),
        ({"kind": "remote", "title": "x"}, 400),
        ({"kind": "remote", "title": "x", "cwd": "relative/path"}, 400),
        ({"kind": "remote", "title": "x", "cwd": "/"}, 400),
        ({"kind": "remote", "title": "x", "cwd": "C:\\"}, 400),
        ({"kind": "remote", "title": "x", "cwd": "/x", "project_id": "p"}, 400),
        ({"kind": "remote", "title": "x", "cwd": "/x", "device_id": "someone-else"}, 403),
    ]
    for payload, status in refused:
        answer = await rig.client.post("/api/sessions", headers=device(token), json=payload)
        assert answer.status_code == status, (payload, answer.text)
    # The operator starts one on a named device, and nowhere else.
    assert (await rig.client.post("/api/sessions", headers=OPERATOR, json={"kind": "remote", "title": "x", "cwd": "/x"})).status_code == 400
    assert (await rig.client.post("/api/sessions", headers=OPERATOR, json={"kind": "remote", "title": "x", "cwd": "/x", "device_id": "nope"})).status_code == 404
    assert (await rig.client.post("/api/sessions", headers=OPERATOR, json={"kind": "chat", "title": "x", "cwd": "/x"})).status_code == 400
    by_operator = await rig.client.post("/api/sessions", headers=OPERATOR, json={"kind": "remote", "title": "x", "cwd": "/srv/x", "device_id": device_id})
    assert by_operator.status_code == 200 and by_operator.json()["remote"]["device_id"] == device_id


async def test_resume_lists_the_operators_conversations_and_hands_move_on_request(rig: Rig) -> None:
    laptop_id, laptop = await rig.pair("laptop")
    desktop_id, desktop = await rig.pair("desktop")
    here = await rig.remote(laptop, "/home/you/a")
    elsewhere = await rig.remote(laptop, "/home/you/b")
    chat = (await rig.manager.create_session("plain chat")).session.id
    listed = (await rig.client.get("/api/cli/sessions?cwd=/home/you/a", headers=device(laptop))).json()["sessions"]
    by_id = {row["id"]: row for row in listed}
    assert {here, elsewhere, chat} <= by_id.keys()
    assert by_id[here]["here"] and by_id[here]["attached"] and not by_id[elsewhere]["here"] and not by_id[chat]["attached"]
    assert [row["id"] for row in (await rig.client.get("/api/cli/sessions?q=plain", headers=device(laptop))).json()["sessions"]] == [chat]
    # Continued on the desktop: attach, then move the hands.
    assert (await rig.client.post(f"/api/cli/sessions/{here}/attach", headers=device(desktop))).status_code == 200
    moved = await rig.client.post(f"/api/cli/sessions/{here}/hands", headers=device(desktop), json={"cwd": "/srv/you/a"})
    assert moved.status_code == 200 and moved.json()["remote"]["device_id"] == desktop_id and moved.json()["remote"]["cwd"] == "/srv/you/a"
    assert (await rig.client.get(f"/api/sessions/{here}", headers=device(laptop))).status_code == 200, "the laptop keeps it to show"
    await rig.client.post(f"/api/cli/sessions/{chat}/attach", headers=device(desktop))
    assert (await rig.client.post(f"/api/cli/sessions/{chat}/hands", headers=device(desktop), json={"cwd": "/x"})).status_code == 400
    assert laptop_id != desktop_id


async def test_a_conversation_made_or_moved_from_a_terminal_runs_its_tools_on_that_terminal(rig: Rig) -> None:
    # The relay's own map of whose hands a session has is told by the create and hands routes; without
    # it the session's tools stay pointed at this server.
    from daedalus.extensions import remote_relay as relay_extension
    from daedalus.remote.hands import DeviceExecBackend

    await relay_extension.install(rig.app)  # type: ignore[arg-type]
    turns = rig.app.extensions["remote_turns"]
    laptop_id, laptop = await rig.pair("laptop")
    desktop_id, desktop = await rig.pair("desktop")
    sid = await rig.remote(laptop, "/home/you/a")
    state = rig.manager.live_state(sid)
    assert state is not None and turns.home_of(state) == {"device_id": laptop_id, "cwd": "/home/you/a"}
    backend = state.services.exec_backend if state.services is not None else None
    assert isinstance(backend, DeviceExecBackend) and backend.device_id == laptop_id
    assert (await rig.client.post(f"/api/cli/sessions/{sid}/attach", headers=device(desktop))).status_code == 200
    assert (await rig.client.post(f"/api/cli/sessions/{sid}/hands", headers=device(desktop), json={"cwd": "/srv/you/a"})).status_code == 200
    assert turns.home_of(state) == {"device_id": desktop_id, "cwd": "/srv/you/a"}
    backend = state.services.exec_backend if state.services is not None else None
    assert isinstance(backend, DeviceExecBackend) and backend.device_id == desktop_id


async def test_the_guard_presence_and_the_live_mark(rig: Rig) -> None:
    _, token = await rig.pair()
    sid = await rig.remote(token)
    other = (await rig.manager.create_session("not mine")).session.id
    assert (await rig.client.put(f"/api/cli/sessions/{sid}/guard", headers=device(token), json={"guard": "ask"})).json() == {"guard": "ask"}
    assert (await rig.client.put(f"/api/cli/sessions/{sid}/guard", headers=device(token), json={"guard": "yolo"})).status_code == 422
    assert (await rig.client.get(f"/api/sessions/{sid}", headers=device(token))).json()["remote"]["guard"] == "ask"
    events = await rig.app.db.fetchall("SELECT type, payload_json FROM app_events WHERE type = 'remote.guard' AND session_id = ?", (sid,))
    assert [json.loads(e["payload_json"])["guard"] for e in events] == ["ask"]

    report = {"client": "cli-1", "kind": "cli", "visible": True, "focused": True, "sessions": [sid, other]}
    assert (await rig.client.post("/api/presence", headers=device(token), json=report)).status_code == 204
    # What a terminal names beyond its scope is dropped; what it shows is attended and marked.
    assert rig.manager.presence.attending(session_id=sid) and not rig.manager.presence.attending(session_id=other)
    assert rig.manager.control_marks.of(sid) == ["remote"] and rig.manager.control_marks.of(other) == []
    rows = await rig.manager.list_sessions(ids=[sid])
    assert rows[0]["controlling"] == ["remote"]
    await rig.devices.revoke((await rig.devices.paired())[0].id)
    assert rig.manager.control_marks.of(sid) == [] and not rig.manager.presence.attending(session_id=sid)


async def test_what_a_terminal_answers_is_recorded_as_the_terminals(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _, token = await rig.pair()
    sid = await rig.remote(token)
    seen: list[str] = []

    async def grant(session_id: str, key: str, *, via: str) -> dict[str, Any]:
        seen.append(via)
        return {}

    monkeypatch.setattr(rig.manager, "grant", grant)
    monkeypatch.setattr(rig.manager, "refuse", grant)
    await rig.client.post(f"/api/sessions/{sid}/policy/grant", headers=device(token), json={"key": "k"})
    await rig.client.post(f"/api/sessions/{sid}/policy/refuse", headers=device(token), json={"key": "k"})
    await rig.client.post(f"/api/sessions/{sid}/policy/grant", headers=OPERATOR, json={"key": "k"})
    assert seen == ["cli", "cli", "app"]


async def test_the_model_list_names_no_address_or_key(rig: Rig) -> None:
    _, token = await rig.pair()
    models = (await rig.client.get("/api/models", headers=device(token))).json()
    assert models["presets"] and models["default"] in {p["id"] for p in models["presets"]}
    assert models["efforts"] == ["low", "medium", "high", "xhigh"]
    text = json.dumps(models)
    assert "http" not in text and "api_key" not in text and "base_url" not in text


# -- the door alone, for what HTTP cannot show ----------------------------------------------------------


class FakeDevices:
    def __init__(self) -> None:
        self.known = {"dcl_good": SimpleNamespace(id="d1", name="laptop")}

    async def check(self, token: str, *, address: str = "") -> Any:
        return self.known.get(token)

    async def in_scope(self, device_id: str, session_id: str) -> bool:
        return session_id == "mine"


async def door_call(scope: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    reached: dict[str, Any] = {}

    async def inner(inner_scope: dict[str, Any], receive: Any, send: Any) -> None:
        reached.update(inner_scope)

    pending = list(messages or [{"type": "websocket.connect"}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return pending.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await DeviceDoor(inner, devices=lambda: FakeDevices())(scope, receive, send)  # type: ignore[arg-type,return-value]
    return sent, reached or None


async def test_the_relay_socket_is_closed_with_a_code_the_client_can_read() -> None:
    def socket(token: str, path: str = "/api/cli/relay", origin: str = "") -> dict[str, Any]:
        headers = [(b"authorization", f"Device {token}".encode())] + ([(b"origin", origin.encode())] if origin else [])
        return {"type": "websocket", "path": path, "headers": headers, "client": ("127.0.0.1", 1)}

    sent, reached = await door_call(socket("dcl_bad"))
    assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"] and sent[-1]["code"] == 4401 and reached is None
    sent, reached = await door_call(socket("dcl_good", "/api/terminals/t/ws"))
    assert sent[-1]["code"] == 4403 and reached is None
    sent, reached = await door_call(socket("dcl_good", origin="https://evil.example"))
    assert sent == [{"type": "websocket.close", "code": 1008}] and reached is None
    sent, reached = await door_call(socket("dcl_good"))
    assert sent == [] and reached is not None and reached[SCOPE_KEY] == {"device": "d1", "device_name": "laptop"}


async def test_a_request_without_the_devices_credential_carries_no_device_mark() -> None:
    forged = {"type": "http", "method": "GET", "path": "/api/settings", "headers": [], SCOPE_KEY: {"device": "d1"}}
    _, reached = await door_call(forged)
    assert reached is not None and SCOPE_KEY not in reached
    scoped = {"type": "http", "method": "GET", "path": "/api/events", "query_string": b"session=mine&kind=cli", "headers": [(b"authorization", b"Device dcl_good")]}
    _, reached = await door_call(scoped)
    assert reached is not None and reached[SCOPE_KEY]["device"] == "d1"


# -- Telegram -----------------------------------------------------------------------------------------


class FakeFront:
    """The two hooks and the one way of posting a choice that the extension uses."""

    def __init__(self) -> None:
        self.callback_hooks: dict[str, Any] = {}
        self.command_hooks: dict[str, Any] = {}
        self.choices: list[tuple[str, list[list[tuple[str, str]]]]] = []

    def _general_outbox(self) -> Any:
        return SimpleNamespace(chat_id=1, thread_id=None)

    def outbox(self, chat_id: int, thread_id: int | None) -> Any:
        return SimpleNamespace(chat_id=chat_id, thread_id=thread_id)

    async def send_choice(self, outbox: Any, text: str, rows: list[list[tuple[str, str]]]) -> int:
        self.choices.append((text, rows))
        return len(self.choices)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[str] = []
        self.edited: list[str] = []
        self.message = SimpleNamespace(text="the request", edit_text=self._edit)

    async def _edit(self, text: str, reply_markup: Any = None) -> None:
        self.edited.append(text)

    async def answer(self, text: str = "") -> None:
        self.answers.append(text)


async def test_telegram_announces_a_request_with_its_code_and_its_buttons_decide_it(settings: Settings, db: Database) -> None:
    import asyncio

    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    front = FakeFront()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=front, extensions={})
    tasks = await remote_extension.install(app)  # type: ignore[arg-type]
    try:
        devices: Devices = app.extensions["remote_devices"]
        found, code = await devices.start_pairing(name="laptop", hostname="laptop", os="linux", address="10.0.0.5")
        async with asyncio.timeout(5):
            while not front.choices:
                await asyncio.sleep(0.02)
        text, rows = front.choices[0]
        assert f"{found.user_code[:4]}-{found.user_code[4:]}" in text and "10.0.0.5" in text
        allow, deny = rows[0]
        assert allow[1] == f"dv:{found.id}:allow" and deny[1] == f"dv:{found.id}:deny"
        query = FakeQuery(allow[1])
        await front.callback_hooks["dv"](query, allow[1].split(":"))
        assert query.answers[0].startswith("Allowed laptop") and "→ Allowed" in query.edited[0]
        again = FakeQuery(deny[1])
        await front.callback_hooks["dv"](again, deny[1].split(":"))
        assert "already" in again.answers[0]
        await devices.db.execute("UPDATE cli_pairings SET polled_at = NULL")
        device_id = (await devices.poll(code))["device"]["id"]
        message = SimpleNamespace(chat=SimpleNamespace(id=1), message_thread_id=None, is_topic_message=False, answer=None)
        await front.command_hooks["devices"](message, None)
        listed, buttons = front.choices[-1]
        assert "laptop" in listed and buttons == [[("Revoke", f"dv:{device_id}:revoke")]]
        revoke = FakeQuery(buttons[0][0][1])
        await front.callback_hooks["dv"](revoke, buttons[0][0][1].split(":"))
        assert revoke.answers[0].startswith("Revoked") and await devices.paired() == []
    finally:
        for task in tasks:
            task.cancel()
        await manager.close()
