"""A worker report and event are one fenced, replayable transaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.extensions.staff_results import StaffReportService
from daedalus.host.events import EventBus
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore


@pytest.mark.asyncio
async def test_done_report_keeps_full_original_and_replays_without_second_event(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    executions = ExecutionStore(db)
    executions.acquire()
    bus = EventBus(db)
    await bus.start()
    try:
        generation = await executions.boot()
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project','Work','2026-01-01','{}')")
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                         " VALUES ('task','Work','doing',3,'project','2026-01-01','2026-01-01')")
        await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                         " snapshot_json,created_at) VALUES ('task',1,'operator','',?, '2026-01-01')",
                         ('{"requirements":[],"checklist":[],"acceptance":"",'
                          '"depends_on":[],"brief":{},"folder_id":null,"file_ids":[]}',))
        await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                         " VALUES ('worker','project','Worker','daedalus','operator','2026-01-01')")
        await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                         " VALUES ('staff-session','worker','daedalus','task','2026-01-01','2026-01-01')")
        operator = Principal.operator({"via": "token", "user_id": 1})
        grant = await ControlStore(db).issue_grant(
            operator, Principal("staff:worker", "agent"), Scope("project", "project"),
            operations=["result.submit", "staff.report"], effects=[], task_id="task",
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        await db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                         " fence_token_hash,state,created_at,updated_at,actor_id,grant_id,grant_generation,"
                         " staff_session_id,runtime_kind) VALUES ('attempt','task',1,?,'aaaaaaaaaaaaaaaaaaaaaaaa',"
                         " 'running','2026-01-01','2026-01-01','staff:worker',?,1,'staff-session','daedalus')",
                         (generation, grant["grant_id"]))
        await db.execute("UPDATE board_tasks SET current_attempt_id = 'attempt' WHERE id = 'task'")
        manager = SimpleNamespace(bus=bus, files=SimpleNamespace(blobs=FileBlobStore(tmp_path / "blobs")),
                                  locator_services=lambda _: None)

        async def cwd_of(live: object) -> tuple[object, str]:
            return SimpleNamespace(env="test"), "/work"

        async def project(project_id: str) -> object:
            return SimpleNamespace(id=project_id)

        async def read_artifacts(*args: object, **kwargs: object) -> tuple[list[tuple[str, str, bytes]], list[str]]:
            return [("artifact.txt", "/work/artifact.txt", b"artifact bytes")], []

        async def worktree_of(session: object) -> None:
            return None

        team = SimpleNamespace(cwd_of=cwd_of, project=project,
                               handoff=SimpleNamespace(read_artifacts=read_artifacts),
                               worktree_of=worktree_of)
        app = SimpleNamespace(db=db, executions=executions, manager=manager, extensions={"staff": team})
        live = SimpleNamespace(id="staff-session", session_id="native-session",
                               session=SimpleNamespace(task_id="task", session_id="native-session"),
                               staff=SimpleNamespace(id="worker", project_id="project", harness="daedalus",
                                                     name="Worker"))
        service = StaffReportService(app)
        checkpoint, _ = await service.submit(live, "checkpoint", "Work in progress",
                                             artifacts=["artifact.txt"], call_id="call-zero")
        assert checkpoint["kind"] == "checkpoint"
        assert checkpoint["kept_files"][0]["name"] == "artifact.txt"
        assert (await db.fetchone("SELECT count(*) AS n FROM artifact_manifests WHERE task_id = 'task'"))["n"] == 1
        waiting, _ = await service.submit(live, "needs_input", "Need operator input", call_id="call-wait")
        assert waiting["kind"] == "needs_input"
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = 'attempt'"))["state"] == "waiting"
        with pytest.raises(ControlConflict):
            await service.submit(live, "done", "different meaning", call_id="call-zero")
        with pytest.raises(ValueError, match="exceed their limits"):
            await service.submit(live, "checkpoint", "too many refs", artifacts=["x"] * 21,
                                 call_id="call-large")
        original_persist = bus.persist_in

        async def failed_event(*args: object, **kwargs: object) -> object:
            raise RuntimeError("event insert failed")

        monkeypatch.setattr(bus, "persist_in", failed_event)
        with pytest.raises(RuntimeError, match="event insert failed"):
            await service.submit(live, "checkpoint", "transaction must roll back", call_id="call-failed")
        monkeypatch.setattr(bus, "persist_in", original_persist)
        assert (await db.fetchone("SELECT count(*) AS n FROM staff_report_records"))["n"] == 2
        assert (await db.fetchone("SELECT count(*) AS n FROM operation_receipts"
                                  " WHERE operation_kind = 'staff.report'"))["n"] == 2
        async def dirty_worktree(session: object) -> object:
            return SimpleNamespace(path=Path("/work/tree"), branch="worker")

        async def dirty_status(worktree: object) -> object:
            return SimpleNamespace(dirty=True)

        team.worktree_of = dirty_worktree
        team.worktrees = SimpleNamespace(status=dirty_status)
        with pytest.raises(ValueError, match="uncommitted changes"):
            await service.submit(live, "done", "not committed", call_id="call-dirty")
        team.worktree_of = worktree_of
        original = "Result complete.\n" + "details " * 10000
        first, event = await service.submit(live, "done", original, call_id="call-one")
        assert event is not None and first["result_id"] == first["report_id"]
        assert await service.original("project", "task", first["report_id"]) == original.encode("utf-8")
        second, replayed_event = await service.submit(live, "done", original, call_id="call-one")
        assert second == first and replayed_event is None
        assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'staff.report'"))["n"] == 3
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = 'attempt'"))["state"] == "completed"
        with pytest.raises(ControlConflict):
            await service.submit(live, "done", "changed report", call_id="call-one")
        await ControlStore(db).revoke_grant(operator, grant["grant_id"], reason="withdrawn")
        with pytest.raises(ControlDenied):
            await service.submit(live, "done", original, call_id="call-one")
    finally:
        await bus.close()
        executions.release()
        await db.close()
