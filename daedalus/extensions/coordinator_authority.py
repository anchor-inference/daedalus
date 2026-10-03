"""A current coordinator acts through an operator-issued grant, never its displayed role name."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from daedalus.stores.control import ControlDenied, ControlStore, Entity, Principal, Scope, now, one

if TYPE_CHECKING:
    from daedalus.app import Application

REVIEW_OPERATIONS = ("review.verdict", "review.return")


async def current_office(conn: Any, project_id: str, session_id: str | None = None) -> str:
    project = await one(conn, "SELECT settings FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise KeyError(project_id)
    settings = json.loads(project["settings"])
    office = settings.get("orchestrator", {})
    current = office.get("session_id")
    if not office.get("enabled") or not current or (session_id is not None and current != session_id):
        raise ControlDenied("the coordinator was disabled or replaced")
    session = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?", (current,))
    if session is None or session["project_id"] != project_id or json.loads(session["metadata"]).get("orchestrator_of") != project_id:
        raise ControlDenied("the host has no current coordinator session for this project")
    return str(current)


async def resolve_authority(app: Application, *, session_id: str, project_id: str,
                            operation: str, task_id: str | None = None) -> Principal:
    """Resolve the approved office again at the point its tool requests a mutation."""
    async with app.db.transaction() as conn:
        await current_office(conn, project_id, session_id)
        scope = Scope("project", project_id)
        if task_id is not None:
            await ControlStore(app.db)._entity(conn, scope, Entity("task", task_id))
        async with conn.execute("SELECT id,generation FROM actor_grants WHERE actor_id = ? AND origin_class = 'agent'"
                                " AND scope_kind = 'project' AND project_id = ? AND revoked_at IS NULL"
                                " ORDER BY created_at DESC,id DESC", (f"orchestrator:{session_id}", project_id)) as cursor:
            grants = await cursor.fetchall()
        for row in grants:
            principal = Principal(f"orchestrator:{session_id}", "agent", row["id"], row["generation"])
            try:
                await ControlStore(app.db).authorize(conn, principal, scope, operation, task_id=task_id)
            except ControlDenied:
                continue
            return principal
    raise ControlDenied("the coordinator has no current grant for this operation")


async def grant_review(app: Application, project_id: str, principal: Principal, *,
                       client_operation_id: str, expected_entity_revision: int,
                       expires_at: str) -> dict[str, Any]:
    scope = Scope("project", project_id)

    async def effect(conn: Any, _: Any) -> dict[str, Any]:
        session_id = await current_office(conn, project_id)
        actor_id = f"orchestrator:{session_id}"
        async with conn.execute("SELECT id,generation,operations_json FROM actor_grants WHERE actor_id = ? AND project_id = ?"
                                " AND revoked_at IS NULL", (actor_id, project_id)) as cursor:
            earlier = await cursor.fetchall()
        for row in earlier:
            if set(json.loads(row["operations_json"])) != set(REVIEW_OPERATIONS):
                continue
            generation = row["generation"] + 1
            await conn.execute("UPDATE actor_grants SET revoked_at = ?,generation = ? WHERE id = ?", (now(), generation, row["id"]))
            await conn.execute("INSERT INTO grant_events(grant_id,actor_id,generation,event,reason,at)"
                               " VALUES (?,?,?,'revoked','review authority replaced',?)",
                               (row["id"], principal.actor_id, generation, now()))
            await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = 'review authority replaced'"
                               " WHERE grant_id = ? AND state = 'pending'", (row["id"],))
        grant = await ControlStore(app.db).issue_grant_in(conn, principal, Principal(actor_id, "agent"), scope,
                                                         operations=list(REVIEW_OPERATIONS), effects=[], expires_at=expires_at)
        return {"project_id": project_id, "session_id": session_id, **grant}

    return await ControlStore(app.db).mutate(principal, scope, "coordinator.review.authorize", client_operation_id,
                                              expected_entity_revision, Entity("project", project_id),
                                              {"expires_at": expires_at}, effect)


async def board_tool_authority(app: Application, session_id: str, operation: str,
                               task_id: str | None = None) -> tuple[Principal, Scope]:
    """Derive the board from the host session, then require an explicit operation grant."""
    session = await app.db.fetchone("SELECT project_id,metadata FROM sessions WHERE id = ?", (session_id,))
    if session is None:
        raise ControlDenied("the tool session is unknown")
    project_id = session["project_id"]
    scope = Scope("project", project_id) if project_id else Scope("global", "global")
    if json.loads(session["metadata"] or "{}").get("orchestrator_of"):
        if not project_id:
            raise ControlDenied("the coordinator has no host project")
        return await resolve_authority(app, session_id=session_id, project_id=project_id,
                                       operation=operation, task_id=task_id), scope
    control = ControlStore(app.db)
    async with app.db.transaction() as conn:
        if task_id is not None:
            await control._entity(conn, scope, Entity("task", task_id))
        async with conn.execute("SELECT id FROM staff_sessions WHERE session_id = ? AND ended_at IS NULL", (session_id,)) as cursor:
            workers = await cursor.fetchall()
        if len(workers) > 1:
            raise ControlDenied("the tool session has ambiguous worker ownership")
        if workers:
            identity = await app.executions.check_staff(conn, workers[0]["id"])
            actor_id = identity.principal.actor_id
        else:
            actor_id = f"session:{session_id}"
        async with conn.execute("SELECT id,generation FROM actor_grants WHERE actor_id = ? AND origin_class = 'agent'"
                                " AND revoked_at IS NULL ORDER BY created_at DESC,id DESC", (actor_id,)) as cursor:
            grants = await cursor.fetchall()
        for row in grants:
            principal = Principal(actor_id, "agent", row["id"], row["generation"])
            try:
                await control.authorize(conn, principal, scope, operation, task_id=task_id)
            except ControlDenied:
                continue
            return principal, scope
    raise ControlDenied("the tool session has no current grant for this board operation")


def install_authority(app: Application) -> None:
    async def resolve(**kwargs: Any) -> Principal:
        return await resolve_authority(app, **kwargs)

    app.extensions["orchestrator_review_authority"] = resolve
    app.extensions["orchestrator_board_authority"] = resolve

    async def board(session_id: str, operation: str, task_id: str | None = None) -> tuple[Principal, Scope]:
        return await board_tool_authority(app, session_id, operation, task_id)

    app.extensions["board_tool_authority"] = board
