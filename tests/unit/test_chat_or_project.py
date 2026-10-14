"""One chat is a chat, two are a project.

A new chat makes a scratch project of its own, marked ephemeral, and the app lists that project as
a chat. A second top-level session started in it, or moved into it, makes it a project, and nothing
automatic ever makes it a chat again. Forks and subagents are drawn inside the chat they came from
and change nothing. A folder a chat is started in (Telegram, a schedule) is adopted the same way,
and deleting its only chat removes the record but never the folder. The migration marks the
projects an installation already has by the same rule, once.

The operator can do both by hand: "Make a project" turns a chat into a project under a name, in
place, and "Turn back into a chat" turns a project with one chat back into that chat — refused,
with the reason, whenever the chat's later removal would lose or trip over the project's work.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import MIGRATIONS, Database, _chat_or_project
from daedalus.stores.projects import ProjectSettings, ProjectStore

HEADERS = {"X-Daedalus-Token": "tok"}


async def _ephemeral(db: Database, project_id: str) -> bool:
    row = await db.fetchone("SELECT json_extract(settings, '$.ephemeral') AS ephemeral FROM projects WHERE id = ?", (project_id,))
    assert row is not None, f"project {project_id} is gone"
    return row["ephemeral"] in (1, True)


async def _started(settings: Settings, config: RuntimeConfig, db: Database) -> SessionManager:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    return manager


# -- the second top-level session ----------------------------------------------------------


async def test_a_second_chat_in_a_scratch_project_makes_it_a_project(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = await _started(settings, config, db)
    try:
        first = await manager.create_session("first")
        assert first.project is not None and first.project.settings.ephemeral
        pid = first.project.id
        second = await manager.create_session("second", project_id=pid)
        assert not await _ephemeral(db, pid)
        # Both loaded sessions hold the fresh value: the browser and the secrets read it from there.
        assert second.project is not None and not second.project.settings.ephemeral
        assert manager.live_state(first.session.id).project.settings.ephemeral is False  # type: ignore[union-attr]
    finally:
        await manager.close()


async def test_a_chat_moved_into_a_scratch_project_makes_it_a_project(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = await _started(settings, config, db)
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
    try:
        host = await manager.create_session("host")
        guest = await manager.create_session("guest")
        pid = host.project.id  # type: ignore[union-attr]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            moved = await client.post(f"/api/sessions/{guest.session.id}/project", headers=HEADERS, json={"project_id": pid})
            assert moved.status_code == 200
            assert not await _ephemeral(db, pid)
            assert manager.live_state(guest.session.id).project.settings.ephemeral is False  # type: ignore[union-attr]
            assert manager.live_state(host.session.id).project.settings.ephemeral is False  # type: ignore[union-attr]
            listed = {p["id"]: p for p in (await client.get("/api/projects", headers=HEADERS)).json()}
            assert listed[pid]["settings"]["ephemeral"] is False
    finally:
        await manager.close()


async def test_a_fork_and_a_subagent_leave_a_chat_a_chat(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = await _started(settings, config, db)
    try:
        chat = await manager.create_session("chat")
        pid = chat.project.id  # type: ignore[union-attr]
        # The metadata the fork endpoint and the subagent tool create their sessions with.
        await manager.create_session("chat (fork @3)", metadata={"forked_from": {"session_id": chat.session.id, "seq": 3}}, project_id=pid)
        await manager.create_session("helper", metadata={"subagent_of": chat.session.id, "subagent_name": "helper"}, project_id=pid)
        assert await _ephemeral(db, pid)
        assert (await manager.projects.summary())[pid]["total"] == 1
    finally:
        await manager.close()


async def test_a_project_made_by_a_second_chat_stays_one_with_no_chat_left(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = await _started(settings, config, db)
    try:
        first = await manager.create_session("first")
        pid = first.project.id  # type: ignore[union-attr]
        second = await manager.create_session("second", project_id=pid)
        await manager.delete_session(second.session.id)
        assert not await _ephemeral(db, pid), "down to one chat it is still a project"
        # The chat whose id the project took goes last; nothing marks the project as a chat again.
        await manager.delete_session(first.session.id)
        kept = await manager.projects.get(pid)
        assert kept is not None and not kept.settings.ephemeral and kept.primary.path.exists()
    finally:
        await manager.close()


async def test_settling_leaves_a_system_project_and_an_operator_project_alone(db: Database, tmp_path: Path) -> None:
    store = ProjectStore(db, managed_root=tmp_path / "workspaces", home=tmp_path)
    operator = await store.create("Mine", [str(tmp_path / "mine")])
    voice = await store.ensure_system("voice", name="Voice", root=tmp_path / "workspaces" / "voice")
    for project in (operator, voice):
        for n in range(2):
            await db.execute(
                "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', '', '2026-01-01', '2026-01-01', '{}', ?)",
                (f"{project.id}-{n}", project.id),
            )
        assert await store.settle(project.id) is False
    scratch = await store.create("A chat", settings=ProjectSettings(snapshots=True, ephemeral=True))
    assert await store.settle(scratch.id) is False, "an empty scratch project is still a chat"


# -- a folder a chat was started in -----------------------------------------------------------


async def test_an_adopted_folder_is_a_chat_until_a_second_chat_uses_it(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "telegram-work"
    folder.mkdir()
    (folder / "notes.md").write_text("mine", encoding="utf-8")
    manager = await _started(settings, config, db)
    try:
        adopted = await manager.projects.adopt_directory("work", folder)
        assert adopted.settings.ephemeral and adopted.settings.snapshots
        first = await manager.create_session("first", workspace=folder)
        assert first.project is not None and first.project.id == adopted.id
        assert await _ephemeral(db, adopted.id)
        again = await manager.projects.adopt_directory("work again", folder)
        assert again.id == adopted.id
        await manager.create_session("second", workspace=folder)
        assert not await _ephemeral(db, adopted.id)
    finally:
        await manager.close()


async def test_deleting_the_only_chat_of_an_adopted_folder_keeps_the_folder(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "operator-folder"
    folder.mkdir()
    (folder / "notes.md").write_text("mine", encoding="utf-8")
    manager = await _started(settings, config, db)
    try:
        chat = await manager.create_session("chat", workspace=folder)
        pid = chat.project.id  # type: ignore[union-attr]
        assert pid != chat.session.id and not chat.project.primary.managed  # type: ignore[union-attr]
        await manager.delete_session(chat.session.id, delete_workspace=True)
        assert await manager.projects.get(pid) is None
        assert not await db.fetchall("SELECT id FROM project_folders WHERE project_id = ?", (pid,))
        assert (folder / "notes.md").read_text(encoding="utf-8") == "mine"
        # Nothing that sweeps the workspaces looks outside the installation's own tree.
        assert folder not in await manager.orphan_workspaces()
    finally:
        await manager.close()


# -- the migration --------------------------------------------------------------------------


def _session(sid: str, project_id: str, metadata: dict[str, Any] | None = None) -> tuple[Any, ...]:
    return (sid, project_id, json.dumps(metadata or {}))


async def _seed(db: Database, workspaces: Path) -> None:
    """Projects as an installation from before the rule has them."""
    projects = [
        # (a) ephemeral, two top-level chats: a project now.
        ("shared", {"ephemeral": True, "snapshots": True}, "", [workspaces / "shared"]),
        # ephemeral with one chat, a fork and a subagent: still a chat.
        ("forked", {"ephemeral": True}, "", [workspaces / "forked"]),
        # (b) an old chat's own managed scratch, never marked: a chat.
        ("lone", {"ephemeral": False}, "", [workspaces / "lone"]),
        # (b) the same with no key at all, as the oldest rows are.
        ("bare", {}, "", [workspaces / "bare"]),
        # (b) a host chat's folder: a chat.
        ("hostchat", {"ephemeral": False}, "", [Path("/somewhere/.local/state/daedalus/chats/hostchat")]),
        # the id of its chat but an operator's folder: left alone.
        ("outside", {"ephemeral": False}, "", [Path("/somewhere/code")]),
        # (c) adopted from a folder, random id: left alone.
        ("a1b2c3d4e5f6", {"ephemeral": False}, "", [workspaces / "sched-1"]),
        # the chat's id, but two folders: the operator made it a project.
        ("twofold", {"ephemeral": False}, "", [workspaces / "twofold", Path("/somewhere/docs")]),
        # the chat's id, but an orchestrator: a project.
        ("orchestrated", {"ephemeral": False, "orchestrator": {"enabled": True}}, "", [workspaces / "orchestrated"]),
        # the chat's id and two chats: a project.
        ("busy", {"ephemeral": False}, "", [workspaces / "busy"]),
        # the installation's own: never touched.
        ("voice-project", {"system": "voice"}, "voice", [workspaces / "voice"]),
    ]
    sessions = [
        _session("shared", "shared"), _session("shared-2", "shared"),
        _session("forked", "forked"),
        _session("forked-fork", "forked", {"forked_from": {"session_id": "forked", "seq": 2}}),
        _session("forked-sub", "forked", {"subagent_of": "forked"}),
        _session("lone", "lone"), _session("bare", "bare"), _session("hostchat", "hostchat"),
        _session("outside", "outside"), _session("sched-run", "a1b2c3d4e5f6"),
        _session("twofold", "twofold"), _session("orchestrated", "orchestrated"),
        _session("busy", "busy"), _session("busy-2", "busy"),
        _session("voice", "voice-project"),
    ]
    async with db.transaction() as conn:
        for pid, settings, system, folders in projects:
            await conn.execute("INSERT INTO projects(id, name, created_at, settings, system) VALUES (?, ?, '2026-01-01', ?, ?)", (pid, pid, json.dumps(settings), system))
            for position, path in enumerate(folders):
                await conn.execute(
                    "INSERT INTO project_folders(id, project_id, path, env, position, created_at) VALUES (?, ?, ?, 'container', ?, '2026-01-01')",
                    (f"f-{pid}-{position}", pid, str(path), position),
                )
        for sid, pid, metadata in sessions:
            await conn.execute(
                "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', '', '2026-01-01', '2026-01-01', ?, ?)",
                (sid, metadata, pid),
            )
        await conn.execute("UPDATE schema_version SET version = ?", (BEFORE_THE_RULE,))


BEFORE_THE_RULE = MIGRATIONS.index(_chat_or_project)
"""The schema just before the migration under test: found by name, since later migrations follow it."""


async def _flags(db: Database) -> dict[str, Any]:
    rows = await db.fetchall("SELECT id, settings FROM projects ORDER BY id")
    return {r["id"]: json.loads(r["settings"]).get("ephemeral") for r in rows}


async def test_the_migration_marks_chats_and_projects_once(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    path = tmp_path / "old.sqlite"
    db = Database(path, workspaces_dir=workspaces)
    await db.open()
    await _seed(db, workspaces)
    await db.close()

    db = Database(path, workspaces_dir=workspaces)
    await db.open()
    try:
        flags = await _flags(db)
        assert flags["shared"] is False, "two top-level chats make a project"
        assert flags["forked"] is True, "a fork and a subagent are not a second chat"
        assert flags["lone"] is True and flags["bare"] is True and flags["hostchat"] is True
        assert flags["outside"] is False, "a folder outside the installation's own is the operator's"
        assert flags["a1b2c3d4e5f6"] is False, "an adopted folder from before the rule is left as it is"
        assert flags["twofold"] is False and flags["orchestrated"] is False and flags["busy"] is False
        assert flags["voice-project"] is None
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        assert not await db.fetchall("PRAGMA foreign_key_check")
        before = flags
        await db.execute("UPDATE schema_version SET version = ?", (BEFORE_THE_RULE,))
    finally:
        await db.close()

    # Run a second time over its own result, it changes nothing.
    db = Database(path, workspaces_dir=workspaces)
    await db.open()
    try:
        assert await _flags(db) == before
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
    finally:
        await db.close()


# -- by hand: a chat made a project, and a project turned back into its chat ----------------


@asynccontextmanager
async def _api(settings: Settings, config: RuntimeConfig, db: Database) -> AsyncIterator[tuple[SessionManager, httpx.AsyncClient]]:
    manager = await _started(settings, config, db)
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            yield manager, client
    finally:
        await manager.close()


async def _revision(client: httpx.AsyncClient, project_id: str) -> int:
    listed = {p["id"]: p for p in (await client.get("/api/projects", headers=HEADERS)).json()}
    return int(listed[project_id]["entity_revision"])


async def _command(client: httpx.AsyncClient, project_id: str, what: str, body: dict[str, Any] | None = None, *, operation: str = "") -> httpx.Response:
    """POST ``as-project`` or ``as-chat`` the way the app does: against the revision it last read."""
    payload = {**(body or {}), "expected_entity_revision": await _revision(client, project_id),
               "client_operation_id": operation or f"test-{uuid.uuid4().hex}"}
    return await client.post(f"/api/projects/{project_id}/{what}", headers=HEADERS, json=payload)


async def _sidebar(client: httpx.AsyncClient, project_id: str) -> dict[str, Any]:
    """The project as the sidebar's listing carries it, with its count of top-level chats."""
    listing = (await client.get("/api/sessions", headers=HEADERS)).json()
    return next(p for p in listing["projects"] if p["id"] == project_id)


