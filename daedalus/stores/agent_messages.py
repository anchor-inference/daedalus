"""The log of what the agents of a project say to each other.

Every call of SendToAgent leaves one row, whether the message went, was refused or failed: the
orchestrator and the operator read the conversation between members from here, and the hop count of
the next message is read from the last one a member received. A reply handed to a sender that waited
for it is a row of its own (``kind = 'reply'``) pointing at the message it answers.

The rows say what was decided; the delivery of a message that went is the staff message it went as
(``staff_message_id``), whose receipts the delivery worker keeps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from daedalus.stores.database import Database

TEXT_MAX = 7_000
"""The longest message one agent may send another; a longer one belongs in a file it names. Under a
staff message's own limit by the room its header takes."""

AgentMessageState = Literal["sent", "refused", "failed", "answered", "unanswered"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class AgentMessage:
    id: str
    project_id: str
    kind: str
    """``message``, or ``reply`` for an answer handed to a sender that waited for it."""
    sender_kind: str
    sender_staff_id: str | None
    sender_session: str
    sender_name: str
    target_kind: str
    target_staff_id: str | None
    target_session: str
    target_name: str
    hop: int
    parent_id: str | None
    """The message this one follows: the last one its sender had received, or the one a reply answers."""
    text: str
    wait_s: int
    state: str
    detail: str
    """Why it was refused or failed, or where a reply came from."""
    staff_message_id: str | None
    created_at: str
    updated_at: str
    delivery: str = ""
    """The receipt of the staff message it went as (``queued`` … ``acknowledged``, ``failed``), when read with it."""

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "sender": {"kind": self.sender_kind, "staff_id": self.sender_staff_id, "session": self.sender_session, "name": self.sender_name},
            "target": {"kind": self.target_kind, "staff_id": self.target_staff_id, "session": self.target_session, "name": self.target_name},
            "hop": self.hop,
            "parent_id": self.parent_id,
            "text": self.text,
            "wait_s": self.wait_s,
            "state": self.state,
            "detail": self.detail,
            "staff_message_id": self.staff_message_id,
            "delivery": self.delivery,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row(row: Any) -> AgentMessage:
    keys = row.keys()
    return AgentMessage(
        row["id"], row["project_id"], row["kind"], row["sender_kind"], row["sender_staff_id"], row["sender_session"], row["sender_name"],
        row["target_kind"], row["target_staff_id"], row["target_session"], row["target_name"], int(row["hop"]), row["parent_id"],
        row["text"], int(row["wait_s"]), row["state"], row["detail"], row["staff_message_id"], row["created_at"], row["updated_at"],
        str(row["delivery"] or "") if "delivery" in keys else "",
    )


_SELECT = "SELECT m.*, s.state AS delivery FROM agent_messages m LEFT JOIN staff_messages s ON s.id = m.staff_message_id"


class AgentMessages:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self, *, project_id: str, kind: str = "message", sender_kind: str, sender_staff_id: str | None, sender_session: str, sender_name: str,
        target_kind: str, target_staff_id: str | None, target_session: str, target_name: str, hop: int, parent_id: str | None, text: str,
        wait_s: int = 0, state: AgentMessageState = "sent", detail: str = "", staff_message_id: str | None = None, message_id: str | None = None,
    ) -> AgentMessage:
        at = _now()
        message = AgentMessage(
            message_id or f"am-{uuid.uuid4().hex[:12]}", project_id, kind, sender_kind, sender_staff_id, sender_session, sender_name[:200],
            target_kind, target_staff_id, target_session, target_name[:200], hop, parent_id, text[:TEXT_MAX], max(0, int(wait_s)), state,
            detail[:2000], staff_message_id, at, at,
        )
        await self._db.execute(
            "INSERT INTO agent_messages(id, project_id, kind, sender_kind, sender_staff_id, sender_session, sender_name, target_kind,"
            " target_staff_id, target_session, target_name, hop, parent_id, text, wait_s, state, detail, staff_message_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, project_id, kind, sender_kind, sender_staff_id, sender_session, message.sender_name, target_kind, target_staff_id,
             target_session, message.target_name, hop, parent_id, message.text, message.wait_s, state, message.detail, staff_message_id, at, at),
        )
        return message

    async def get(self, message_id: str) -> AgentMessage | None:
        row = await self._db.fetchone(f"{_SELECT} WHERE m.id = ?", (message_id,))
        return _row(row) if row is not None else None

    async def by_staff_message(self, staff_message_id: str) -> AgentMessage | None:
        row = await self._db.fetchone(f"{_SELECT} WHERE m.staff_message_id = ?", (staff_message_id,))
        return _row(row) if row is not None else None

    async def settle(self, message_id: str, state: AgentMessageState, detail: str = "", *, staff_message_id: str | None = None) -> None:
        """Record what became of a message: it went (with the staff message it went as), it failed,
        or the sender's wait ended with a reply or without one."""
        await self._db.execute(
            "UPDATE agent_messages SET state = ?, detail = CASE WHEN ? = '' THEN detail ELSE ? END,"
            " staff_message_id = coalesce(?, staff_message_id), updated_at = ? WHERE id = ?",
            (state, detail, detail[:2000], staff_message_id, _now(), message_id),
        )

    async def last_received(self, staff_id: str) -> AgentMessage | None:
        """The newest message or reply that reached a member: typed into its session, or handed to a
        call of its that waited. A refused one never reached it."""
        row = await self._db.fetchone(
            f"{_SELECT} WHERE m.target_staff_id = ? AND m.state != 'refused' AND m.state != 'failed'"
            " ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1",
            (staff_id,),
        )
        return _row(row) if row is not None else None

    async def last_received_by_orchestrator(self, project_id: str, session_id: str) -> AgentMessage | None:
        row = await self._db.fetchone(
            f"{_SELECT} WHERE m.project_id = ? AND m.target_kind = 'orchestrator' AND m.target_session = ? AND m.state != 'refused'"
            " ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1",
            (project_id, session_id),
        )
        return _row(row) if row is not None else None

    async def list(self, project_id: str, *, staff_id: str | None = None, before: str | None = None, limit: int = 50) -> list[AgentMessage]:
        """A project's messages, newest first; with ``staff_id``, those the member sent or received.
        ``before`` is the id of the oldest row a reader has, for the page before it."""
        where, params = ["m.project_id = ?"], [project_id]
        if staff_id:
            where.append("(m.sender_staff_id = ? OR m.target_staff_id = ?)")
            params += [staff_id, staff_id]
        if before:
            where.append("m.rowid < (SELECT rowid FROM agent_messages WHERE id = ?)")
            params.append(before)
        rows = await self._db.fetchall(
            f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY m.rowid DESC LIMIT ?", (*params, max(1, min(int(limit), 200))),
        )
        return [_row(r) for r in rows]


__all__ = ["TEXT_MAX", "AgentMessage", "AgentMessageState", "AgentMessages"]
