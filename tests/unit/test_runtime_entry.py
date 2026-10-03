"""A refused pre-entry launch can recover; lost runtime replies retain physical ownership."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.runtime_observations import enter_runtime, observe_no_entry
from daedalus.stores.control import ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.runtime_release import attempt_released_in, no_entry_in, physical_exit_in
from tests.unit.test_execution_ownership import owner


async def test_host_refusal_is_distinct_from_physical_exit_and_drains_owned_cancellation(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store, extensions={"effects": SimpleNamespace(notify=lambda: None)})
    try:
        assert await observe_no_entry(app, identity, staff_session_id="staff-session", reason="context changed")
        async with db.transaction() as conn:
            assert await no_entry_in(conn, identity.id)
            assert await attempt_released_in(conn, identity.id)
            assert not await physical_exit_in(conn, identity.id)
        assert not await db.fetchall("SELECT * FROM runtime_exit_observations")
        assert not await db.fetchall("SELECT * FROM result_receipts")
        lifecycle = Lifecycle(app)
        preview = await lifecycle.preview("task", "task")
        await lifecycle.cancel_command(Principal.operator({"via": "token", "user_id": 1}), "task", "task",
                                       "cancel refused work", expected_entity_revision=preview["entity_revision"],
                                       expected_source_revision=1, preview_fingerprint=preview["preview_fingerprint"],
                                       client_operation_id="cancel-refused")
        assert await lifecycle.drain_verified() == 1
        assert (await lifecycle.preview("task", "task"))["cancel_state"] == "drained"
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_owners"))[0] == "drained"
        with pytest.raises(sqlite3.IntegrityError, match="refused execution"):
            await db.execute("UPDATE execution_attempts SET runtime_entered_at = 'later' WHERE id = ?", (identity.id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await db.execute("DELETE FROM runtime_no_entry_observations WHERE attempt_id = ?", (identity.id,))
    finally:
        store.release()


async def test_entry_without_a_returned_runtime_reference_remains_uncertain(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await enter_runtime(app, identity, capacity_slot_id=None)
        assert not await observe_no_entry(app, identity, staff_session_id="staff-session", reason="lost reply")
        # Even a superficially ended failed row cannot replace a send boundary's missing exit.
        await db.execute("UPDATE execution_attempts SET state = 'failed' WHERE id = ?", (identity.id,))
        await db.execute("UPDATE staff_sessions SET ended_at = 'now' WHERE id = 'staff-session'")
        async with db.transaction() as conn:
            assert not await attempt_released_in(conn, identity.id)
        with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced"):
            await db.execute("UPDATE execution_attempts SET runtime_entered_at = NULL WHERE id = ?", (identity.id,))
        assert not await db.fetchall("SELECT * FROM runtime_no_entry_observations")
    finally:
        store.release()


async def test_failed_legacy_row_or_wrong_staff_is_not_a_host_no_entry_observation(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        assert not await observe_no_entry(app, identity, staff_session_id="another-session", reason="not ours")
        await db.execute("UPDATE execution_attempts SET state = 'failed' WHERE id = ?", (identity.id,))
        await db.execute("UPDATE staff_sessions SET ended_at = 'now' WHERE id = 'staff-session'")
        assert not await observe_no_entry(app, identity, staff_session_id="staff-session", reason="assumed stopped")
        async with db.transaction() as conn:
            assert not await attempt_released_in(conn, identity.id)
    finally:
        store.release()


async def test_entry_checks_current_worker_authority_before_publishing_boundary(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await store.control.revoke_grant(Principal.operator({"via": "token", "user_id": 1}),
                                         identity.principal.grant_id, reason="withdraw launch")
        with pytest.raises(ControlDenied):
            await enter_runtime(app, identity, capacity_slot_id=None)
        assert (await db.fetchone("SELECT runtime_entered_at FROM execution_attempts"))[0] is None
        assert await observe_no_entry(app, identity, staff_session_id="staff-session", reason="authority withdrawn")
    finally:
        store.release()
