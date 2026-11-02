"""Every request the server sent, or meant to send, to an operator's terminal: the audit and the delivery record.

One row per request (``remote_calls``), keyed by the request's id, ``<session>:<tool call>:<n>``.
It is written before the request leaves and moved as it goes, so it answers two questions.

The operator's: what did the agent run on my computer, for which conversation, when, and what came
of it. Every row stays for :data:`KEPT_DAYS` days whatever happened to the call.

The backend's: a tool call parked by a disconnect is handed over again later, perhaps by another
process after a restart, and it issues the same requests in the same order. A request already
answered is answered again from ``response`` here, without going to the terminal; one that was sent
and not answered is the terminal's to account for (``delivery`` = ``unknown``); one never sent
(``not_delivered``) is sent now. ``response`` is kept only while its tool call is open: once the
call has its result it is cleared, and the row is the audit alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.stores.database import Database

KEPT_DAYS = 90
RESPONSE_MAX_BYTES = 8 << 20
"""The largest answer kept for a repeat. A larger one (a read of a big file) is asked again."""

HELD = "held"
"""Recorded, not sent: the terminal was away. ``delivery`` is ``not_delivered``."""
SENT = "sent"
"""Handed to the socket, no answer yet. ``delivery`` is ``unknown`` until it is answered."""
DONE = "done"
FAILED = "failed"
"""The terminal answered with an error that is not a refusal: not found, too large, unsupported."""
REFUSED = "refused"
"""The terminal's guard or jail refused it (``1004``)."""
LOST = "lost"
"""Sent, and the terminal came back without knowing it: it was not run again."""
CANCELLED = "cancelled"

NOT_DELIVERED = "not_delivered"
DELIVERED = "delivered"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    session_id: str
    tool_call_id: str
    device_id: str
    method: str
    state: str
    delivery: str
    response: Any = None
    has_response: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Journal:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, request_id: str) -> Entry | None:
        row = await self.db.fetchone("SELECT * FROM remote_calls WHERE id = ?", (request_id,))
        if row is None:
            return None
        raw = row["response"]
        return Entry(
            id=row["id"], session_id=row["session_id"], tool_call_id=row["tool_call_id"], device_id=row["device_id"],
            method=row["method"], state=row["state"], delivery=row["delivery"],
            response=json.loads(raw) if raw is not None else None, has_response=raw is not None,
        )

    async def record(self, request_id: str, *, session_id: str, tool_call_id: str, seq: int, device_id: str, tool: str, method: str, intent: str, title: str, state: str) -> None:
        """The request before it leaves: ``held`` when the terminal is away, ``sent`` as it is handed over."""
        now = _now()
        delivery = NOT_DELIVERED if state == HELD else UNKNOWN
        await self.db.execute(
            "INSERT INTO remote_calls(id, session_id, tool_call_id, seq, device_id, tool, method, intent, title, state, delivery, outcome, response, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET device_id = excluded.device_id, state = excluded.state, delivery = excluded.delivery, updated_at = excluded.updated_at",
            (request_id, session_id, tool_call_id, seq, device_id, tool[:80], method, intent, title[:200], state, delivery, now, now),
        )

    async def settle(self, request_id: str, state: str, *, outcome: str = "", response: Any = None, keep: bool = False) -> None:
        """The request's end: answered, refused, failed, lost or cancelled. ``keep`` stores ``response``
        for a repeat of the request while its tool call is open."""
        stored = None
        if keep:
            text = json.dumps(response, separators=(",", ":"), ensure_ascii=False)
            stored = text if len(text.encode()) <= RESPONSE_MAX_BYTES else None
        delivery = NOT_DELIVERED if state in (HELD,) else (DELIVERED if state in (DONE, FAILED, REFUSED) else UNKNOWN)
        if state == CANCELLED:
            row = await self.db.fetchone("SELECT delivery FROM remote_calls WHERE id = ?", (request_id,))
            delivery = row["delivery"] if row is not None else NOT_DELIVERED
        await self.db.execute(
            "UPDATE remote_calls SET state = ?, delivery = ?, outcome = ?, response = ?, updated_at = ? WHERE id = ?",
            (state, delivery, outcome[:300], stored, _now(), request_id),
        )

    async def close_call(self, session_id: str, tool_call_id: str) -> None:
        """The tool call has its result: what was kept for a repeat of its requests goes."""
        await self.db.execute("UPDATE remote_calls SET response = NULL WHERE session_id = ? AND tool_call_id = ? AND response IS NOT NULL", (session_id, tool_call_id))

    async def cancel_call(self, session_id: str, tool_call_id: str) -> None:
        """The operator ended a parked call: its open requests are cancelled and nothing is kept."""
        now = _now()
        await self.db.execute(
            "UPDATE remote_calls SET state = ?, response = NULL, updated_at = ? WHERE session_id = ? AND tool_call_id = ? AND state IN (?, ?)",
            (CANCELLED, now, session_id, tool_call_id, HELD, SENT),
        )
        await self.close_call(session_id, tool_call_id)

    async def open_requests(self, device_id: str) -> list[str]:
        """The ids of the requests to this device that were sent and never answered."""
        rows = await self.db.fetchall("SELECT id FROM remote_calls WHERE device_id = ? AND state = ?", (device_id, SENT))
        return [row["id"] for row in rows]

    async def list(self, *, session_id: str | None = None, device_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """The audit, newest first, without the kept answers."""
        clauses, params = [], []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.db.fetchall(
            f"SELECT id, session_id, tool_call_id, seq, device_id, tool, method, intent, title, state, delivery, outcome, created_at, updated_at"  # noqa: S608 — fixed clauses, values bound
            f" FROM remote_calls {where} ORDER BY created_at DESC, seq DESC LIMIT ?",
            (*params, max(1, min(limit, 1000))),
        )
        return [dict(row) for row in rows]

    async def prune(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=KEPT_DAYS)).isoformat()
        rows = await self.db.fetchall("SELECT count(*) AS n FROM remote_calls WHERE created_at < ?", (cutoff,))
        await self.db.execute("DELETE FROM remote_calls WHERE created_at < ?", (cutoff,))
        return int(rows[0]["n"]) if rows else 0


__all__ = [
    "CANCELLED",
    "DELIVERED",
    "DONE",
    "FAILED",
    "HELD",
    "KEPT_DAYS",
    "LOST",
    "NOT_DELIVERED",
    "REFUSED",
    "SENT",
    "UNKNOWN",
    "Entry",
    "Journal",
]
