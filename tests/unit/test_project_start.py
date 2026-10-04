"""The first project command commits its folder, goal and scoped task exactly once."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_project_start import register
from daedalus.extensions.project_start import ProjectStart
from daedalus.host.events import EventBus
from daedalus.stores.control import ControlConflict, Principal
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectStore


@pytest.mark.asyncio
async def test_guided_start_replays_after_lost_reply_and_pins_contract(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    bus = EventBus(db)
    await bus.start()
    projects = ProjectStore(db, managed_root=tmp_path / "managed", local_env=db.local_env)
    request = {"client_operation_id": "first-project", "expected_collection_revision": 1,
               "name": "Bakery", "goal": "Publish a clear menu", "constraints": "Do not change prices",
               "task_title": "Prepare menu", "checks": ["Menu has all items", "Review the wording"],
               "owner_intent": "manual"}
    principal = Principal.operator({"via": "token", "user_id": 1})
    try:
        start = ProjectStart(db, projects, bus)
        first = await start.create(principal, **request)
        assert first["receipt_id"] and first["entity_revision"] == 2
        assert await start.create(principal, **request) == first
        project = await projects.get(first["project_id"])
        assert project and project.primary.path.is_dir()
        assert len(await projects.list()) == 1
        goal = await db.fetchone("SELECT body,checks_json FROM project_goal_revisions WHERE project_id = ?",
                                 (project.id,))
        assert goal["body"] == request["goal"] and json.loads(goal["checks_json"]) == request["checks"]
        task = await db.fetchone("SELECT project_id,assignee_staff_id,checklist,brief_json,status FROM board_tasks WHERE id = ?",
                                 (first["task_id"],))
        assert task and task["project_id"] == project.id and task["assignee_staff_id"] is None
        assert task["status"] == "todo" and len(json.loads(task["checklist"])) == 2
        assert json.loads(task["brief_json"])["boundaries"] == request["constraints"]
        assert json.loads(task["brief_json"])["deliverable"] == request["task_title"]
        contract = await db.fetchone("SELECT snapshot_json FROM task_contract_versions WHERE task_id = ?",
                                     (first["task_id"],))
        assert contract and json.loads(contract["snapshot_json"])["folder_id"] == project.primary.id
        journal = await db.fetchone("SELECT refs_json FROM project_journal WHERE project_id = ? AND kind = 'decision'",
                                    (project.id,))
        assert journal and json.loads(journal["refs_json"])["owner_intent"] == "manual"
        assert await db.fetchone("SELECT id FROM execution_attempts WHERE task_id = ?", (first["task_id"],)) is None
        assert len(await db.fetchall("SELECT id FROM operation_receipts WHERE operation_kind = 'project.guided_start'")) == 1
        with pytest.raises(ControlConflict, match="different request"):
            await start.create(principal, **{**request, "goal": "Different goal"})
        with pytest.raises(ControlConflict, match="entity has changed"):
            await start.create(principal, **{**request, "client_operation_id": "second-project"})
        assert await start.create(principal, **request) == first
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_guided_start_rolls_back_project_when_task_event_fails(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    bus = EventBus(db)
    await bus.start()
    projects = ProjectStore(db, managed_root=tmp_path / "managed", local_env=db.local_env)
    await db.execute("CREATE TRIGGER reject_first_task BEFORE INSERT ON app_events"
                     " WHEN NEW.type = 'task.created' BEGIN SELECT RAISE(ABORT,'event refused'); END")
    try:
        with pytest.raises(Exception, match="event refused"):
            await ProjectStart(db, projects, bus).create(
                Principal.operator({"via": "token", "user_id": 1}), client_operation_id="rollback",
                expected_collection_revision=1, name="Bakery", goal="Publish a clear menu",
                constraints="Keep current prices", task_title="Prepare menu",
                checks=["All items listed"], owner_intent="later")
        assert await db.fetchone("SELECT id FROM projects") is None
        assert await db.fetchone("SELECT id FROM board_tasks") is None
        assert await db.fetchone("SELECT id FROM operation_receipts WHERE client_operation_id = 'rollback'") is None
        assert not (tmp_path / "managed").exists()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_guided_start_rejects_thirteenth_check_without_side_effect(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    bus = EventBus(db)
    await bus.start()
    projects = ProjectStore(db, managed_root=tmp_path / "managed", local_env=db.local_env)
    app = SimpleNamespace(db=db, manager=SimpleNamespace(projects=projects, bus=bus))
    api = FastAPI()
    register(api, app, lambda: {"via": "token", "user_id": 1})  # type: ignore[arg-type]
    request = {"client_operation_id": "too-many", "expected_collection_revision": 1,
               "name": "Bakery", "goal": "Publish a clear menu", "constraints": "Keep old prices",
               "task_title": "Prepare menu", "checks": [f"Check {i}" for i in range(13)],
               "owner_intent": "later"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            response = await client.post("/api/project-start", json=request)
        assert response.status_code == 422
        assert await db.fetchone("SELECT id FROM projects") is None
        assert await db.fetchone("SELECT id FROM operation_receipts WHERE client_operation_id = 'too-many'") is None
        assert not (tmp_path / "managed").exists()
    finally:
        await db.close()
