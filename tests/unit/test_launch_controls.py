"""Launching cannot leave spare grants or resurrect an already completed worker."""

from __future__ import annotations

import secrets
from types import SimpleNamespace

import pytest

from daedalus.extensions.launch_controls import observe_bind, prepare_attempt
from daedalus.staff_runtime import BoardTask, Started
from daedalus.stores.control import ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.staff import StaffStore

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


async def launch_fixture(db: Database):
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('task','Work','doing',3,'project','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                     " VALUES ('task',1,'operator','','{}','2026-01-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('worker','project','Worker','daedalus','operator','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                     " VALUES ('staff-session','worker','daedalus','task','2026-01-01','2026-01-01')")
    staff = StaffStore(db)
    member, session = await staff.get("worker"), await staff.session("staff-session")
    assert member is not None and session is not None
    executions = ExecutionStore(db)
    executions.acquire()
    await executions.boot()
    app = SimpleNamespace(db=db, executions=executions)
    task = BoardTask("task", "Work", "doing", project_id="project", assignee_staff_id="worker")
    return app, member, task, session


async def test_attempt_and_its_report_grant_commit_together(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        grant = await db.fetchone("SELECT * FROM actor_grants")
        assert grant["actor_id"] == "staff:worker"
        assert grant["task_id"] == task.id
        assert grant["operations_json"] == '["result.submit","staff.report"]'
        assert grant["effects_json"] == "[]"
        owner = await db.fetchone("SELECT * FROM lifecycle_owners WHERE child_id = ?", (identity.id,))
        assert (owner["parent_kind"], owner["parent_id"], owner["child_kind"], owner["source_revision"]) == ("task", task.id, "execution_attempt", 1)
        async with db.transaction() as conn:
            assert await app.executions.check_staff(conn, session.id) == identity
        with pytest.raises(ControlDenied, match="reconciled"):
            await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 1
        assert (await db.fetchone("SELECT count(*) FROM grant_events"))[0] == 1
    finally:
        app.executions.release()


async def test_cancelled_parent_refuses_launch_without_orphan_grant(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        await db.execute("INSERT INTO lifecycle_parents(parent_kind,parent_id,project_id,generation,cancel_state,updated_at,contract_revision)"
                         " VALUES ('task','task','project',1,'requested','2026-01-01',1)")
        with pytest.raises(ValueError, match="cancelled"):
            await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 0
        assert (await db.fetchone("SELECT count(*) FROM execution_attempts"))[0] == 0
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = 'task'"))[0] is None
    finally:
        app.executions.release()


async def test_missing_contract_rolls_back_the_new_report_grant(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        await db.execute("DELETE FROM task_contract_versions")
        with pytest.raises(ControlDenied, match="contract"):
            await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 0
        assert (await db.fetchone("SELECT count(*) FROM execution_attempts"))[0] == 0
    finally:
        app.executions.release()


async def test_fast_result_is_not_revived_when_start_returns(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session, fence_token=secrets.token_urlsafe(32))
        async with db.transaction() as conn:
            await app.executions.complete(conn, identity, outcome="complete")
        await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at)"
                         " VALUES ('native-session','tenant','project','Work','2026-01-01','2026-01-01')")
        await db.execute("UPDATE staff_sessions SET session_id = 'native-session' WHERE id = ?", (session.id,))
        await observe_bind(app, identity, session, Started(None, None, None, "native-session"))
        row = await db.fetchone("SELECT state,provider_session_ref FROM execution_attempts")
        assert (row["state"], row["provider_session_ref"]) == ("completed", "session:native-session")
        with pytest.raises(ControlDenied, match="not bound"):
            await observe_bind(app, identity, session, Started(None, None, None, "unrelated-session"))
        app.executions.release()
        app.executions.acquire()
        await app.executions.boot()
        with pytest.raises(ControlDenied, match="previous host generation"):
            await observe_bind(app, identity, session, Started(None, None, None, "native-session"))
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "completed"
    finally:
        app.executions.release()
