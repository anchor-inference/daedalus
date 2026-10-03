"""Explicit parent-child ownership for bounded cancellation.

Call admission inside the transaction that creates a child. A creator label or matching project is
not ownership: the caller must supply the parent and host-attested principal at creation time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope, one


class LifecycleRefused(ValueError):
    """An ownership edge is stale, ambiguous or outside its parent scope."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def admit_child(
    conn: aiosqlite.Connection, *, control: ControlStore, principal: Principal,
    parent_kind: str, parent_id: str, project_id: str, child_kind: str, child_id: str,
    goal_revision: int | None = None,
) -> dict[str, Any]:
    """Atomically pin a real child to the parent's current uncancelled generation."""
    if parent_kind not in {"task", "project_goal"} or child_kind not in {"task", "execution_attempt"}:
        raise LifecycleRefused("unsupported owner or child kind")
    if not child_id or not parent_id or not project_id:
        raise LifecycleRefused("an exact parent, child and project are required")
    if child_kind == "task" and child_id == parent_id:
        raise LifecycleRefused("a task cannot own itself")
    if parent_kind == "project_goal":
        if parent_id != project_id or principal.origin_class != "operator":
            raise ControlDenied("goal ownership requires an authenticated project operator")
        await control.authorize(conn, principal, Scope("project", project_id), "goal.revise")
        project = await one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (project_id,))
        if project is None or goal_revision != project["goal_revision"]:
            raise LifecycleRefused("project goal revision changed before admission")
        source_revision = goal_revision
        contract_revision = None
    else:
        task = await one(conn, "SELECT project_id,contract_revision FROM board_tasks WHERE id = ?", (parent_id,))
        if task is None or task["project_id"] != project_id or goal_revision is not None:
            raise LifecycleRefused("task parent or contract is outside this project")
        await control.authorize(conn, principal, Scope("project", project_id), "task.launch", task_id=parent_id)
        source_revision = task["contract_revision"]
        contract_revision = task["contract_revision"]
    if child_kind == "task":
        child = await one(conn, "SELECT project_id FROM board_tasks WHERE id = ?", (child_id,))
        if child is None or child["project_id"] != project_id:
            raise LifecycleRefused("child task is outside the parent project")
    else:
        child = await one(
            conn, "SELECT t.project_id,a.task_id,a.staff_session_id FROM execution_attempts a "
            "JOIN board_tasks t ON t.id = a.task_id WHERE a.id = ?", (child_id,),
        )
        if child is None or child["project_id"] != project_id or child["task_id"] != parent_id or not child["staff_session_id"]:
            raise LifecycleRefused("execution attempt is not hosted by its task parent")
    parent = await one(conn, "SELECT * FROM lifecycle_parents WHERE parent_kind = ? AND parent_id = ?", (parent_kind, parent_id))
    at = _now()
    if parent is None:
        generation = 1
        await conn.execute(
            "INSERT INTO lifecycle_parents(parent_kind,parent_id,project_id,generation,cancel_state,updated_at,goal_revision,contract_revision) "
            "VALUES (?,?,?,1,'active',?,?,?)",
            (parent_kind, parent_id, project_id, at, goal_revision, contract_revision),
        )
    else:
        if parent["project_id"] != project_id:
            raise LifecycleRefused("parent project changed")
        if parent_kind == "project_goal" and goal_revision > (parent["goal_revision"] or 0):
            generation = parent["generation"] + 1
            await conn.execute(
                "UPDATE lifecycle_parents SET generation = ?,cancel_state = 'active',goal_revision = ?,updated_at = ? "
                "WHERE parent_kind = ? AND parent_id = ?",
                (generation, goal_revision, at, parent_kind, parent_id),
            )
        elif parent_kind == "task" and contract_revision > (parent["contract_revision"] or 0):
            generation = parent["generation"] + 1
            await conn.execute(
                "UPDATE lifecycle_parents SET generation = ?,cancel_state = 'active',contract_revision = ?,updated_at = ? "
                "WHERE parent_kind = ? AND parent_id = ?",
                (generation, contract_revision, at, parent_kind, parent_id),
            )
        elif parent["cancel_state"] != "active" or source_revision != (parent["goal_revision"] if parent_kind == "project_goal" else parent["contract_revision"]):
            raise LifecycleRefused("parent was cancelled or its source revision changed")
        else:
            generation = parent["generation"]
    existing = await one(
        conn, "SELECT * FROM lifecycle_owners WHERE child_kind = ? AND child_id = ? "
        "AND cancel_state IN ('active','requested','acknowledged','unknown')", (child_kind, child_id),
    )
    if existing is not None:
        exact = (existing["parent_kind"], existing["parent_id"], existing["project_id"],
                 existing["generation"], existing["source_revision"]) == (
                     parent_kind, parent_id, project_id, generation, source_revision)
        if exact:
            return dict(existing)
        transferable = (child_kind == "task" and existing["cancel_state"] == "active"
                        and existing["parent_kind"] == parent_kind and existing["parent_id"] == parent_id
                        and existing["project_id"] == project_id and existing["generation"] < generation)
        if not transferable:
            raise LifecycleRefused("child already belongs to another parent generation")
        moved = await conn.execute(
            "UPDATE lifecycle_owners SET cancel_state = 'transferred',updated_at = ? "
            "WHERE parent_kind = ? AND parent_id = ? AND child_kind = ? AND child_id = ? "
            "AND generation = ? AND cancel_state = 'active'",
            (at, parent_kind, parent_id, child_kind, child_id, existing["generation"]),
        )
        if moved.rowcount != 1:
            raise LifecycleRefused("child ownership changed during transfer")
    await conn.execute(
        "INSERT INTO lifecycle_owners(parent_kind,parent_id,project_id,generation,child_kind,child_id,cancel_state,created_at,updated_at,source_revision) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?)",
        (parent_kind, parent_id, project_id, generation, child_kind, child_id, at, at, source_revision),
    )
    return {"parent_kind": parent_kind, "parent_id": parent_id, "project_id": project_id,
            "generation": generation, "child_kind": child_kind, "child_id": child_id,
            "cancel_state": "active", "source_revision": source_revision}


