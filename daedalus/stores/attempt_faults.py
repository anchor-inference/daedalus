"""Capture safe fault categories without persisting exception material."""

from __future__ import annotations

from typing import Any

import aiosqlite

from daedalus.stores.attempt_diagnostics import REDACTION_VERSION
from daedalus.stores.control import now, one

KINDS = frozenset({"adapter_error", "renderer_error", "launch_error", "runtime_error", "cancelled"})
CANCELLERS = frozenset({"operator", "worker", "system"})


async def record_fault(conn: aiosqlite.Connection, attempt_id: str, kind: str, *,
                       cancelled_by: str | None = None) -> int:
    """Record only an allowlisted category; raw exception text never crosses this boundary."""
    if kind not in KINDS or (cancelled_by is not None and cancelled_by not in CANCELLERS):
        raise ValueError("invalid attempt fault category")
    if (kind == "cancelled") != (cancelled_by is not None):
        raise ValueError("cancellation requires its actor and errors cannot have one")
    if await one(conn, "SELECT 1 FROM execution_attempts WHERE id = ?", (attempt_id,)) is None:
        raise KeyError(attempt_id)
    cursor = await conn.execute(
        "INSERT INTO attempt_faults(attempt_id,kind,cancelled_by,diagnostic_ref,redaction_version,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (attempt_id, kind, cancelled_by, None, REDACTION_VERSION, now()),
    )
    fault_id = cursor.lastrowid
    await cursor.close()
    return int(fault_id)


async def attempt_fault_rows(conn: aiosqlite.Connection, attempt_id: str, *, limit: int = 21) -> list[dict[str, Any]]:
    """Read at most one row beyond the displayed bound to report truncation."""
    async with conn.execute("SELECT id,attempt_id,kind,cancelled_by,created_at FROM attempt_faults"
                            " WHERE attempt_id = ? ORDER BY id DESC LIMIT ?", (attempt_id, limit)) as cursor:
        return [dict(row) for row in await cursor.fetchall()]
