"""Board commands bind card writes to authority, collection CAS and immutable contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.orchestrator_domain import DomainConflict
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
        commands = BoardCommands(db)
        principal = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", "project1")
        request = {"client_operation_id": "create-one", "expected_collection_revision": 1,
                   "title": "Check the menu", "acceptance": "A test order succeeds",
                   "checklist": ["Place a test order"]}
        created = await commands.create(principal, scope, **request)
        assert created["contract_revision"] == 1
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
        with pytest.raises(DomainConflict):
            await commands.update(principal, scope, task_id, client_operation_id="finish-early",
                                  expected_entity_revision=rearranged["entity_revision"], status="done")
        assert (await db.fetchone("SELECT count(*) AS n FROM task_contract_versions WHERE task_id = ?",
                                  (task_id,)))["n"] == 3
    finally:
        await db.close()