async def record_owned_exit(
    conn: aiosqlite.Connection, *, attempt_id: str, staff_session_id: str,
    host_generation: int, provider_session_ref: str,
) -> bool:
    """Close an owned cancellation only from a host-observed exit of the exact runtime identity.

    Call this after the native session or terminal exit event is persisted. A stop request, an ended
    database row alone, or a caller-supplied report is not process-exit evidence.
    """
    row = await one(
        conn, "SELECT a.task_id,a.contract_revision,a.state,a.host_generation,a.provider_session_ref,"
        "a.staff_session_id,t.current_attempt_id,s.ended_at,o.parent_kind,o.parent_id,o.generation,"
        "o.source_revision,o.cancel_state FROM execution_attempts a "
        "JOIN board_tasks t ON t.id = a.task_id "
        "JOIN staff_sessions s ON s.id = a.staff_session_id "
        "JOIN lifecycle_owners o ON o.child_kind = 'execution_attempt' AND o.child_id = a.id "
        "AND o.cancel_state IN ('requested','acknowledged','unknown') WHERE a.id = ?",
        (attempt_id,),
    )
    if (row is None or row["staff_session_id"] != staff_session_id or row["host_generation"] != host_generation
            or row["provider_session_ref"] != provider_session_ref or not provider_session_ref
            or row["current_attempt_id"] != attempt_id or row["source_revision"] != row["contract_revision"]
            or not row["ended_at"]):
        return False
    host = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
    proof = await one(conn, "SELECT 1 FROM runtime_exit_observations e JOIN execution_attempts a ON a.id = e.attempt_id"
                      " WHERE e.attempt_id = ? AND e.staff_session_id = ? AND e.host_generation = ?"
                      " AND e.provider_session_ref = ? AND e.contract_revision = a.contract_revision"
                      " AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                      " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance))",
                      (attempt_id, staff_session_id, host_generation, provider_session_ref))
    if host is None or json.loads(host["value"]) != host_generation or proof is None:
        return False
    if row["state"] in {"queued", "starting", "running", "waiting", "recovering"}:
        await conn.execute(
            "UPDATE execution_attempts SET state = 'cancelled',updated_at = ? WHERE id = ?",
            (_now(), attempt_id),
        )
    await conn.execute(
        "UPDATE lifecycle_owners SET cancel_state = 'drained',last_observed_at = ?,updated_at = ? "
        "WHERE parent_kind = ? AND parent_id = ? AND generation = ? "
        "AND child_kind = 'execution_attempt' AND child_id = ?",
        (_now(), _now(), row["parent_kind"], row["parent_id"], row["generation"], attempt_id),
    )
    return True


__all__ = ["LifecycleRefused", "admit_child", "record_owned_exit"]
