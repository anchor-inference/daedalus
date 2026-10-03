"""A report, ended row or stale runtime cannot stand in for physical exit evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from daedalus.extensions.launch_controls import prepare_attempt
from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.orchestrator_domain import apply_goal_revision
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.staff_runtime import BoardTask
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Principal
from daedalus.stores.database import Database
from daedalus.stores.lifecycle import record_owned_exit
from daedalus.stores.staff import StaffStore
from tests.unit.test_execution_ownership import owner


async def native_session(db: Database) -> None:
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at,metadata)"
                     " VALUES ('native','tenant','project','Work','now','now',?)",
                     (json.dumps({"staff_session_id": "staff-session"}),))
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('run','tenant','native','running','now','now')")


async def bound_cli(db: Database, state: str = "running") -> None:
    await db.execute("UPDATE staff_sessions SET kind = 'cli',terminal_id = 'terminal' WHERE id = 'staff-session'")
    await db.execute("UPDATE execution_attempts SET runtime_kind = 'cli',provider_session_ref = 'terminal:terminal',"
                     "runtime_instance = 'daemon-one',state = ?", (state,))
    await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,status,created_at,ptyd_instance)"
                     " VALUES ('terminal','container','staff','/tmp','exited','now','daemon-one')")
    await db.execute("INSERT INTO terminal_exit_observations VALUES ('terminal','daemon-one','now')")


async def test_unexpected_cli_exit_frees_an_unreported_attempt(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await bound_cli(db)
        assert await observe_exit(app, staff_session_id="staff-session", runtime_ref="terminal",
                                  observed_status="exited", runtime_instance="daemon-one")
        row = await db.fetchone("SELECT state FROM execution_attempts WHERE id = ?", (identity.id,))
        session = await db.fetchone("SELECT status,ended_at FROM staff_sessions WHERE id = 'staff-session'")
        assert row is not None and row["state"] == "failed"
        assert session is not None and session["status"] == "exited" and session["ended_at"]
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = 'task'"))[0] == identity.id
        assert not await db.fetchall("SELECT * FROM result_receipts")
    finally:
        store.release()


async def test_old_cli_exit_does_not_revive_a_superseded_attempt(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await bound_cli(db, "superseded")
        await db.execute("UPDATE board_tasks SET current_attempt_id = NULL WHERE id = 'task'")
        assert await observe_exit(app, staff_session_id="staff-session", runtime_ref="terminal",
                                  observed_status="exited", runtime_instance="daemon-one")
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = ?", (identity.id,)))[0] == "superseded"
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = 'task'"))[0] is None
    finally:
        store.release()


async def test_cli_exit_preserves_a_completed_result(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await bound_cli(db, "completed")
        assert await observe_exit(app, staff_session_id="staff-session", runtime_ref="terminal",
                                  observed_status="exited", runtime_instance="daemon-one")
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = ?", (identity.id,)))[0] == "completed"
    finally:
        store.release()


async def test_done_report_and_ended_row_do_not_drain_owned_cancellation(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store, extensions={"effects": SimpleNamespace(notify=lambda: None)})
    lifecycle = Lifecycle(app)
    try:
        await native_session(db)
        await admit_native_run(app, "staff-session", "native", "run")
        async with db.transaction() as conn:
            await store.complete(conn, identity, outcome="complete")
        preview = await lifecycle.preview("task", "task")
        await lifecycle.cancel_command(Principal.operator({"via": "token", "user_id": 1}), "task", "task", "stop owned work",
                                       expected_entity_revision=preview["entity_revision"], expected_source_revision=1,
                                       preview_fingerprint=preview["preview_fingerprint"], client_operation_id="stop")
        await db.execute("UPDATE staff_sessions SET ended_at = 'now' WHERE id = 'staff-session'")
        async with db.transaction() as conn:
            assert not await record_owned_exit(conn, attempt_id=identity.id, staff_session_id="staff-session",
                                               host_generation=identity.host_generation, provider_session_ref="session:native")
        assert await lifecycle.drain_verified() == 0
        assert not await observe_exit(app, staff_session_id="staff-session", runtime_ref="run", observed_status="completed")
        await db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        assert not await observe_exit(app, staff_session_id="staff-session", runtime_ref="different-run", observed_status="completed")
        assert await observe_exit(app, staff_session_id="staff-session", runtime_ref="run", observed_status="completed")
        assert await lifecycle.drain_verified() == 1
        assert (await lifecycle.preview("task", "task"))["cancel_state"] == "drained"
        assert (await db.fetchone("SELECT count(*) FROM runtime_exit_observations"))[0] == 1
    finally:
        store.release()


async def test_unknown_native_run_prevents_overlap_and_old_host_cannot_observe_exit(db: Database) -> None:
    store, _, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await native_session(db)
        await admit_native_run(app, "staff-session", "native", "run")
        with pytest.raises(ControlDenied, match="previous native run"):
            await admit_native_run(app, "staff-session", "native", "later-run")
        await db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        store.release()
        store.acquire()
        await store.boot()
        assert not await observe_exit(app, staff_session_id="staff-session", runtime_ref="run", observed_status="completed")
        assert not await db.fetchall("SELECT * FROM runtime_exit_observations")
    finally:
        store.release()


async def test_goal_replacement_keeps_old_physical_work_owned_until_its_exact_exit(db: Database) -> None:
    store, identity, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    operator = Principal.operator({"via": "cookie", "user_id": 1})
    try:
        await db.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                         " VALUES ('project',1,'Original objective','operator','now')")
        await native_session(db)
        await admit_native_run(app, "staff-session", "native", "run")
        async with db.transaction() as conn:
            await apply_goal_revision(conn, project_id="project", expected_goal_revision=1,
                                      body="A revised objective", root_task_ids=["task"], origin_kind="operator", origin_ref="goal-edit",
                                      principal=operator, control=ControlStore(db))
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = 'task'"))[0] is None
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = ?", (identity.id,)))[0] == "superseded"
        staff = StaffStore(db)
        await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                         " VALUES ('other-worker','project','Another worker','daedalus','operator','now')")
        await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                         " VALUES ('replacement','other-worker','daedalus','task','now','now')")
        member, replacement = await staff.get("other-worker"), await staff.session("replacement")
        task = BoardTask("task", "Work", "doing", project_id="project", assignee_staff_id="other-worker")
        with pytest.raises(ControlDenied, match="host-observed runtime exit"):
            await prepare_attempt(app, operator, member, task, replacement, fence_token="r" * 32)
        assert (await db.fetchone("SELECT count(*) FROM execution_attempts"))[0] == 1
        assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 1
        await db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        assert await observe_exit(app, staff_session_id="staff-session", runtime_ref="run", observed_status="completed")
        successor = await prepare_attempt(app, operator, member, task, replacement, fence_token="r" * 32)
        assert successor.id != identity.id
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = 'task'"))[0] == successor.id
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = ?", (identity.id,)))[0] == "superseded"
    finally:
        store.release()


async def test_cancellation_scope_cannot_expand_after_preview(db: Database) -> None:
    store, _, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store, extensions={"effects": SimpleNamespace(notify=lambda: None)})
    lifecycle = Lifecycle(app)
    try:
        preview = await lifecycle.preview("task", "task")
        await db.execute("UPDATE lifecycle_owners SET cancel_state = 'unknown' WHERE child_id = 'attempt'")
        with pytest.raises(ControlConflict, match="changed after"):
            await lifecycle.cancel_command(Principal.operator({"via": "token", "user_id": 1}), "task", "task", "stop owned work",
                                           expected_entity_revision=preview["entity_revision"], expected_source_revision=1,
                                           preview_fingerprint=preview["preview_fingerprint"], client_operation_id="stop")
        assert (await db.fetchone("SELECT cancel_state FROM lifecycle_parents"))[0] == "active"
        assert not await db.fetchall("SELECT * FROM effect_outbox")
    finally:
        store.release()


async def test_cli_exited_row_and_different_daemon_do_not_release_attempt(db: Database) -> None:
    store, _, _ = await owner(db)
    app = SimpleNamespace(db=db, executions=store)
    try:
        await db.execute("UPDATE staff_sessions SET kind = 'cli',terminal_id = 'terminal' WHERE id = 'staff-session'")
        await db.execute("UPDATE execution_attempts SET runtime_kind = 'cli',provider_session_ref = 'terminal:terminal',"
                         "runtime_instance = 'daemon-one',state = 'recovering'")
        await db.execute("INSERT INTO terminals(id,env,owner_kind,cwd,status,created_at,ptyd_instance)"
                         " VALUES ('terminal','container','staff','/tmp','exited','now','daemon-one')")
        assert not await observe_exit(app, staff_session_id="staff-session", runtime_ref="terminal",
                                      observed_status="exited", runtime_instance="daemon-one")
        await db.execute("INSERT INTO terminal_exit_observations VALUES ('terminal','daemon-two','now')")
        assert not await observe_exit(app, staff_session_id="staff-session", runtime_ref="terminal",
                                      observed_status="exited", runtime_instance="daemon-two")
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "recovering"
        await db.execute("INSERT INTO terminal_exit_observations VALUES ('terminal','daemon-one','now')")
        assert await observe_exit(app, staff_session_id="staff-session", runtime_ref="terminal",
                                  observed_status="exited", runtime_instance="daemon-one")
        assert (await db.fetchone("SELECT state FROM execution_attempts"))[0] == "cancelled"
        assert (await db.fetchone("SELECT count(*) FROM runtime_exit_observations"))[0] == 1
    finally:
        store.release()