async def test_a_chat_is_made_a_project_in_place_under_its_new_name(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    async with _api(settings, config, db) as (manager, client):
        chat = await manager.create_session("Plan the garden")
        pid = chat.project.id  # type: ignore[union-attr]
        workspace = chat.workspace
        assert (await _sidebar(client, pid))["settings"]["ephemeral"] is True

        made = await _command(client, pid, "as-project", {"name": "  Garden  "})
        assert made.status_code == 200, made.text
        assert made.json()["change"] == "kept"
        listed = await _sidebar(client, pid)
        assert listed["settings"]["ephemeral"] is False and listed["name"] == "Garden" and listed["total"] == 1
        # In place: the same project, the same chat in it, and its files where they were.
        moved = manager.live_state(chat.session.id)
        assert moved is not None and moved.project is not None
        assert moved.project.id == pid and moved.project.settings.ephemeral is False and moved.workspace == workspace
        assert (await client.get(f"/api/sessions/{chat.session.id}", headers=HEADERS)).json()["title"] == "Plan the garden"
        events = await db.fetchall("SELECT payload_json FROM app_events WHERE type = 'project.changed' AND project_id = ?", (pid,))
        assert "kept" in [json.loads(e["payload_json"])["change"] for e in events]


async def test_making_a_project_twice_is_the_same_result(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    async with _api(settings, config, db) as (manager, client):
        chat = await manager.create_session("Notes")
        pid = chat.project.id  # type: ignore[union-attr]
        first = await _command(client, pid, "as-project", {"name": "Notes"}, operation="make-once")
        assert first.status_code == 200
        # The same command sent again (a retry after a dropped answer) is answered from its receipt.
        payload = {"name": "Notes", "expected_entity_revision": first.json()["entity_revision"] - 1, "client_operation_id": "make-once"}
        replay = await client.post(f"/api/projects/{pid}/as-project", headers=HEADERS, json=payload)
        assert replay.status_code == 200 and replay.json()["receipt_id"] == first.json()["receipt_id"]
        # Pressed again from another screen: still a project, renamed, nothing else.
        again = await _command(client, pid, "as-project", {"name": "Notebook"})
        assert again.status_code == 200
        listed = await _sidebar(client, pid)
        assert listed["settings"]["ephemeral"] is False and listed["name"] == "Notebook"
        # A stale revision is a conflict, never a silent overwrite.
        stale = await client.post(f"/api/projects/{pid}/as-project", headers=HEADERS,
                                  json={"name": "Old", "expected_entity_revision": 1, "client_operation_id": "stale"})
        assert stale.status_code == 409


async def test_making_a_project_refuses_the_installations_own_and_an_orchestrated_one(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    async with _api(settings, config, db) as (manager, client):
        voice = await manager.projects.ensure_system("voice", name="Voice", root=tmp_path / "workspaces" / "voice")
        refused = await _command(client, voice.id, "as-project", {"name": "Mine now"})
        assert refused.status_code == 400 and "installation's own" in refused.json()["detail"]
        assert (await manager.projects.get(voice.id)).name == "Voice"  # type: ignore[union-attr]

        made = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(tmp_path / "bakery")}]})).json()
        await manager.projects.update_orchestrator(made["id"], enabled=True)
        refused = await _command(client, made["id"], "as-project", {"name": "Renamed"})
        assert refused.status_code == 400 and "orchestrated" in refused.json()["detail"]
        assert (await manager.projects.get(made["id"])).name == "Bakery"  # type: ignore[union-attr]

        chat = await manager.create_session("x")
        blank = await _command(client, chat.project.id, "as-project", {"name": "   "})  # type: ignore[union-attr]
        assert blank.status_code == 400
        assert (await _command(client, chat.project.id, "as-project", {"name": ""})).status_code == 422  # type: ignore[union-attr]
        assert (await client.post("/api/projects/nope/as-project", headers=HEADERS,
                                  json={"name": "x", "expected_entity_revision": 1, "client_operation_id": "x"})).status_code == 404


