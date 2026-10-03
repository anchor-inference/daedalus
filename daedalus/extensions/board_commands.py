"""Authenticated board writes share one durable command and one SQLite transaction."""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from daedalus.extensions.orchestrator_domain import DomainConflict, capture_contract_change, check_planning_capacity
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Mutation, Principal, Scope, now
from daedalus.stores.database import Database


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def _dependencies(conn: aiosqlite.Connection, task_id: str | None, project_id: str | None,
                        dependencies: list[str]) -> bool:
    if len(dependencies) != len(set(dependencies)) or len(dependencies) > 20:
        raise ValueError("a task needs at most twenty distinct dependencies")
    frontier = list(dependencies)
    seen: set[str] = set()
    ready = True
    while frontier:
        current = frontier.pop()
        if current == task_id:
            raise DomainConflict("a task cannot depend on itself")
        if current in seen:
            continue
        if len(seen) >= 500:
            raise DomainConflict("the dependency graph is too large to verify")
        seen.add(current)
        row = await _one(conn, "SELECT project_id,depends_on,status,accepted_result_id FROM board_tasks"
                         " WHERE id = ?", (current,))
        if row is None or row["project_id"] != project_id:
            raise DomainConflict("a dependency is absent or outside the board")
        if current in dependencies and (row["status"] != "done" or not row["accepted_result_id"]):
            ready = False
        frontier.extend(json.loads(row["depends_on"] or "[]"))
    return ready


async def insert_task(
    conn: aiosqlite.Connection, mutation: Mutation, *, scope: Scope, title: str,
    acceptance: str, checklist: list[str], dependencies: list[str], priority: int,
    brief: dict[str, str], notes: str, source_session_id: str | None,
    origin_kind: str,
) -> dict[str, Any]:
    """Insert the card, its first contract and dependency gates inside the caller's receipt."""
    if not title.strip() or len(title) > 200 or len(acceptance) > 2000 or len(notes) > 4000:
        raise ValueError("task title, acceptance or notes exceed their bounds")
    if not 1 <= priority <= 5 or len(checklist) > 12 or any(not c.strip() or len(c) > 200 for c in checklist):
        raise ValueError("task priority or checklist is invalid")
    if set(brief) - {"objective", "deliverable", "boundaries", "done_when"} or any(len(value) > 4000 for value in brief.values()):
        raise ValueError("task brief contains invalid fields")
    project_id = scope.id if scope.kind == "project" else None
    if project_id and await _one(conn, "SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
        raise KeyError(project_id)
    task_id = mutation.object_id[:12]
    ready = await _dependencies(conn, task_id, project_id, dependencies)
    if project_id:
        await check_planning_capacity(conn, project_id, dependencies)
    status = "todo" if ready else "blocked"
    checks = [{"id": f"C{index}", "text": item.strip()} for index, item in enumerate(checklist, 1)]
    await conn.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                       " session_id,origin_session_id,notes,created_at,updated_at,project_id,brief_json)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (task_id, title.strip(), status, priority, acceptance.strip(),
                        _canonical([{**item, "done": False} for item in checks]), _canonical(dependencies),
                        source_session_id, source_session_id, notes, now(), now(), project_id, _canonical(brief)))
    await conn.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                       " snapshot_json,created_at) VALUES (?,1,?,?,?,?)",
                       (task_id, origin_kind, source_session_id or "",
                        _canonical({"requirements": [], "checklist": checks, "acceptance": acceptance.strip(),
                                    "depends_on": dependencies, "brief": brief}), now()))
    await conn.execute("INSERT INTO workflow_steps(id,task_id,step_kind,state,contract_revision)"
                       " VALUES (?,?,'work','pending',1)", (f"task:{task_id}:work", task_id))
    for dependency in dependencies:
        await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                           " resolution_state,created_at) VALUES (?,?,?,'required','awaiting_result',?)",
                           (uuid.uuid4().hex, task_id, dependency, now()))
    return {"task_id": task_id, "title": title.strip(), "status": status,
            "contract_revision": 1, "project_id": project_id}


