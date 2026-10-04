"""Atomic host admission for native and terminal staff, with durable waiting order."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from daedalus.stores.control import one

HOST_ID = "local"
def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _reconcile(conn: aiosqlite.Connection, generation: int, at: str) -> None:
    """Release only claims with observed end or a timed-out start without a live session."""
    await conn.execute(
        "UPDATE capacity_reservations SET state = 'released',released_at = ?"
        " WHERE state = 'active' AND EXISTS (SELECT 1 FROM execution_attempts a"
        " JOIN staff_sessions s ON s.id = a.staff_session_id"
        " WHERE a.id = capacity_reservations.attempt_id AND s.ended_at IS NOT NULL"
        " AND (EXISTS (SELECT 1 FROM runtime_exit_observations x WHERE x.attempt_id = a.id)"
        " OR EXISTS (SELECT 1 FROM runtime_no_entry_observations n WHERE n.attempt_id = a.id)))",
        (at,))
    await conn.execute(
        "UPDATE capacity_reservations SET state = 'active'"
        " WHERE state = 'held' AND EXISTS (SELECT 1 FROM execution_attempts a"
        " JOIN staff_sessions s ON s.id = a.staff_session_id"
        " WHERE a.id = capacity_reservations.attempt_id AND s.ended_at IS NULL)")
    await conn.execute(
        "UPDATE capacity_reservations SET state = 'released',released_at = ?"
        " WHERE state = 'held' AND (generation != ? OR expires_at <= ?)"
        " AND NOT EXISTS (SELECT 1 FROM execution_attempts a JOIN staff_sessions s"
        " ON s.id = a.staff_session_id WHERE a.id = capacity_reservations.attempt_id"
        " AND NOT EXISTS (SELECT 1 FROM runtime_exit_observations x WHERE x.attempt_id = a.id)"
        " AND NOT EXISTS (SELECT 1 FROM runtime_no_entry_observations n WHERE n.attempt_id = a.id))",
        (at, generation, at))
    await conn.execute(
        "UPDATE scheduler_claims SET state = 'cancelled',updated_at = ?"
        " WHERE state = 'admitted' AND reservation_id IN"
        " (SELECT id FROM capacity_reservations WHERE state = 'released')", (at,))
    await conn.execute("UPDATE scheduler_claims SET state = 'cancelled',updated_at = ?"
                       " WHERE state = 'waiting' AND EXISTS (SELECT 1 FROM effect_outbox e"
                       " WHERE json_extract(e.payload_json,'$.data.attempt_id') = scheduler_claims.attempt_id"
                       " AND e.state IN ('completed','failed','cancelled','unknown'))", (at,))


async def snapshot_in(conn: aiosqlite.Connection, *, generation: int, cap: int) -> dict[str, Any]:
    """Count real live staff plus starts promised before their sessions are visible."""
    at = _now()
    await _reconcile(conn, generation, at)
    observed = await one(conn,
        "SELECT count(*) AS n FROM staff_sessions s WHERE s.ended_at IS NULL AND"
        " (s.kind = 'cli' OR s.status IN ('starting','working','question','permission','no_signal'))")
    other_terminals = await one(conn,
        "SELECT count(*) AS n FROM terminals t WHERE t.status = 'running' AND NOT EXISTS"
        " (SELECT 1 FROM staff_sessions s WHERE s.terminal_id = t.id AND s.ended_at IS NULL)")
    main_runs = await one(conn,
        "SELECT count(*) AS n FROM runs r WHERE r.status = 'running' AND NOT EXISTS"
        " (SELECT 1 FROM staff_sessions s WHERE s.session_id = r.session_id AND s.ended_at IS NULL)")
    held = await one(conn,
        "SELECT count(*) AS n FROM capacity_reservations r WHERE r.host_id = ?"
        " AND r.state IN ('held','active') AND NOT EXISTS"
        " (SELECT 1 FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id"
        " WHERE a.id = r.attempt_id AND s.ended_at IS NULL AND"
        " (s.kind = 'cli' OR s.status IN ('starting','working','question','permission','no_signal')))",
        (HOST_ID,))
    active = int(observed["n"]) + int(other_terminals["n"]) + int(main_runs["n"])
    reserved = int(held["n"])
    return {"host_id": HOST_ID, "cap": cap, "active": active, "reserved": reserved,
            "available": max(0, cap - active - reserved)}


async def claim_in(conn: aiosqlite.Connection, *, attempt_id: str, project_id: str,
                   runtime_kind: str, role_class: str, generation: int, cap: int) -> str | None:
    """Reserve one slot under the caller's immediate transaction; return a concrete blocker."""
    if role_class not in ("coordinator", "reviewer", "worker"):
        raise ValueError("unknown admission role")
    at = _now()
    view = await snapshot_in(conn, generation=generation, cap=cap)
    existing = await one(conn, "SELECT * FROM capacity_reservations WHERE attempt_id = ?", (attempt_id,))
    if existing is not None and existing["state"] in ("held", "active"):
        if (existing["generation"] != generation or existing["project_id"] != project_id
                or existing["runtime_kind"] != runtime_kind):
            raise ValueError("the capacity claim belongs to another launch")
        return None
    if existing is not None:
        raise ValueError("a released capacity claim cannot be reused")
    claim = await one(conn, "SELECT * FROM scheduler_claims WHERE attempt_id = ?", (attempt_id,))
    if claim is None:
        seq = await one(conn, "SELECT COALESCE(max(enqueue_seq),0) + 1 AS next FROM scheduler_claims")
        await conn.execute("INSERT INTO scheduler_claims(id,attempt_id,project_id,role_class,enqueue_seq,"
                           "state,created_at,updated_at) VALUES (?,?,?,?,?,'waiting',?,?)",
                           (uuid.uuid4().hex, attempt_id, project_id, role_class, seq["next"], at, at))
    elif claim["state"] == "cancelled" or claim["project_id"] != project_id or claim["role_class"] != role_class:
        raise ValueError("the waiting claim is no longer current")
    # One slot remains available to a coordinator or reviewer while worker starts are flooding in.
    limit = cap if role_class != "worker" else max(0, cap - 1)
    occupied = view["active"] + view["reserved"]
    if occupied >= limit:
        reason = f"{occupied} of {cap} host slots are occupied"
        if role_class == "worker" and occupied < cap:
            reason += "; the remaining slot is reserved for coordination or review"
        await conn.execute("UPDATE scheduler_claims SET reason = ?,updated_at = ? WHERE attempt_id = ?",
                           (reason, at, attempt_id))
        return reason
    # Control work wins the first available place. Within a role, an older waiting project gets
    # credit on every successful admission, capped so one stale claim cannot monopolize the host.
    ahead = await one(conn,
        "SELECT attempt_id FROM scheduler_claims WHERE state = 'waiting'"
        " ORDER BY (CASE role_class WHEN 'coordinator' THEN 0 WHEN 'reviewer' THEN 1 ELSE 2 END),"
        " age_credit DESC,enqueue_seq LIMIT 1")
    if ahead is not None and ahead["attempt_id"] != attempt_id:
        reason = "an older project is waiting for the next host slot"
        await conn.execute("UPDATE scheduler_claims SET reason = ?,updated_at = ? WHERE attempt_id = ?",
                           (reason, at, attempt_id))
        return reason
    reservation_id = uuid.uuid4().hex
    await conn.execute("INSERT INTO capacity_reservations(id,attempt_id,project_id,host_id,runtime_kind,"
                       "role_class,state,expires_at,generation,created_at)"
                       " VALUES (?,?,?,?,?,?,'held',?,?,?)",
                       (reservation_id, attempt_id, project_id, HOST_ID, runtime_kind, role_class,
                        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), generation, at))
    await conn.execute("UPDATE scheduler_claims SET state = 'admitted',reservation_id = ?,reason = '',"
                       " updated_at = ? WHERE attempt_id = ?", (reservation_id, at, attempt_id))
    await conn.execute("UPDATE scheduler_claims SET age_credit = min(8,age_credit + 1)"
                       " WHERE state = 'waiting' AND project_id != ?", (project_id,))
    return None


