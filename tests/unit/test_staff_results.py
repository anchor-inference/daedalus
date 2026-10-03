"""Staff hand-in preserves full bytes and closes only its host-owned attempt."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.extensions.orchestrator_domain import OrchestratorDomain
from daedalus.extensions.staff_results import submit_staff_report
from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.files import StoredFile


class Files:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, data: bytes, *, name: str, mime: str, origin: str, origin_ref: str,
                  scope: str, actor: str) -> StoredFile:
        digest = hashlib.sha256(data).hexdigest()
        file_id = digest[:12]
        await self.db.execute("INSERT OR IGNORE INTO files(id,name,mime,size,sha256,origin,origin_ref,created_at)"
                              " VALUES (?,?,?,?,?,?,?,'2026-01-01')",
                              (file_id, name, mime, len(data), digest, origin, origin_ref))
        return StoredFile(file_id, name, mime, len(data), digest, origin, origin_ref, "2026-01-01")

    async def attach_to_task(self, task_id: str, files: list[StoredFile], *, actor: str) -> None:
        for file in files:
            await self.db.execute("INSERT OR IGNORE INTO task_files(task_id,file_id,added_at,added_by)"
                                  " VALUES (?,?,'2026-01-01',?)", (task_id, file.id, actor))


async def test_staff_report_preserves_large_original_and_replays_after_completion(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    executions = ExecutionStore(db)
    executions.acquire()
    try:
        await executions.boot()
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                         " created_at,updated_at,project_id,brief_json) VALUES"
                         " ('task1','Task','doing',3,'','[]','[]','2026-01-01','2026-01-01','project1','{}')")
        await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                         " snapshot_json,created_at) VALUES"
                         " ('task1',1,'operator','','{\"requirements\":[],\"checklist\":[],\"acceptance\":\"\","
                         "\"depends_on\":[],\"brief\":{}}','2026-01-01')")
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
            identity = await executions.create(conn, attempt_id="attempt1", task_id="task1",
                                               contract_revision=1, launcher=operator, worker=worker,
                                               staff_session_id="staffsession1", runtime_kind="daedalus",
                                               fence_token="a-long-host-secret-fence-token")
            assert identity.id == "attempt1"
        live: Any = SimpleNamespace(id="staffsession1", staff=SimpleNamespace(id="member1", project_id="project1"),
                                    session=SimpleNamespace(task_id="task1"))
        app: Any = SimpleNamespace(db=db, executions=executions, manager=SimpleNamespace(files=Files(db)))
        original = "Result " + "x" * 70000
        first = await submit_staff_report(app, live, original, [], [], "tool-call-1")
        replay = await submit_staff_report(app, live, original, [], [], "tool-call-1")
        assert replay == first
        assert await OrchestratorDomain(db).original("task1", first["result_id"]) == original.encode()
        assert (await db.fetchone("SELECT state FROM execution_attempts WHERE id = 'attempt1'"))["state"] == "completed"
        assert (await db.fetchone("SELECT count(*) AS n FROM result_receipts WHERE task_id = 'task1'"))["n"] == 1
        with pytest.raises(ControlDenied):
            await submit_staff_report(app, live, original + " changed", [], [], "tool-call-1")
        await control.revoke_grant(operator, grant["grant_id"], reason="worker released")
        with pytest.raises(ControlDenied):
            await submit_staff_report(app, live, original, [], [], "tool-call-1")
    finally:
        executions.release()
        await db.close()
