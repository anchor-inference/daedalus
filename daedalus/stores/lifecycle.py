"""Explicit parent-child ownership for bounded cancellation.

Call admission inside the transaction that creates a child. A creator label or matching project is
not ownership: the caller must supply the parent and host-attested principal at creation time.
"""

from __future__ import annotations

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
    existing = await one(conn, "SELECT * FROM lifecycle_owners WHERE child_kind = ? AND child_id = ?", (child_kind, child_id))
    if existing is not None:
        if (existing["parent_kind"], existing["parent_id"], existing["project_id"], existing["generation"], existing["source_revision"]) != (parent_kind, parent_id, project_id, generation, source_revision):
            raise LifecycleRefused("child already belongs to another parent generation")
        return dict(existing)
    await conn.execute(
        "INSERT INTO lifecycle_owners(parent_kind,parent_id,project_id,generation,child_kind,child_id,cancel_state,created_at,updated_at,source_revision) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?)",
        (parent_kind, parent_id, project_id, generation, child_kind, child_id, at, at, source_revision),
    )
    return {"parent_kind": parent_kind, "parent_id": parent_id, "project_id": project_id,
            "generation": generation, "child_kind": child_kind, "child_id": child_id,
            "cancel_state": "active", "source_revision": source_revision}


__all__ = ["LifecycleRefused", "admit_child"]
