"""Faults at durable lifecycle boundaries preserve one authority and an honest outcome."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from daedalus.extensions.auto_handoff import advance, on_capacity_released
from daedalus.stores.capacity import claim_in, snapshot_in
from daedalus.stores.control import ControlStore, Entity, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.update_drains import UpdateDrainActive, UpdateDrains, assert_admission_open_in
from tests.unit.test_auto_handoff import ready_successor
from tests.unit.test_effect_outbox import OPERATOR, enqueue
from tests.unit.test_update_drains import begin


async def test_crash_inside_command_transaction_leaves_no_phantom_receipt(db: Database) -> None:
    control = ControlStore(db)
    scope = Scope("global", "global")
    collection = Entity("collection", "global")
    revision = await control.revision(scope, collection)

    async def interrupted(conn, mutation):
        await OutboxStore.enqueue(conn, mutation, OPERATOR, kind="test.effect",
                                  operation="test.queue", payload={"text": "send"})
        raise RuntimeError("process stopped before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        await control.mutate(OPERATOR, scope, "test.queue", "interrupted", revision,
                             collection, {}, interrupted)
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 0
    assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
    assert await control.revision(scope, collection) == revision
    assert await enqueue(db, command="interrupted")
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 1


@pytest.mark.parametrize("external_effect_observed", [False, True])
async def test_restart_after_claim_requires_reconciliation_before_any_redelivery(
    db: Database, external_effect_observed: bool,
) -> None:
    action_id = await enqueue(db)
    first = OutboxStore(db)
    claim = await first.claim(("test.effect",))
    assert claim is not None
    deliveries = [action_id] if external_effect_observed else []

    successor_db = Database(db.path, workspaces_dir=db.workspaces_dir)
    await successor_db.open()
    try:
        successor = OutboxStore(successor_db)
        assert await successor.recover() == 1
        assert await successor.claim(("test.effect",)) is None
        assert not await successor.finish(claim, state="completed")
        assert await successor.recover() == 0
        assert (await successor.view(action_id))["state"] == "unknown"
        assert deliveries == ([action_id] if external_effect_observed else [])
        assert (await successor_db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 1
        assert (await successor_db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 1
    finally:
        await successor_db.close()


async def test_lost_handoff_response_and_duplicate_capacity_callbacks_launch_once(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, dispatcher, team, starts = await ready_successor(db)
    monkeypatch.setattr("daedalus.extensions.auto_handoff.current_office", AsyncMock(return_value="office"))
    monkeypatch.setattr("daedalus.extensions.auto_handoff.resolve_authority", AsyncMock(return_value=OPERATOR))
    from daedalus.extensions import auto_handoff

    committed = auto_handoff.queue_launch

    async def lost_response(*args, **kwargs):
        await committed(*args, **kwargs)
        raise ConnectionError("response lost after commit")

    monkeypatch.setattr(auto_handoff, "queue_launch", lost_response)
    try:
        with pytest.raises(ConnectionError, match="response lost"):
            await advance(app, "task")
        monkeypatch.setattr(auto_handoff, "queue_launch", committed)
        retry = await advance(app, "task")
        assert retry["state"] == "claimed"
        assert (await db.fetchone("SELECT count(*) FROM handoff_claims"))[0] == 1
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox WHERE kind = 'task.launch'"))[0] == 1
        assert await dispatcher.step()
        callback = type("Event", (), {"type": "staff.status", "project_id": "project",
                                      "payload": {"status": "exited"}})()
        await on_capacity_released(app, callback)
        await on_capacity_released(app, callback)
        assert not await dispatcher.step()
        assert len(starts) == 1
        assert (await db.fetchone("SELECT count(*) FROM execution_attempts"))[0] == 1
    finally:
        team.queue.close()
        app.executions.release()


async def test_drain_lost_response_remains_closed_across_database_reopen(db: Database) -> None:
    executions = ExecutionStore(db)
    executions.acquire()
    try:
        await executions.boot()
        revision = await UpdateDrains(db, executions).control.revision(
            Scope("global", "global"), Entity("collection", "global"))
        first = await begin(UpdateDrains(db, executions))
        successor_db = Database(db.path, workspaces_dir=db.workspaces_dir)
        await successor_db.open()
        try:
            drains = UpdateDrains(successor_db, executions)
            assert await begin(drains, expected_collection_revision=revision) == first
            assert (await drains.read())["admission_open"] is False
            assert (await successor_db.fetchone("SELECT count(*) FROM update_drains"))[0] == 1
            assert (await successor_db.fetchone("SELECT count(*) FROM operation_receipts"
                                                " WHERE operation_kind = 'runtime.update.drain'"))[0] == 1
            async with successor_db.transaction() as conn:
                with pytest.raises(UpdateDrainActive, match="closed"):
                    await assert_admission_open_in(conn)
        finally:
            await successor_db.close()
    finally:
        executions.release()


async def test_expired_unbound_capacity_lease_cannot_be_reused_after_restart(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Project','now','{}')")
    async with db.transaction() as conn:
        assert await claim_in(conn, attempt_id="stopped", project_id="project",
                              runtime_kind="cli", role_class="reviewer", generation=1, cap=1) is None
    await db.execute("UPDATE capacity_reservations SET expires_at = '2000-01-01' WHERE attempt_id = 'stopped'")
    successor_db = Database(db.path, workspaces_dir=db.workspaces_dir)
    await successor_db.open()
    try:
        async with successor_db.transaction() as conn:
            view = await snapshot_in(conn, generation=2, cap=1)
            assert view["reserved"] == 0 and view["available"] == 1
            with pytest.raises(ValueError, match="released capacity claim"):
                await claim_in(conn, attempt_id="stopped", project_id="project",
                               runtime_kind="cli", role_class="reviewer", generation=2, cap=1)
            assert await claim_in(conn, attempt_id="replacement", project_id="project",
                                  runtime_kind="cli", role_class="reviewer", generation=2, cap=1) is None
        assert (await successor_db.fetchone("SELECT count(*) FROM capacity_reservations WHERE state = 'held'"))[0] == 1
        assert (await successor_db.fetchone("SELECT count(*) FROM capacity_reservations WHERE state = 'released'"))[0] == 1
    finally:
        await successor_db.close()
