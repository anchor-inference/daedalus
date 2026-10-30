"""The operator's own chat writes its board; a team's workers and coordinator keep their own authority.

Board writes once needed a grant that nothing ever issued to an ordinary chat, so BoardAdd and
BoardUpdate from the operator's chats were always refused while BoardList worked. These tests go
through the same hook the Board tools call, and pin both sides: the chat now writes without an
approval step, and nobody on a project's team gains a right by it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import board as board_extension
from daedalus.extensions.coordinator_authority import install_authority
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.executions import AttemptIdentity, ExecutionStore
from tests.support.notifications import RecordingNotifications


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None,
                          extensions={}, guard=None)
    app.notifications = RecordingNotifications()
    app.executions = ExecutionStore(db)
    app.executions.acquire()
    await app.executions.boot()
    install_authority(app)
    loops = await board_extension.install(app)
    yield app
    for loop in loops:
        loop.cancel()
    await asyncio.gather(*loops, return_exceptions=True)
    app.executions.release()
    await manager.close()


def hook(app: Any) -> Any:
    return app.manager.service_hooks["board_commands"]


async def standing_grants(app: Any) -> list[Any]:
    return await app.db.fetchall("SELECT * FROM actor_grants WHERE issuer_id = 'operator:standing-board'")


async def project_with_chat(app: Any, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "bakery"
    root.mkdir()
    project = await app.manager.projects.create("Bakery", [str(root)])
    chat = await app.manager.create_session("a chat in the project", project_id=project.id)
    return SimpleNamespace(project=project.id, chat=chat.session.id)


async def test_a_chat_in_a_project_adds_a_task_and_updates_it(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    revision = await hook(app)("revision", session_id=where.chat)
    task = await hook(app)("add", session_id=where.chat, client_operation_id="call-1",
                           expected_collection_revision=revision, title="Write the menu page",
                           acceptance="the page lists every dish", checklist=["draft", "review"], priority=1)
    assert task["project_id"] == where.project and task["origin_session_id"] == where.chat
    assert task["status"] == "todo"

    updated = await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-2",
                              expected_entity_revision=task["entity_revision"], note="started on the draft",
                              check_ids=["C1"], status="blocked")
    assert updated["status"] == "blocked"
    assert [item["done"] for item in updated["checklist"]] == [True, False]
    assert "started on the draft" in updated["notes"]

    # One standing grant, issued on first use for this chat and this project, serves every write after it.
    grants = await standing_grants(app)
    assert len(grants) == 1
    grant = grants[0]
    assert (grant["actor_id"], grant["origin_class"], grant["scope_kind"], grant["scope_id"]) == (
        f"session:{where.chat}", "agent", "project", where.project)
    assert set(json.loads(grant["operations_json"])) == {"board.task.create", "board.task.update"}
    receipts = await app.db.fetchall("SELECT actor_id,grant_id FROM operation_receipts WHERE operation_kind LIKE 'board.task.%'")
    assert {(row["actor_id"], row["grant_id"]) for row in receipts} == {(f"session:{where.chat}", grant["id"])}


async def test_a_chat_reads_actionable_refusals_on_its_board(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    with pytest.raises(ValueError, match="current revision, now 1; BoardList"):
        await hook(app)("add", session_id=where.chat, client_operation_id="call-1",
                        expected_collection_revision=0, title="A task")
    task = await hook(app)("add", session_id=where.chat, client_operation_id="call-2",
                           expected_collection_revision=1, title="A task")
    with pytest.raises(ValueError, match="expected_collection_revision is now 2"):
        await hook(app)("add", session_id=where.chat, client_operation_id="call-3",
                        expected_collection_revision=1, title="Another task")
    with pytest.raises(ValueError, match="operator closes it as done"):
        await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-4",
                        expected_entity_revision=task["entity_revision"], status="done")

    # The project's team cards share the chat's scope but are not on its board.
    other = await app.manager.create_session("another chat", project_id=where.project)
    theirs = await hook(app)("add", session_id=other.session.id, client_operation_id="call-5",
                             expected_collection_revision=2, title="Another chat's plan")
    with pytest.raises(KeyError):
        await hook(app)("update", session_id=where.chat, task_id=theirs["id"], client_operation_id="call-6",
                        expected_entity_revision=theirs["entity_revision"], note="not mine")
    with pytest.raises(ValueError, match="unknown dependency"):
        await hook(app)("add", session_id=where.chat, client_operation_id="call-7",
                        expected_collection_revision=3, title="Depends on theirs", depends_on=[theirs["id"]])


async def test_a_chat_started_without_a_project_writes_its_own_board(app: Any) -> None:
    # A chat started outside every project is given a scratch project of its own; its board is that one.
    chat = (await app.manager.create_session("a chat outside projects")).session.id
    scratch = (await app.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (chat,)))["project_id"]
    assert json.loads((await app.db.fetchone("SELECT settings FROM projects WHERE id = ?", (scratch,)))["settings"])["ephemeral"]
    revision = await hook(app)("revision", session_id=chat)
    task = await hook(app)("add", session_id=chat, client_operation_id="call-1",
                           expected_collection_revision=revision, title="Rotate the keys", checklist=["old", "new"])
    assert task["project_id"] == scratch
    updated = await hook(app)("update", session_id=chat, task_id=task["id"], client_operation_id="call-2",
                              expected_entity_revision=task["entity_revision"], check_ids=["C2"])
    assert [item["done"] for item in updated["checklist"]] == [False, True]
    grants = await standing_grants(app)
    assert [(row["actor_id"], row["scope_kind"], row["scope_id"]) for row in grants] == [(f"session:{chat}", "project", scratch)]


async def test_a_staff_worker_without_a_grant_is_still_refused(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    manager: SessionManager = app.manager
    ada = await manager.staff.hire(where.project, name="Ada", role="Menu page", isolation="shared")
    worker = (await manager.create_session("Ada · menu", project_id=where.project)).session.id
    live = await manager.staff.claim_session(ada.id, kind="daedalus")
    await manager.staff.started(live.id, session_id=worker)

    async def check_staff(conn: Any, staff_session_id: str, **_: Any) -> AttemptIdentity:
        assert staff_session_id == live.id
        return AttemptIdentity("attempt", "task", 1, 1, Principal(f"staff:{ada.id}", "agent"))

    app.executions.check_staff = check_staff
    with pytest.raises(ControlDenied, match="team member has no approval for board.task.create.*Report"):
        await hook(app)("add", session_id=worker, client_operation_id="call-1",
                        expected_collection_revision=1, title="Plan the team")

    # A helper of the worker looks like a plain chat to the board; it must not take the chat's grant.
    helper = (await manager.create_session("Ada's helper", project_id=where.project)).session.id
    await app.db.execute("UPDATE sessions SET metadata = ? WHERE id = ?", (json.dumps({"subagent_of": worker}), helper))
    with pytest.raises(ControlDenied, match="works for a project's team"):
        await hook(app)("add", session_id=helper, client_operation_id="call-2",
                        expected_collection_revision=1, title="Plan the team")

    # Nor does the worker once its session has ended and no live row names it.
    await app.db.execute("UPDATE staff_sessions SET ended_at = '2026-01-01' WHERE id = ?", (live.id,))
    with pytest.raises(ControlDenied, match="works for a project's team"):
        await hook(app)("add", session_id=worker, client_operation_id="call-3",
                        expected_collection_revision=1, title="Plan the team")
    assert await standing_grants(app) == []


async def test_the_coordinator_path_is_unchanged(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    manager: SessionManager = app.manager
    coordinator = (await manager.create_session("orchestrator", project_id=where.project)).session.id
    assert await manager.projects.set_orchestrator(where.project, expect="", value=coordinator)
    await app.db.execute("UPDATE sessions SET metadata = ? WHERE id = ?",
                         (json.dumps({"orchestrator_of": where.project}), coordinator))
    await manager.projects.update_orchestrator(where.project, enabled=True, autonomy="ask")
    with pytest.raises(ControlDenied, match="the coordinator has no current grant"):
        await hook(app)("add", session_id=coordinator, client_operation_id="call-1",
                        expected_collection_revision=1, title="Plan")

    await manager.projects.update_orchestrator(where.project, autonomy="normal")
    task = await hook(app)("add", session_id=coordinator, client_operation_id="call-2",
                           expected_collection_revision=1, title="Plan")
    issuers = await app.db.fetchall("SELECT DISTINCT issuer_id,actor_id FROM actor_grants")
    assert [(row["issuer_id"], row["actor_id"]) for row in issuers] == [("operator:autonomy", f"orchestrator:{coordinator}")]
    assert task["project_id"] == where.project

    # The chat's standing grant covers its board only, never a coordinator's operations.
    with pytest.raises(ControlDenied, match="watch.create belongs to a project's coordinator"):
        await app.extensions["board_tool_authority"](where.chat, "watch.create")
    assert await standing_grants(app) == []
