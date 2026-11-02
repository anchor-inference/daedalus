"""The terminal client's devices from the store's side: pairing by device code, the token kept as its hash
and handed out once, revocation, rotation and lapse, each device's scope, and the migrations that make
room for them (the ``remote`` kind, the ``cli_*`` tables). The contract is ``docs/architecture/cli.md``."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from daedalus.remote.control import SHOWN_FOR, RemoteControl
from daedalus.remote.devices import (
    DEVICE_IDLE,
    PAIRING_TTL,
    PENDING_MAX,
    POLL_INTERVAL,
    TOKEN_PREFIX,
    Conflict,
    Devices,
    NotFound,
    TooMany,
)
from daedalus.stores import database
from daedalus.stores.database import MIGRATIONS, Database, _remote_kind


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


class Published:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


async def session(db: Database, sid: str, *, kind: str = "chat", metadata: dict[str, Any] | None = None) -> str:
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = 'p'"):
        await db.execute("INSERT INTO projects(id, name, created_at, settings, system) VALUES ('p', 'P', 'now', '{}', '')")
    await db.execute(
        "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id, kind) VALUES (?, 't', ?, 'now', 'now', ?, 'p', ?)",
        (sid, sid, json.dumps(metadata or {}), kind),
    )
    return sid


async def paired(devices: Devices, clock: Clock, *, name: str = "laptop", via: str = "app") -> tuple[str, str]:
    """A device paired the whole way through: ``(device id, token)``."""
    found, code = await devices.start_pairing(name=name, hostname=name, os="linux", arch="amd64", client_version="0.1.0", address="10.0.0.5")
    await devices.decide(found, approve=True, via=via)  # type: ignore[arg-type]
    clock.at += timedelta(seconds=POLL_INTERVAL)
    answer = await devices.poll(code, address="10.0.0.5")
    assert answer["status"] == "approved"
    return answer["device"]["id"], answer["token"]


# -- pairing ------------------------------------------------------------------------------------------


async def test_a_terminal_pairs_by_device_code_and_gets_its_token_exactly_once(db: Database) -> None:
    clock, published = Clock(), Published()
    devices = Devices(db, now=clock, publish=published)
    found, device_code = await devices.start_pairing(name="laptop", hostname="laptop", os="linux", address="10.0.0.5")
    assert len(found.user_code) == 8 and found.view(clock.at)["user_code"] == f"{found.user_code[:4]}-{found.user_code[4:]}"
    assert published.events[-1] == ("device.pairing", found.event(clock.at)) and found.event(clock.at)["state"] == "pending"
    # Only the device code's hash is kept; the user code is no credential and is kept as shown.
    stored = await db.fetchone("SELECT * FROM cli_pairings")
    assert device_code not in repr(dict(stored)) and stored["user_code"] == found.user_code

    assert (await devices.poll(device_code))["status"] == "pending"
    clock.at += timedelta(seconds=1)
    assert await devices.poll(device_code) == {"status": "slow_down", "interval": POLL_INTERVAL * 2}
    # Typed in any case, with or without the dash, the code finds its request.
    assert (await devices.pairing(found.user_code.lower()[:4] + "-" + found.user_code.lower()[4:])) is not None
    assert [p.id for p in await devices.pending()] == [found.id]

    decided = await devices.decide(await devices.pairing(found.user_code), approve=True, via="telegram", name="Work laptop")
    assert decided.stored_state == "approved" and decided.name == "Work laptop"
    with pytest.raises(Conflict):
        await devices.decide(decided, approve=False, via="app")
    assert await db.fetchone("SELECT 1 FROM cli_devices") is None, "the device is made at the poll, not at the approval"

    clock.at += timedelta(seconds=POLL_INTERVAL)
    answer = await devices.poll(device_code, address="10.0.0.6")
    assert answer["status"] == "approved" and answer["token"].startswith(TOKEN_PREFIX)
    device = answer["device"]
    assert device["name"] == "Work laptop" and device["approved_via"] == "telegram" and device["last_address"] == "10.0.0.6"
    row = await db.fetchone("SELECT token_hash FROM cli_devices WHERE id = ?", (device["id"],))
    assert row["token_hash"] and answer["token"] not in row["token_hash"]
    assert "device.changed" in published.names()
    # The token was in that one answer; the next poll hands out nothing.
    clock.at += timedelta(seconds=POLL_INTERVAL)
    assert await devices.poll(device_code) == {"status": "expired"}
    assert (await devices.check(answer["token"])).id == device["id"]  # type: ignore[union-attr]


async def test_a_denied_an_expired_and_an_unknown_request(db: Database) -> None:
    clock = Clock()
    devices = Devices(db, now=clock)
    denied, denied_code = await devices.start_pairing(name="a")
    await devices.decide(denied, approve=False, via="app")
    assert await devices.poll(denied_code) == {"status": "denied"}
    late, late_code = await devices.start_pairing(name="b")
    clock.at += PAIRING_TTL + timedelta(seconds=1)
    assert await devices.poll(late_code) == {"status": "expired"}
    assert await devices.pairing(late.user_code) is None
    with pytest.raises(NotFound):
        await devices.decide(late, approve=True, via="app")
    # Wrong device codes count toward the same bounds as the browser's pairing; past them even a
    # right one waits.
    for _ in range(5):
        with pytest.raises(NotFound):
            await devices.poll("guess", address="10.0.0.9")
    with pytest.raises(TooMany):
        await devices.poll(late_code, address="10.0.0.9")
    with pytest.raises(TooMany):
        await devices.start_pairing(address="10.0.0.9")


async def test_at_most_a_handful_of_requests_wait_for_the_operator(db: Database) -> None:
    devices = Devices(db, now=Clock())
    for index in range(PENDING_MAX):
        await devices.start_pairing(name=f"t{index}")
    with pytest.raises(TooMany):
        await devices.start_pairing(name="one too many")


async def test_two_polls_racing_hand_out_one_token(db: Database) -> None:
    clock = Clock()
    devices = Devices(db, now=clock)
    found, code = await devices.start_pairing(name="laptop")
    await devices.decide(found, approve=True, via="app")
    clock.at += timedelta(seconds=POLL_INTERVAL)
    first = await devices.poll(code)
    second = await devices.poll(code)
    assert first["status"] == "approved" and second["status"] == "expired"
    assert len(await devices.paired()) == 1


# -- tokens -------------------------------------------------------------------------------------------


async def test_a_token_is_revoked_rotated_and_lapses_when_unused(db: Database) -> None:
    clock, published = Clock(), Published()
    devices = Devices(db, now=clock, publish=published)
    device_id, token = await paired(devices, clock)
    other_id, other = await paired(devices, clock, name="desktop")
    assert await devices.check("") is None and await devices.check(token + "x") is None and await devices.check("pnl_" + token) is None

    issued = (await devices.get(device_id)).token_issued_at  # type: ignore[union-attr]
    assert issued == (await devices.get(device_id)).paired_at  # type: ignore[union-attr]
    clock.at += timedelta(days=3)
    rotated = await devices.rotate(device_id)
    assert await devices.check(token) is None and (await devices.check(rotated)).id == device_id  # type: ignore[union-attr]
    assert (await devices.get(device_id)).token_issued_at == clock.at.isoformat() != issued  # type: ignore[union-attr]
    assert (await devices.get(device_id)).view()["token_issued_at"] == clock.at.isoformat()  # type: ignore[union-attr]
    assert await devices.check(other) is not None  # used now: its idle period starts over from here

    seen: list[str] = []

    async def hook(dev: str) -> None:
        seen.append(dev)

    devices.revoked_hooks.append(hook)
    assert await devices.revoke(device_id) is True and seen == [device_id]
    assert await devices.revoke(device_id) is False
    assert await devices.check(rotated) is None
    assert ("device.changed", {"device_id": device_id, "change": "revoked"}) in published.events
    assert [d.id for d in await devices.paired()] == [other_id]
    assert {d.id for d in await devices.paired(everything=True)} == {device_id, other_id}

    # Used, it lives on; unused for the idle period, it lapses.
    clock.at += DEVICE_IDLE - timedelta(days=1)
    assert await devices.check(other) is not None
    clock.at += DEVICE_IDLE - timedelta(seconds=1)
    assert await devices.check(other) is not None
    clock.at += DEVICE_IDLE + timedelta(seconds=1)
    assert await devices.check(other) is None


async def test_a_device_is_renamed_and_its_last_use_written_at_most_once_a_minute(db: Database) -> None:
    clock = Clock()
    devices = Devices(db, now=clock)
    device_id, token = await paired(devices, clock)
    assert (await devices.rename(device_id, "  Work   box ")).name == "Work box"
    first = (await devices.get(device_id)).last_seen_at  # type: ignore[union-attr]
    clock.at += timedelta(seconds=30)
    await devices.check(token, address="10.0.0.5")
    assert (await devices.get(device_id)).last_seen_at == first  # type: ignore[union-attr]
    clock.at += timedelta(seconds=31)
    await devices.check(token)
    assert (await devices.get(device_id)).last_seen_at == clock.at.isoformat()  # type: ignore[union-attr]
    await devices.check(token, address="10.0.0.7")
    assert (await devices.get(device_id)).last_address == "10.0.0.7"  # type: ignore[union-attr]


# -- scope --------------------------------------------------------------------------------------------


async def test_a_devices_scope_is_what_it_started_what_it_resumed_and_their_subagents(db: Database) -> None:
    clock = Clock()
    devices = Devices(db, now=clock)
    laptop, _ = await paired(devices, clock)
    desktop, _ = await paired(devices, clock, name="desktop")
    home = await session(db, "home", kind="remote")
    chat = await session(db, "chat")
    child = await session(db, "child", metadata={"subagent_of": "home"})
    grandchild = await session(db, "grandchild", metadata={"subagent_of": "child"})
    await devices.bind_home(laptop, home, "/home/you/project")

    assert await devices.in_scope(laptop, home) and await devices.in_scope(laptop, child) and await devices.in_scope(laptop, grandchild)
    assert not await devices.in_scope(laptop, chat) and not await devices.in_scope(desktop, home)
    assert not await devices.in_scope(laptop, "missing")

    await devices.attach(laptop, chat)
    await devices.attach(laptop, home)  # already in scope: stays home
    assert (await devices.binding(laptop, home)).role == "home"  # type: ignore[union-attr]
    assert await devices.in_scope(laptop, chat)
    with pytest.raises(Conflict):
        await devices.detach(laptop, home)
    assert await devices.detach(laptop, chat) is True and not await devices.in_scope(laptop, chat)
    assert await devices.detach(laptop, chat) is False

    # The operator continues on the desktop: its hands move, the laptop keeps it to show.
    await devices.move_home(desktop, home, "/srv/you/project")
    assert (await devices.home(home)).device_id == desktop  # type: ignore[union-attr]
    assert (await devices.binding(laptop, home)).role == "resumed"  # type: ignore[union-attr]
    assert (await devices.homes([home, chat])).keys() == {home}
    # Revoking the laptop drops what it resumed; a device's home sessions keep naming it.
    await devices.revoke(laptop)
    assert await devices.binding(laptop, home) is None
    await devices.revoke(desktop)
    assert (await devices.home(home)).device_id == desktop  # type: ignore[union-attr]


async def test_the_live_mark_follows_what_connected_terminals_show() -> None:
    class Marks:
        def __init__(self) -> None:
            self.held: dict[str, set[str]] = {}

        async def update(self, control: str, sessions: Any) -> None:
            self.held[control] = set(sessions)

    now = [100.0]
    dropped: list[str] = []
    announced: list[tuple[str, dict[str, Any], str]] = []

    async def drop(client: str) -> None:
        dropped.append(client)

    async def publish(name: str, payload: dict[str, Any], sid: str | None) -> None:
        announced.append((name, payload, sid))  # type: ignore[arg-type]

    marks = Marks()
    wall = Clock()
    control = RemoteControl(marks, publish=publish, drop_presence=drop, clock=lambda: now[0], now=wall)
    await control.reported("laptop", "c1", ["a"])
    await control.linked("desktop", ["b"], client="p1")
    await control.linked("desktop", ["b"], client="p1")  # a second hello is no new connection
    assert [e for e in announced if e[0] == "device.connection"] == [("device.connection", {"device_id": "desktop", "connected": True, "disconnected_at": None}, None)]
    assert marks.held["remote"] == {"a", "b"} and control.connected("desktop") and not control.connected("laptop")
    assert control.disconnected_at("desktop") is None and control.disconnected_at("laptop") is None
    now[0] += SHOWN_FOR + 1
    await control.refresh()
    assert marks.held["remote"] == {"b"}, "a client that went quiet loses its mark"
    await control.unlinked("desktop", client="p1")
    assert marks.held["remote"] == set()
    # Every window can tell a drop a moment ago from a device gone for long: the moment is kept.
    assert control.disconnected_at("desktop") == wall.at.isoformat()
    assert announced[-1] == ("device.connection", {"device_id": "desktop", "connected": False, "disconnected_at": wall.at.isoformat()}, None)
    await control.linked("desktop", [], client="p1")
    assert control.disconnected_at("desktop") is None
    await control.unlinked("desktop", client="p1")

    # Two processes on one device hold a connection each; a session goes to the one that carries it,
    # the newest when both do, and neither going down takes the other's sessions.
    await control.linked("laptop", ["a"], client="p1")
    await control.linked("laptop", ["b"], client="p2")
    assert control.links("laptop") == ["p1", "p2"] and control.carrier("laptop", "a") == "p1" and control.carrier("laptop", "b") == "p2"
    await control.linked("laptop", ["a", "b"], client="p1")
    assert control.carrier("laptop", "b") == "p1"
    wall.at += timedelta(minutes=1)
    await control.linked("laptop", ["b"], client="p1")
    assert control.carrier("laptop", "a") is None and control.session_disconnected_at("laptop", "a") == wall.at.isoformat()
    assert control.session_disconnected_at("laptop", "b") is None and control.disconnected_at("laptop") is None
    await control.unlinked("laptop", client="p1")
    assert control.carrier("laptop", "b") == "p2" and control.connected("laptop")
    for index in range(3, 10):
        await control.linked("laptop", [], client=f"p{index}")
    assert not control.may_link("laptop", "p10") and control.may_link("laptop", "p2")
    for client in control.links("laptop"):
        await control.unlinked("laptop", client=client)

    assert await control.set_guard("laptop", "a", "plan") is True and await control.set_guard("laptop", "a", "plan") is False
    assert control.guard_of("laptop", "a") == "plan" and control.guard_of("desktop", "a") is None
    assert announced[-1] == ("remote.guard", {"device_id": "laptop", "guard": "plan"}, "a")
    with pytest.raises(ValueError):
        await control.set_guard("laptop", "a", "yolo")

    await control.set_waiting("a", "laptop", [{"tool_call_id": "t1", "tool": "Exec"}], since="2026-11-02T12:00:00+00:00")
    assert control.waiting_of("a") == {"since": "2026-11-02T12:00:00+00:00", "calls": [{"tool_call_id": "t1", "tool": "Exec"}]}
    await control.set_waiting("a", "laptop", None)
    assert control.waiting_of("a") is None and announced[-1] == ("remote.waiting", {"waiting": False, "device_id": "laptop"}, "a")

    await control.reported("laptop", "c2", ["a"])
    await control.forget_device("laptop")
    assert dropped == ["c2"] and marks.held["remote"] == set() and control.guard_of("laptop", "a") is None


# -- the migrations -----------------------------------------------------------------------------------


async def test_the_kind_column_takes_remote_and_keeps_every_row_and_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = next(i for i, m in enumerate(MIGRATIONS) if getattr(m, "step", None) is _remote_kind)
    path = tmp_path / "old.sqlite"
    monkeypatch.setattr(database, "MIGRATIONS", MIGRATIONS[:before])
    db = Database(path, workspaces_dir=tmp_path / "ws")
    await db.open()
    await session(db, "c1", kind="chrome")
    await session(db, "c2", kind="computer")
    await session(db, "c3")
    await db.execute("INSERT INTO transcript(session_id, key, message) VALUES ('c1', 'user:2026-11-02T12:00:00+00:00', '{}')")
    await db.close()
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE sessions SET kind = 'remote' WHERE id = 'c3'")
    raw.close()
    monkeypatch.setattr(database, "MIGRATIONS", MIGRATIONS)

    for _ in range(2):  # the second open finds nothing left to run, and a rerun of the step changes nothing
        db = Database(path, workspaces_dir=tmp_path / "ws")
        await db.open()
        assert {r["id"]: r["kind"] for r in await db.fetchall("SELECT id, kind FROM sessions")} == {"c1": "chrome", "c2": "computer", "c3": "chat"}
        assert (await db.fetchone("SELECT count(*) AS n FROM transcript WHERE session_id = 'c1'"))["n"] == 1
        await db.execute("UPDATE sessions SET kind = 'remote' WHERE id = 'c3'")
        await db.execute("UPDATE sessions SET kind = 'chat' WHERE id = 'c3'")
        assert (await db.fetchone("SELECT max(version) AS v FROM schema_version"))["v"] == len(MIGRATIONS)
        await db.close()
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
    assert "kind_widened" not in sql and "'remote'" in sql
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE sessions SET kind = 'robot' WHERE id = 'c3'")
    # The step again on a widened table leaves it alone.
    raw.close()
    db = Database(path, workspaces_dir=tmp_path / "ws")
    await db.open()
    async with db.transaction() as conn:
        await _remote_kind(conn)
    assert (await db.fetchone("SELECT sql FROM sqlite_master WHERE name = 'sessions'"))["sql"] == sql
    await db.close()


async def test_a_session_deleted_takes_its_bindings_and_a_device_cannot_have_two_homes_for_one_session(db: Database) -> None:
    clock = Clock()
    devices = Devices(db, now=clock)
    laptop, _ = await paired(devices, clock)
    desktop, _ = await paired(devices, clock, name="desktop")
    sid = await session(db, "s", kind="remote")
    await devices.bind_home(laptop, sid, "/x")
    with pytest.raises(sqlite3.IntegrityError):
        await devices.bind_home(desktop, sid, "/y")
    await db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    assert await devices.scope(laptop) == []
