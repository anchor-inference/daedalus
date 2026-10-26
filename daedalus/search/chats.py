"""Which past conversations the agent's chat search may answer from, and the listing of recent ones.

The rules live here once, as a Python predicate and its SQL twin, because two places apply them and
must never disagree: the ranked search filters inside its own read connection
(:func:`daedalus.search.store.search`), and the tools check a single session before reading it back.

What the agent may reach is narrower than what the operator's own search in the app reaches. The app
shows the operator their own chats; the agent quotes them back into a conversation, so a chat the
operator marked private, and every session that is not the operator talking (a subagent, a staff
member, a schedule, a heartbeat, a coach beside a draft), stays out of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.host.prompts import without_host_notes
from daedalus.memory.privacy import is_private, public_sessions_sql
from daedalus.security.redact import redact


NOT_CONVERSATION_KEYS = ("subagent_of", "unattended", "heartbeat", "staff_id", "prompt_coach_of", "btw_of", "schedule_id")
"""Session metadata that marks a session which is not the operator's conversation. It covers every key of
:data:`daedalus.host.session_runner.NOT_OPERATOR_FACING` (a guard test holds the two together; that module
is too heavy to import from the search worker) plus the schedule id, which a scheduled task also carries.
Imported sessions are the operator's own conversations held elsewhere and are kept."""


def private(metadata: Any) -> bool:
    """Whether the operator made this chat private: nothing remembers it and no search returns it.
    The mark is :mod:`daedalus.memory.privacy`'s; two spellings of it once let a private chat into search."""
    return is_private(metadata)


def conversation(metadata: Any) -> bool:
    """Whether the session is the operator talking rather than an agent's or a timer's work."""
    return not (isinstance(metadata, dict) and any(metadata.get(key) for key in NOT_CONVERSATION_KEYS))


def searchable(metadata: Any) -> bool:
    """Whether the agent may find and read this session as one of the operator's past chats."""
    return conversation(metadata) and not private(metadata)


def _unset(key: str) -> str:
    # A JSON false reads as 0 and a missing key as NULL; both mean the mark is absent, as in Python.
    return f"coalesce(json_extract(s.metadata, '$.{key}'), '') IN ('', 0)"


SEARCHABLE_SQL = (
    "(CASE WHEN json_valid(s.metadata) THEN "
    + " AND ".join(_unset(key) for key in NOT_CONVERSATION_KEYS)
    + f" AND {public_sessions_sql('s')} ELSE 1 END)"
)
""":func:`searchable` over the row ``s`` of ``sessions``. ``CASE`` rather than ``AND`` because only it
promises that ``json_extract`` never sees text that is not JSON."""

PLAIN_PROJECT_SQL = "(p.system != '' OR CASE WHEN json_valid(p.settings) THEN coalesce(json_extract(p.settings, '$.ephemeral'), 0) = 1 ELSE 0 END)"
"""Whether the row ``p`` of ``projects`` is no project of the operator's: the scratch project a plain chat
gets (``ephemeral``), or one the installation keeps for itself (the voice concierge's)."""

PLAIN_SQL = f"s.project_id IN (SELECT p.id FROM projects p WHERE {PLAIN_PROJECT_SQL})"


@dataclass(frozen=True, slots=True)
class Reach:
    """The conversations one search or listing may answer from.

    ``project`` empty is the plain chats, all together, as the peers see them: every plain chat has a
    project of its own underneath, and taken literally "this project's chats" would be the chat alone.
    """

    project: str = ""
    besides: str = ""
    """The asking session: it already has its own history, and HistorySearch searches it."""

    def where(self) -> tuple[str, tuple[str, ...]]:
        """The SQL condition over ``sessions s`` and its parameters."""
        clause = f"{SEARCHABLE_SQL} AND " + ("s.project_id = ?" if self.project else PLAIN_SQL)
        params: tuple[str, ...] = (self.project,) if self.project else ()
        if self.besides:
            clause += " AND s.id != ?"
            params += (self.besides,)
        return clause, params


@dataclass(frozen=True, slots=True)
class Place:
    """Where a session lives for chat search: a project of the operator's, or the plain chats (``id`` empty)."""

    id: str = ""
    name: str = ""


async def place_of(db: Any, session_id: str) -> Place:
    row = await db.fetchone(
        f"SELECT p.id, p.name, {PLAIN_PROJECT_SQL} plain FROM sessions s JOIN projects p ON p.id = s.project_id WHERE s.id = ?",
        (session_id,),
    )
    if row is None or row["plain"]:
        return Place()
    return Place(str(row["id"]), str(row["name"]))


