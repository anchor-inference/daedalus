"""Contract history, immutable report bytes, and exact-result acceptance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from daedalus.extensions.orchestrator_domain import (
    DomainConflict,
    OrchestratorDomain,
    OriginalReports,
    accept_result,
    add_artifact_manifest,
    add_review_comment,
    add_review_evidence,
    advance_workflow_step,
    apply_goal_revision,
    capture_contract_change,
    claim_handoff,
    configure_workflow,
    dependency_readiness,
    next_action_readiness,
    record_verdict,
    replace_contract,
    resolve_dependency,
    scope_impact_preview,
    set_next_action,
    submit_result,
    workflow_readiness,
)
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore


@pytest.fixture
async def domain_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute(
        "INSERT INTO board_tasks(id, title, status, priority, acceptance, checklist, depends_on,"
        " created_at, updated_at, brief_json) VALUES"
        " ('task1', 'Draft', 'review', 3, '', '[{\"text\":\"Clear result\",\"done\":false}]',"
        " '[]', '2026-01-01', '2026-01-01', '{}')"
    )
    await db.execute("INSERT INTO task_contract_versions(task_id, contract_revision, origin_kind, origin_ref,"
                     " snapshot_json, created_at) VALUES ('task1', 1, 'legacy', '',"
                     " '{\"requirements\":[],\"checklist\":[{\"id\":\"C1\",\"text\":\"Clear result\"}],"
                     "\"acceptance\":\"\",\"depends_on\":[],\"brief\":{}}', '2026-01-01')")
    yield db
    await db.close()


async def test_unverified_legacy_contract_projection(domain_db: Database) -> None:
    row = await domain_db.fetchone("SELECT snapshot_json FROM task_contract_versions WHERE task_id = 'task1'")
    assert row is not None
    assert json.loads(row["snapshot_json"])["checklist"][0]["text"] == "Clear result"
    assert await domain_db.fetchall("SELECT id FROM result_receipts") == []
    assert (await domain_db.fetchone("PRAGMA integrity_check"))[0] == "ok"
    assert await domain_db.fetchall("PRAGMA foreign_key_check") == []


async def test_editorial_edit_keeps_version_and_semantic_edit_invalidates_old_result(domain_db: Database) -> None:
    async with domain_db.transaction() as conn:
        original = await replace_contract(conn, task_id="task1", requirements=[],
                                          checks=[{"text": "Clear result"}], acceptance="", brief={},
                                          origin_kind="operator", origin_ref="message1", change_kind="editorial")
    assert original["contract_revision"] == 1
    async with domain_db.transaction() as conn:
        revised = await replace_contract(conn, task_id="task1", requirements=[{"text": "No invented facts", "kind": "quality"}],
                                         checks=[{"text": "Clear result"}], acceptance="", brief={},
                                         origin_kind="operator", origin_ref="message2", change_kind="semantic")
    assert revised["contract_revision"] == 2
    assert (await OrchestratorDomain(domain_db).contract("task1"))["requirements"][0]["kind"] == "quality"
    with pytest.raises(DomainConflict, match="semantically"):
        async with domain_db.transaction() as conn:
            await replace_contract(conn, task_id="task1", requirements=[], checks=[], acceptance="", brief={},
                                   origin_kind="operator", origin_ref="message3", change_kind="editorial")


async def test_result_review_and_acceptance_pin_exact_contract_and_evidence(domain_db: Database) -> None:
    original = b"Complete report"
    digest = hashlib.sha256(original).hexdigest()
    async with domain_db.transaction() as conn:
        await add_artifact_manifest(conn, manifest_id="manifest1", project_id=None, task_id="task1",
                                    artifact_kind="document", artifact_key="report", artifact_revision=1,
                                    digest=digest, size_bytes=len(original))
        report = await submit_result(conn, result_id="result1", task_id="task1", attempt_id=None,
                                     contract_revision=1, outcome="complete", original_text=original.decode(),
                                     original_blob_ref=None, original_digest=digest, original_size_bytes=len(original),
                                     actor_id="worker", manifest_ids=["manifest1"], checks=[], limitations=[])
    assert report["verification"] == "unverified"
    assert await OrchestratorDomain(domain_db).original("task1", "result1") == original
    async with domain_db.transaction() as conn:
        evidence = await add_review_evidence(conn, evidence_id="evidence1", result_id="result1", criterion_id="C1",
                                             command="check", exit_code=0, environment_digest="env1",
                                             manifest_digest_before=digest, manifest_digest_after=digest)
        assert evidence["verification"] == "verified"
        with pytest.raises(DomainConflict, match="worker"):
            await record_verdict(conn, verdict_id="self", result_id="result1", reviewer_actor_id="worker",
                                 verification="verified", accepted=True, head=None, base=None,
                                 environment_digest="env1", evidence_ids=["evidence1"], reason="")
        await conn.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,actor_id,operation_kind,"
                           " client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
                           " VALUES ('unrelated','global','global','reviewer','unrelated','call1','hash',1,'committed',"
                           " '{\"approved\":true,\"task_id\":\"task1\",\"result_id\":\"result1\",\"contract_revision\":1}',"
                           " '2026-01-01')")
        with pytest.raises(DomainConflict, match="worker"):
            await record_verdict(conn, verdict_id="self", result_id="result1", reviewer_actor_id="worker",
                                 verification="verified", accepted=True, head=None, base=None,
                                 environment_digest="env1", evidence_ids=["evidence1"], reason="",
                                 self_review_waiver_receipt_id="unrelated")
        await record_verdict(conn, verdict_id="verdict1", result_id="result1", reviewer_actor_id="reviewer",
                             verification="verified", accepted=True, head=None, base=None,
                             environment_digest="env1", evidence_ids=["evidence1"], reason="checked")
        await accept_result(conn, task_id="task1", result_id="result1", verdict_id="verdict1",
                            contract_revision=1, current_head=None, current_base=None)
    results = await OrchestratorDomain(domain_db).results("task1")
    assert results[0]["accepted"] is True
    assert results[0]["acceptance_state"] == "operator_approved"
    assert (await domain_db.fetchone("SELECT status FROM board_tasks WHERE id = 'task1'"))["status"] == "done"


async def test_approval_needs_evidence_for_each_requirement_as_well_as_each_check(domain_db: Database) -> None:
    original = b"Documented result"
    digest = hashlib.sha256(original).hexdigest()
    async with domain_db.transaction() as conn:
        revised = await replace_contract(conn, task_id="task1",
                                         requirements=[{"text": "No invented facts", "kind": "quality"}],
                                         checks=[{"id": "C1", "text": "Clear result"}], acceptance="", brief={},
                                         origin_kind="operator", origin_ref="request", change_kind="semantic")
        await add_artifact_manifest(conn, manifest_id="manifest1", project_id=None, task_id="task1",
                                    artifact_kind="document", artifact_key="report", artifact_revision=1,
                                    digest=digest, size_bytes=len(original))
        await submit_result(conn, result_id="result1", task_id="task1", attempt_id=None,
                            contract_revision=revised["contract_revision"], outcome="complete",
                            original_text=original.decode(), original_blob_ref=None, original_digest=digest,
                            original_size_bytes=len(original), actor_id="worker", manifest_ids=["manifest1"],
                            checks=[], limitations=[])
        await add_review_evidence(conn, evidence_id="check1", result_id="result1", criterion_id="C1",
                                  command="inspect", exit_code=0, environment_digest="env",
                                  manifest_digest_before=digest, manifest_digest_after=digest)
        with pytest.raises(DomainConflict, match="requirement"):
            await record_verdict(conn, verdict_id="incomplete", result_id="result1", reviewer_actor_id="reviewer",
                                 verification="verified", accepted=True, head=None, base=None,
                                 environment_digest="env", evidence_ids=["check1"], reason="checked")
        requirement = await conn.execute("SELECT id FROM task_requirements WHERE task_id = 'task1' AND state = 'active'")
        row = await requirement.fetchone()
        await requirement.close()
        assert row is not None
        await add_review_evidence(conn, evidence_id="requirement1", result_id="result1", criterion_id=row["id"],
                                  command="inspect", exit_code=0, environment_digest="env",
                                  manifest_digest_before=digest, manifest_digest_after=digest)
        await record_verdict(conn, verdict_id="complete", result_id="result1", reviewer_actor_id="reviewer",
                             verification="verified", accepted=True, head=None, base=None,
                             environment_digest="env", evidence_ids=["check1", "requirement1"], reason="checked")


async def test_changed_manifest_during_check_cannot_support_acceptance(domain_db: Database) -> None:
    original = b"report"
    digest = hashlib.sha256(original).hexdigest()
    async with domain_db.transaction() as conn:
        await add_artifact_manifest(conn, manifest_id="manifest1", project_id=None, task_id="task1",
                                    artifact_kind="document", artifact_key="report", artifact_revision=1,
                                    digest=digest, size_bytes=len(original))
        await submit_result(conn, result_id="result1", task_id="task1", attempt_id=None,
                            contract_revision=1, outcome="complete", original_text=original.decode(),
                            original_blob_ref=None, original_digest=digest, original_size_bytes=len(original),
                            actor_id="worker", manifest_ids=["manifest1"], checks=[], limitations=[])
        observation = await add_review_evidence(conn, evidence_id="changed", result_id="result1", criterion_id="C1",
                                                command="check", exit_code=0, environment_digest="env",
                                                manifest_digest_before=digest, manifest_digest_after="new-digest")
        assert observation["verification"] == "stale"
        with pytest.raises(DomainConflict, match="stale or failed"):
            await record_verdict(conn, verdict_id="verdict1", result_id="result1", reviewer_actor_id="reviewer",
                                 verification="verified", accepted=True, head=None, base=None,
                                 environment_digest="env", evidence_ids=["changed"], reason="")


async def test_late_blocking_comment_and_new_result_fence_acceptance(domain_db: Database) -> None:
    original = b"report"
    digest = hashlib.sha256(original).hexdigest()
    async with domain_db.transaction() as conn:
        await add_artifact_manifest(conn, manifest_id="manifest1", project_id=None, task_id="task1",
                                    artifact_kind="document", artifact_key="report", artifact_revision=1,
                                    digest=digest, size_bytes=len(original))
        await submit_result(conn, result_id="result1", task_id="task1", attempt_id=None,
                            contract_revision=1, outcome="complete", original_text=original.decode(),
                            original_blob_ref=None, original_digest=digest, original_size_bytes=len(original),
                            actor_id="worker", manifest_ids=["manifest1"], checks=[], limitations=[])
        await add_review_evidence(conn, evidence_id="evidence1", result_id="result1", criterion_id="C1",
                                  command="check", exit_code=0, environment_digest="env",
                                  manifest_digest_before=digest, manifest_digest_after=digest)
        await record_verdict(conn, verdict_id="verdict1", result_id="result1", reviewer_actor_id="reviewer",
                             verification="verified", accepted=True, head=None, base=None,
                             environment_digest="env", evidence_ids=["evidence1"], reason="checked")
        await add_review_comment(conn, comment_id="comment1", result_id="result1", author_actor_id="reviewer",
                                 source="operator", priority="blocking", body="Evidence needs another check")
        with pytest.raises(DomainConflict, match="blocking"):
            await accept_result(conn, task_id="task1", result_id="result1", verdict_id="verdict1",
                                contract_revision=1, current_head=None, current_base=None)
        await conn.execute("INSERT INTO review_comment_resolutions(id,comment_id,result_id,actor_id,resolution,reason,created_at)"
                           " VALUES ('resolution1','comment1','result1','reviewer','resolved','checked','2026-01-01')")
        await submit_result(conn, result_id="result2", task_id="task1", attempt_id=None,
                            contract_revision=1, outcome="complete", original_text=original.decode(),
                            original_blob_ref=None, original_digest=digest, original_size_bytes=len(original),
                            actor_id="worker", manifest_ids=["manifest1"], checks=[], limitations=[])
        with pytest.raises(DomainConflict, match="newer result"):
            await accept_result(conn, task_id="task1", result_id="result1", verdict_id="verdict1",
                                contract_revision=1, current_head=None, current_base=None)


async def test_duplicate_check_ids_rejected_and_reorder_preserves_ids(domain_db: Database) -> None:
    with pytest.raises(ValueError, match="unique"):
        async with domain_db.transaction() as conn:
            await replace_contract(conn, task_id="task1", requirements=[],
                                   checks=[{"id": "C1", "text": "First"}, {"id": "C1", "text": "Second"}],
                                   acceptance="", brief={}, origin_kind="operator", origin_ref="edit",
                                   change_kind="semantic")
    async with domain_db.transaction() as conn:
        await replace_contract(conn, task_id="task1", requirements=[],
                               checks=[{"id": "C1", "text": "Clear result"}, {"id": "C2", "text": "Other"}],
                               acceptance="", brief={}, origin_kind="operator", origin_ref="edit",
                               change_kind="semantic")
        await conn.execute("UPDATE board_tasks SET checklist = ? WHERE id = 'task1'",
                           ('[{"text":"Other","done":false},{"text":"Clear result","done":false}]',))
        await capture_contract_change(conn, "task1", origin_kind="operator")
    projected = await OrchestratorDomain(domain_db).contract("task1")
    assert [(item["id"], item["text"]) for item in projected["checklist"]] == [
        ("C2", "Other"), ("C1", "Clear result")]


async def test_operator_merge_effect_is_claimable_after_worker_attempt_completed(domain_db: Database) -> None:
    await domain_db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                            " fence_token_hash,state,created_at,updated_at)"
                            " VALUES ('attempt1','task1',1,1,'fence','completed','2026-01-01','2026-01-01')")
    await domain_db.execute("UPDATE board_tasks SET current_attempt_id = 'attempt1' WHERE id = 'task1'")
    principal = Principal.operator({"via": "token", "user_id": 1})
    control = ControlStore(domain_db)
    scope = Scope("global", "global")

    async def queue(conn: Any, mutation: Any) -> dict[str, Any]:
        action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="review.merge",
                                               operation="review.merge", payload={"task_id": "task1"},
                                               effects=("git.merge",), task_id="task1")
        return {"action_id": action_id}

    revision = await control.revision(scope, Entity("task", "task1"))
    queued = await control.mutate(principal, scope, "review.merge", "merge1", revision,
                                  Entity("task", "task1"), {"task_id": "task1"}, queue,
                                  effects=("git.merge",))
    claim = await OutboxStore(domain_db).claim(("review.merge",))
    assert claim is not None and claim.id == queued["action_id"]
    assert claim.attempt_id is None


async def test_legacy_dependency_requires_resolution_and_claim_is_single(domain_db: Database) -> None:
    await domain_db.execute("INSERT INTO board_tasks(id, title, status, priority, acceptance, checklist, depends_on,"
                            " created_at, updated_at, brief_json, contract_revision) VALUES"
                            " ('task2', 'Next', 'blocked', 3, '', '[]', '[]', '2026-01-01', '2026-01-01', '{}', 1)")
    await domain_db.execute("INSERT INTO task_contract_versions(task_id, contract_revision, origin_kind, origin_ref,"
                            " snapshot_json, created_at) VALUES ('task2', 1, 'legacy', '',"
                            " '{\"requirements\":[],\"checklist\":[],\"acceptance\":\"\",\"brief\":{}}', '2026-01-01')")
    await domain_db.execute("INSERT INTO task_dependency_edges(id, successor_task_id, predecessor_task_id, kind,"
                            " resolution_state, created_at) VALUES ('edge1', 'task2', 'task1', 'required',"
                            " 'unresolved_legacy', '2026-01-01')")
    await domain_db.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,actor_id,operation_kind,"
                            " client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
                            " VALUES ('waiver1','global','global','operator:1','dependency.waive','waiver',"
                            " 'digest',1,'committed','{}','2026-01-01')")
    async with domain_db.transaction() as conn:
        assert (await dependency_readiness(conn, "task2"))["edges"][0]["state"] == "unknown_legacy"
        with pytest.raises(DomainConflict, match="readiness"):
            await claim_handoff(conn, claim_id="claim1", task_id="task2", operation_id="waiver1", reservation_id=None)
        with pytest.raises(DomainConflict, match="waiver"):
            await resolve_dependency(conn, edge_id="edge1", resolution="waived")
        await resolve_dependency(conn, edge_id="edge1", resolution="waived", waiver_receipt_id="waiver1")
        successor = await conn.execute_fetchall("SELECT status FROM board_tasks WHERE id = 'task2'")
        assert successor[0]["status"] == "todo"
        first = await claim_handoff(conn, claim_id="claim1", task_id="task2", operation_id="waiver1", reservation_id="r1")
        second = await claim_handoff(conn, claim_id="claim2", task_id="task2", operation_id="waiver1", reservation_id="r2")
        assert first["claim_id"] == second["claim_id"] == "claim1"
        assert second["replayed"] is True


async def test_workflow_rejects_cycle_and_uses_typed_gates(domain_db: Database) -> None:
    await domain_db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = 'task1'")
    async with domain_db.transaction() as conn:
        steps = [{"id": "work", "kind": "work"}, {"id": "review", "kind": "review", "gate": {"not_before": "2999-01-01T00:00:00+00:00"}}]
        with pytest.raises(ValueError, match="cycle"):
            await configure_workflow(conn, task_id="task1", steps=steps, edges=[("work", "review"), ("review", "work")])
        with pytest.raises(ValueError, match="unsupported"):
            await configure_workflow(conn, task_id="task1", steps=[{"id": "x", "kind": "work", "gate": {"javascript": "run()"}}], edges=[])
        await configure_workflow(conn, task_id="task1", steps=steps, edges=[("work", "review")])
        state = await workflow_readiness(conn, "task1")
        assert state[0]["ready"] is False if state[0]["step_id"] == "review" else True
        assert next(item for item in state if item["step_id"] == "review")["blockers"] == [
            "predecessor_step_incomplete", "not_before", "reviewed_result_missing"]


async def test_workflow_review_cannot_complete_without_the_exact_accepted_result(domain_db: Database) -> None:
    await domain_db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = 'task1'")
    async with domain_db.transaction() as conn:
        await configure_workflow(conn, task_id="task1", steps=[{"id": "review", "kind": "review"},
                                                                {"id": "z_followup", "kind": "wait"}],
                                 edges=[("review", "z_followup")])
        with pytest.raises(DomainConflict, match="blocked"):
            await advance_workflow_step(conn, task_id="task1", step_id="review", action="complete")
    await domain_db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                            "fence_token_hash,state,created_at,updated_at)"
                            " VALUES ('attempt1','task1',1,1,'fence','completed','2026-01-01','2026-01-01')")
    await domain_db.execute("UPDATE board_tasks SET current_attempt_id = 'attempt1',status = 'done'"
                            " WHERE id = 'task1'")
    await domain_db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,"
                            " original_text,original_digest,original_size_bytes,actor_id,created_at)"
                            " VALUES ('result1','task1',1,'attempt1','complete','',?,0,'staff:worker','2026-01-01')",
                            (hashlib.sha256(b"").hexdigest(),))
    await domain_db.execute("UPDATE board_tasks SET accepted_result_id = 'result1',accepted_contract_revision = 1"
                            " WHERE id = 'task1'")
    async with domain_db.transaction() as conn:
        with pytest.raises(DomainConflict, match="blocked"):
            await advance_workflow_step(conn, task_id="task1", step_id="review", action="complete")
    await domain_db.execute("INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,"
                            " verification,accepted,reason,created_at)"
                            " VALUES ('verdict1','result1',1,'operator:1','verified',1,'checked','2026-01-01')")
    async with domain_db.transaction() as conn:
        readiness = await workflow_readiness(conn, "task1")
        assert readiness[0]["ready"] is True
        assert readiness[0]["review_binding"] == {"result_id": "result1", "verdict_id": "verdict1",
                                                    "contract_revision": 1, "head": None, "base": None}
        await conn.execute("UPDATE board_tasks SET branch = 'agent/work',merge_state = 'proposed'"
                           " WHERE id = 'task1'")
        assert (await workflow_readiness(conn, "task1"))[0]["blockers"] == ["reviewed_merge_missing"]
        await conn.execute("UPDATE board_tasks SET branch = NULL,merge_state = '' WHERE id = 'task1'")
        completed = await advance_workflow_step(conn, task_id="task1", step_id="review", action="complete")
        assert completed["review_binding"] == readiness[0]["review_binding"]
        assert (await workflow_readiness(conn, "task1"))[1]["ready"] is True
    await domain_db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,attempt_id,outcome,"
                            " original_text,original_digest,original_size_bytes,actor_id,created_at)"
                            " VALUES ('result2','task1',1,'attempt1','complete','',?,0,'staff:worker','2026-01-02')",
                            (hashlib.sha256(b"").hexdigest(),))
    async with domain_db.transaction() as conn:
        stale = await workflow_readiness(conn, "task1")
        assert "reviewed_result_superseded" in stale[0]["blockers"] and stale[0]["effective_complete"] is False
        assert stale[1]["blockers"] == ["predecessor_step_incomplete"] and stale[1]["ready"] is False


async def test_human_workflow_acknowledgement_rejects_a_new_contract(domain_db: Database) -> None:
    await domain_db.execute("UPDATE board_tasks SET status = 'todo' WHERE id = 'task1'")
    async with domain_db.transaction() as conn:
        await configure_workflow(conn, task_id="task1", steps=[{"id": "first", "kind": "human"},
                                                                {"id": "later", "kind": "human"}], edges=[])
        acknowledged = await advance_workflow_step(conn, task_id="task1", step_id="first", action="ack")
        assert acknowledged["contract_revision"] == 1 and acknowledged["acknowledged"] is True
        await replace_contract(conn, task_id="task1", requirements=[],
                               checks=[{"text": "A different acceptance check"}], acceptance="", brief={},
                               origin_kind="operator", origin_ref="edit", change_kind="semantic")
        with pytest.raises(DomainConflict, match="blocked"):
            await advance_workflow_step(conn, task_id="task1", step_id="later", action="ack")


async def test_next_action_requires_current_prerequisites_and_is_replaced_atomically(domain_db: Database) -> None:
    async with domain_db.transaction() as conn:
        await set_next_action(conn, action_id="action1", task_id="task1", kind="review",
                              owner_kind="operator", owner_id=None,
                              prerequisites=[{"kind": "artifact", "ref": "missing"}])
        first = await next_action_readiness(conn, "task1")
        assert first is not None and first["blockers"] == ["artifact_missing"]
        await set_next_action(conn, action_id="action2", task_id="task1", kind="wait",
                              owner_kind="system", owner_id=None, prerequisites=[])
        second = await next_action_readiness(conn, "task1")
        assert second is not None and second["action_id"] == "action2" and second["enabled"] is True
        old = await conn.execute("SELECT state FROM next_actions WHERE id = 'action1'")
        assert (await old.fetchone())[0] == "cancelled"
        await old.close()


async def test_goal_change_fences_only_named_dependency_closure(domain_db: Database) -> None:
    await domain_db.execute("INSERT INTO projects(id,name,created_at,settings)"
                            " VALUES ('project1','Example','2026-01-01','{}')")
    await domain_db.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                            " VALUES ('project1',1,'Old goal','legacy','2026-01-01')")
    for task_id in ("root", "child", "unrelated"):
        await domain_db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                                " created_at,updated_at,brief_json,project_id) VALUES"
                                " (?,?, 'todo',3,'','[]','[]','2026-01-01','2026-01-01','{}','project1')",
                                (task_id, task_id))
    await domain_db.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                            " resolution_state,created_at) VALUES"
                            " ('edge-child','child','root','required','awaiting_result','2026-01-01')")
    async with domain_db.transaction() as conn:
        preview = await scope_impact_preview(conn, "project1", ["root"])
        assert preview["affected_task_ids"] == ["child", "root"]
        principal = Principal.operator({"via": "token", "user_id": 1})
        applied = await apply_goal_revision(conn, project_id="project1", expected_goal_revision=1,
                                            body="New goal", root_task_ids=["root"], origin_kind="operator", origin_ref="",
                                            control=ControlStore(domain_db), principal=principal)
        assert applied["goal_revision"] == 2
    async with domain_db.transaction() as conn:
        revised = await apply_goal_revision(conn, project_id="project1", expected_goal_revision=2,
                                            body="Refined goal", root_task_ids=["root"], origin_kind="operator",
                                            origin_ref="", control=ControlStore(domain_db), principal=principal)
        assert revised["goal_revision"] == 3
    owners = await domain_db.fetchall("SELECT generation,source_revision,cancel_state FROM lifecycle_owners"
                                      " WHERE child_kind = 'task' AND child_id = 'root' ORDER BY generation")
    assert [(row["generation"], row["source_revision"], row["cancel_state"]) for row in owners] == [
        (1, 2, "transferred"), (2, 3, "active")]
    assert (await domain_db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'unrelated'"))[0] == 1
    assert (await domain_db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'child'"))[0] == 3
    assert len(await domain_db.fetchall("SELECT id FROM scope_impacts WHERE project_id = 'project1'")) == 4


async def test_guided_goal_criteria_are_versioned_and_ordinary_revision_preserves_them(domain_db: Database) -> None:
    await domain_db.execute("INSERT INTO projects(id,name,created_at,settings)"
                            " VALUES ('project1','Example','2026-01-01','{}')")
    await domain_db.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                            " VALUES ('project1',1,'','system','2026-01-01')")
    principal = Principal.operator({"via": "token", "user_id": 1})
    async with domain_db.transaction() as conn:
        first = await apply_goal_revision(conn, project_id="project1", expected_goal_revision=1,
                                          body="Publish a menu", root_task_ids=[], origin_kind="operator", origin_ref="",
                                          control=ControlStore(domain_db), principal=principal,
                                          checks=["All items listed", "Prices checked"])
        assert first["goal_revision"] == 2
    async with domain_db.transaction() as conn:
        revised = await apply_goal_revision(conn, project_id="project1", expected_goal_revision=2,
                                            body="Publish the final menu", root_task_ids=[], origin_kind="operator",
                                            origin_ref="", control=ControlStore(domain_db), principal=principal)
        assert revised["goal_revision"] == 3
    rows = await domain_db.fetchall("SELECT goal_revision,body,checks_json FROM project_goal_revisions"
                                    " WHERE project_id = 'project1' ORDER BY goal_revision")
    assert [(row["goal_revision"], row["body"]) for row in rows] == [
        (1, ""), (2, "Publish a menu"), (3, "Publish the final menu")]
    assert rows[0]["checks_json"] is None
    assert [json.loads(row["checks_json"]) for row in rows[1:]] == [
        ["All items listed", "Prices checked"], ["All items listed", "Prices checked"]]
    assert (await domain_db.fetchone("SELECT body FROM project_briefs WHERE project_id = 'project1'"
                                     " AND section = 'done_when'"))["body"] == "All items listed\nPrices checked"


@pytest.mark.parametrize("launch_state", ["pending", "claimed", "late_error"])
async def test_goal_change_cancels_only_unclaimed_launch_in_its_transaction(
    domain_db: Database, launch_state: str,
) -> None:
    await domain_db.execute("INSERT INTO projects(id,name,created_at,settings)"
                            " VALUES ('project1','Example','2026-01-01','{}')")
    await domain_db.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)"
                            " VALUES ('project1',1,'Old goal','operator','2026-01-01')")
    await domain_db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                            " created_at,updated_at,brief_json,project_id) VALUES"
                            " ('root','Work','todo',3,'','[]','[]','2026-01-01','2026-01-01','{}','project1')")
    control = ControlStore(domain_db)
    principal = Principal.operator({"via": "token", "user_id": 1})
    scope = Scope("project", "project1")

    async def launch(conn, mutation):
        effect_id = await OutboxStore.enqueue(conn, mutation, principal, kind="task.launch",
                                              operation="task.launch", payload={"task_id": "root"},
                                              task_id="root", effects=("execution.start",))
        return {"effect_id": effect_id}

    task_revision = await control.revision(scope, Entity("task", "root"))
    queued = await control.mutate(principal, scope, "task.launch", "launch", task_revision,
                                  Entity("task", "root"), {}, launch, effects=("execution.start",))
    if launch_state == "claimed":
        assert await OutboxStore(domain_db).claim(("task.launch",)) is not None
    if launch_state == "late_error":
        await domain_db.execute("CREATE TRIGGER refuse_brief BEFORE INSERT ON project_briefs"
                                " BEGIN SELECT RAISE(ABORT,'brief write refused'); END")

    async def revise(conn, mutation):
        return await apply_goal_revision(conn, project_id="project1", expected_goal_revision=1,
                                         body="New goal", root_task_ids=["root"], origin_kind="operator",
                                         origin_ref="", control=control, principal=principal, mutation=mutation)

    project_revision = await control.revision(scope, Entity("project", "project1"))
    command = control.mutate(principal, scope, "goal.revise", "revise", project_revision,
                             Entity("project", "project1"), {"body": "New goal"}, revise,
                             effects=("lifecycle.stop",))
    if launch_state == "claimed":
        with pytest.raises(DomainConflict, match="in flight or unknown"):
            await command
        assert (await domain_db.fetchone("SELECT goal_revision FROM projects WHERE id = 'project1'"))[0] == 1
        assert (await domain_db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                         (queued["effect_id"],)))[0] == "claimed"
        assert not await domain_db.fetchall("SELECT * FROM scope_impacts")
    elif launch_state == "late_error":
        with pytest.raises(sqlite3.IntegrityError, match="brief write refused"):
            await command
        assert (await domain_db.fetchone("SELECT goal_revision FROM projects WHERE id = 'project1'"))[0] == 1
        assert (await domain_db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                         (queued["effect_id"],)))[0] == "pending"
        assert not await domain_db.fetchall("SELECT * FROM scope_impacts")
    else:
        changed = await command
        assert changed["goal_revision"] == 2
        assert (await domain_db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                         (queued["effect_id"],)))[0] == "cancelled"


def test_original_report_storage_keeps_full_bytes_and_rejects_oversize(tmp_path: Path) -> None:
    store = OriginalReports(tmp_path / "originals")
    inline = store.stage(b"short")
    assert inline[:2] == ("short", None)
    large = b"x" * 65537
    text, blob, digest, size = store.stage(large)
    assert text is None and blob == digest and size == len(large)
    assert store.read(blob, digest) == large
    with pytest.raises(ValueError, match="4 MiB"):
        store.stage(b"x" * (4 * 1024 * 1024 + 1))
