"""Authenticated board writes share one durable command and one SQLite transaction."""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from daedalus.extensions.orchestrator_domain import DomainConflict, capture_contract_change, check_planning_capacity
from daedalus.extensions.task_contract import REQUIREMENT_KINDS, REQUIREMENTS_MAX, check_items
from daedalus.host.events import AppEvent, EventBus
from daedalus.stores.comparison_funding import physical_exit_in
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


async def _assignee(conn: aiosqlite.Connection, staff_id: str, project_id: str | None) -> None:
    if project_id is None:
        raise DomainConflict("a global card cannot assign project staff")
    staff = await _one(conn, "SELECT project_id,archived_at FROM staff WHERE id = ?", (staff_id,))
    if staff is None or staff["project_id"] != project_id or staff["archived_at"] is not None:
        raise DomainConflict("assignee is absent, archived or outside the project")


async def _handoff(conn: aiosqlite.Connection, task_id: str, project_id: str | None, *,
                   folder_id: str | None, file_ids: list[str],
                   requirements: list[dict[str, str]]) -> None:
    if not folder_id and not file_ids and not requirements:
        return
    if project_id is None:
        raise DomainConflict("a project handoff needs a project task")
    if folder_id:
        folder = await _one(conn, "SELECT project_id FROM project_folders WHERE id = ?", (folder_id,))
        if folder is None or folder["project_id"] != project_id:
            raise DomainConflict("handoff folder is outside the project")
        await conn.execute("UPDATE board_tasks SET folder_id = ? WHERE id = ?", (folder_id, task_id))
    for file_id in set(file_ids).union(str(item.get("file_id") or "") for item in requirements):
        if not file_id:
            continue
        access = await _one(conn, "SELECT 1 FROM file_access WHERE file_id = ? AND scope = ?",
                            (file_id, project_id))
        if access is None:
            raise DomainConflict("handoff file is no longer available to the project")
        await conn.execute("INSERT OR IGNORE INTO task_files(task_id,file_id,added_at,added_by)"
                           " VALUES (?,?,?,'orchestrator')", (task_id, file_id, now()))
    count = await _one(conn, "SELECT SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) AS n,"
                       " COALESCE(MAX(number),0) AS last_number FROM task_requirements WHERE task_id = ?", (task_id,))
    assert count is not None
    if int(count["n"] or 0) + len(requirements) > REQUIREMENTS_MAX:
        raise DomainConflict("too many active requirements for this task")
    number = int(count["last_number"])
    for item in requirements:
        body = " ".join(str(item.get("text") or "").split())
        kind = str(item.get("kind") or "")
        source = str(item.get("source") or "")
        if not body or len(body) > 1000 or kind not in REQUIREMENT_KINDS or not source:
            raise ValueError("handoff requirement is invalid")
        existing = await _one(conn, "SELECT id FROM task_requirements WHERE task_id = ? AND state = 'active'"
                              " AND text = ? AND file_id IS ?", (task_id, body, item.get("file_id") or None))
        if existing is not None:
            continue
        number += 1
        await conn.execute("INSERT INTO task_requirements(id,task_id,project_id,number,text,kind,source,file_id,"
                           " created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex[:10], task_id, project_id, number, body, kind, source,
                            item.get("file_id") or None, now(), now()))


