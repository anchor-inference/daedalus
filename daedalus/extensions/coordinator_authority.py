"""A current coordinator acts through an operator-issued grant, never its displayed role name."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, now, one

if TYPE_CHECKING:
    from daedalus.app import Application

REVIEW_OPERATIONS = ("review.verdict", "review.return")
AUTHORITY_BUNDLES = {
    "planning": {"operations": ["board.task.create", "board.task.update", "contract.require", "contract.apply", "contract.withdraw"],
                 "effects": [], "scope_kind": "project"},
    "execution": {"operations": ["task.launch", "task.stop", "staff.release"],
                  "effects": ["execution.start", "execution.stop"], "scope_kind": "task"},
    "execution_project": {"operations": ["task.launch", "task.stop", "staff.release"],
                          "effects": ["execution.start", "execution.stop"], "scope_kind": "project"},
    "review": {"operations": list(REVIEW_OPERATIONS), "effects": [], "scope_kind": "project"},
    "watch": {"operations": ["watch.create", "watch.change", "watch.remove", "watch.deliver"],
                "effects": ["watch.wake", "watch.tell", "watch.notify"], "scope_kind": "project"},
}


async def _office_or_none(conn: Any, project_id: str) -> str | None:
    try:
        return await current_office(conn, project_id)
    except ControlDenied:
        return None


async def authority_view(app: Application, project_id: str) -> dict[str, Any]:
    """Describe actual approvals, including stale ones that the operator can still withdraw."""
    async with app.db.transaction() as conn:
        row = await one(conn, "SELECT entity_revision FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise KeyError(project_id)
        session_id = await _office_or_none(conn, project_id)
        blockers = [] if session_id else ["no_current_coordinator"]
        async with conn.execute("SELECT g.*,(SELECT id FROM operation_receipts r"
                                " WHERE r.project_id = g.project_id AND json_extract(r.response_json,'$.grant_id') = g.id"
                                " ORDER BY r.created_at,r.id LIMIT 1) AS receipt_id FROM actor_grants g"
                                " WHERE g.project_id = ? AND g.actor_id LIKE 'orchestrator:%' AND g.parent_grant_id IS NULL"
                                " ORDER BY g.created_at DESC,g.id DESC", (project_id,)) as cursor:
            rows = await cursor.fetchall()
        grants = []
        for grant in rows:
            owner = grant["actor_id"].partition(":")[2]
            state = "revoked" if grant["revoked_at"] else "expired" if datetime.fromisoformat(grant["expires_at"]) <= datetime.now(UTC) else "stale" if owner != session_id else "active"
            grants.append({"grant_id": grant["id"], "generation": grant["generation"], "session_id": owner,
                           "scope": {"kind": grant["scope_kind"], "id": grant["scope_id"]},
                           "operations": json.loads(grant["operations_json"]), "effects": json.loads(grant["effects_json"]),
                           "expires_at": grant["expires_at"], "revoked_at": grant["revoked_at"], "created_at": grant["created_at"],
                           "receipt_id": grant["receipt_id"], "state": state, "parent_grant_id": grant["parent_grant_id"],
                           "parent_grant_generation": grant["parent_grant_generation"]})
        return {"project_id": project_id, "entity_revision": row["entity_revision"],
                "current_coordinator_session_id": session_id, "readiness_blockers": blockers, "grants": grants,
                "available_bundles": [{"id": name, **bundle, "blockers": blockers,
                                       "max_expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat()}
                                      for name, bundle in AUTHORITY_BUNDLES.items()]}


async def approve_authority(app: Application, project_id: str, principal: Principal, *,
                            client_operation_id: str, expected_entity_revision: int,
                            expected_coordinator_session_id: str, bundle_id: str,
                            expires_at: str, task_id: str | None = None) -> dict[str, Any]:
    scope = Scope("project", project_id)
    control = ControlStore(app.db)
    bundle = AUTHORITY_BUNDLES.get(bundle_id)
    if bundle is None or (bundle["scope_kind"] == "task") != bool(task_id):
        raise ValueError("the bundle and its explicit task scope must agree")
    async def effect(conn: Any, _: Any) -> dict[str, Any]:
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None or not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(hours=24):
            raise ValueError("approval requires a timezone-aware expiry within twenty-four hours")
        session_id = await current_office(conn, project_id)
        if session_id != expected_coordinator_session_id:
            raise ControlConflict("the coordinator changed after the approval preview")
        if task_id:
            await control._entity(conn, scope, Entity("task", task_id))
        actor_id = f"orchestrator:{session_id}"
        async with conn.execute("SELECT id,operations_json,effects_json FROM actor_grants WHERE actor_id = ?"
                                " AND project_id = ? AND task_id IS ? AND revoked_at IS NULL",
                                (actor_id, project_id, task_id)) as cursor:
            earlier = await cursor.fetchall()
        for row in earlier:
            if (set(json.loads(row["operations_json"])) == set(bundle["operations"])
                    and set(json.loads(row["effects_json"])) == set(bundle["effects"])):
                await control.revoke_grant_in(conn, principal, row["id"], reason="approval replaced")
        granted = await control.issue_grant_in(conn, principal, Principal(actor_id, "agent"), scope,
                                               operations=bundle["operations"], effects=bundle["effects"],
                                               expires_at=expiry.isoformat(), task_id=task_id)
        return {"project_id": project_id, "session_id": session_id, "bundle_id": bundle_id, **granted}

    payload = {"expected_coordinator_session_id": expected_coordinator_session_id, "bundle_id": bundle_id,
               "expires_at": expires_at, "task_id": task_id}
    async with app.db.authority_effect_lock("project", project_id):
        return await control.mutate(principal, scope, "orchestrator.authority.issue", client_operation_id,
                                    expected_entity_revision, Entity("project", project_id), payload, effect)


async def withdraw_authority(app: Application, project_id: str, principal: Principal, grant_id: str, *,
                             client_operation_id: str, expected_entity_revision: int,
                             expected_grant_generation: int, expected_coordinator_session_id: str | None,
                             reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("an approval withdrawal needs a reason")
    scope = Scope("project", project_id)
    control = ControlStore(app.db)

    async def effect(conn: Any, _: Any) -> dict[str, Any]:
        if await _office_or_none(conn, project_id) != expected_coordinator_session_id:
            raise ControlConflict("the coordinator changed after the withdrawal preview")
        grant = await one(conn, "SELECT * FROM actor_grants WHERE id = ?", (grant_id,))
        if grant is None or grant["project_id"] != project_id or not grant["actor_id"].startswith("orchestrator:"):
            raise KeyError(grant_id)
        if grant["generation"] != expected_grant_generation:
            raise ControlConflict("the approval generation has changed")
        await control.revoke_grant_in(conn, principal, grant_id, reason=reason)
        updated = await one(conn, "SELECT generation,revoked_at FROM actor_grants WHERE id = ?", (grant_id,))
        return {"project_id": project_id, "grant_id": grant_id, "generation": updated["generation"],
                "revoked_at": updated["revoked_at"]}

    async with app.db.authority_effect_lock("project", project_id):
        return await control.mutate(principal, scope, "orchestrator.authority.revoke", client_operation_id,
                                    expected_entity_revision, Entity("project", project_id),
                                    {"grant_id": grant_id, "expected_grant_generation": expected_grant_generation,
                                     "expected_coordinator_session_id": expected_coordinator_session_id, "reason": reason}, effect)


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
                                " AND project_id = ? AND revoked_at IS NULL"
                                " AND (scope_kind = 'project' OR (scope_kind = 'task' AND task_id = ?))"
                                " ORDER BY created_at DESC,id DESC", (f"orchestrator:{session_id}", project_id, task_id)) as cursor:
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
