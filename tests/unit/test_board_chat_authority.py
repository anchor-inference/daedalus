"""The operator's own chat writes its board; a team's workers and coordinator keep their own authority.

Board writes once needed a grant that nothing ever issued to an ordinary chat, so BoardAdd and
BoardUpdate from the operator's chats were always refused while BoardList worked. These tests go
through the same hook the Board tools call, and pin both sides: the chat now writes without an
approval step, and nobody on a project's team gains a right by it. A chat also takes its own tasks to
doing and done on light receipts of its own, behind the same dependency gate as everyone, while the
coordinator and the workers keep their launches and reports.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import board as board_extension
from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.coordinator_authority import install_authority
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope
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
    with pytest.raises(ValueError, match="review is for a team's work"):
        await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-4",
                        expected_entity_revision=task["entity_revision"], status="review")

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


async def say(app: Any, session_id: str, text: str) -> int:
    """A message in the chat, standing for the turn a board call runs in."""
    tenant = (await app.db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?", (session_id,)))["tenant_id"]
    await app.db.execute("INSERT INTO session_messages(session_id,tenant_id,message) VALUES (?,?,?)",
                         (session_id, tenant, json.dumps({"role": "user", "content": text})))
    return int((await app.db.fetchone("SELECT MAX(seq) AS seq FROM session_messages WHERE session_id = ?", (session_id,)))["seq"])


OPERATOR = Principal("operator:1", "operator")


async def test_a_chat_takes_its_own_task_to_doing_and_finishes_it(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    task = await hook(app)("add", session_id=where.chat, client_operation_id="call-1", expected_collection_revision=1,
                           title="Write the menu page", checklist=["draft", "publish"])

    doing = await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-2",
                            expected_entity_revision=task["entity_revision"], status="doing", note="drafting")
    assert doing["status"] == "doing" and doing["session_id"] == where.chat and doing["heartbeat_at"]
    claim = await app.db.fetchone("SELECT actor_id,response_json FROM operation_receipts WHERE client_operation_id = 'call-2'")
    assert claim["actor_id"] == f"session:{where.chat}"
    assert json.loads(claim["response_json"])["session_id"] == where.chat

    # Done waits for every criterion, and a refused call stores nothing.
    with pytest.raises(ValueError, match=r"checklist is not complete: C2 \(publish\).*Nothing was stored"):
        await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-3",
                        expected_entity_revision=doing["entity_revision"], status="done", check_ids=["C1"])
    turn = await say(app, where.chat, "publish it and close the task")
    done = await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-4",
                           expected_entity_revision=doing["entity_revision"], status="done", check_ids=["C1", "C2"],
                           note="the menu page is live at /menu")
    assert done["status"] == "done" and done["acceptance_state"] == "accepted"
    result = await app.db.fetchone("SELECT * FROM result_receipts WHERE id = ?", (done["accepted_result_id"],))
    assert (result["actor_id"], result["original_text"], result["attempt_id"]) == (
        f"session:{where.chat}", "the menu page is live at /menu", None)
    anchor = await app.db.fetchone("SELECT session_id,turn_seq FROM result_turn_anchors WHERE result_id = ?", (result["id"],))
    assert (anchor["session_id"], anchor["turn_seq"]) == (where.chat, turn)
    verdict = await app.db.fetchone("SELECT reviewer_actor_id,accepted FROM review_verdicts WHERE result_id = ?", (result["id"],))
    assert (verdict["reviewer_actor_id"], verdict["accepted"]) == (f"session:{where.chat}", 1)
    with pytest.raises(ValueError, match="already done; the operator reopens it"):
        await hook(app)("update", session_id=where.chat, task_id=task["id"], client_operation_id="call-5",
                        expected_entity_revision=done["entity_revision"], status="doing")

    # The card says who finished it and what they said; the operator's word is still theirs to give.
    board = await app.extensions["board"].project_board(where.project, include_done=True)
    card = next(item for item in board["tasks"] if item["id"] == task["id"])
    assert card["finished_by"]["session_id"] == where.chat
    assert card["finished_by"]["title"] == "a chat in the project"
    assert card["finished_by"]["note"] == "the menu page is live at /menu"
    reopened = await BoardCommands(app.db).update(OPERATOR, Scope("project", where.project), task["id"],
                                                  client_operation_id="app-1", expected_entity_revision=done["entity_revision"],
                                                  status="todo", note="the prices are missing")
    assert reopened["status"] == "todo"
    after = await app.extensions["board"].get(task["id"])
    assert (after["status"], after["acceptance_state"], after["accepted_result_id"]) == ("todo", "returned", None)
    assert [item["done"] for item in after["checklist"]] == [False, False]
    returned = await app.db.fetchone("SELECT actor_id,reason,result_id FROM review_returns WHERE task_id = ?", (task["id"],))
    assert (returned["actor_id"], returned["reason"], returned["result_id"]) == ("operator:1", "the prices are missing", result["id"])


async def test_a_chat_cannot_finish_a_task_whose_dependencies_are_open(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    first = await hook(app)("add", session_id=where.chat, client_operation_id="call-1", expected_collection_revision=1,
                            title="Bake the bread")
    second = await hook(app)("add", session_id=where.chat, client_operation_id="call-2", expected_collection_revision=2,
                             title="Slice the bread", depends_on=[first["id"]])
    assert second["status"] == "blocked"
    for status in ("done", "doing"):
        with pytest.raises(ValueError, match=rf'waits on {first["id"]} "Bake the bread" \(todo\); finish those first'):
            await hook(app)("update", session_id=where.chat, task_id=second["id"], client_operation_id=f"call-{status}",
                            expected_entity_revision=second["entity_revision"], status=status)

    finished = await hook(app)("update", session_id=where.chat, task_id=first["id"], client_operation_id="call-3",
                               expected_entity_revision=first["entity_revision"], status="done")
    assert finished["status"] == "done"
    # Its accepted result makes the waiting card ready, as any other acceptance does.
    ready = await app.extensions["board"].get(second["id"])
    assert ready["status"] == "todo"
    done = await hook(app)("update", session_id=where.chat, task_id=second["id"], client_operation_id="call-4",
                           expected_entity_revision=ready["entity_revision"], status="done", note="sliced")
    assert done["status"] == "done"


async def test_a_chat_moves_only_its_own_unassigned_tasks(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    other = (await app.manager.create_session("another chat", project_id=where.project)).session.id
    theirs = await hook(app)("add", session_id=other, client_operation_id="call-1", expected_collection_revision=1,
                             title="Another chat's plan")
    for status in ("doing", "done"):
        with pytest.raises(KeyError):
            await hook(app)("update", session_id=where.chat, task_id=theirs["id"], client_operation_id=f"call-{status}",
                            expected_entity_revision=theirs["entity_revision"], status=status)

    # A card the operator posted to the project's board is the team's, not on the chat's board.
    scope = Scope("project", where.project)
    team_card = await BoardCommands(app.db).create(OPERATOR, scope, client_operation_id="app-1",
                                                   expected_collection_revision=2, title="Team work")
    team_row = await app.extensions["board"].get(team_card["task_id"])
    with pytest.raises(KeyError):
        await hook(app)("update", session_id=where.chat, task_id=team_card["task_id"], client_operation_id="call-2",
                        expected_entity_revision=team_row["entity_revision"], status="done")

    # The chat's own card, once the operator hands it to a team member, moves through the team.
    mine = await hook(app)("add", session_id=where.chat, client_operation_id="call-3", expected_collection_revision=3,
                           title="Menu page")
    ada = await app.manager.staff.hire(where.project, name="Ada", role="Menu page", isolation="shared")
    assigned = await BoardCommands(app.db).update(OPERATOR, scope, mine["id"], client_operation_id="app-2",
                                                  expected_entity_revision=mine["entity_revision"],
                                                  assignee_staff_id=ada.id)
    with pytest.raises(ValueError, match="a team member works this task"):
        await hook(app)("update", session_id=where.chat, task_id=mine["id"], client_operation_id="call-4",
                        expected_entity_revision=assigned["entity_revision"], status="doing")

    # Nor may a chat reopen what someone else finished: the operator's Close is not the chat's to undo.
    closed = await BoardCommands(app.db).close(OPERATOR, scope, theirs["id"], client_operation_id="app-3",
                                               expected_entity_revision=theirs["entity_revision"], status="done",
                                               reason="settled elsewhere")
    with pytest.raises(Exception, match="return the exact reviewed result before reopening"):
        await BoardCommands(app.db).update(OPERATOR, scope, theirs["id"], client_operation_id="app-4",
                                           expected_entity_revision=closed["entity_revision"], status="todo")


async def test_the_coordinator_and_a_worker_still_need_their_own_receipts(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    manager: SessionManager = app.manager
    coordinator = (await manager.create_session("orchestrator", project_id=where.project)).session.id
    assert await manager.projects.set_orchestrator(where.project, expect="", value=coordinator)
    await app.db.execute("UPDATE sessions SET metadata = ? WHERE id = ?",
                         (json.dumps({"orchestrator_of": where.project}), coordinator))
    await manager.projects.update_orchestrator(where.project, enabled=True, autonomy="normal")
    task = await hook(app)("add", session_id=coordinator, client_operation_id="call-1",
                           expected_collection_revision=1, title="Plan")
    for status in ("doing", "done"):
        with pytest.raises(ValueError, match="require their dedicated result or launch command"):
            await hook(app)("update", session_id=coordinator, task_id=task["id"], client_operation_id=f"call-{status}",
                            expected_entity_revision=task["entity_revision"], status=status)

    # A worker with an explicit approval for updates still cannot take its card to doing or done.
    ada = await manager.staff.hire(where.project, name="Ada", role="Menu page", isolation="shared")
    worker = (await manager.create_session("Ada · menu", project_id=where.project)).session.id
    live = await manager.staff.claim_session(ada.id, kind="daedalus")
    await manager.staff.started(live.id, session_id=worker)

    async def check_staff(conn: Any, staff_session_id: str, **_: Any) -> AttemptIdentity:
        return AttemptIdentity("attempt", task["id"], 1, 1, Principal(f"staff:{ada.id}", "agent"))

    app.executions.check_staff = check_staff
    await ControlStore(app.db).issue_grant(OPERATOR, Principal(f"staff:{ada.id}", "agent"), Scope("project", where.project),
                                           operations=["board.task.update"], effects=[],
                                           expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    for status in ("doing", "done"):
        with pytest.raises(ValueError, match="require their dedicated result or launch command"):
            await hook(app)("update", session_id=worker, task_id=task["id"], client_operation_id=f"worker-{status}",
                            expected_entity_revision=task["entity_revision"], status=status)
    noted = await hook(app)("update", session_id=worker, task_id=task["id"], client_operation_id="worker-note",
                            expected_entity_revision=task["entity_revision"], note="looked at it")
    assert noted["status"] == "todo" and noted["accepted_result_id"] is None


async def test_a_chat_holds_no_more_tasks_in_doing_than_its_limit(app: Any, tmp_path: Path) -> None:
    where = await project_with_chat(app, tmp_path)
    app.config.board.wip_limit = 1
    first = await hook(app)("add", session_id=where.chat, client_operation_id="call-1", expected_collection_revision=1,
                            title="Bake")
    second = await hook(app)("add", session_id=where.chat, client_operation_id="call-2", expected_collection_revision=2,
                             title="Slice")
    taken = await hook(app)("update", session_id=where.chat, task_id=first["id"], client_operation_id="call-3",
                            expected_entity_revision=first["entity_revision"], status="doing")
    with pytest.raises(ValueError, match=r"work-in-progress limit reached \(1 tasks in doing\)"):
        await hook(app)("update", session_id=where.chat, task_id=second["id"], client_operation_id="call-4",
                        expected_entity_revision=second["entity_revision"], status="doing")
    # Finishing the first frees the slot.
    await hook(app)("update", session_id=where.chat, task_id=first["id"], client_operation_id="call-5",
                    expected_entity_revision=taken["entity_revision"], status="done")
    moved = await hook(app)("update", session_id=where.chat, task_id=second["id"], client_operation_id="call-6",
                            expected_entity_revision=second["entity_revision"], status="doing")
    assert moved["status"] == "doing"