async def test_the_settings_write_no_longer_keeps_a_chat(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    async with _api(settings, config, db) as (manager, client):
        chat = await manager.create_session("x")
        pid = chat.project.id  # type: ignore[union-attr]
        payload = {"keep": True, "expected_entity_revision": await _revision(client, pid), "client_operation_id": "keep"}
        assert (await client.patch(f"/api/projects/{pid}", headers=HEADERS, json=payload)).status_code == 422
        assert await _ephemeral(db, pid)


async def test_a_project_with_one_chat_turns_back_into_it(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    async with _api(settings, config, db) as (manager, client):
        chat = await manager.create_session("Trip")
        pid = chat.project.id  # type: ignore[union-attr]
        # A fork and a helper hang inside the chat; they do not make it two.
        await manager.create_session("Trip (fork @3)", metadata={"forked_from": {"session_id": chat.session.id, "seq": 3}}, project_id=pid)
        await manager.create_session("helper", metadata={"subagent_of": chat.session.id, "subagent_name": "helper"}, project_id=pid)
        assert (await _command(client, pid, "as-project", {"name": "Trip to Porto"})).status_code == 200

        back = await _command(client, pid, "as-chat")
        assert back.status_code == 200, back.text
        assert back.json()["change"] == "chat"
        assert (await _sidebar(client, pid))["settings"]["ephemeral"] is True
        assert manager.live_state(chat.session.id).project.settings.ephemeral is True  # type: ignore[union-attr]
        # Already a chat: nothing to do, and nothing refused.
        assert (await _command(client, pid, "as-chat")).status_code == 200

        # A chat again, so a second chat moved in makes it a project by the usual rule.
        guest = await manager.create_session("guest")
        moved = await client.post(f"/api/sessions/{guest.session.id}/project", headers=HEADERS, json={"project_id": pid})
        assert moved.status_code == 200
        assert not await _ephemeral(db, pid)
        assert (await _sidebar(client, pid))["total"] == 2


async def test_a_chat_again_goes_with_its_chat_and_leaves_the_folder(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "operator-folder"
    folder.mkdir()
    (folder / "notes.md").write_text("mine", encoding="utf-8")
    async with _api(settings, config, db) as (manager, client):
        # A project the operator made on purpose, around a folder of their own, with one chat.
        made = (await client.post("/api/projects", headers=HEADERS, json={"name": "Mine", "folders": [{"path": str(folder)}]})).json()
        chat = await manager.create_session("only", project_id=made["id"])
        assert (await _command(client, made["id"], "as-chat")).status_code == 200
        await manager.delete_session(chat.session.id, delete_workspace=True)
        assert await manager.projects.get(made["id"]) is None
        assert (folder / "notes.md").read_text(encoding="utf-8") == "mine"


async def test_turning_back_into_a_chat_is_refused_with_the_reason(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    async with _api(settings, config, db) as (manager, client):
        empty = (await client.post("/api/projects", headers=HEADERS, json={"name": "Empty"})).json()
        refused = await _command(client, empty["id"], "as-chat")
        assert refused.status_code == 409 and "exactly one chat" in refused.json()["detail"] and "has 0" in refused.json()["detail"]

        two = (await client.post("/api/projects", headers=HEADERS, json={"name": "Two"})).json()
        await manager.create_session("one", project_id=two["id"])
        await manager.create_session("two", project_id=two["id"])
        refused = await _command(client, two["id"], "as-chat")
        assert refused.status_code == 409 and "has 2" in refused.json()["detail"]
        assert not await _ephemeral(db, two["id"])

        voice = await manager.projects.ensure_system("voice", name="Voice", root=tmp_path / "workspaces" / "voice")
        await manager.create_session("talk", project_id=voice.id)
        refused = await _command(client, voice.id, "as-chat")
        assert refused.status_code == 409 and "installation's own" in refused.json()["detail"]

        orchestrated = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery"})).json()
        await manager.create_session("one", project_id=orchestrated["id"])
        await manager.projects.update_orchestrator(orchestrated["id"], enabled=True)
        refused = await _command(client, orchestrated["id"], "as-chat")
        assert refused.status_code == 409 and "orchestrat" in refused.json()["detail"]

        archived = (await client.post("/api/projects", headers=HEADERS, json={"name": "Old"})).json()
        await manager.create_session("one", project_id=archived["id"])
        await db.execute("UPDATE projects SET settings = json_set(settings, '$.archived', json('true')) WHERE id = ?", (archived["id"],))
        refused = await _command(client, archived["id"], "as-chat")
        assert refused.status_code == 409 and "archived" in refused.json()["detail"]


async def test_turning_back_into_a_chat_is_refused_while_the_project_holds_work(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    async with _api(settings, config, db) as (manager, client):
        # A schedule's key does not cascade: the chat's removal would fail, and with it the deletion.
        scheduled = (await client.post("/api/projects", headers=HEADERS, json={"name": "Digest"})).json()
        await manager.create_session("one", project_id=scheduled["id"])
        await db.execute("INSERT INTO schedules(id, name, prompt, workspace, created_at, project_id) VALUES ('sch1', 'daily', 'go', '/w', '2026-01-01', ?)", (scheduled["id"],))
        refused = await _command(client, scheduled["id"], "as-chat")
        assert refused.status_code == 409 and "automation history" in refused.json()["detail"]
        assert not await _ephemeral(db, scheduled["id"])

        # A brief would cascade away with the chat, unseen.
        briefed = (await client.post("/api/projects", headers=HEADERS, json={"name": "Shop"})).json()
        await manager.create_session("one", project_id=briefed["id"])
        await manager.projects.set_brief(briefed["id"], "notes", "Sell bread online", "operator")
        refused = await _command(client, briefed["id"], "as-chat")
        assert refused.status_code == 409 and "a brief" in refused.json()["detail"]

        # The journal and the pin are a chat's as much as a project's, and stay.
        plain = (await client.post("/api/projects", headers=HEADERS, json={"name": "Plain"})).json()
        await manager.create_session("one", project_id=plain["id"])
        await manager.projects.pin(plain["id"], True)
        await client.post(f"/api/projects/{plain['id']}/journal", headers=HEADERS, json={"text": "started"})
        assert (await _command(client, plain["id"], "as-chat")).status_code == 200
        assert (await manager.projects.pins()).get(plain["id"])