async def finish_in(conn: aiosqlite.Connection, attempt_id: str, *, started: bool) -> None:
    """A failed launch returns its promise; a started launch stays charged until observed end."""
    at = _now()
    if started:
        await conn.execute("UPDATE capacity_reservations SET state = 'active'"
                           " WHERE attempt_id = ? AND state = 'held'", (attempt_id,))
    else:
        bound = await one(conn, "SELECT 1 FROM execution_attempts a JOIN staff_sessions s"
                          " ON s.id = a.staff_session_id WHERE a.id = ? AND s.ended_at IS NULL", (attempt_id,))
        if bound is not None:
            await conn.execute("UPDATE capacity_reservations SET state = 'active'"
                               " WHERE attempt_id = ? AND state = 'held'", (attempt_id,))
            return
        await conn.execute("UPDATE capacity_reservations SET state = 'released',released_at = ?"
                           " WHERE attempt_id = ? AND state = 'held'", (at, attempt_id))
        await conn.execute("UPDATE scheduler_claims SET state = 'cancelled',updated_at = ?"
                           " WHERE attempt_id = ? AND state = 'admitted'", (at, attempt_id))


async def cancel_in(conn: aiosqlite.Connection, attempt_id: str) -> None:
    """Withdraw a waiting command without consuming a future slot."""
    await conn.execute("UPDATE scheduler_claims SET state = 'cancelled',updated_at = ?"
                       " WHERE attempt_id = ? AND state = 'waiting'", (_now(), attempt_id))
