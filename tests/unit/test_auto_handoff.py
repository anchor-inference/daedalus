"""A ready dependency wake cannot spend more than one authorized launch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from daedalus.extensions.auto_handoff import advance, on_capacity_released
from daedalus.extensions.orchestrator_domain import set_next_action
from daedalus.stores.control import ControlDenied
from daedalus.stores.database import Database
from tests.unit.test_launch_controls import OPERATOR
from tests.unit.test_task_launch import queued_fixture


async def ready_successor(db: Database):
    app, dispatcher, team, starts, _ = await queued_fixture(db)
    await db.execute("INSERT INTO board_tasks(id,project_id,title,status,priority,acceptance,checklist,depends_on,"
                     " created_at,updated_at,brief_json,contract_revision)"
                     " VALUES ('previous','project','Previous','done',1,'','[]','[]','2026-01-01',"
                     " '2026-01-01','{}',1)")
    await db.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,actor_id,operation_kind,"
                     " client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
                     " VALUES ('waiver','project','project','operator:1','dependency.resolve','waiver',"
                     " 'digest',1,'committed','{}','2026-01-01')")
    await db.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                     " resolution_state,waiver_receipt_id,created_at)"
                     " VALUES ('edge','task','previous','required','waived','waiver','2026-01-01')")
    async with db.transaction() as conn:
        await set_next_action(conn, action_id="next", task_id="task", kind="assign",
                              owner_kind="staff", owner_id="worker",
                              prerequisites=[{"kind": "dependency_ready", "ref": "edge"}])
    return app, dispatcher, team, starts


async def test_duplicate_wake_commits_one_claim_and_one_launch(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    app, dispatcher, team, starts = await ready_successor(db)
    monkeypatch.setattr("daedalus.extensions.auto_handoff.current_office", AsyncMock(return_value="office"))
    monkeypatch.setattr("daedalus.extensions.auto_handoff.resolve_authority", AsyncMock(return_value=OPERATOR))
    try:
        first = await advance(app, "task")
        second = await advance(app, "task")
        assert first["state"] == "assigned" and second["claim_id"] == first["claim_id"]
        assert (await db.fetchone("SELECT count(*) FROM handoff_claims"))[0] == 1
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox WHERE kind = 'task.launch'"))[0] == 1
        await db.execute("UPDATE handoff_claims SET operation_id = NULL WHERE id = ?", (first["claim_id"],))
        assert await dispatcher.step()
        assert len(starts) == 1
        assert (await db.fetchone("SELECT state FROM handoff_claims"))[0] == "launched"
    finally:
        team.queue.close()
        app.executions.release()


async def test_no_slot_leaves_no_claim(db: Database) -> None:
    app, _, team, _ = await ready_successor(db)
    team.queue._concurrency = AsyncMock(return_value=0)
    try:
        assert (await advance(app, "task"))["state"] == "waiting_capacity"
        assert (await db.fetchone("SELECT count(*) FROM handoff_claims"))[0] == 0
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox WHERE kind = 'task.launch'"))[0] == 0
    finally:
        team.queue.close()
        app.executions.release()


async def test_released_slot_retries_saved_wait_without_manual_wake(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    app, dispatcher, team, starts = await ready_successor(db)
    concurrency = team.queue._concurrency
    team.queue._concurrency = AsyncMock(return_value=0)
    monkeypatch.setattr("daedalus.extensions.auto_handoff.current_office", AsyncMock(return_value="office"))
    monkeypatch.setattr("daedalus.extensions.auto_handoff.resolve_authority", AsyncMock(return_value=OPERATOR))
    try:
        assert (await advance(app, "task"))["state"] == "waiting_capacity"
        assert (await db.fetchone("SELECT count(*) FROM handoff_claims"))[0] == 0
        team.queue._concurrency = concurrency
        await on_capacity_released(app, SimpleNamespace(type="staff.status", project_id="project",
                                                       payload={"status": "exited"}))
        assert (await db.fetchone("SELECT count(*) FROM handoff_claims"))[0] == 1
        assert await dispatcher.step()
        assert len(starts) == 1
    finally:
        team.queue.close()
        app.executions.release()


async def test_changed_next_action_refuses_queued_handoff(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    app, dispatcher, team, starts = await ready_successor(db)
    monkeypatch.setattr("daedalus.extensions.auto_handoff.current_office", AsyncMock(return_value="office"))
    monkeypatch.setattr("daedalus.extensions.auto_handoff.resolve_authority", AsyncMock(return_value=OPERATOR))
    try:
        receipt = await advance(app, "task")
        async with db.transaction() as conn:
            await set_next_action(conn, action_id="replacement", task_id="task", kind="wait",
                                  owner_kind="operator", owner_id=None, prerequisites=[])
        assert await dispatcher.step()
        assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "failed"
        assert starts == []
    finally:
        team.queue.close()
        app.executions.release()


async def test_expired_authority_asks_once_without_launch(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, team, _ = await ready_successor(db)
    monkeypatch.setattr("daedalus.extensions.auto_handoff.current_office", AsyncMock(return_value="office"))
    monkeypatch.setattr("daedalus.extensions.auto_handoff.resolve_authority",
                        AsyncMock(side_effect=ControlDenied("grant expired")))
    async def open_question(*args, **kwargs):
        await db.execute("INSERT INTO asks(id,short_id,project_id,origin,kind,task_id,request_ref,text,detail_json,"
                         " routed_to,suggestion,created_at,routed_at,title) VALUES"
                         " ('question','Q1','project','orchestrator','question','task',?,'Approve launch','{}',"
                         " 'operator','','2026-01-01','2026-01-01','Approve')", (kwargs["request_ref"],))

    app.manager.asks = SimpleNamespace(open=AsyncMock(side_effect=open_question))
    try:
        assert (await advance(app, "task"))["state"] == "waiting_authority"
        assert (await advance(app, "task"))["state"] == "waiting_authority"
        assert app.manager.asks.open.await_count == 1
        assert (await db.fetchone("SELECT count(*) FROM handoff_claims"))[0] == 0
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox WHERE kind = 'task.launch'"))[0] == 0
    finally:
        team.queue.close()
        app.executions.release()