async def insert_task(
    conn: aiosqlite.Connection, mutation: Mutation, *, scope: Scope, title: str,
    acceptance: str, checklist: list[str], dependencies: list[str], priority: int,
    brief: dict[str, str], notes: str, source_session_id: str | None,
    origin_kind: str, assignee_staff_id: str | None = None,
    folder_id: str | None = None, file_ids: list[str] | None = None,
    requirements: list[dict[str, str]] | None = None,
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
    if assignee_staff_id:
        await _assignee(conn, assignee_staff_id, project_id)
    task_id = mutation.object_id[:12]
    ready = await _dependencies(conn, task_id, project_id, dependencies)
    if project_id:
        await check_planning_capacity(conn, project_id, dependencies)
    status = "todo" if ready else "blocked"
    checks = [{"id": f"C{index}", "text": item.strip()} for index, item in enumerate(checklist, 1)]
    await conn.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                       " session_id,origin_session_id,notes,created_at,updated_at,project_id,brief_json,assignee_staff_id)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (task_id, title.strip(), status, priority, acceptance.strip(),
                        _canonical([{**item, "done": False} for item in checks]), _canonical(dependencies),
                        source_session_id, source_session_id, notes, now(), now(), project_id,
                        _canonical(brief), assignee_staff_id))
    for dependency in dependencies:
        await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,kind,"
                           " resolution_state,created_at) VALUES (?,?,?,'required','awaiting_result',?)",
                           (uuid.uuid4().hex, task_id, dependency, now()))
    await _handoff(conn, task_id, project_id, folder_id=folder_id,
                   file_ids=file_ids or [], requirements=requirements or [])
    async with conn.execute("SELECT id,text,kind,source,file_id FROM task_requirements WHERE task_id = ?"
                            " AND state = 'active' ORDER BY number", (task_id,)) as cursor:
        current_requirements = [dict(row) for row in await cursor.fetchall()]
    async with conn.execute("SELECT file_id FROM task_files WHERE task_id = ? ORDER BY file_id",
                            (task_id,)) as cursor:
        current_files = [row["file_id"] for row in await cursor.fetchall()]
    await conn.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                       " snapshot_json,created_at) VALUES (?,1,?,?,?,?)",
                       (task_id, origin_kind, source_session_id or "",
                        _canonical({"title": title.strip(), "requirements": current_requirements, "checklist": checks,
                                    "acceptance": acceptance.strip(), "depends_on": dependencies,
                                    "brief": brief, "folder_id": folder_id, "file_ids": current_files}), now()))
    await conn.execute("INSERT INTO workflow_steps(id,task_id,step_kind,state,contract_revision)"
                       " VALUES (?,?,'work','pending',1)", (f"task:{task_id}:work", task_id))
    return {"task_id": task_id, "title": title.strip(), "status": status,
            "contract_revision": 1, "project_id": project_id}


