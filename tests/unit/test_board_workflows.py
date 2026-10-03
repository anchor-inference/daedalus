"""Workflow definitions must be bounded before any run or effect is created."""

from __future__ import annotations

import pytest

from daedalus.extensions.board_workflows import BoardWorkflows, WorkflowRefused, validate
from daedalus.extensions.effects import EffectDispatcher
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from daedalus.stores.files import FileStore
from daedalus.stores.outbox import OutboxStore


def _definition() -> dict:
    return {
        "nodes": [
            {"id": "draft", "kind": "task", "task_id": "task-a", "capabilities": ["board.read"]},
            {"id": "gate", "kind": "approval", "task_id": "task-a"},
        ],
        "edges": [["draft", "gate"]],
        "budget": {"max_steps": 2, "max_parallel": 1},
    }


def test_valid_definition_has_stable_topology_and_digest() -> None:
    a = validate(_definition())
    b = validate(_definition())
    assert a == b
    assert a["topology"] == ["draft", "gate"]


@pytest.mark.parametrize("change", [
    lambda d: d["edges"].append(["gate", "draft"]),
    lambda d: d["nodes"][0].update({"capabilities": ["filesystem.write"]}),
    lambda d: d["nodes"].append({"id": "shell", "kind": "code", "task_id": "task-a"}),
    lambda d: d["nodes"].append({"id": "gate", "kind": "task", "task_id": "task-a"}),
    lambda d: d["budget"].update({"max_parallel": 100}),
])
def test_bad_graph_or_ambient_authority_is_rejected(change) -> None:
    definition = _definition()
    change(definition)
    with pytest.raises(WorkflowRefused):
        validate(definition)


async def test_workflow_start_is_receipted_and_replayed_once(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id, name, created_at) VALUES ('project', 'Project', 'now')")
        await db.execute("INSERT INTO board_tasks(id, title, status, project_id, created_at, updated_at) VALUES ('task-a', 'Task', 'todo', 'project', 'now', 'now')")
        principal = Principal.operator({"via": "token", "user_id": 1})
        workflows = BoardWorkflows(db)
        first = await workflows.start(
            principal, "project", "task-a", _definition(),
            expected_entity_revision=1, client_operation_id="workflow-one",
        )
        assert first == await workflows.start(
            principal, "project", "task-a", _definition(),
            expected_entity_revision=1, client_operation_id="workflow-one",
        )
        rows = await db.fetchall("SELECT id FROM board_workflow_runs")
        assert [row["id"] for row in rows] == [first["run_id"]]
    finally:
        await db.close()


async def test_approval_requires_current_host_source_and_replays_exact_receipt(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
            "VALUES ('task-a','Task','todo','project','now','now')"
        )
        principal = Principal.operator({"via": "token", "user_id": 1})
        workflows = BoardWorkflows(db)
        definition = {
            "nodes": [{"id": "operator", "kind": "approval", "task_id": "task-a"}],
            "edges": [], "budget": {"max_steps": 1, "max_parallel": 1},
        }
        started = await workflows.start(
            principal, "project", "task-a", definition,
            expected_entity_revision=1, client_operation_id="start-approval",
        )
        run_id = started["run_id"]
        await workflows.advance_run(run_id)
        before = (await workflows.inspect(run_id))["steps"][0]
        assert before["can_approve"] and before["current_input_digest"] == before["input_digest"]
        await db.execute("UPDATE board_tasks SET contract_revision = 2 WHERE id = 'task-a'")
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task-a'"))["entity_revision"]
        stale = (await workflows.inspect(run_id))["steps"][0]
        assert not stale["source_current"] and not stale["can_approve"]
        with pytest.raises(WorkflowRefused, match="source or contract changed"):
            await workflows.approve_command(
                principal, run_id, "operator", expected_entity_revision=revision,
                expected_step_revision=before["step_revision"],
                expected_source_contract_revision=before["source_contract_revision"],
                expected_input_digest=before["current_input_digest"], client_operation_id="old-approval",
            )
        await workflows.advance_run(run_id)
        current = (await workflows.inspect(run_id))["steps"][0]
        assert current["can_approve"] and current["step_revision"] > before["step_revision"]
        assert current["source_contract_revision"] == 2
        payload = {
            "expected_entity_revision": revision, "expected_step_revision": current["step_revision"],
            "expected_source_contract_revision": current["source_contract_revision"],
            "expected_input_digest": current["current_input_digest"],
            "client_operation_id": "current-approval",
        }
        approved = await workflows.approve_command(principal, run_id, "operator", **payload)
        assert approved["status"] == "completed"
        assert approved == await workflows.approve_command(principal, run_id, "operator", **payload)
        assert (await workflows.inspect(run_id))["steps"][0]["receipt_id"] == approved["receipt_id"]
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task-a'"))["entity_revision"]
        newer = await workflows.start(
            principal, "project", "task-a", definition,
            expected_entity_revision=revision, client_operation_id="newer-approval-run",
        )
        await db.execute("UPDATE board_tasks SET contract_revision = 3 WHERE id = 'task-a'")
        await workflows.advance_run(run_id)
        historical = await workflows.inspect(run_id)
        assert historical["superseded_by"] == newer["run_id"]
        assert not historical["steps"][0]["can_approve"]
        assert historical["steps"][0]["approval_blocker"] == "newer_run"
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task-a'"))["entity_revision"]
        with pytest.raises(WorkflowRefused, match="newer workflow"):
            await workflows.approve_command(
                principal, run_id, "operator", expected_entity_revision=revision,
                expected_step_revision=historical["steps"][0]["step_revision"],
                expected_source_contract_revision=3,
                expected_input_digest=historical["steps"][0]["current_input_digest"],
                client_operation_id="stale-old-run-approval",
            )
    finally:
        await db.close()


