"""A report, ended row or stale runtime cannot stand in for physical exit evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.database import Database
from daedalus.stores.lifecycle import record_owned_exit
from tests.unit.test_execution_ownership import owner


async def native_session(db: Database) -> None:
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at,metadata)"
                     " VALUES ('native','tenant','project','Work','now','now',?)",
                     (json.dumps({"staff_session_id": "staff-session"}),))
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('run','tenant','native','running','now','now')")


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