class BoardCommands:
    """Public task mutations accept only host identities, never a `by` label."""

    def __init__(self, db: Database, *, bus: EventBus | None = None) -> None:
        self.db = db
        self.control = ControlStore(db)
        self.bus = bus

    async def _mutate(self, principal: Principal, scope: Scope, operation: str, operation_id: str,
                      revision: int, entity: Entity, payload: dict[str, Any], effect: Any,
                      event_type: str) -> dict[str, Any]:
        if self.bus is None:
            return await self.control.mutate(principal, scope, operation, operation_id, revision, entity, payload, effect)
        events: list[AppEvent] = []

        async def commit(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            prior = await _one(conn, "SELECT status,assignee_staff_id FROM board_tasks WHERE id = ?", (entity.id,)) if entity.kind == "task" else None
            response = await effect(conn, mutation)
            task = await _one(conn, "SELECT id,title,status,project_id,assignee_staff_id FROM board_tasks WHERE id = ?",
                              (response["task_id"],))
            assert task is not None
            payload = {"task_id": task["id"], "title": task["title"], "actor": principal.origin_class,
                       "actor_id": principal.actor_id}
            announced = [(event_type, payload)]
            if prior is not None and prior["status"] != task["status"]:
                announced.append(("task.moved", {**payload, "from": prior["status"], "to": task["status"]}))
            if (prior["assignee_staff_id"] if prior is not None else None) != task["assignee_staff_id"]:
                assigned = {**payload, "assignee_staff_id": task["assignee_staff_id"]} if task["assignee_staff_id"] else payload
                announced.append(("task.assigned", assigned))
            for kind, body in announced:
                events.append(await self.bus.persist_in(conn, kind, body, project_id=task["project_id"],
                                                        staff_id=task["assignee_staff_id"]))
            return response

        async with self.bus.transaction_guard():
            response = await self.control.mutate(principal, scope, operation, operation_id, revision, entity, payload, commit)
            for event in events:
                self.bus.announce_committed(event)
        return response

    async def create(self, principal: Principal, scope: Scope, *, client_operation_id: str,
                     expected_collection_revision: int, title: str, acceptance: str = "",
                     checklist: list[str] | None = None, depends_on: list[str] | None = None,
                     priority: int = 3, brief: dict[str, str] | None = None, notes: str = "",
                     source_session_id: str | None = None,
                     assignee_staff_id: str | None = None,
                     folder_id: str | None = None, file_ids: list[str] | None = None,
                     requirements: list[dict[str, str]] | None = None) -> dict[str, Any]:
        payload = {"title": title, "acceptance": acceptance, "checklist": checklist or [],
                   "depends_on": depends_on or [], "priority": priority, "brief": brief or {},
                   "notes": notes, "source_session_id": source_session_id,
                   "assignee_staff_id": assignee_staff_id, "folder_id": folder_id,
                   "file_ids": file_ids or [], "requirements": requirements or []}

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            return await insert_task(conn, mutation, scope=scope, title=title, acceptance=acceptance,
                                     checklist=checklist or [], dependencies=depends_on or [], priority=priority,
                                     brief=brief or {}, notes=notes, source_session_id=source_session_id,
                                     origin_kind=principal.origin_class,
                                     assignee_staff_id=assignee_staff_id, folder_id=folder_id,
                                     file_ids=file_ids, requirements=requirements)

        return await self._mutate(principal, scope, "board.task.create", client_operation_id,
                                  expected_collection_revision, Entity("collection", scope.id), payload, effect, "task.created")

    async def update(self, principal: Principal, scope: Scope, task_id: str, *,
                     client_operation_id: str, expected_entity_revision: int,
                     title: str | None = None, acceptance: str | None = None,
                     checklist: list[str] | None = None, depends_on: list[str] | None = None,
                     priority: int | None = None, brief: dict[str, str] | None = None,
                     note: str = "", status: str | None = None,
                     check_ids: list[str] | None = None, uncheck_ids: list[str] | None = None,
                     assignee_staff_id: str | None = None,
                     folder_id: str | None = None, file_ids: list[str] | None = None,
                     requirements: list[dict[str, str]] | None = None,
                     reassignment_reason: str | None = None) -> dict[str, Any]:
        payload = {"title": title, "acceptance": acceptance, "checklist": checklist,
                   "depends_on": depends_on, "priority": priority, "brief": brief,
                   "note": note, "status": status, "check_ids": check_ids,
                   "uncheck_ids": uncheck_ids, "assignee_staff_id": assignee_staff_id,
                   "folder_id": folder_id, "file_ids": file_ids or [],
                   "requirements": requirements or [], "reassignment_reason": reassignment_reason}

        async def effect(conn: aiosqlite.Connection, _: Mutation) -> dict[str, Any]:
            row = await _one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
            if row is None or row["project_id"] != (scope.id if scope.kind == "project" else None):
                raise ControlConflict("task is outside the command scope")
            if status not in (None, "todo", "blocked", "dropped"):
                raise DomainConflict("work, review and completion require their dedicated result or launch command")
            if status is not None and status != row["status"] and row["status"] in ("review", "done"):
                raise DomainConflict("return the exact reviewed result before reopening")
            if status in ("blocked", "dropped") and row["current_attempt_id"]:
                attempt = await _one(conn, "SELECT state FROM execution_attempts WHERE id = ?",
                                     (row["current_attempt_id"],))
                if attempt is not None and attempt["state"] in ("queued", "starting", "running", "waiting", "recovering"):
                    raise DomainConflict("stop or reconcile the current execution before changing task state")
            if assignee_staff_id is not None and assignee_staff_id != row["assignee_staff_id"]:
                if row["current_attempt_id"]:
                    attempt = await _one(conn, "SELECT state FROM execution_attempts WHERE id = ?",
                                         (row["current_attempt_id"],))
                    if attempt is not None and attempt["state"] in ("queued", "starting", "running", "waiting", "recovering"):
                        raise DomainConflict("stop or reconcile the current execution before reassigning")
                if assignee_staff_id:
                    await _assignee(conn, assignee_staff_id, row["project_id"])
            if reassignment_reason:
                if (not assignee_staff_id or assignee_staff_id == row["assignee_staff_id"]
                        or not row["assignee_staff_id"] or len(reassignment_reason.strip()) < 8):
                    raise DomainConflict("a named owner change and its reason are required for a handover")
                old_owner = await _one(conn, "SELECT name FROM staff WHERE id = ? AND project_id = ?",
                                       (row["assignee_staff_id"], row["project_id"]))
                new_owner = await _one(conn, "SELECT name FROM staff WHERE id = ? AND project_id = ?",
                                       (assignee_staff_id, row["project_id"]))
                if old_owner is None or new_owner is None:
                    raise DomainConflict("the handover owners are outside this project")
                await conn.execute("INSERT INTO project_journal(project_id,at,author,kind,text,refs_json)"
                                   " VALUES (?,?,?,'reassignment',?,?)",
                                   (row["project_id"], now(), "orchestrator",
                                    f"Task {task_id} \"{row['title']}\" passed from {old_owner['name']} to {new_owner['name']}: "
                                    f"{reassignment_reason.strip()}",
                                    _canonical({"task_id": task_id, "actor_id": principal.actor_id,
                                                "from_staff_id": row["assignee_staff_id"],
                                                "to_staff_id": assignee_staff_id})))
            semantic = any(value is not None for value in (acceptance, checklist, depends_on, brief, folder_id))
            semantic = semantic or (title is not None and title.strip() != row["title"])
            semantic = semantic or bool(file_ids or requirements)
            if semantic and await _one(conn, "SELECT 1 FROM comparison_groups WHERE task_id = ?"
                                        " AND state IN ('planned','active','ready') LIMIT 1", (task_id,)):
                raise DomainConflict("close the comparison before revising its contract")
            if semantic and row["status"] in ("review", "done"):
                raise DomainConflict("return or revise the exact result before changing its contract")
            if semantic and row["current_attempt_id"]:
                attempt = await _one(conn, "SELECT state FROM execution_attempts WHERE id = ?",
                                     (row["current_attempt_id"],))
                if attempt is not None and not await physical_exit_in(conn, row["current_attempt_id"]):
                    raise DomainConflict("stop the active attempt before revising its contract")
            if semantic and await _one(conn, "SELECT 1 FROM effect_outbox WHERE kind = 'task.launch'"
                                       " AND state IN ('pending','claimed','unknown')"
                                       " AND json_extract(payload_json,'$.control.task_id') = ? LIMIT 1", (task_id,)):
                raise DomainConflict("reconcile the pending launch before revising its contract")
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
                checks = check_items(row["checklist"])
            else:
                if len(checklist) > 12 or any(not item.strip() or len(item) > 200 for item in checklist):
                    raise ValueError("task checklist is invalid")
                prior = check_items(row["checklist"])
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
            selected = set(check_ids or [])
            cleared = set(uncheck_ids or [])
            if selected & cleared or len(selected) != len(check_ids or []) or len(cleared) != len(uncheck_ids or []):
                raise ValueError("criterion IDs must be unique and cannot be checked and unchecked together")
            known = {str(item.get("id")) for item in checks}
            if not selected.union(cleared) <= known:
                raise DomainConflict("criterion ID is absent from the current contract")
            if selected or cleared:
                if row["status"] in ("review", "done"):
                    raise DomainConflict("reviewed criteria need a new result or a returned review")
                for item in checks:
                    if item["id"] in selected:
                        item["done"] = True
                    elif item["id"] in cleared:
                        item["done"] = False
            next_status = status or row["status"]
            if next_status in ("todo", "blocked") and depends_on is not None:
                next_status = "todo" if ready else "blocked"
            notes = row["notes"] or ""
            if note:
                notes = (notes + "\n" if notes else "") + f"[{now()[:16]}] {note}"
            await conn.execute("UPDATE board_tasks SET title = ?,acceptance = ?,checklist = ?,depends_on = ?,"
                               " priority = ?,brief_json = ?,notes = ?,status = ?,assignee_staff_id = ?,updated_at = ? WHERE id = ?",
                               (title.strip() if title is not None else row["title"],
                                acceptance.strip() if acceptance is not None else row["acceptance"],
                                _canonical(checks), _canonical(dependencies), priority or row["priority"],
                                _canonical(current_brief), notes[-8000:], next_status,
                                (assignee_staff_id or None) if assignee_staff_id is not None else row["assignee_staff_id"],
                                now(), task_id))
            await _handoff(conn, task_id, row["project_id"], folder_id=folder_id,
                           file_ids=file_ids or [], requirements=requirements or [])
            contract_revision = await capture_contract_change(conn, task_id,
                                                              origin_kind=principal.origin_class,
                                                              origin_ref=principal.actor_id,
                                                              previous_title=row["title"])
            if depends_on is not None and dependencies != json.loads(row["depends_on"] or "[]"):
                await conn.execute("UPDATE task_dependency_edges SET kind = 'cancelled',resolution_state = 'cancelled'"
                                   " WHERE successor_task_id = ? AND resolution_state != 'cancelled'", (task_id,))
                for dependency in dependencies:
                    await conn.execute("INSERT INTO task_dependency_edges(id,successor_task_id,predecessor_task_id,"
                                       " kind,resolution_state,created_at)"
                                       " VALUES (?,?,?,'required','awaiting_result',?)",
                                       (uuid.uuid4().hex, task_id, dependency, now()))
            return {"task_id": task_id, "status": next_status, "contract_revision": contract_revision}

        return await self._mutate(principal, scope, "board.task.update", client_operation_id,
                                  expected_entity_revision, Entity("task", task_id), payload, effect, "task.changed")


__all__ = ["BoardCommands", "insert_task"]