async def test_cross_task_run_advances_from_accepted_result_and_explicit_approval(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
            "VALUES ('task-a','Task','todo','project','now','now')"
        )
        await db.execute(
            "INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,snapshot_json,created_at) "
            "VALUES ('task-a',1,'operator','{}','now')"
        )
        dispatcher = EffectDispatcher(OutboxStore(db))
        workflows = BoardWorkflows(db, dispatcher)
        dispatcher.register("workflow.advance", workflows)
        principal = Principal.operator({"via": "token", "user_id": 1})
        started = await workflows.start(
            principal, "project", "task-a", _definition(),
            expected_entity_revision=1, client_operation_id="start-cross-task-run",
        )
        assert await dispatcher.step()
        assert (await db.fetchone(
            "SELECT status FROM board_workflow_steps WHERE run_id = ? AND node_id = 'draft'",
            (started["run_id"],),
        ))["status"] == "ready"
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,original_size_bytes,actor_id,created_at) "
            "VALUES ('result','task-a',1,'complete','Report','digest',6,'operator:1','now')"
        )
        await db.execute(
            "UPDATE board_tasks SET accepted_result_id = 'result', accepted_contract_revision = 1, status = 'done' "
            "WHERE id = 'task-a'"
        )
        unverified = await workflows.advance_run(started["run_id"])
        assert unverified["status"] == "blocked" and (await db.fetchone(
            "SELECT status FROM board_workflow_steps WHERE run_id = ? AND node_id = 'draft'", (started["run_id"],),
        ))["status"] != "completed"
        await db.execute(
            "INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,verification,accepted,created_at) "
            "VALUES ('verdict','result',1,'operator:1','verified',1,'now')"
        )
        progressed = await workflows.advance_run(started["run_id"])
        assert progressed["status"] == "running" and progressed["changed"] == 2
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task-a'"))["entity_revision"]
        inspected = await workflows.inspect(started["run_id"])
        gate = next(step for step in inspected["steps"] if step["node_id"] == "gate")
        assert gate["can_approve"] and gate["source_current"]
        approved = await workflows.approve_command(
            principal, started["run_id"], "gate", expected_entity_revision=revision,
            expected_step_revision=gate["step_revision"],
            expected_source_contract_revision=gate["source_contract_revision"],
            expected_input_digest=gate["current_input_digest"],
            client_operation_id="approve-cross-task-gate",
        )
        assert approved["status"] == "completed"
        assert (await db.fetchone(
            "SELECT status FROM board_workflow_runs WHERE id = ?", (started["run_id"],)
        ))["status"] == "completed"
        draft = await db.fetchone(
            "SELECT input_digest FROM board_workflow_steps WHERE run_id = ? AND node_id = 'draft'",
            (started["run_id"],),
        )
        preview = await workflows.reconcile_preview(started["run_id"], "draft", draft["input_digest"])
        assert preview["decision"] == "needs_review" and preview["reason"] == "artifact_provenance_unknown"
        assert preview["receipt_id"] == "result"
        await db.execute(
            "UPDATE board_tasks SET accepted_result_id = NULL, accepted_contract_revision = NULL "
            "WHERE id = 'task-a'"
        )
        stale = await workflows.advance_run(started["run_id"])
        assert stale["status"] == "blocked" and stale["changed"] == 2
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task-a'"))["entity_revision"]
        cancelled = await workflows.cancel_command(
            principal, started["run_id"], "source result withdrawn",
            expected_entity_revision=revision, client_operation_id="cancel-stale-run",
        )
        assert cancelled["status"] == "cancelled"
        assert cancelled == await workflows.cancel_command(
            principal, started["run_id"], "source result withdrawn",
            expected_entity_revision=revision, client_operation_id="cancel-stale-run",
        )
    finally:
        await db.close()


