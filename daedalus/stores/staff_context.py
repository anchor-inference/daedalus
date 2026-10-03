"""Persist the exact packet whose bytes were supplied to one staff launch."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


class ContextPinRefused(ValueError):
    """A packet does not match the session's current task and project."""


async def pin_staff_context(db: Any, staff_session_id: str, packet: dict[str, Any], *,
                            role_hint: str) -> None:
    """A later role or task change must not silently rewrite this launch's evidence."""
    async with db.transaction() as conn:
        async with conn.execute(
            "SELECT s.task_id,t.contract_revision,t.project_id,m.project_id AS member_project"
            " FROM staff_sessions s JOIN board_tasks t ON t.id = s.task_id"
            " JOIN staff m ON m.id = s.staff_id WHERE s.id = ? AND s.ended_at IS NULL",
            (staff_session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if (row is None or row["task_id"] != packet["task_id"] or
                row["contract_revision"] != packet["contract_revision"] or
                row["project_id"] != row["member_project"]):
            raise ContextPinRefused("the staff session cannot own this task context")
        await conn.execute(
            "INSERT INTO staff_context_packets(staff_session_id,task_id,role,role_hint,contract_revision,"
            "packet_hash,packet_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (staff_session_id, packet["task_id"], packet["role"], role_hint,
             packet["contract_revision"], packet["packet_hash"],
             json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
             datetime.now(UTC).isoformat()),
        )


async def staff_context_packet(db: Any, staff_session_id: str) -> dict[str, Any] | None:
    row = await db.fetchone("SELECT task_id,role,role_hint,packet_hash,packet_json FROM staff_context_packets"
                            " WHERE staff_session_id = ?", (staff_session_id,))
    if row is None:
        return None
    return {"task_id": row["task_id"], "role": row["role"], "role_hint": row["role_hint"],
            "packet_hash": row["packet_hash"], "packet": json.loads(row["packet_json"])}
