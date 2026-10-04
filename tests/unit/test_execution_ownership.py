"""Restart, replacement and revoked workers cannot publish into another attempt."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import AttemptIdentity, ExecutionStore
from daedalus.stores.phase_clocks import PhaseClocks

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


async def owner(db: Database) -> tuple[ExecutionStore, AttemptIdentity, str]:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('task','Work','doing',3,'project','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                     " VALUES ('task',1,'operator','','{}','2026-01-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('worker','project','Worker','daedalus','operator','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                     " VALUES ('staff-session','worker','daedalus','task','2026-01-01','2026-01-01')")
    subject = Principal("staff:worker", "agent")
    grant = await ControlStore(db).issue_grant(OPERATOR, subject, Scope("project", "project"),
                                              operations=["result.submit"], effects=[], task_id="task",
                                              expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    worker = Principal(subject.actor_id, "agent", grant["grant_id"], grant["generation"])
    store = ExecutionStore(db)
    store.acquire()
    try:
        await store.boot()
        token = secrets.token_urlsafe(32)
        async with db.transaction() as conn:
            identity = await store.create(conn, attempt_id="attempt", task_id="task", contract_revision=1,
                                          launcher=OPERATOR, worker=worker, staff_session_id="staff-session",
                                          runtime_kind="daedalus", fence_token=token)
        return store, identity, token
    except BaseException:
        store.release()
        raise


async def test_database_owner_excludes_another_runtime_and_advances_only_after_release(db: Database) -> None:
    first = ExecutionStore(db)
    other = ExecutionStore(db)
    first.acquire()
    try:
        assert await first.boot() == await first.boot() == 1
        with pytest.raises(RuntimeError, match="another runtime"):
            other.acquire()
        assert await db.kv_get("execution_host_generation") == 1
    finally:
        first.release()
    other.acquire()
    try:
        assert await other.boot() == 2
    finally:
        other.release()


async def test_authenticated_staff_context_and_provider_binding_are_exact(db: Database) -> None:
    store, identity, token = await owner(db)
    try:
        async with db.transaction() as conn:
            assert await store.check_staff(conn, "staff-session") == identity
            await store.bind(conn, identity, provider_session_ref="native-session:run")
            with pytest.raises(ControlDenied, match="cannot be replaced"):
                await store.bind(conn, identity, provider_session_ref="native-session:later-run")
        row = await db.fetchone("SELECT provider_session_ref,fence_token_hash FROM execution_attempts")
        assert row["provider_session_ref"] == "native-session:run"
        assert token not in str(dict(row))
        with pytest.raises(ControlDenied, match="no current"):
            async with db.transaction() as conn:
                await store.check_staff(conn, "unrelated-session")
    finally:
        store.release()


async def test_heartbeat_cannot_satisfy_first_output_or_extend_its_deadline(db: Database) -> None:
    store, identity, _ = await owner(db)
    clocks = PhaseClocks(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with db.transaction() as conn:
            await clocks.start(conn, identity, "first_output", timeout_seconds=30, at=start)
            await clocks.heartbeat(conn, identity, at=start + timedelta(seconds=29))
            assert await clocks.expire(conn, identity, at=start + timedelta(seconds=31)) == "first_output"
            with pytest.raises(ControlDenied, match="needs reconciliation"):
                await clocks.start(conn, identity, "idle", timeout_seconds=30, at=start + timedelta(seconds=32))
        row = await db.fetchone("SELECT deadline_at,last_signal_at,outcome FROM attempt_phase_clocks")
        assert row["deadline_at"] == (start + timedelta(seconds=30)).isoformat()
        assert row["last_signal_at"] == (start + timedelta(seconds=29)).isoformat()
        assert row["outcome"] == "timed_out"
    finally:
        store.release()


async def test_auth_and_idle_have_separate_deadlines_and_output_renews_only_idle(db: Database) -> None:
    store, identity, _ = await owner(db)
    clocks = PhaseClocks(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with db.transaction() as conn:
            await clocks.start(conn, identity, "auth", timeout_seconds=90, at=start)
            assert await clocks.expire(conn, identity, at=start + timedelta(seconds=60)) is None
            await clocks.start(conn, identity, "first_output", timeout_seconds=15,
                               at=start + timedelta(seconds=60))
            await clocks.output(conn, identity, idle_timeout_seconds=40,
                                at=start + timedelta(seconds=70))
            await clocks.heartbeat(conn, identity, at=start + timedelta(seconds=100))
            assert await clocks.expire(conn, identity, at=start + timedelta(seconds=109)) is None
            await clocks.output(conn, identity, idle_timeout_seconds=40,
                                at=start + timedelta(seconds=109))
            assert await clocks.expire(conn, identity, at=start + timedelta(seconds=140)) is None
            assert await clocks.expire(conn, identity, at=start + timedelta(seconds=150)) == "idle"
        rows = await db.fetchall("SELECT phase,outcome FROM attempt_phase_clocks ORDER BY started_at")
        assert [(row["phase"], row["outcome"]) for row in rows] == [
            ("auth", "completed"), ("first_output", "completed"), ("idle", "timed_out")]
    finally:
        store.release()


async def test_superseded_attempt_cannot_change_its_phase_clock(db: Database) -> None:
    store, identity, _ = await owner(db)
    clocks = PhaseClocks(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with db.transaction() as conn:
            await clocks.start(conn, identity, "spawn", timeout_seconds=20, at=start)
        await db.execute("UPDATE board_tasks SET current_attempt_id = NULL WHERE id = 'task'")
        with pytest.raises(ControlDenied, match="superseded"):
            async with db.transaction() as conn:
                await clocks.heartbeat(conn, identity, at=start + timedelta(seconds=10))
        with pytest.raises(ControlDenied, match="superseded"):
            async with db.transaction() as conn:
                await clocks.expire(conn, identity, at=start + timedelta(seconds=30))
        assert (await db.fetchone("SELECT outcome FROM attempt_phase_clocks"))[0] == "active"
    finally:
        store.release()


@pytest.mark.parametrize("change", ["token", "contract", "revoked", "ended", "replacement", "restart"])
async def test_late_or_forged_callback_is_quarantined_without_changing_task(db: Database, change: str) -> None:
    store, identity, token = await owner(db)
    try:
        if change == "contract":
            await db.execute("UPDATE board_tasks SET contract_revision = 2 WHERE id = 'task'")
        elif change == "revoked":
            await ControlStore(db).revoke_grant(OPERATOR, identity.principal.grant_id, reason="withdrawn")
        elif change == "ended":
            await db.execute("UPDATE staff_sessions SET ended_at = '2026-01-02' WHERE id = 'staff-session'")
        elif change == "replacement":
            await db.execute("UPDATE board_tasks SET current_attempt_id = NULL WHERE id = 'task'")
        elif change == "restart":
            store.release()
            store.acquire()
            assert await store.boot() == 2
            assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "recovering"

        async def forbidden(conn: Any) -> None:
            pytest.fail("stale worker publication reached its write")

        assert not await store.ingest(identity, "wrong-token" if change == "token" else token,
                                      {"report": "late"}, forbidden)
        assert (await db.fetchone("SELECT title FROM board_tasks"))[0] == "Work"
        assert (await db.fetchone("SELECT count(*) FROM quarantined_attempt_events"))[0] == 1
    finally:
        store.release()


async def test_current_callback_and_completion_commit_together_and_later_callback_is_rejected(db: Database) -> None:
    store, identity, token = await owner(db)
    try:
        async def publish(conn: Any) -> None:
            await conn.execute("UPDATE board_tasks SET notes = 'original result' WHERE id = 'task'")
            await store.complete(conn, identity, outcome="complete")

        assert await store.ingest(identity, token, {"report": "original"}, publish)
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "completed"
        assert not await store.ingest(identity, token, {"report": "late duplicate"}, publish)
        assert (await db.fetchone("SELECT notes FROM board_tasks"))[0] == "original result"
    finally:
        store.release()


async def test_unobserved_previous_attempt_cannot_be_replaced_with_a_fresh_launch(db: Database) -> None:
    store, identity, token = await owner(db)
    try:
        with pytest.raises(ControlDenied, match="reconciled"):
            async with db.transaction() as conn:
                await store.create(conn, attempt_id="later", task_id="task", contract_revision=1,
                                   launcher=OPERATOR, worker=identity.principal, staff_session_id="staff-session",
                                   runtime_kind="daedalus", fence_token=token)
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks"))[0] == "attempt"
    finally:
        store.release()