async def test_missing_manifest_bytes_block_reuse_without_rerunning_effect(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
            "VALUES ('task-a','Task','todo','project','now','now')"
        )
        await db.execute(
            "INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,snapshot_json,created_at) "
            "VALUES ('task-a',1,'operator','{}','now')"
        )
        files = FileStore(db, FileBlobStore(tmp_path / "blobs"))
        file = await files.add(b"output", name="output.txt", origin="operator", scope="project", actor="operator")
        await db.execute(
            "INSERT INTO artifact_manifests(id,project_id,task_id,artifact_kind,artifact_key,artifact_revision,digest,size_bytes,file_id,created_at) "
            "VALUES ('artifact','project','task-a','document','output',1,?,6,?,'now')",
            (file.sha256, file.id),
        )
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,original_size_bytes,actor_id,created_at) "
            "VALUES ('result','task-a',1,'complete','Report','digest',6,'operator:1','now')"
        )
        await db.execute(
            "INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,verification,accepted,created_at) "
            "VALUES ('verdict','result',1,'operator:1','verified',1,'now')"
        )
        definition = {"nodes": [{"id": "output", "kind": "task", "task_id": "task-a"}], "edges": [], "budget": {"max_steps": 1}}
        workflows = BoardWorkflows(db, files=files)
        run = await workflows.start(
            Principal.operator({"via": "token", "user_id": 1}), "project", "task-a", definition,
            expected_entity_revision=1, client_operation_id="output-run",
        )
        await db.execute(
            "UPDATE board_tasks SET accepted_result_id = 'result', accepted_contract_revision = 1, status = 'done' WHERE id = 'task-a'"
        )
        assert (await workflows.advance_run(run["run_id"]))["status"] == "completed"
        step = await db.fetchone(
            "SELECT input_digest FROM board_workflow_steps WHERE run_id = ? AND node_id = 'output'", (run["run_id"],),
        )
        assert (await workflows.reconcile_preview(run["run_id"], "output", step["input_digest"]))["decision"] == "reuse"
        assert (await workflows.reconcile_preview(run["run_id"], "output", "0" * 64))["reason"] == "source_changed"
        await files.blobs.delete("files", file.sha256)
        preview = await workflows.reconcile_preview(run["run_id"], "output", step["input_digest"])
        assert preview["decision"] == "needs_review" and preview["reason"] == "effect_missing_or_stale"
        assert (await workflows.advance_run(run["run_id"]))["status"] == "blocked"
    finally:
        await db.close()


async def test_branch_result_without_matching_merge_receipt_cannot_complete_workflow(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,branch,created_at,updated_at) "
            "VALUES ('task-a','Task','todo','project','work','now','now')"
        )
        await db.execute(
            "INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,snapshot_json,created_at) "
            "VALUES ('task-a',1,'operator','{}','now')"
        )
        workflows = BoardWorkflows(db)
        definition = {"nodes": [{"id": "work", "kind": "task", "task_id": "task-a"}],
                      "edges": [], "budget": {"max_steps": 1, "max_parallel": 1}}
        started = await workflows.start(
            Principal.operator({"via": "token", "user_id": 1}), "project", "task-a", definition,
            expected_entity_revision=1, client_operation_id="branch-run",
        )
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,original_size_bytes,actor_id,created_at) "
            "VALUES ('result','task-a',1,'complete','Report','digest',6,'operator:1','now')"
        )
        await db.execute(
            "INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,verification,accepted,head,base,created_at) "
            "VALUES ('verdict','result',1,'operator:1','verified',1,'head','base','now')"
        )
        await db.execute(
            "UPDATE board_tasks SET accepted_result_id = 'result', accepted_contract_revision = 1, "
            "merge_state = 'merged', status = 'done' WHERE id = 'task-a'"
        )
        projected = await workflows.advance_run(started["run_id"])
        assert projected["status"] == "blocked"
        assert (await workflows.inspect(started["run_id"]))["steps"][0]["status"] == "blocked"
    finally:
        await db.close()


async def test_rejected_check_uses_latest_current_contract_result(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
            "VALUES ('task-a','Task','todo','project','now','now')"
        )
        await db.execute(
            "INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,snapshot_json,created_at) "
            "VALUES ('task-a',1,'operator','{}','now')"
        )
        await db.execute(
            "INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,original_digest,original_size_bytes,actor_id,created_at) "
            "VALUES ('result','task-a',1,'failed','Report','digest',6,'operator:1','now')"
        )
        await db.execute(
            "INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,verification,accepted,created_at) "
            "VALUES ('verdict','result',1,'operator:1','verified',0,'now')"
        )
        definition = {
            "nodes": [{"id": "check", "kind": "check", "task_id": "task-a", "check": {"receipt_status": "rejected"}}],
            "edges": [], "budget": {"max_steps": 1, "max_parallel": 1},
        }
        workflows = BoardWorkflows(db)
        run = await workflows.start(
            Principal.operator({"via": "token", "user_id": 1}), "project", "task-a", definition,
            expected_entity_revision=1, client_operation_id="check-rejection",
        )
        projected = await workflows.advance_run(run["run_id"])
        assert projected["status"] == "completed"
    finally:
        await db.close()
