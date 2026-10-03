"""Board commands bind card writes to authority, collection CAS and immutable contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.orchestrator_domain import DomainConflict
from daedalus.host.events import EventBus
from daedalus.stores.control import ControlConflict, Principal, Scope
from daedalus.stores.database import Database


@pytest.mark.asyncio
async def test_project_card_creation_and_semantic_edit_are_replayable(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens)"
                         " VALUES ('project1',3,2,100000)")
        await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                         " VALUES ('member1','project1','Member','daedalus','operator','2026-01-01')")
        commands = BoardCommands(db)
        principal = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", "project1")
        request = {"client_operation_id": "create-one", "expected_collection_revision": 1,
                   "title": "Check the menu", "acceptance": "A test order succeeds",
                   "checklist": ["Place a test order"], "assignee_staff_id": "member1"}
        created = await commands.create(principal, scope, **request)
        assert created["contract_revision"] == 1
        assert (await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = ?",
                                  (created["task_id"],)))["assignee_staff_id"] == "member1"
        assert await commands.create(principal, scope, **request) == created
        task_id = created["task_id"]
        assert (await db.fetchone("SELECT id FROM workflow_steps WHERE task_id = ?", (task_id,))) is not None
        with pytest.raises(ControlConflict):
            await commands.create(principal, scope, client_operation_id="create-two",
                                  expected_collection_revision=1, title="Stale card")
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = ?", (task_id,)))["entity_revision"]
        changed = await commands.update(principal, scope, task_id, client_operation_id="update-one",
                                        expected_entity_revision=revision,
                                        acceptance="A test order succeeds and appears in history")
        assert changed["contract_revision"] == 2
        assert await commands.update(principal, scope, task_id, client_operation_id="update-one",
                                     expected_entity_revision=revision,
                                     acceptance="A test order succeeds and appears in history") == changed
        rearranged = await commands.update(principal, scope, task_id, client_operation_id="checks-two",
                                           expected_entity_revision=changed["entity_revision"],
                                           checklist=["Inspect the receipt", "Place a test order"])
        checks = json.loads((await db.fetchone("SELECT checklist FROM board_tasks WHERE id = ?",
                                              (task_id,)))["checklist"])
        assert [(check["id"], check["text"]) for check in checks] == [
            ("C2", "Inspect the receipt"), ("C1", "Place a test order")]
        marked = await commands.update(principal, scope, task_id, client_operation_id="mark-one",
                                       expected_entity_revision=rearranged["entity_revision"], check_ids=["C1"],
                                       assignee_staff_id="")
        checks = json.loads((await db.fetchone("SELECT checklist FROM board_tasks WHERE id = ?",
                                              (task_id,)))["checklist"])
        assert [check["done"] for check in checks] == [False, True]
        assert (await db.fetchone("SELECT assignee_staff_id FROM board_tasks WHERE id = ?",
                                  (task_id,)))["assignee_staff_id"] is None
        with pytest.raises(DomainConflict, match="criterion ID"):
            await commands.update(principal, scope, task_id, client_operation_id="bad-check",
                                  expected_entity_revision=marked["entity_revision"], check_ids=["C99"])
        with pytest.raises(DomainConflict, match="assignee"):
            await commands.update(principal, scope, task_id, client_operation_id="bad-staff",
                                  expected_entity_revision=marked["entity_revision"], assignee_staff_id="absent")
        with pytest.raises(DomainConflict):
            await commands.update(principal, scope, task_id, client_operation_id="finish-early",
                                  expected_entity_revision=marked["entity_revision"], status="done")
        await db.execute("UPDATE board_tasks SET status = 'review' WHERE id = ?", (task_id,))
        review_revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = ?",
                                             (task_id,)))["entity_revision"]
        with pytest.raises(DomainConflict, match="return the exact"):
            await commands.update(principal, scope, task_id, client_operation_id="drop-review",
                                  expected_entity_revision=review_revision, status="dropped")
        assert (await db.fetchone("SELECT count(*) AS n FROM task_contract_versions WHERE task_id = ?",
                                  (task_id,)))["n"] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_card_receipt_and_notifications_commit_once_or_rollback_together(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    bus = EventBus(db)
    await bus.start()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens)"
                         " VALUES ('project1',3,20,100000)")
        commands = BoardCommands(db, bus=bus)
        principal = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", "project1")
        request = {"client_operation_id": "create-once", "expected_collection_revision": 1,
                   "title": "Inspect the delivery"}
        async with bus.subscribe() as subscription:
            created = await commands.create(principal, scope, **request)
            event = await anext(subscription)
            assert event.type == "task.created" and event.payload["task_id"] == created["task_id"]
            assert await commands.create(principal, scope, **request) == created
            assert len(await db.fetchall("SELECT seq FROM app_events")) == 1
            head = bus.head
            await db.execute("CREATE TRIGGER reject_card_event BEFORE INSERT ON app_events"
                             " WHEN NEW.type = 'task.changed' BEGIN SELECT RAISE(ABORT,'reject notification'); END")
            update = {"client_operation_id": "edit-once", "expected_entity_revision": 1,
                      "title": "Inspect the revised delivery"}
            with pytest.raises(sqlite3.IntegrityError, match="reject notification"):
                await commands.update(principal, scope, created["task_id"], **update)
            row = await db.fetchone("SELECT title,entity_revision FROM board_tasks WHERE id = ?", (created["task_id"],))
            assert (row["title"], row["entity_revision"]) == (request["title"], 1)
            assert len(await db.fetchall("SELECT id FROM operation_receipts")) == 1
            assert bus.head == head
            await db.execute("DROP TRIGGER reject_card_event")
            changed = await commands.update(principal, scope, created["task_id"], **update)
            assert (await anext(subscription)).type == "task.changed"
            assert await commands.update(principal, scope, created["task_id"], **update) == changed
            assert len(await db.fetchall("SELECT seq FROM app_events")) == 2
    finally:
        await bus.close()
        await db.close()


@pytest.mark.asyncio
async def test_handoff_requirement_and_folder_commit_with_card_receipt(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens)"
                         " VALUES ('project1',3,3,100000)")
        await db.execute("INSERT INTO project_folders(id,project_id,path,env,created_at)"
                         " VALUES ('folder1','project1','/tmp/project-folder','container','2026-01-01')")
        commands = BoardCommands(db)
        principal = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", "project1")
        args = {"client_operation_id": "handoff-one", "expected_collection_revision": 1,
                "title": "Inspect the result", "folder_id": "folder1",
                "requirements": [{"text": "Check the invoice total", "kind": "quality", "source": "operator"}]}
        created = await commands.create(principal, scope, **args)
        assert created["contract_revision"] == 1
        assert await commands.create(principal, scope, **args) == created
        row = await db.fetchone("SELECT folder_id,contract_revision FROM board_tasks WHERE id = ?",
                                (created["task_id"],))
        assert (row["folder_id"], row["contract_revision"]) == ("folder1", 1)
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM task_requirements WHERE task_id = ?",
                                  (created["task_id"],)))["n"] == 1
        with pytest.raises(DomainConflict, match="folder"):
            await commands.create(principal, scope, client_operation_id="bad-folder",
                                  expected_collection_revision=2, title="Unsafe folder",
                                  folder_id="other-folder")
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM board_tasks WHERE project_id = 'project1'"))["n"] == 1
    finally:
        await db.close()
