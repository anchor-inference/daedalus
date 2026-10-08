"""The orchestrator can judge an exact result only with a host-issued task grant."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.config import ORCHESTRATOR_ONLY_TOOLS
from daedalus.extensions.orchestrator_contract import review_result
from daedalus.extensions.orchestrator_domain import submit_result
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.control import ControlStore, Principal, Scope
from daedalus.stores.database import Database
from daedalus.tools.orchestrator import TOOLS


@pytest.mark.asyncio
async def test_result_tool_replaces_accept_and_requires_host_grant(tmp_path: Path) -> None:
    assert "ReviewResult" in ORCHESTRATOR_ONLY_TOOLS
    assert "Accept" not in ORCHESTRATOR_ONLY_TOOLS
    assert "ReviewResult" in {tool().name for tool in TOOLS if callable(tool)}
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01',?)",
                         (json.dumps({"orchestrator": {"enabled": True, "session_id": "session1"}}),))
        await db.execute("INSERT INTO sessions(id,tenant_id,project_id,metadata,created_at,last_message_at)"
                         " VALUES ('session1','tenant','project1',?,'2026-01-01','2026-01-01')",
                         (json.dumps({"orchestrator_of": "project1"}),))
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                         " created_at,updated_at,project_id,brief_json) VALUES"
                         " ('task1','Task','review',3,'','[]','[]','2026-01-01','2026-01-01','project1','{}')")
        await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                         " snapshot_json,created_at) VALUES"
                         " ('task1',1,'operator','','{\"requirements\":[],\"checklist\":[],\"brief\":{}}','2026-01-01')")
        report = "Not ready"
        async with db.transaction() as conn:
            await submit_result(conn, result_id="result1", task_id="task1", attempt_id=None,
                                contract_revision=1, outcome="partial", original_text=report,
                                original_blob_ref=None, original_digest=hashlib.sha256(report.encode()).hexdigest(),
                                original_size_bytes=len(report), actor_id="staff:member1",
                                manifest_ids=[], checks=[], limitations=["test gap"])

        async def current(session_id: str):  # type: ignore[no-untyped-def]
            assert session_id == "session1"
            return SimpleNamespace(id="project1"), None

        class Board:
            async def get(self, task_id: str, *, actor: str):
                assert task_id == "task1" and actor == "session1"
                return {"id": "task1", "project_id": "project1"}

        app = SimpleNamespace(extensions={})
        orch = SimpleNamespace(board=Board(), manager=SimpleNamespace(db=db), app=app, current=current)
        project = SimpleNamespace(id="project1")
        inspected = json.loads(await review_result(orch, project, "session1", task_id="task1"))
        assert inspected["results"][0]["result_id"] == "result1"
        revision = inspected["contract"]["entity_revision"]
        with pytest.raises(Refused, match="authority is unavailable"):
            await review_result(orch, project, "session1", task_id="task1", op="verdict",
                                result_id="result1", expected_entity_revision=revision,
                                client_operation_id="call-one", verification="failed", reason="Missing tests")
        control = ControlStore(db)
        grant = await control.issue_grant(Principal.operator({"via": "token", "user_id": 1}),
                                          Principal("orchestrator:session1", "agent"),
                                          Scope("project", "project1"), operations=["review.verdict"], effects=[],
                                          task_id="task1",
                                          expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())

        async def authority(*, session_id: str, project_id: str, task_id: str, operation: str) -> Principal:
            assert (session_id, project_id, task_id, operation) == (
                "session1", "project1", "task1", "review.verdict")
            return Principal("orchestrator:session1", "agent", grant["grant_id"], grant["generation"])

        app.extensions["orchestrator_review_authority"] = authority
        result = json.loads(await review_result(orch, project, "session1", task_id="task1", op="verdict",
                                                result_id="result1", expected_entity_revision=revision,
                                                client_operation_id="call-one", verification="failed",
                                                reason="Missing tests"))
        assert result["verification"] == "failed" and result["accepted"] is False
        assert json.loads(await review_result(orch, project, "session1", task_id="task1", op="verdict",
                                              result_id="result1", expected_entity_revision=revision,
                                              client_operation_id="call-one", verification="failed",
                                              reason="Missing tests")) == result
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_report_with_no_branch_is_accepted_and_its_card_finished(tmp_path: Path) -> None:
    """A diagnosis has no check to bind, so reading its report is the review and accepting it closes the card."""
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01',?)",
                         (json.dumps({"orchestrator": {"enabled": True, "session_id": "session1"}}),))
        await db.execute("INSERT INTO sessions(id,tenant_id,project_id,metadata,created_at,last_message_at)"
                         " VALUES ('session1','tenant','project1',?,'2026-01-01','2026-01-01')",
                         (json.dumps({"orchestrator_of": "project1"}),))
        for task_id, branch in (("task1", None), ("task2", "agent/ops/fix")):
            await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                             " created_at,updated_at,project_id,brief_json,branch) VALUES"
                             " (?,'Task','review',3,'','[{\"id\":\"C1\",\"text\":\"answers 401\"}]','[]',"
                             "'2026-01-01','2026-01-01','project1','{}',?)", (task_id, branch))
            await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                             " snapshot_json,created_at) VALUES (?,1,'operator','',"
                             "'{\"requirements\":[],\"checklist\":[{\"id\":\"C1\",\"text\":\"answers 401\"}],\"brief\":{}}',"
                             "'2026-01-01')", (task_id,))
            report = "Nothing is broken: both domains answer 401 in 0.03 s."
            async with db.transaction() as conn:
                await submit_result(conn, result_id=f"result-{task_id}", task_id=task_id, attempt_id=None,
                                    contract_revision=1, outcome="complete", original_text=report,
                                    original_blob_ref=None, original_digest=hashlib.sha256(report.encode()).hexdigest(),
                                    original_size_bytes=len(report), actor_id="staff:member1",
                                    manifest_ids=[], checks=[], limitations=[],
                                    allow_empty_manifest=True)

        async def current(session_id: str):  # type: ignore[no-untyped-def]
            return SimpleNamespace(id="project1"), None

        class Board:
            async def get(self, task_id: str, *, actor: str):
                return {"id": task_id, "project_id": "project1"}

        grant = await ControlStore(db).issue_grant(Principal.operator({"via": "token", "user_id": 1}),
                                                   Principal("orchestrator:session1", "agent"),
                                                   Scope("project", "project1"), operations=["review.verdict"],
                                                   effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())

        async def authority(*, session_id: str, project_id: str, task_id: str, operation: str) -> Principal:
            assert operation == "review.verdict"
            return Principal("orchestrator:session1", "agent", grant["grant_id"], grant["generation"])

        orch = SimpleNamespace(board=Board(), manager=SimpleNamespace(db=db), current=current,
                               app=SimpleNamespace(extensions={"orchestrator_review_authority": authority}))
        project = SimpleNamespace(id="project1")

        async def accept(task_id: str, call: str, reason: str) -> dict:  # type: ignore[type-arg]
            revision = json.loads(await review_result(orch, project, "session1", task_id=task_id))["contract"]["entity_revision"]
            return json.loads(await review_result(orch, project, "session1", task_id=task_id, op="accept",
                                                  result_id=f"result-{task_id}", expected_entity_revision=revision,
                                                  client_operation_id=call, reason=reason))

        with pytest.raises(Refused, match="in a sentence"):
            await accept("task1", "call-short", "ok")
        with pytest.raises(Refused, match="branch work"):
            await accept("task2", "call-branch", "The report shows both domains answer 401 quickly.")
        accepted = await accept("task1", "call-one", "The report shows both domains answer 401 quickly.")
        assert accepted["acceptance_state"] == "accepted" and accepted["status"] == "done"
        row = await db.fetchone("SELECT status,acceptance_state,accepted_result_id,checklist FROM board_tasks WHERE id='task1'")
        assert (row["status"], row["acceptance_state"], row["accepted_result_id"]) == ("done", "accepted", "result-task1")
        assert all(item["done"] for item in json.loads(row["checklist"]))
        verdict = await db.fetchone("SELECT verification,accepted,reason FROM review_verdicts WHERE result_id='result-task1'")
        assert verdict["verification"] == "verified" and verdict["accepted"] == 1
        assert verdict["reason"].startswith("report read: ")
    finally:
        await db.close()
