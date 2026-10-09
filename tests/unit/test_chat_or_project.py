"""One chat is a chat, two are a project.

A new chat makes a scratch project of its own, marked ephemeral, and the app lists that project as
a chat. A second top-level session started in it, or moved into it, makes it a project for good:
the flag is cleared on the server and never comes back. Forks and subagents are drawn inside the
chat they came from and change nothing. A folder a chat is started in (Telegram, a schedule) is
adopted the same way, and deleting its only chat removes the record but never the folder. The
migration marks the projects an installation already has by the same rule, once.
"""

from __future__ import annotations

import json
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
