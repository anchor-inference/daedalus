"""Workflow definitions must be bounded before any run or effect is created."""

from __future__ import annotations

import pytest

from daedalus.extensions.board_workflows import BoardWorkflows, WorkflowRefused, validate
from daedalus.stores.control import Principal
from daedalus.stores.database import Database


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
