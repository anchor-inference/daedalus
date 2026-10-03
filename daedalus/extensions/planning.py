"""Bound project plan admission by measured use, finite task count, and dependency depth."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import aiosqlite

from daedalus.extensions.orchestrator_domain import DomainConflict, check_planning_capacity
from daedalus.stores.control import Mutation, now


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def token_readiness(conn: aiosqlite.Connection, project_id: str) -> dict[str, Any]:
    """Unknown CLI metering blocks further plan admission; silence is not zero use."""
    budget = await _one(conn, "SELECT max_tokens,used_tokens,goal_contract_revision FROM planning_budgets"
                        " WHERE project_id = ?", (project_id,))
    if budget is None:
        raise DomainConflict("the project has no planning budget")
    native = await _one(conn, "SELECT coalesce(sum(u.input_tokens + u.output_tokens),0) AS tokens"
                        " FROM usage_events u JOIN sessions s ON s.id = u.session_id"
                        " WHERE s.project_id = ?", (project_id,))
    async with conn.execute("SELECT ss.usage_json FROM staff_sessions ss JOIN staff m ON m.id = ss.staff_id"
                            " WHERE m.project_id = ? AND ss.kind = 'cli'", (project_id,)) as cursor:
        cli_rows = await cursor.fetchall()
    cli_tokens = 0
    for row in cli_rows:
        try:
            usage = json.loads(row["usage_json"] or "{}")
            if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
                raise ValueError("missing token counters")
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            if input_tokens < 0 or output_tokens < 0:
                raise ValueError("negative token counter")
        except (TypeError, ValueError):
            return {"project_id": project_id, "status": "unknown", "reason": "CLI token usage is unavailable",
                    "max_tokens": budget["max_tokens"], "reserved_tokens": budget["used_tokens"]}
        cli_tokens += input_tokens + output_tokens
    measured = int(native["tokens"]) + cli_tokens if native is not None else cli_tokens
    return {"project_id": project_id, "status": "measured", "measured_tokens": measured,
            "reserved_tokens": budget["used_tokens"], "max_tokens": budget["max_tokens"],
            "goal_contract_revision": budget["goal_contract_revision"],
            "remaining_tokens": max(0, budget["max_tokens"] - measured - budget["used_tokens"])}


async def create_plan(conn: aiosqlite.Connection, mutation: Mutation, *, project_id: str,
                      tasks: list[dict[str, Any]], fanout_reason: str) -> dict[str, Any]:
    """Create a bounded set of cards and their first immutable contracts in one receipt transaction."""
    if not 1 <= len(tasks) <= 10:
        raise ValueError("a plan needs one to ten tasks")
    if len(tasks) > 1 and not fanout_reason.strip():
        raise ValueError("parallel tasks need a fanout reason")
    titles = [str(task.get("title") or "").strip() for task in tasks]
    if any(not title or len(title) > 200 for title in titles):
        raise ValueError("every planned task needs a title of at most 200 characters")
    usage = await token_readiness(conn, project_id)
    if usage["status"] != "measured":
        raise DomainConflict("token usage is unknown; resolve metering before planning more work")
    estimate = max(1, len(_canonical(tasks).encode("utf-8")) // 4)
    if estimate > usage["remaining_tokens"]:
        raise DomainConflict("the plan exceeds the measured token admission budget")
    goal = await _one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (project_id,))
    if goal is None:
        raise KeyError(project_id)
    digest = hashlib.sha256(_canonical({"goal_revision": goal["goal_revision"], "tasks": tasks}).encode()).hexdigest()
    repeated = await _one(conn, "SELECT count FROM replan_fingerprints WHERE project_id = ?"
                          " AND contract_revision = ? AND digest = ?", (project_id, goal["goal_revision"], digest))
    if repeated is not None and repeated["count"] >= 2:
        raise DomainConflict("the same plan has already been repeated twice")
    created = []
    for index, task in enumerate(tasks):
        dependencies = list(task.get("depends_on") or [])
        if not all(isinstance(item, str) and item for item in dependencies):
            raise ValueError("dependencies must name existing task ids")
        capacity = await check_planning_capacity(conn, project_id, dependencies)
        task_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mutation.object_id}:{index}").hex[:12]
        title = titles[index]
        status = "blocked" if dependencies else "todo"
        await conn.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                           " created_at,updated_at,project_id,brief_json) VALUES (?,?,?,3,'','[]',?,?,?,?,?)",
                           (task_id, title, status, _canonical(dependencies), now(), now(), project_id,
                            _canonical(task.get("brief") or {})))
        await conn.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                           " snapshot_json,created_at) VALUES (?,1,'operator','',?,?)",
                           (task_id, _canonical({"requirements": [], "checklist": [], "acceptance": "",
                                                  "depends_on": dependencies, "brief": task.get("brief") or {}}), now()))
        await conn.execute("INSERT INTO workflow_steps(id,task_id,step_kind,state,contract_revision)"
                           " VALUES (?,?,'work','pending',1)", (f"task:{task_id}:work", task_id))
        for dependency in dependencies:
            await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                               " resolution_state,created_at) VALUES (?,?,?,'required','awaiting_result',?)",
                               (uuid.uuid4().hex, task_id, dependency, now()))
        created.append({"task_id": task_id, "title": title, "depth": capacity["depth"]})
    await conn.execute("UPDATE planning_budgets SET used_tokens = used_tokens + ?,"
                       " entity_revision = entity_revision + 1 WHERE project_id = ?", (estimate, project_id))
    await conn.execute("INSERT INTO replan_fingerprints(project_id,contract_revision,digest,count) VALUES (?,?,?,1)"
                       " ON CONFLICT(project_id,contract_revision,digest) DO UPDATE SET count = count + 1",
                       (project_id, goal["goal_revision"], digest))
    return {"plan_id": mutation.object_id, "accepted_tasks": created,
            "remaining_tasks": capacity["remaining_tasks"], "estimated_tokens_reserved": estimate,
            "measured_tokens_before": usage["measured_tokens"], "token_status": "measured"}


__all__ = ["create_plan", "token_readiness"]