async def project_named(db: Any, wanted: str) -> Place | None:
    """The operator's project with this id or, ignoring case, this name; the most recently active of
    several with one name. Plain chats' scratch projects are nobody's to name."""
    wanted = wanted.strip()
    if not wanted:
        return None
    row = await db.fetchone(
        f"SELECT p.id, p.name FROM projects p WHERE NOT {PLAIN_PROJECT_SQL} AND (p.id = ? OR lower(p.name) = lower(?))"
        " ORDER BY p.id = ? DESC, (SELECT max(last_message_at) FROM sessions WHERE project_id = p.id) DESC LIMIT 1",
        (wanted, wanted, wanted),
    )
    return Place(str(row["id"]), str(row["name"])) if row is not None else None


ASK_CHARS = 200
HEADLINE_CHARS = 400


def _line(text: str, limit: int) -> str:
    flat = " ".join(redact(without_host_notes(text or "")).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


async def described(db: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Title, project, dates, transcript range and last headline of each session, by id, in one query.

    The headline is the hidden line every final reply ends with (task, status, anchors), which the
    learning records keep per run: the chat's own one-line summary, written when it happened."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = await db.fetchall(
        f"SELECT s.id, s.title, s.created_at, s.last_message_at, p.name project, {PLAIN_PROJECT_SQL} plain,"
        " (SELECT min(seq) FROM transcript WHERE session_id = s.id) first_seq,"
        " (SELECT max(seq) FROM transcript WHERE session_id = s.id) last_seq,"
        " (SELECT headline FROM learning_records WHERE session_id = s.id AND coalesce(headline, '') != '' ORDER BY id DESC LIMIT 1) headline"
        f" FROM sessions s JOIN projects p ON p.id = s.project_id WHERE s.id IN ({marks})",
        tuple(ids),
    )
    return {
        str(r["id"]): {
            "title": str(r["title"] or ""),
            "project": "" if r["plain"] else str(r["project"] or ""),
            "created_at": str(r["created_at"] or ""),
            "last_message_at": str(r["last_message_at"] or ""),
            "first_seq": int(r["first_seq"] or 0),
            "last_seq": int(r["last_seq"] or 0),
            "headline": _line(str(r["headline"] or ""), HEADLINE_CHARS),
        }
        for r in rows
    }


async def recent(db: Any, tenant: str, reach: Reach, *, limit: int, before: datetime | None = None, after: datetime | None = None) -> list[dict[str, Any]]:
    """The newest chats in ``reach`` by last activity, described as :func:`described` does, each with
    what was first asked there. SQL over sessions and the per-run learning records alone: no model
    call, no index, nothing that waits for the semantic search."""
    where, params = reach.where()
    if before is not None:
        where += " AND s.last_message_at < ?"
        params += (before.astimezone(UTC).isoformat(),)
    if after is not None:
        where += " AND s.last_message_at > ?"
        params += (after.astimezone(UTC).isoformat(),)
    rows = await db.fetchall(
        f"SELECT s.id FROM sessions s WHERE s.tenant_id = ? AND {where} ORDER BY s.last_message_at DESC, s.id LIMIT ?",
        (tenant, *params, limit),
    )
    ids = [str(r["id"]) for r in rows]
    about = await described(db, ids)
    out: list[dict[str, Any]] = []
    for sid in ids:
        info = about.get(sid)
        if info is None:
            continue
        ask = await db.fetchone("SELECT ask FROM learning_records WHERE session_id = ? AND coalesce(ask, '') != '' ORDER BY id LIMIT 1", (sid,))
        if ask is None:
            # An imported conversation has no runs of ours behind it; its first operator message stands in.
            ask = await db.fetchone(
                "SELECT json_extract(message, '$.content_blocks[0].text') ask FROM transcript"
                " WHERE session_id = ? AND json_extract(message, '$.role') = 'user' ORDER BY seq LIMIT 1",
                (sid,),
            )
        out.append({"id": sid, **info, "ask": _line(str(ask["ask"] or ""), ASK_CHARS) if ask is not None else ""})
    return out


__all__ = [
    "NOT_CONVERSATION_KEYS",
    "PLAIN_PROJECT_SQL",
    "PLAIN_SQL",
    "SEARCHABLE_SQL",
    "Place",
    "Reach",
    "conversation",
    "described",
    "place_of",
    "private",
    "project_named",
    "recent",
    "searchable",
]
