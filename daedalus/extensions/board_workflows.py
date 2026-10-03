"""Bounded, deterministic board workflow definitions and persisted step transitions.

Definitions contain references and checks only. Running arbitrary code in a workflow would give
the definition ambient filesystem and network authority, so node kinds and capabilities are closed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database

MAX_NODES = 32
MAX_DEPTH = 8
MAX_FANOUT = 4
MAX_PARALLEL = 8
KINDS = {"task", "check", "review", "approval"}
CAPABILITIES = {"board.read", "board.transition", "contract.read", "review.read"}


class WorkflowRefused(ValueError):
    """A definition or transition exceeds a bounded workflow's authority."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(definition: dict[str, Any]) -> dict[str, Any]:
    if set(definition) != {"nodes", "edges", "budget"}:
        raise WorkflowRefused("definition accepts only nodes, edges and budget")
    nodes = definition["nodes"]
    edges = definition["edges"]
    budget = definition["budget"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES or not isinstance(edges, list) or len(edges) > MAX_NODES * MAX_FANOUT:
        raise WorkflowRefused("workflow size exceeds its bound")
    if not isinstance(budget, dict) or set(budget) - {"max_steps", "max_parallel"}:
        raise WorkflowRefused("unsupported budget")
    max_steps = budget.get("max_steps", MAX_NODES)
    parallel = budget.get("max_parallel", 1)
    if type(max_steps) is not int or not len(nodes) <= max_steps <= MAX_NODES or type(parallel) is not int or not 1 <= parallel <= MAX_PARALLEL:
        raise WorkflowRefused("invalid budget")
    by_id: dict[str, dict[str, Any]] = {}
    caps: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"id", "kind", "task_id", "capabilities", "check"}:
            raise WorkflowRefused("node has unsupported fields")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not 1 <= len(node_id) <= 64 or not node_id.replace("-", "").replace("_", "").isalnum() or node_id in by_id:
            raise WorkflowRefused("invalid or duplicate node id")
        if not isinstance(node.get("kind"), str) or node["kind"] not in KINDS or not isinstance(node.get("task_id"), str) or not node["task_id"]:
            raise WorkflowRefused("node needs a supported kind and task id")
        declared = node.get("capabilities", [])
        if not isinstance(declared, list) or not all(isinstance(cap, str) for cap in declared) or not set(declared) <= CAPABILITIES:
            raise WorkflowRefused("node requests an ambient or unknown capability")
        if "check" in node and (node["kind"] != "check" or not isinstance(node["check"], dict) or set(node["check"]) != {"receipt_status"} or node["check"]["receipt_status"] not in {"accepted", "rejected"}):
            raise WorkflowRefused("check nodes accept only a receipt status predicate")
        by_id[node_id] = node
        caps.update(declared)
    after = {node_id: [] for node_id in by_id}
    before = {node_id: [] for node_id in by_id}
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 2 or not all(isinstance(item, str) for item in edge) or edge[0] not in by_id or edge[1] not in by_id or edge[0] == edge[1]:
            raise WorkflowRefused("edge refers to an unknown or identical node")
        if edge[1] in after[edge[0]]:
            raise WorkflowRefused("duplicate edge")
        after[edge[0]].append(edge[1])
        before[edge[1]].append(edge[0])
        if len(after[edge[0]]) > MAX_FANOUT:
            raise WorkflowRefused("fanout exceeds four")
    ready = sorted(node_id for node_id, deps in before.items() if not deps)
    order: list[str] = []
    depth = {node_id: 1 for node_id in ready}
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for child in after[node_id]:
            depth[child] = max(depth.get(child, 1), depth[node_id] + 1)
            if depth[child] > MAX_DEPTH:
                raise WorkflowRefused("workflow depth exceeds eight")
            before[child].remove(node_id)
            if not before[child]:
                ready.append(child)
        ready.sort()
    if len(order) != len(nodes):
        raise WorkflowRefused("workflow contains a cycle")
    return {"valid": True, "topology": order, "required_capabilities": sorted(caps), "definition_digest": digest(definition)}


