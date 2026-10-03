"""Bounded comparisons retain uncertainty until both alternatives have evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from daedalus.extensions.comparisons import (
    AdmissionProof,
    ComparisonRefused,
    admit_attempt,
    choose_result,
    comparison_readiness,
    create_group,
)
from daedalus.extensions.orchestrator_domain import record_verdict, submit_result
from daedalus.stores.database import Database


@pytest.fixture
async def comparison_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                     "created_at,updated_at,brief_json) VALUES"
                     "('task1','Compare','todo',3,'','[]','[]','2026-01-01','2026-01-01','{}')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES ('task1',1,'operator','','{}','2026-01-01')")
    yield db
    await db.close()


async def test_group_requires_priced_cap_and_exact_contract(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        with pytest.raises(ComparisonRefused, match="positive priced"):
            await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                               budget_cap_microusd=0, actor_id="operator")
        with pytest.raises(ComparisonRefused, match="current immutable contract"):
            await create_group(conn, group_id="group1", task_id="task1", contract_revision=2,
                               budget_cap_microusd=1000, actor_id="operator")
        created = await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                                     budget_cap_microusd=1000, actor_id="operator")
    assert created["state"] == "planned"
    with pytest.raises(ComparisonRefused, match="already has"):
        async with comparison_db.transaction() as conn:
            await create_group(conn, group_id="group2", task_id="task1", contract_revision=1,
                               budget_cap_microusd=1000, actor_id="operator")


async def test_group_without_two_proven_results_stays_blocked(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        readiness = await comparison_readiness(conn, "group1", observed_cost=lambda _conn, _attempt: None,
                                               physical_exit=lambda _conn, _attempt: False)
    assert readiness["state"] == "planned"
    assert readiness["blockers"] == ["alternatives_missing"]
    assert readiness["selected_result_id"] is None
    assert await comparison_db.fetchall("PRAGMA foreign_key_check") == []


async def test_alternative_result_does_not_become_the_task_candidate(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        await conn.execute("UPDATE comparison_groups SET state = 'active' WHERE id = 'group1'")
        await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                           "fence_token_hash,state,created_at,updated_at) VALUES"
                           "('attempt1','task1',1,1,'digest','completed','2026-01-01','2026-01-01')")
        await conn.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,reserved_microusd)"
                           " VALUES ('group1','attempt1',1,500)")
        original = "First alternative"
        encoded = original.encode()
        response = await submit_result(conn, result_id="result1", task_id="task1", attempt_id="attempt1",
                                       contract_revision=1, outcome="partial", original_text=original,
                                       original_blob_ref=None, original_digest=hashlib.sha256(encoded).hexdigest(),
                                       original_size_bytes=len(encoded), actor_id="worker", manifest_ids=[],
                                       checks=[], limitations=["needs comparison"])
        verdict = await record_verdict(conn, verdict_id="verdict1", result_id="result1",
                                       reviewer_actor_id="reviewer", verification="failed", accepted=False,
                                       head="head1", base="base1", environment_digest=None,
                                       evidence_ids=[], reason="alternative incomplete")
    assert response["acceptance_state"] == "comparison_pending"
    assert verdict["verification"] == "failed"
    task = await comparison_db.fetchone("SELECT current_attempt_id,acceptance_state FROM board_tasks WHERE id = 'task1'")
    assert task is not None and task["current_attempt_id"] is None
    assert task["acceptance_state"] != "handed_in"


async def test_approved_first_alternative_does_not_approve_shared_task(comparison_db: Database) -> None:
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                           "fence_token_hash,state,created_at,updated_at) VALUES"
                           "('attempt1','task1',1,1,'digest','completed','2026-01-01','2026-01-01')")
        await conn.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,reserved_microusd)"
                           " VALUES ('group1','attempt1',1,500)")
        original = "First complete alternative"
        encoded = original.encode()
        content_digest = hashlib.sha256(encoded).hexdigest()
        await conn.execute("INSERT INTO artifact_manifests(id,task_id,artifact_kind,artifact_key,"
                           "artifact_revision,digest,size_bytes,created_at)"
                           " VALUES ('manifest1','task1','document','report',1,?,?, '2026-01-01')",
                           (content_digest, len(encoded)))
        await submit_result(conn, result_id="result1", task_id="task1", attempt_id="attempt1",
                            contract_revision=1, outcome="complete", original_text=original,
                            original_blob_ref=None, original_digest=content_digest,
                            original_size_bytes=len(encoded), actor_id="worker",
                            manifest_ids=["manifest1"], checks=[], limitations=[])
        await conn.execute("INSERT INTO review_evidence(id,result_id,contract_revision,criterion_id,command,"
                           "exit_code,manifest_digest_before,manifest_digest_after,observed_at)"
                           " VALUES ('evidence1','result1',1,'check','test',0,?,?, '2026-01-01')",
                           (content_digest, content_digest))
        response = await record_verdict(conn, verdict_id="verdict1", result_id="result1",
                                        reviewer_actor_id="reviewer", verification="verified", accepted=True,
                                        head="head1", base="base1", environment_digest=None,
                                        evidence_ids=["evidence1"], reason="passes")
    assert response["accepted"] is True
    task = await comparison_db.fetchone("SELECT status,current_attempt_id,acceptance_state"
                                        " FROM board_tasks WHERE id = 'task1'")
    assert dict(task) == {"status": "todo", "current_attempt_id": None, "acceptance_state": ""}


async def test_second_reservation_cannot_exceed_cap(comparison_db: Database) -> None:
    await comparison_db.execute("INSERT INTO projects(id,name,created_at,settings)"
                                " VALUES ('project1','Example','2026-01-01','{}')")
    await comparison_db.execute("UPDATE board_tasks SET project_id = 'project1' WHERE id = 'task1'")
    async with comparison_db.transaction() as conn:
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        for slot in (1, 2):
            staff_id = f"staff{slot}"
            session_id = f"session{slot}"
            attempt_id = f"attempt{slot}"
            await conn.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                               " VALUES (?,?,?,'claude','operator','2026-01-01')",
                               (staff_id, "project1", staff_id))
            await conn.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at,"
                               "worktree_path,branch) VALUES (?,?, 'cli','task1','2026-01-01','2026-01-01',?,?)",
                               (session_id, staff_id, f"/tmp/alternative-{slot}", f"branch-{slot}"))
            await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                               "fence_token_hash,state,created_at,updated_at,staff_session_id)"
                               " VALUES (?,'task1',1,1,'digest','queued','2026-01-01','2026-01-01',?)",
                               (attempt_id, session_id))

        async def budget(_conn, _group, _attempt, _proof) -> bool:
            return True

        async def capacity(_conn, _attempt, _slot) -> bool:
            return True

        def proof(slot: int) -> AdmissionProof:
            return AdmissionProof(f"reservation{slot}", 600, "rate1",
                                  hashlib.sha256(f"/tmp/alternative-{slot}".encode()).hexdigest(),
                                  f"capacity{slot}")

        first = await admit_attempt(conn, group_id="group1", attempt_id="attempt1", slot=1,
                                    proof=proof(1), verify_budget=budget, verify_capacity=capacity)
        assert first["reserved_microusd"] == 600
        with pytest.raises(ComparisonRefused, match="exceed"):
            await admit_attempt(conn, group_id="group1", attempt_id="attempt2", slot=2,
                                proof=proof(2), verify_budget=budget, verify_capacity=capacity)
    assert len(await comparison_db.fetchall("SELECT attempt_id FROM comparison_group_attempts")) == 1


async def test_selection_projects_the_reviewed_worktree_and_keeps_the_loser(comparison_db: Database) -> None:
    """A selected result must not leave the shared board pointing at the losing branch."""
    async with comparison_db.transaction() as conn:
        await conn.execute("INSERT INTO projects(id,name,created_at,settings)"
                           " VALUES ('project1','Example','2026-01-01','{}')")
        await conn.execute("INSERT INTO project_folders(id,project_id,path,env,created_at)"
                           " VALUES ('folder1','project1','/tmp/comparison-source','host','2026-01-01')")
        await conn.execute("UPDATE board_tasks SET project_id = 'project1',folder_id = 'folder1',"
                           " branch = 'loser-branch' WHERE id = 'task1'")
        await create_group(conn, group_id="group1", task_id="task1", contract_revision=1,
                           budget_cap_microusd=1000, actor_id="operator")
        await conn.execute("UPDATE comparison_groups SET state = 'active' WHERE id = 'group1'")
        for slot in (1, 2):
            staff_id = f"staff{slot}"
            session_id = f"session{slot}"
            attempt_id = f"attempt{slot}"
            path = f"/tmp/comparison-alternative-{slot}"
            await conn.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                               " VALUES (?, 'project1', ?, 'claude', 'operator', '2026-01-01')",
                               (staff_id, staff_id))
            await conn.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at,"
                               "folder_id,worktree_path,branch,base_ref) VALUES"
                               " (?,?,'cli','task1','2026-01-01','2026-01-01','folder1',?,?,?)",
                               (session_id, staff_id, path, f"branch-{slot}", "base"))
            await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                               "fence_token_hash,state,created_at,updated_at,staff_session_id)"
                               " VALUES (?,'task1',1,1,'digest','completed','2026-01-01','2026-01-01',?)",
                               (attempt_id, session_id))
            await conn.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,"
                               "reserved_microusd,worktree_identity) VALUES ('group1',?,?,400,?)",
                               (attempt_id, slot, hashlib.sha256(path.encode()).hexdigest()))
            body = f"Alternative {slot}"
            await conn.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,"
                               "original_text,original_digest,original_size_bytes,actor_id,created_at)"
                               " VALUES (?,'task1',1,?,'complete',?,?,?,'worker','2026-01-01')",
                               (f"result{slot}", attempt_id, body, hashlib.sha256(body.encode()).hexdigest(), len(body)))
        await conn.execute("INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,"
                           "verification,accepted,head,base,created_at)"
                           " VALUES ('verdict2','result2',1,'reviewer','verified',1,'head','base','2026-01-01')")
        await conn.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,project_id,actor_id,"
                           "operation_kind,client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
                           " VALUES ('op1','project','project1','project1','operator','comparison.choose',"
                           " 'choose1','digest',1,'committed','{}','2026-01-01')")

        async def cost(_conn, _attempt):
            return 200

        async def exited(_conn, _attempt):
            return True

        await conn.execute("UPDATE comparison_group_attempts SET worktree_identity = ?"
                           " WHERE group_id = 'group1' AND attempt_id = 'attempt2'", ("0" * 64,))
        with pytest.raises(ComparisonRefused, match="worktree"):
            await choose_result(conn, group_id="group1", result_id="result2", verdict_id="verdict2",
                                operation_receipt_id="op1", selection_receipt_id="selection1",
                                observed_cost=cost, physical_exit=exited)
        await conn.execute("UPDATE comparison_group_attempts SET worktree_identity = ?"
                           " WHERE group_id = 'group1' AND attempt_id = 'attempt2'",
                           (hashlib.sha256(b"/tmp/comparison-alternative-2").hexdigest(),))
        await conn.execute("SAVEPOINT newer_result")
        await conn.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,"
                           "original_text,original_digest,original_size_bytes,actor_id,created_at)"
                           " VALUES ('newer','task1',1,'attempt2','partial','new',?,?,"
                           " 'worker','2026-01-02')", (hashlib.sha256(b"new").hexdigest(), 3))
        with pytest.raises(ComparisonRefused, match="newer result"):
            await choose_result(conn, group_id="group1", result_id="result2", verdict_id="verdict2",
                                operation_receipt_id="op1", selection_receipt_id="selection1",
                                observed_cost=cost, physical_exit=exited)
        await conn.execute("ROLLBACK TO newer_result")
        await conn.execute("RELEASE newer_result")
        selected = await choose_result(conn, group_id="group1", result_id="result2", verdict_id="verdict2",
                                       operation_receipt_id="op1", selection_receipt_id="selection1",
                                       observed_cost=cost, physical_exit=exited)
    assert selected["state"] == "chosen"
    task = await comparison_db.fetchone("SELECT current_attempt_id,status,folder_id,branch,merge_state"
                                       " FROM board_tasks WHERE id = 'task1'")
    assert tuple(task) == ("attempt2", "review", "folder1", "branch-2", "proposed")
    assert len(await comparison_db.fetchall("SELECT id FROM result_receipts WHERE task_id = 'task1'")) == 2
    assert await comparison_db.fetchone("SELECT attempt_id FROM comparison_selection_receipts"
                                       " WHERE id = 'selection1'") is not None