class BoardCommands:
    """Public task mutations accept only host identities, never a `by` label."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.control = ControlStore(db)

    async def create(self, principal: Principal, scope: Scope, *, client_operation_id: str,
                     expected_collection_revision: int, title: str, acceptance: str = "",
                     checklist: list[str] | None = None, depends_on: list[str] | None = None,
                     priority: int = 3, brief: dict[str, str] | None = None, notes: str = "",
                     source_session_id: str | None = None) -> dict[str, Any]:
        payload = {"title": title, "acceptance": acceptance, "checklist": checklist or [],
                   "depends_on": depends_on or [], "priority": priority, "brief": brief or {},
                   "notes": notes, "source_session_id": source_session_id}

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            return await insert_task(conn, mutation, scope=scope, title=title, acceptance=acceptance,
                                     checklist=checklist or [], dependencies=depends_on or [], priority=priority,
                                     brief=brief or {}, notes=notes, source_session_id=source_session_id,
                                     origin_kind=principal.origin_class)

        return await self.control.mutate(principal, scope, "board.task.create", client_operation_id,
                                         expected_collection_revision, Entity("collection", scope.id), payload, effect)

    async def update(self, principal: Principal, scope: Scope, task_id: str, *,
                     client_operation_id: str, expected_entity_revision: int,
                     title: str | None = None, acceptance: str | None = None,
                     checklist: list[str] | None = None, depends_on: list[str] | None = None,
                     priority: int | None = None, brief: dict[str, str] | None = None,
                     note: str = "", status: str | None = None) -> dict[str, Any]:
        payload = {"title": title, "acceptance": acceptance, "checklist": checklist,
                   "depends_on": depends_on, "priority": priority, "brief": brief,
                   "note": note, "status": status}

        async def effect(conn: aiosqlite.Connection, _: Mutation) -> dict[str, Any]:
            row = await _one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
            if row is None or row["project_id"] != (scope.id if scope.kind == "project" else None):
                raise ControlConflict("task is outside the command scope")
            if status not in (None, "todo", "blocked", "dropped"):
                raise DomainConflict("work, review and completion require their dedicated result or launch command")
            if status == "todo" and row["status"] in ("review", "done"):
                raise DomainConflict("return the exact reviewed result before reopening")
            if status in ("blocked", "dropped") and row["current_attempt_id"]:
                attempt = await _one(conn, "SELECT state FROM execution_attempts WHERE id = ?",
                                     (row["current_attempt_id"],))
                if attempt is not None and attempt["state"] in ("queued", "starting", "running", "waiting", "recovering"):
                    raise DomainConflict("stop or reconcile the current execution before changing task state")
            semantic = any(value is not None for value in (acceptance, checklist, depends_on, brief))
            if semantic and row["current_attempt_id"]:
                attempt = await _one(conn, "SELECT state FROM execution_attempts WHERE id = ?",
                                     (row["current_attempt_id"],))
                if attempt is not None and attempt["state"] in ("queued", "starting", "running", "waiting", "recovering"):
                    raise DomainConflict("stop the active attempt before revising its contract")
            if title is not None and (not title.strip() or len(title) > 200):
                raise ValueError("task title is invalid")
            if acceptance is not None and len(acceptance) > 2000:
                raise ValueError("task acceptance is too long")
            if priority is not None and (isinstance(priority, bool) or not 1 <= priority <= 5):
                raise ValueError("task priority is invalid")
            if len(note) > 1000:
                raise ValueError("task note is too long")
            current_brief = json.loads(row["brief_json"] or "{}")
            if brief is not None:
                if set(brief) - {"objective", "deliverable", "boundaries", "done_when"} or any(len(value) > 4000 for value in brief.values()):
                    raise ValueError("task brief contains invalid fields")
                current_brief.update(brief)
            dependencies = json.loads(row["depends_on"] or "[]") if depends_on is None else depends_on
            ready = await _dependencies(conn, task_id, row["project_id"], dependencies)
            if checklist is None:
                checks = json.loads(row["checklist"] or "[]")
            else:
                if len(checklist) > 12 or any(not item.strip() or len(item) > 200 for item in checklist):
                    raise ValueError("task checklist is invalid")
                prior = json.loads(row["checklist"] or "[]")
                remaining = {item["text"]: item for item in prior if item.get("id") and item.get("text")}
                next_id = max((int(item["id"][1:]) for item in prior
                               if str(item.get("id", "")).startswith("C") and str(item["id"])[1:].isdigit()), default=0)
                checks = []
                for text in checklist:
                    text = text.strip()
                    old = remaining.pop(text, None)
                    if old is None:
                        next_id += 1
                        old = {"id": f"C{next_id}", "text": text, "done": False}
                    checks.append(old)
            next_status = status or row["status"]
            if next_status in ("todo", "blocked") and depends_on is not None:
                next_status = "todo" if ready else "blocked"
            notes = row["notes"] or ""
            if note:
                notes = (notes + "\n" if notes else "") + f"[{now()[:16]}] {note}"
            await conn.execute("UPDATE board_tasks SET title = ?,acceptance = ?,checklist = ?,depends_on = ?,"
                               " priority = ?,brief_json = ?,notes = ?,status = ?,updated_at = ? WHERE id = ?",
                               (title.strip() if title is not None else row["title"],
                                acceptance.strip() if acceptance is not None else row["acceptance"],
                                _canonical(checks), _canonical(dependencies), priority or row["priority"],
                                _canonical(current_brief), notes[-8000:], next_status, now(), task_id))
            contract_revision = await capture_contract_change(conn, task_id,
                                                              origin_kind=principal.origin_class,
                                                              origin_ref=principal.actor_id)
            if depends_on is not None and dependencies != json.loads(row["depends_on"] or "[]"):
                await conn.execute("UPDATE task_dependency_edges SET kind = 'cancelled',resolution_state = 'cancelled'"
                                   " WHERE successor_task_id = ? AND resolution_state != 'cancelled'", (task_id,))
                for dependency in dependencies:
                    await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,"
                                       " kind,resolution_state,created_at)"
                                       " VALUES (?,?,?,'required','awaiting_result',?)",
                                       (uuid.uuid4().hex, task_id, dependency, now()))
            return {"task_id": task_id, "status": next_status, "contract_revision": contract_revision}

        return await self.control.mutate(principal, scope, "board.task.update", client_operation_id,
                                         expected_entity_revision, Entity("task", task_id), payload, effect)


__all__ = ["BoardCommands", "insert_task"]