class BoardWorkflows:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def start(
        self, principal: Principal, project_id: str, task_id: str, definition: dict[str, Any],
        *, expected_entity_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        checked = validate(definition)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await self._start(conn, mutation.object_id, project_id, task_id, definition, checked)

        return await ControlStore(self.db).mutate(
            principal, Scope("project", project_id), "workflow.start", client_operation_id,
            expected_entity_revision, Entity("task", task_id), {"definition": definition}, effect,
        )

    async def _start(
        self, conn: Any, run_id: str, project_id: str, task_id: str,
        definition: dict[str, Any], checked: dict[str, Any],
    ) -> dict[str, Any]:
        at = datetime.now(UTC).isoformat()
        cursor = await conn.execute(
            "SELECT status, project_id FROM board_tasks WHERE id = ?", (task_id,)
        )
        task = await cursor.fetchone()
        await cursor.close()
        if task is None or task["project_id"] != project_id or task["status"] not in {"todo", "doing"}:
            raise WorkflowRefused("task is not ready on this project board")
        for node in definition["nodes"]:
            cursor = await conn.execute("SELECT project_id FROM board_tasks WHERE id = ?", (node["task_id"],))
            child = await cursor.fetchone()
            await cursor.close()
            if child is None or child["project_id"] != project_id:
                raise WorkflowRefused("workflow node refers outside the project board")
        await conn.execute(
            "INSERT INTO board_workflow_runs "
            "(id, project_id, task_id, definition_digest, definition, budget, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (run_id, project_id, task_id, checked["definition_digest"], json.dumps(definition, sort_keys=True),
             json.dumps(definition["budget"], sort_keys=True), at, at),
        )
        incoming = {edge[1] for edge in definition["edges"]}
        for node in definition["nodes"]:
            await conn.execute(
                "INSERT INTO board_workflow_steps(run_id, node_id, kind, status) VALUES (?, ?, ?, ?)",
                (run_id, node["id"], node["kind"], "pending" if node["id"] in incoming else "ready"),
            )
        return {"run_id": run_id, "status": "pending", **checked}

    async def step(self, run_id: str, node_id: str, *, expected_revision: int, status: str, fingerprints: dict[str, str]) -> dict[str, Any]:
        if status not in {"running", "completed", "blocked", "cancelled", "failed"} or set(fingerprints) != {"input", "env", "capability", "artifact_manifest"}:
            raise WorkflowRefused("unsupported step transition or fingerprint")
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT s.*, r.status run_status FROM board_workflow_steps s "
                "JOIN board_workflow_runs r ON r.id = s.run_id WHERE s.run_id = ? AND s.node_id = ?", (run_id, node_id)
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is None or current["run_status"] in {"cancelled", "completed", "failed"} or current["step_revision"] != expected_revision:
                raise WorkflowRefused("step revision or run state changed")
            allowed = {"ready": {"running", "cancelled"}, "running": {"completed", "blocked", "failed", "cancelled"}, "blocked": {"running", "cancelled"}}
            if status not in allowed.get(current["status"], set()):
                raise WorkflowRefused("invalid step transition")
            reuse = digest([run_id, node_id, *(fingerprints[key] for key in ("input", "env", "capability", "artifact_manifest"))])
            at = datetime.now(UTC).isoformat()
            await conn.execute(
                "UPDATE board_workflow_steps SET status = ?, step_revision = step_revision + 1, "
                "input_digest = ?, env_digest = ?, capability_digest = ?, artifact_manifest_digest = ?, reuse_fingerprint = ? "
                "WHERE run_id = ? AND node_id = ? AND step_revision = ?",
                (status, fingerprints["input"], fingerprints["env"], fingerprints["capability"],
                 fingerprints["artifact_manifest"], reuse, run_id, node_id, expected_revision),
            )
            await conn.execute("UPDATE board_workflow_runs SET status = 'running', updated_at = ? WHERE id = ?", (at, run_id))
        return {"run_id": run_id, "node_id": node_id, "step_revision": expected_revision + 1, "status": status, "reuse_fingerprint": reuse}
