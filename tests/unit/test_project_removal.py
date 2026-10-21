"""Removing a project: only the operator's own chats and work in progress keep it, everything that is
the project's own machinery goes with it, and not one file is touched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.project_removal import RemovalRefused, remove_project
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from tests.unit.test_orchestrator import rig
from tests.unit.test_projects import HEADERS, _client
from tests.unit.test_session_purge import _refusal, _triggers
from tests.unit.test_staff_runtime import board_task, close_team


async def test_the_listing_and_the_refusal_count_only_the_operators_chats(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    root = tmp_path / "firm"
    root.mkdir()
    (root / "charter.txt").write_text("kept", encoding="utf-8")
    try:
        async with await _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Firm", "folders": [{"path": str(root)}]})).json()
            retired = await manager.create_session("Orchestrator · Firm", project_id=project["id"], metadata={"orchestrator_retired_of": project["id"]})
            for n in range(3):
                await manager.create_session(f"clerk · task {n}", project_id=project["id"],
                                             metadata={"staff_id": "st-clerk", "staff_session_id": f"ss-{n}", "archived": True})
            await _refusal(manager, retired.session.id, "run-orchestrator")
            chat = await manager.create_session("Notes", project_id=project["id"])

            listed = next(p for p in (await client.get("/api/projects", headers=HEADERS)).json() if p["id"] == project["id"])
            assert listed["sessions"] == [{"id": chat.session.id, "title": "Notes", "running": False}]
            assert listed["machinery"]["orchestrator"] is True

            refused = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS)
            assert refused.status_code == 409
            assert refused.json()["detail"].startswith("1 chat works in Firm: “Notes”")
            assert "clerk" not in refused.json()["detail"] and "Orchestrator" not in refused.json()["detail"]

            await manager.delete_session(chat.session.id)
            removed = await client.delete(f"/api/projects/{project['id']}", headers=HEADERS)
            assert removed.status_code == 200, removed.text
            assert await db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project["id"],)) is None
            assert await db.fetchone("SELECT 1 FROM sessions WHERE project_id = ?", (project["id"],)) is None
            assert (root / "charter.txt").read_text(encoding="utf-8") == "kept"
    finally:
        await manager.close()


async def test_an_archived_chat_of_the_operators_is_named_apart(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        (tmp_path / "shelf").mkdir()
        project = await manager.projects.create("Shelf", [FolderSpec(str(tmp_path / "shelf"))])
        await manager.create_session("Old plan", project_id=project.id, metadata={"archived": True})
        app: Any = type("App", (), {"manager": manager, "extensions": {}})()
        with pytest.raises(RemovalRefused, match="1 archived chat of yours is in Shelf: “Old plan”") as refused:
            await remove_project(app, project.id)
        assert "chat works" not in str(refused.value), "the sidebar shows none, so none is counted as shown"
    finally:
        await manager.close()


async def test_an_orchestrated_project_goes_with_its_orchestrator_team_and_board(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Nothing to do yet."}])
    try:
        project = await r.orch.enable(r.project.id)
        office = project.settings.orchestrator.session_id
        clerk = await r.manager.staff.hire(project.id, name="clerk")
        scribe = await r.manager.staff.hire(project.id, name="scribe")
        for n in range(2):
            await r.manager.create_session(f"clerk · task {n}", project_id=project.id,
                                           metadata={"staff_id": clerk.id, "staff_session_id": f"ss-old-{n}", "archived": True})
        await board_task(r.manager, project, "Draft the charter")
        await _refusal(r.manager, office, "run-office")
        (tmp_path / "neighbour").mkdir()
        other = await r.manager.projects.create("Neighbour", [FolderSpec(str(tmp_path / "neighbour"))])
        neighbour = await r.manager.create_session("Neighbour chat", project_id=other.id)
        files = sorted(p.relative_to(r.repo) for p in r.repo.rglob("*"))
        chat = await r.manager.create_session("Operator's question", project_id=project.id)
        app = r.team.app

        with pytest.raises(RemovalRefused) as refused:
            await remove_project(app, project.id)
        assert str(refused.value) == f"1 chat works in {project.name}: “Operator's question”; move it to another project or delete it first"

        # A member mid-task is named and keeps the project.
        await r.manager.delete_session(chat.session.id)
        live = await r.manager.staff.claim_session(scribe.id, kind="daedalus")
        with pytest.raises(RemovalRefused, match="“scribe” is at work in"):
            await remove_project(app, project.id)

        # One at rest is released through the team, and then everything goes.
        await r.manager.staff.set_status(live.id, "idle")
        guards = await _triggers(db)
        await remove_project(app, project.id)
        assert await r.manager.projects.get(project.id) is None
        assert (await r.manager.staff.session(live.id)) is None
        for table in ("sessions", "staff", "board_tasks", "project_folders", "project_journal"):
            assert await db.fetchone(f"SELECT 1 FROM {table} WHERE project_id = ?", (project.id,)) is None, table
        assert await _triggers(db) == guards
        assert project.id not in r.orch.queues
        # The other project and its chat are untouched, and so is every file of the folder.
        assert await r.manager.projects.get(other.id) is not None
        assert await db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (neighbour.session.id,)) is not None
        assert sorted(p.relative_to(r.repo) for p in r.repo.rglob("*")) == files
    finally:
        await close_team(r.manager)
        await r.manager.close()
