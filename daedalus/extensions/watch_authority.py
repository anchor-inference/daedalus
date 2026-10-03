"""Check the operator's standing watch approval at each intent and effect boundary."""

from __future__ import annotations

import json
from typing import Any

from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope, digest, now, one


def action_kind(action: dict[str, Any]) -> str:
    kind = action.get("action")
    if kind not in ("wake", "tell", "notify"):
        raise ControlDenied("the watch action is not supported")
    return f"watch.{kind}"


async def approve(conn: Any, watch_id: str, condition_revision: int, project_id: str,
                  principal: Principal, action: dict[str, Any]) -> None:
    if principal.origin_class not in ("operator", "agent"):
        raise ControlDenied("standing watches need an operator or a currently granted agent")
    await conn.execute(
        "INSERT INTO watch_authorities"
        " (watch_id,condition_revision,project_id,actor_id,origin_class,grant_id,grant_generation,"
        " action_kind,action_digest,approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (watch_id, condition_revision, project_id, principal.actor_id, principal.origin_class,
         principal.grant_id, principal.grant_generation, action_kind(action), digest(action), now()),
    )


async def revoke(conn: Any, watch_id: str, condition_revision: int) -> None:
    await conn.execute("UPDATE watch_authorities SET revoked_at = ? WHERE watch_id = ?"
                       " AND condition_revision = ? AND revoked_at IS NULL", (now(), watch_id, condition_revision))


async def _current_office(conn: Any, project_id: str, expected_session_id: str | None = None) -> bool:
    project = await one(conn, "SELECT settings FROM projects WHERE id = ?", (project_id,))
    try:
        office = json.loads(project["settings"] or "{}").get("orchestrator", {}) if project else {}
    except (TypeError, ValueError):
        return False
    session_id = office.get("session_id")
    if not office.get("enabled") or not session_id or (expected_session_id is not None and session_id != expected_session_id):
        return False
    session = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?", (session_id,))
    try:
        return bool(session is not None and session["project_id"] == project_id
                    and json.loads(session["metadata"] or "{}").get("orchestrator_of") == project_id)
    except (TypeError, ValueError):
        return False


async def status(conn: Any, db: Any, *, watch_id: str, condition_revision: int,
                 project_id: str, action: dict[str, Any]) -> str:
    row = await one(conn, "SELECT * FROM watch_authorities WHERE watch_id = ? AND condition_revision = ?",
                    (watch_id, condition_revision))
    try:
        requested_action = action_kind(action)
    except ControlDenied:
        return "needs_approval"
    if (row is None or row["project_id"] != project_id or row["revoked_at"]
            or row["action_kind"] != requested_action or row["action_digest"] != digest(action)):
        return "needs_approval"
    if row["action_kind"] == "watch.wake" and not await _current_office(conn, project_id):
        return "capability_unavailable"
    if row["origin_class"] == "operator":
        return "current" if row["actor_id"].startswith("operator:") else "needs_approval"
    principal = Principal(row["actor_id"], "agent", row["grant_id"], row["grant_generation"])
    try:
        await ControlStore(db).authorize(
            conn, principal, Scope("project", project_id), "watch.deliver", effects=(row["action_kind"],),
        )
        if principal.actor_id.startswith("orchestrator:"):
            session_id = principal.actor_id.partition(":")[2]
            if not await _current_office(conn, project_id, session_id):
                return "needs_approval"
    except (ControlDenied, KeyError, ValueError):
        return "needs_approval"
    return "current"


async def authorized(conn: Any, db: Any, *, watch_id: str, condition_revision: int,
                     project_id: str, action: dict[str, Any]) -> bool:
    return await status(conn, db, watch_id=watch_id, condition_revision=condition_revision,
                        project_id=project_id, action=action) == "current"
