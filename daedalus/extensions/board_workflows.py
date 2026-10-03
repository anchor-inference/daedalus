"""Bounded, deterministic board workflow definitions and persisted step transitions.

Definitions contain references and checks only. Running arbitrary code in a workflow would give
the definition ambient filesystem and network authority, so node kinds and capabilities are closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome
from daedalus.extensions.orchestrator_domain import dependency_readiness, workflow_readiness
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.files import FILES_TENANT
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application

MAX_NODES = 32
MAX_DEPTH = 8
MAX_FANOUT = 4
MAX_PARALLEL = 8
KINDS = {"task", "check", "review", "approval"}
CAPABILITIES = {"board.read", "board.transition", "contract.read", "review.read"}
logger = logging.getLogger(__name__)


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
        if node["kind"] == "check" and "check" not in node:
            raise WorkflowRefused("check node needs an exact receipt predicate")
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
    def __init__(self, db: Database, dispatcher: Any = None, files: Any = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self.files = files

    async def start(
        self, principal: Principal, project_id: str, task_id: str, definition: dict[str, Any],
        *, expected_entity_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        checked = validate(definition)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            response = await self._start(conn, mutation.object_id, project_id, task_id, definition, checked)
            await OutboxStore.enqueue(
                conn, mutation, principal, kind="workflow.advance", operation="workflow.start",
                payload={"run_id": mutation.object_id}, effects=("workflow.advance",), task_id=task_id,
            )
            return response

        response = await ControlStore(self.db).mutate(
            principal, Scope("project", project_id), "workflow.start", client_operation_id,
            expected_entity_revision, Entity("task", task_id), {"definition": definition}, effect,
            effects=("workflow.advance",),
        )
        if self.dispatcher is not None:
            self.dispatcher.notify()
        return response

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
        cursor = await conn.execute(
            "SELECT id,status FROM board_workflow_runs WHERE task_id = ? "
            "ORDER BY created_at DESC,id DESC LIMIT 1", (task_id,),
        )
        latest = await cursor.fetchone()
        await cursor.close()
        if latest is not None and latest["status"] in {"pending", "running", "blocked"}:
            raise WorkflowRefused("the task already has an active workflow")
        if not (await dependency_readiness(conn, task_id))["ready"]:
            raise WorkflowRefused("root task has unresolved Board dependencies")
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

    async def advance_run(self, run_id: str) -> dict[str, Any]:
        """Project one cross-task DAG from current Board receipts without changing task contracts."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM board_workflow_runs WHERE id = ?", (run_id,))
            run = await cursor.fetchone()
            await cursor.close()
            if run is None:
                raise KeyError(run_id)
            if run["status"] in {"cancelled", "failed"}:
                return {"run_id": run_id, "status": run["status"], "changed": 0}
            definition = json.loads(run["definition"])
            checked = validate(definition)
            if checked["definition_digest"] != run["definition_digest"]:
                raise WorkflowRefused("workflow definition changed")
            cursor = await conn.execute("SELECT * FROM board_workflow_steps WHERE run_id = ?", (run_id,))
            step_rows = {row["node_id"]: row for row in await cursor.fetchall()}
            await cursor.close()
            nodes = {node["id"]: node for node in definition["nodes"]}
            predecessors: dict[str, set[str]] = {node_id: set() for node_id in nodes}
            for source, target in definition["edges"]:
                predecessors[target].add(source)
            states = {node_id: row["status"] for node_id, row in step_rows.items()}
            parallel = definition["budget"].get("max_parallel", 1)
            active = sum(state in {"ready", "running"} for state in states.values())
            changed = 0
            for node_id in checked["topology"]:
                row = step_rows[node_id]
                if row["status"] in {"cancelled", "failed"}:
                    continue
                if any(states[parent] != "completed" for parent in predecessors[node_id]):
                    desired = "blocked" if row["reuse_fingerprint"] else "pending"
                    fingerprint = None
                else:
                    node = nodes[node_id]
                    desired, fingerprint = await self._project_node(conn, node, row["status"])
                    if row["reuse_fingerprint"]:
                        desired = "completed" if desired == "completed" and fingerprint == row["reuse_fingerprint"] else "blocked"
                    if desired in {"ready", "running"} and row["status"] not in {"ready", "running"} and active >= parallel:
                        desired = "pending"
                if desired == row["status"]:
                    # A ready approval still needs its first host-derived input fingerprint. Without
                    # it, a later approval could be recorded against an empty source and never stale.
                    if fingerprint and fingerprint != row["input_digest"]:
                        cursor = await conn.execute(
                            "UPDATE board_workflow_steps SET input_digest = ?, step_revision = step_revision + 1 "
                            "WHERE run_id = ? AND node_id = ? AND step_revision = ?",
                            (fingerprint, run_id, node_id, row["step_revision"]),
                        )
                        if cursor.rowcount != 1:
                            raise WorkflowRefused("workflow step revision changed")
                        changed += 1
                    continue
                if row["status"] in {"ready", "running"}:
                    active -= 1
                if desired in {"ready", "running"}:
                    active += 1
                receipt_id = await self._node_receipt(conn, nodes[node_id]) if desired == "completed" else row["receipt_id"]
                cursor = await conn.execute(
                    "UPDATE board_workflow_steps SET status = ?, step_revision = step_revision + 1, "
                    "input_digest = ?, reuse_fingerprint = ?, receipt_id = ? "
                    "WHERE run_id = ? AND node_id = ? AND step_revision = ?",
                    (desired, fingerprint or row["input_digest"],
                     fingerprint if desired == "completed" else row["reuse_fingerprint"],
                     receipt_id,
                     run_id, node_id, row["step_revision"]),
                )
                if cursor.rowcount != 1:
                    raise WorkflowRefused("workflow step revision changed")
                states[node_id] = desired
                changed += 1
            if all(state == "completed" for state in states.values()):
                status = "completed"
            elif any(state == "blocked" for state in states.values()):
                status = "blocked"
            elif any(state in {"ready", "running"} for state in states.values()):
                status = "running"
            else:
                status = "pending"
            if run["status"] != status:
                await conn.execute(
                    "UPDATE board_workflow_runs SET status = ?, updated_at = ? WHERE id = ?",
                    (status, datetime.now(UTC).isoformat(), run_id),
                )
            return {"run_id": run_id, "status": status, "changed": changed}

    async def _project_node(self, conn: Any, node: dict[str, Any], current: str) -> tuple[str, str]:
        cursor = await conn.execute(
            "SELECT status,contract_revision,current_attempt_id,accepted_result_id,accepted_contract_revision,branch,merge_state "
            "FROM board_tasks WHERE id = ?", (node["task_id"],),
        )
        task = await cursor.fetchone()
        await cursor.close()
        if task is None or task["status"] in {"cancelled", "dropped"}:
            return "blocked", ""
        artifact_digest, artifacts_present, _ = await self._artifact_state(conn, node["task_id"])
        deps = await dependency_readiness(conn, node["task_id"])
        gates = await workflow_readiness(conn, node["task_id"])
        gate_ok = all(gate["state"] == "complete" for gate in gates)
        cursor = await conn.execute(
            "SELECT id,attempt_id,outcome FROM result_receipts WHERE task_id = ? AND contract_revision = ? "
            "AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (node["task_id"], task["contract_revision"], task["current_attempt_id"]),
        )
        latest_result = await cursor.fetchone()
        await cursor.close()
        accepted_result_id = task["accepted_result_id"]
        accepted_result = latest_result if latest_result is not None and latest_result["id"] == accepted_result_id else None
        accepted = (accepted_result is not None and accepted_result["outcome"] == "complete"
                    and task["accepted_contract_revision"] == task["contract_revision"])
        result = latest_result if node["kind"] == "check" else accepted_result
        result_id = result["id"] if result is not None else None
        cursor = await conn.execute(
            "SELECT id,accepted,verification,head,base FROM review_verdicts WHERE result_id = ? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1", (result_id,),
        )
        verdict = await cursor.fetchone()
        await cursor.close()
        approved_verdict = bool(verdict is not None and verdict["accepted"] and verdict["verification"] == "verified")
        merge = None
        if task["branch"] and approved_verdict and accepted_result_id == result_id:
            cursor = await conn.execute(
                "SELECT state,head_sha,base_sha,merge_sha FROM task_merge_receipts "
                "WHERE task_id = ? AND result_id = ? AND verdict_id = ?",
                (node["task_id"], result_id, verdict["id"]),
            )
            merge = await cursor.fetchone()
            await cursor.close()
        branch_current = not task["branch"] or bool(
            task["merge_state"] == "merged" and merge is not None and merge["state"] == "merged"
            and merge["merge_sha"] and verdict is not None
            and merge["head_sha"] == verdict["head"] and merge["base_sha"] == verdict["base"]
        )
        evidence = {
            "result_id": result_id, "outcome": result["outcome"] if result is not None else None,
            "attempt_id": result["attempt_id"] if result is not None else None,
            "verdict_id": verdict["id"] if verdict else None,
            "verdict_status": (verdict["verification"], verdict["accepted"], verdict["head"], verdict["base"]) if verdict else None,
            "merge": (merge["state"], merge["head_sha"], merge["base_sha"], merge["merge_sha"]) if merge else None,
        }
        fingerprint = digest({
            "task_id": node["task_id"], "contract_revision": task["contract_revision"],
            "accepted_result_id": task["accepted_result_id"], "dependency": deps["dependency_fingerprint"],
            "gates": [(gate["step_id"], gate["state"], gate["blockers"]) for gate in gates],
            "evidence": evidence, "artifact_manifest_digest": artifact_digest,
        })
        if not deps["ready"] or not artifacts_present:
            return "blocked", fingerprint
        if node["kind"] == "task":
            if accepted and approved_verdict and branch_current and gate_ok:
                return "completed", fingerprint
            if accepted_result_id is not None and not (accepted and approved_verdict and branch_current):
                return "blocked", fingerprint
            return "running" if task["status"] == "doing" else "ready", fingerprint
        if node["kind"] in {"review", "check"}:
            if node["kind"] == "review" and accepted_result_id is not None and not (accepted and approved_verdict and branch_current):
                return "blocked", fingerprint
            if verdict is not None and verdict["verification"] == "verified":
                if node["kind"] == "review" and accepted and approved_verdict and branch_current:
                    return "completed", fingerprint
                if node["kind"] == "check" and bool(verdict["accepted"]) == (node.get("check", {}).get("receipt_status") == "accepted"):
                    return "completed", fingerprint
            return "ready", fingerprint
        if node["kind"] == "approval":
            return current if current == "completed" else "ready", fingerprint
        raise WorkflowRefused("unsupported workflow node")

    async def _node_receipt(self, conn: Any, node: dict[str, Any]) -> str | None:
        if node["kind"] == "approval":
            return None
        if node["kind"] == "check":
            cursor = await conn.execute(
                "SELECT id FROM result_receipts WHERE task_id = ? AND contract_revision = "
                "(SELECT contract_revision FROM board_tasks WHERE id = ?) "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (node["task_id"], node["task_id"]),
            )
        else:
            cursor = await conn.execute(
                "SELECT accepted_result_id id FROM board_tasks WHERE id = ?", (node["task_id"],),
            )
        row = await cursor.fetchone()
        await cursor.close()
        return row["id"] if row else None

    async def _artifact_state(self, conn: Any, task_id: str) -> tuple[str, bool, bool]:
        """A file-backed manifest is current only while its content-addressed bytes are present."""
        cursor = await conn.execute(
            "SELECT a.id,a.artifact_key,a.artifact_revision,a.digest,a.file_id,f.sha256 "
            "FROM artifact_manifests a LEFT JOIN files f ON f.id = a.file_id "
            "WHERE a.task_id = ? ORDER BY a.artifact_key,a.artifact_revision,a.id", (task_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        manifests = [(row["id"], row["artifact_key"], row["artifact_revision"], row["digest"]) for row in rows]
        present = True
        verifiable = bool(rows)
        for row in rows:
            if row["file_id"] is None:
                verifiable = False
                continue
            if row["sha256"] != row["digest"] or self.files is None:
                present = False
                continue
            if not await self.files.blobs.exists(FILES_TENANT, row["sha256"]):
                present = False
        return digest(manifests), present, verifiable

    async def reconcile_preview(self, run_id: str, node_id: str, expected_input_hash: str) -> dict[str, Any]:
        """Explain reuse from persisted state; uncertain external effects cannot be replayed."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT definition,status FROM board_workflow_runs WHERE id = ?", (run_id,))
            run = await cursor.fetchone()
            await cursor.close()
            if run is None:
                raise KeyError(run_id)
            cursor = await conn.execute(
                "SELECT status,input_digest,reuse_fingerprint,receipt_id,attempt_id FROM board_workflow_steps "
                "WHERE run_id = ? AND node_id = ?", (run_id, node_id),
            )
            step = await cursor.fetchone()
            await cursor.close()
            if step is None:
                raise KeyError(node_id)
            node = next((item for item in json.loads(run["definition"])["nodes"] if item["id"] == node_id), None)
            if node is None:
                raise WorkflowRefused("workflow node is missing from its definition")
            projected, current_hash = await self._project_node(conn, node, step["status"])
            _, _, artifacts_verifiable = await self._artifact_state(conn, node["task_id"])
            reason = ""
            if step["status"] != "completed" or run["status"] in {"cancelled", "failed"}:
                reason = "step_not_completed"
            elif expected_input_hash != step["input_digest"] or current_hash != step["reuse_fingerprint"]:
                reason = "source_changed"
            elif projected != "completed":
                reason = "effect_missing_or_stale"
            elif node["kind"] == "task" and not artifacts_verifiable:
                reason = "artifact_provenance_unknown"
            elif not step["receipt_id"]:
                reason = "effect_receipt_unknown"
            else:
                cursor = await conn.execute(
                    "SELECT id FROM operation_receipts WHERE id = ? UNION ALL "
                    "SELECT id FROM result_receipts WHERE id = ?", (step["receipt_id"], step["receipt_id"]),
                )
                receipt = await cursor.fetchone()
                await cursor.close()
                if receipt is None:
                    reason = "effect_receipt_missing"
            return {
                "run_id": run_id, "node_id": node_id,
                "decision": "reuse" if not reason else "needs_review",
                "reason": reason or "matching_verified_receipt",
                "receipt_id": step["receipt_id"],
                "current_input_hash": current_hash,
            }

    async def inspect(self, run_id: str) -> dict[str, Any]:
        """Read persisted steps with a current-source preview, without advancing a run on GET."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM board_workflow_runs WHERE id = ?", (run_id,))
            run = await cursor.fetchone()
            await cursor.close()
            if run is None:
                raise KeyError(run_id)
            cursor = await conn.execute(
                "SELECT id FROM board_workflow_runs WHERE task_id = ? "
                "ORDER BY created_at DESC,id DESC LIMIT 1", (run["task_id"],),
            )
            latest = await cursor.fetchone()
            await cursor.close()
            superseded = latest is not None and latest["id"] != run_id
            definition = json.loads(run["definition"])
            nodes = {node["id"]: node for node in definition["nodes"]}
            cursor = await conn.execute(
                "SELECT * FROM board_workflow_steps WHERE run_id = ? ORDER BY node_id", (run_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            steps_by_id = {row["node_id"]: row for row in rows}
            views: list[dict[str, Any]] = []
            all_current = True
            for row in rows:
                node = nodes[row["node_id"]]
                projected, source_digest = await self._project_node(conn, node, row["status"])
                cursor = await conn.execute(
                    "SELECT contract_revision FROM board_tasks WHERE id = ?", (node["task_id"],),
                )
                source = await cursor.fetchone()
                await cursor.close()
                source_current = bool(source_digest and row["input_digest"] == source_digest
                                      and (row["status"] != "completed" or row["reuse_fingerprint"] == source_digest))
                if row["status"] in {"completed", "ready", "running"} and not source_current:
                    all_current = False
                reason = ""
                can_approve = False
                if node["kind"] == "approval":
                    if superseded:
                        reason = "newer_run"
                    elif run["status"] in {"cancelled", "failed"}:
                        reason = "run_ended"
                    elif row["status"] == "completed" and source_current:
                        reason = "already_approved"
                    elif not source_current:
                        reason = "source_changed_or_reconciling"
                    elif projected != "ready":
                        reason = "source_blocked"
                    elif not await self._predecessors_current(conn, definition, steps_by_id, row["node_id"]):
                        reason = "predecessor_unready"
                    elif row["status"] not in {"ready", "blocked"}:
                        reason = "step_not_ready"
                    else:
                        can_approve = True
                views.append({**dict(row), "source_contract_revision": source["contract_revision"] if source else None,
                              "current_input_digest": source_digest, "source_current": source_current,
                              "projected_state": projected, "can_approve": can_approve,
                              "approval_blocker": reason or None})
            return {**dict(run), "run_id": run["id"], "definition_value": definition,
                    "projection_current": all_current, "superseded_by": latest["id"] if superseded else None,
                    "steps": views}

    async def _predecessors_current(
        self, conn: Any, definition: dict[str, Any], steps: dict[str, Any], node_id: str,
    ) -> bool:
        nodes = {node["id"]: node for node in definition["nodes"]}
        for source, target in definition["edges"]:
            if target != node_id:
                continue
            prior = steps[source]
            if prior["status"] != "completed":
                return False
            projected, source_digest = await self._project_node(conn, nodes[source], prior["status"])
            if projected != "completed" or not source_digest or prior["reuse_fingerprint"] != source_digest:
                return False
        return True

    async def run(self, claim: Claim, check: Any) -> EffectOutcome:
        await check(claim)
        try:
            projected = await self.advance_run(claim.payload["run_id"])
        except (KeyError, WorkflowRefused) as exc:
            return EffectOutcome("failed", str(exc))
        if projected["status"] in {"cancelled", "failed"}:
            return EffectOutcome("failed", "workflow is no longer active")
        return EffectOutcome("completed")

    async def approve_command(
        self, principal: Principal, run_id: str, node_id: str, *,
        expected_entity_revision: int, expected_step_revision: int,
        expected_source_contract_revision: int, expected_input_digest: str,
        client_operation_id: str,
    ) -> dict[str, Any]:
        run = await self.db.fetchone("SELECT project_id,task_id FROM board_workflow_runs WHERE id = ?", (run_id,))
        if run is None:
            raise KeyError(run_id)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT status,definition FROM board_workflow_runs WHERE id = ?", (run_id,))
            run_row = await cursor.fetchone()
            await cursor.close()
            if run_row is None or run_row["status"] in {"cancelled", "failed"}:
                raise WorkflowRefused("workflow is no longer active")
            cursor = await conn.execute(
                "SELECT id FROM board_workflow_runs WHERE task_id = ? "
                "ORDER BY created_at DESC,id DESC LIMIT 1", (run["task_id"],),
            )
            latest = await cursor.fetchone()
            await cursor.close()
            if latest is None or latest["id"] != run_id:
                raise WorkflowRefused("a newer workflow superseded this approval")
            definition = json.loads(run_row["definition"])
            node = next((item for item in definition["nodes"] if item["id"] == node_id), None)
            if node is None or node["kind"] != "approval":
                raise WorkflowRefused("the selected node is not an approval")
            cursor = await conn.execute(
                "SELECT * FROM board_workflow_steps WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            )
            step = await cursor.fetchone()
            await cursor.close()
            if step is None or step["status"] not in {"ready", "blocked"} or step["step_revision"] != expected_step_revision:
                raise WorkflowRefused("approval state or revision changed")
            cursor = await conn.execute(
                "SELECT contract_revision FROM board_tasks WHERE id = ?", (node["task_id"],),
            )
            source = await cursor.fetchone()
            await cursor.close()
            projected, source_digest = await self._project_node(conn, node, step["status"])
            if (source is None or source["contract_revision"] != expected_source_contract_revision
                    or projected != "ready" or not source_digest or source_digest != expected_input_digest
                    or step["input_digest"] != source_digest):
                raise WorkflowRefused("approval source or contract changed")
            cursor = await conn.execute("SELECT * FROM board_workflow_steps WHERE run_id = ?", (run_id,))
            rows = {row["node_id"]: row for row in await cursor.fetchall()}
            await cursor.close()
            if not await self._predecessors_current(conn, definition, rows, node_id):
                raise WorkflowRefused("approval predecessors changed")
            await conn.execute(
                "UPDATE board_workflow_steps SET status = 'completed', step_revision = step_revision + 1, "
                "receipt_id = ?, reuse_fingerprint = ? WHERE run_id = ? AND node_id = ? AND step_revision = ?",
                (mutation.receipt_id, source_digest, run_id, node_id, step["step_revision"]),
            )
            return {"run_id": run_id, "node_id": node_id, "status": "completed",
                    "source_contract_revision": expected_source_contract_revision,
                    "source_digest": source_digest}

        response = await ControlStore(self.db).mutate(
            principal, Scope("project", run["project_id"]), "workflow.approve", client_operation_id,
            expected_entity_revision, Entity("task", run["task_id"]),
            {"run_id": run_id, "node_id": node_id, "expected_step_revision": expected_step_revision,
             "expected_source_contract_revision": expected_source_contract_revision,
             "expected_input_digest": expected_input_digest}, effect,
        )
        try:
            await self.advance_run(run_id)
        except (KeyError, WorkflowRefused):
            # The approval receipt has committed; the sweeper will reconcile the projection. A
            # projection failure cannot be reported as if the approval write itself were refused.
            logger.exception("approved workflow %s needs projection reconciliation", run_id)
        return response

    async def cancel_command(
        self, principal: Principal, run_id: str, reason: str, *,
        expected_entity_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        if not 1 <= len(reason.strip()) <= 1000:
            raise WorkflowRefused("cancellation needs a bounded reason")
        run = await self.db.fetchone("SELECT project_id,task_id FROM board_workflow_runs WHERE id = ?", (run_id,))
        if run is None:
            raise KeyError(run_id)

        async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute("SELECT status FROM board_workflow_runs WHERE id = ?", (run_id,))
            current = await cursor.fetchone()
            await cursor.close()
            if current is None or current["status"] in {"cancelled", "completed", "failed"}:
                raise WorkflowRefused("workflow cannot be cancelled in its current state")
            at = datetime.now(UTC).isoformat()
            await conn.execute(
                "UPDATE board_workflow_runs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (at, run_id),
            )
            await conn.execute(
                "UPDATE board_workflow_steps SET status = 'cancelled', step_revision = step_revision + 1 "
                "WHERE run_id = ? AND status NOT IN ('completed','cancelled','failed')",
                (run_id,),
            )
            return {"run_id": run_id, "status": "cancelled"}

        return await ControlStore(self.db).mutate(
            principal, Scope("project", run["project_id"]), "workflow.cancel", client_operation_id,
            expected_entity_revision, Entity("task", run["task_id"]),
            {"run_id": run_id, "reason": reason.strip()}, effect,
        )

    async def sweep(self) -> None:
        while True:
            rows = await self.db.fetchall(
                "SELECT id FROM board_workflow_runs WHERE status IN ('pending','running','blocked','completed') "
                "ORDER BY CASE WHEN status = 'completed' THEN 1 ELSE 0 END, updated_at DESC,id LIMIT 100"
            )
            for row in rows:
                try:
                    await self.advance_run(row["id"])
                except Exception:
                    # The persisted run stays inspectable; another run must not be starved by one bad row.
                    logger.exception("board workflow %s could not advance", row["id"])
            await asyncio.sleep(5)


async def install(app: Application) -> list[asyncio.Task[None]]:
    dispatcher = app.extensions["effects"]
    service = BoardWorkflows(app.db, dispatcher, app.manager.files if app.manager is not None else None)
    dispatcher.register("workflow.advance", service)
    app.extensions["board_workflows"] = service
    return [asyncio.create_task(service.sweep(), name="board-workflow-sweep")]
