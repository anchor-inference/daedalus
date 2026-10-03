"""A host-owned worker result remains readable and replayable after execution closes."""

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
async def test_staff_report_preserves_original_and_scoped_replay(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    executions = ExecutionStore(db)
    executions.acquire()
    bus = EventBus(db)
    await bus.start()
    try:
        await executions.boot()
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                         " VALUES ('task1','Task','doing',3,'project1','2026-01-01','2026-01-01')")
        await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                         " snapshot_json,created_at) VALUES ('task1',1,'operator','',?, '2026-01-01')",
                         ('{"requirements":[],"checklist":[],"acceptance":"",'
                          '"depends_on":[],"brief":{},"folder_id":null,"file_ids":[]}',))
        await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                         " VALUES ('member1','project1','Member','daedalus','operator','2026-01-01')")
        await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                         " VALUES ('staffsession1','member1','daedalus','task1','2026-01-01','2026-01-01')")
        control = ControlStore(db)
        operator = Principal.operator({"via": "token", "user_id": 1})
        grant = await control.issue_grant(operator, Principal("staff:member1", "agent"),
                                          Scope("project", "project1"), operations=["result.submit"],
                                          effects=[], task_id="task1",
                                          expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        worker = Principal("staff:member1", "agent", grant["grant_id"], grant["generation"])
        async with db.transaction() as conn:
            await executions.create(conn, attempt_id="attempt1", task_id="task1",
                                    contract_revision=1, launcher=operator, worker=worker,
                                    staff_session_id="staffsession1", runtime_kind="daedalus",
                                    fence_token="a-long-host-secret-fence-token")

        async def worktree_of(session: object) -> None:
            return None

        live = SimpleNamespace(id="staffsession1", session_id="native-session",
                               staff=SimpleNamespace(id="member1", project_id="project1",
                                                     harness="daedalus", name="Member"),
                               session=SimpleNamespace(task_id="task1", session_id="native-session"))
        manager = SimpleNamespace(files=SimpleNamespace(blobs=FileBlobStore(tmp_path / "blobs")), bus=bus)
        app = SimpleNamespace(db=db, executions=executions, manager=manager,
                              extensions={"staff": SimpleNamespace(worktree_of=worktree_of)})
        service = StaffReportService(app)
        original = "Result " + "x" * 70000
        first, event = await service.submit(live, "done", original, call_id="tool-call-1")
        assert event is not None
        replay, replayed_event = await service.submit(live, "done", original, call_id="tool-call-1")
        assert replay == first and replayed_event is None
        assert await service.original("project1", "task1", first["report_id"]) == original.encode()
        with pytest.raises(KeyError):
            await service.original("another-project", "task1", first["report_id"])
        with pytest.raises(ControlConflict):
            await service.submit(live, "done", original + " changed", call_id="tool-call-1")
        await control.revoke_grant(operator, grant["grant_id"], reason="worker released")
        with pytest.raises(ControlDenied):
            await service.submit(live, "done", original, call_id="tool-call-1")
    finally:
        await bus.close()
        executions.release()
        await db.close()
