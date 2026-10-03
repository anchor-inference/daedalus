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
        progressed = await workflows.advance_run(started["run_id"])
        assert progressed["status"] == "running" and progressed["changed"] == 2
        revision = (await db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = 'task-a'"))["entity_revision"]
        approved = await workflows.approve_command(
            principal, started["run_id"], "gate", expected_entity_revision=revision,
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
