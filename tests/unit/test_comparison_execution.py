"""Funded alternatives own separate attempts without changing the task singleton."""

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from daedalus.extensions.staff_results import StaffReportService
from daedalus.host.events import EventBus
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.comparisons import create_group
from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


async def test_two_funded_attempts_bind_exact_slots_without_projecting_a_winner(db: Database, tmp_path) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO project_folders(id,project_id,path,label,env,created_at)"
                     " VALUES ('folder','project',?,'Source','host','2026-01-01')", (str(tmp_path),))
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,folder_id,created_at,updated_at)"
                     " VALUES ('task','Compare','todo',3,'project','folder','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES ('task',1,'operator','','{}','2026-01-01')")
    store = ExecutionStore(db)
    store.acquire()
    try:
        generation = await store.boot()
        async with db.transaction() as conn:
            await create_group(conn, group_id="group", task_id="task", contract_revision=1,
                               budget_cap_microusd=2000, actor_id=OPERATOR.actor_id)
        workers = []
        for slot in (1, 2):
            staff_id = f"worker{slot}"
            session_id = f"session{slot}"
            await db.execute("INSERT INTO staff(id,project_id,name,harness,isolation,created_by,created_at)"
                             " VALUES (?,'project',?,'daedalus','worktree','operator','2026-01-01')",
                             (staff_id, staff_id))
            await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,folder_id,status_at,started_at,"
                             "worktree_path,branch) VALUES (?,?,'daedalus','task','folder','2026-01-01',"
                             "'2026-01-01',?,?)",
                             (session_id, staff_id, str(tmp_path / f"worker{slot}"), f"branch{slot}"))
            grant = await ControlStore(db).issue_grant(
                OPERATOR, Principal(f"staff:{staff_id}", "agent"), Scope("project", "project"),
                operations=["result.submit"], effects=[], task_id="task",
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
            workers.append(Principal(f"staff:{staff_id}", "agent", grant["grant_id"], grant["generation"]))
            await db.execute("INSERT INTO comparison_funding_slots(id,group_id,slot,staff_id,project_id,task_id,"
                             "contract_revision,host_generation,provider_id,model,allowance_microusd,rate_version,"
                             "quote_json,state,created_at) VALUES (?, 'group', ?, ?, 'project','task',1,1,"
                             "'local','model',1000,?,'{}','held','2026-01-01')",
                             (f"slot{slot}", slot, staff_id, "a" * 64))
        assert generation == 1
        async with db.transaction() as conn:
            first = await store.create(conn, attempt_id="attempt1", task_id="task", contract_revision=1,
                                       launcher=OPERATOR, worker=workers[0], staff_session_id="session1",
                                       runtime_kind="daedalus", fence_token=secrets.token_urlsafe(32),
                                       comparison_slot_id="slot1")
            assert await store.check_staff(conn, "session1") == first
        assert (await db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id='task'"))[0] is None
        with pytest.raises(ControlDenied, match="funded isolated slot"):
            async with db.transaction() as conn:
                await store.create(conn, attempt_id="wrong", task_id="task", contract_revision=1,
                                   launcher=OPERATOR, worker=workers[1], staff_session_id="session2",
                                   runtime_kind="daedalus", fence_token=secrets.token_urlsafe(32),
                                   comparison_slot_id="slot1")
        async with db.transaction() as conn:
            second = await store.create(conn, attempt_id="attempt2", task_id="task", contract_revision=1,
                                        launcher=OPERATOR, worker=workers[1], staff_session_id="session2",
                                        runtime_kind="daedalus", fence_token=secrets.token_urlsafe(32),
                                        comparison_slot_id="slot2")
            assert await store.check_staff(conn, "session2") == second
        task = await db.fetchone("SELECT status,current_attempt_id,branch,accepted_result_id"
                                 " FROM board_tasks WHERE id='task'")
        assert dict(task) == {"status": "todo", "current_attempt_id": None,
                              "branch": None, "accepted_result_id": None}
        members = await db.fetchall("SELECT slot,attempt_id FROM comparison_group_attempts ORDER BY slot")
        assert [(row["slot"], row["attempt_id"]) for row in members] == [(1, "attempt1"), (2, "attempt2")]
        bus = EventBus(db)
        await bus.start()
        try:
            async def no_worktree(session: object) -> None:
                return None

            team = SimpleNamespace(worktree_of=no_worktree)
            manager = SimpleNamespace(bus=bus, files=SimpleNamespace(blobs=FileBlobStore(tmp_path / "blobs")),
                                      locator_services=lambda _: None)
            app = SimpleNamespace(db=db, executions=store, manager=manager, extensions={"staff": team})
            service = StaffReportService(app)
            for slot in (1, 2):
                live = SimpleNamespace(id=f"session{slot}", session_id=f"native{slot}",
                                       session=SimpleNamespace(task_id="task", session_id=f"native{slot}"),
                                       staff=SimpleNamespace(id=f"worker{slot}", project_id="project",
                                                             harness="daedalus", name=f"Worker {slot}"))
                response, event = await service.submit(live, "done", f"Alternative {slot} complete",
                                                       call_id=f"comparison:{slot}")
                assert response["result_id"] and event is not None
                replayed, repeated_event = await service.submit(live, "done", f"Alternative {slot} complete",
                                                                call_id=f"comparison:{slot}")
                assert replayed == response and repeated_event is None
                task = await db.fetchone("SELECT status,current_attempt_id,acceptance_state"
                                         " FROM board_tasks WHERE id='task'")
                assert dict(task) == {"status": "todo", "current_attempt_id": None,
                                      "acceptance_state": ""}
            receipts = await db.fetchall("SELECT attempt_id FROM result_receipts ORDER BY attempt_id")
            assert [row["attempt_id"] for row in receipts] == ["attempt1", "attempt2"]
        finally:
            await bus.close()
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        store.release()
