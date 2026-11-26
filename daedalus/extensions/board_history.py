"""A card's history, read from its own table (:mod:`daedalus.stores.task_history`).

Each row is one change of one field a reader of the card cares about, written in the transaction of
the change. A line is drawn in the vocabulary the task events use (``task.created``, ``task.moved``,
``task.assigned``, ``task.accepted``, ``task.changed`` for a title, a priority or a date, which
``field`` names, and ``task.removed`` / ``task.restored``), so a client that already reads those
reads these. A line marked ``backfilled`` was written when the table was made, from what was left
of the card then: what happened to it before is not all there.
"""

from __future__ import annotations

import json
from typing import Any

from daedalus.stores.database import Database

_TYPES = {"created": "task.created", "moved": "task.moved", "assigned": "task.assigned", "accepted": "task.accepted",
          "titled": "task.changed", "prioritised": "task.changed", "dated": "task.changed",
          "removed": "task.removed", "restored": "task.restored"}
_FIELDS = {"titled": "title", "prioritised": "priority", "dated": "due_at"}


def _owner(value: Any, names: dict[str, str]) -> dict[str, Any] | None:
    text = str(value or "")
    if text == "operator":
        return {"kind": "operator"}
    if text.startswith("staff:"):
        return {"kind": "staff", "id": text.removeprefix("staff:"), "name": names.get(text.removeprefix("staff:"))}
    return None


def _actor(kind: str | None, ident: str | None, titles: dict[str, str], names: dict[str, str]) -> dict[str, Any]:
    """Who acted: the command's principal, its id, and the chat's or the member's name when it was one.

    A change no command claimed (a sweep of the legacy board, a migration) is the system's and names no id."""
    prefix, _, rest = (ident or "").partition(":")
    name = titles.get(rest) if prefix in ("session", "orchestrator") else names.get(rest) if prefix == "staff" else None
    return {"kind": kind or "system", "id": ident or None, "name": name}


def _text(kind: str, change: dict[str, Any], assignee: dict[str, Any] | None) -> str:
    """One line for the list; the app may draw its own from the fields and ignore this."""
    if kind == "created":
        return f"Created in {change['to']}" if change.get("to") else "Created"
    if kind == "moved":
        return f"{change.get('from') or '?'} → {change.get('to') or '?'}"
    if kind == "assigned":
        if assignee is None:
            return "Unassigned"
        return "Kept by the operator" if assignee["kind"] == "operator" else f"Assigned to {assignee.get('name') or 'a member'}"
    if kind == "accepted":
        return "Result accepted"
    if kind == "titled":
        return f"Renamed to “{change.get('to')}”"
    if kind == "prioritised":
        return f"Priority P{change.get('from')} → P{change.get('to')}"
    if kind == "dated":
        return f"Due {change['to']}" if change.get("to") else "Due date cleared"
    return "Removed from the board" if kind == "removed" else "Back on the board"


async def task_history(db: Database, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """The card's history, oldest first, at most ``limit`` of the newest lines."""
    rows = list(reversed(await db.fetchall(
        "SELECT seq, at, kind, change_json, actor_kind, actor_id, operation, cause_task_id, backfilled"
        " FROM board_task_history WHERE task_id = ? ORDER BY seq DESC LIMIT ?", (task_id, limit))))
    changes = [json.loads(row["change_json"] or "{}") for row in rows]
    sessions: set[str] = set()
    staff: set[str] = set()
    for row, change in zip(rows, changes, strict=True):
        prefix, _, rest = str(row["actor_id"] or "").partition(":")
        if prefix in ("session", "orchestrator") and rest:
            sessions.add(rest)
        elif prefix == "staff" and rest:
            staff.add(rest)
        if row["kind"] == "assigned":
            staff.update(str(value).removeprefix("staff:") for value in (change.get("from"), change.get("to"))
                         if str(value or "").startswith("staff:"))
    causes = {str(row["cause_task_id"]) for row in rows if row["cause_task_id"]}
    titles = {r["id"]: r["title"] for r in await _in(db, "SELECT id, title FROM sessions", sessions)}
    names = {r["id"]: r["name"] for r in await _in(db, "SELECT id, name FROM staff", staff)}
    cards = {r["id"]: r["title"] for r in await _in(db, "SELECT id, title FROM board_tasks", causes)}
    out: list[dict[str, Any]] = []
    for row, change in zip(rows, changes, strict=True):
        kind = str(row["kind"])
        assignee = _owner(change.get("to"), names) if kind == "assigned" else None
        moved = kind in ("created", "moved")
        item: dict[str, Any] = {
            "seq": int(row["seq"]), "type": _TYPES[kind], "at": str(row["at"]),
            "actor": _actor(row["actor_kind"], row["actor_id"], titles, names),
            "from": change.get("from") if moved else None, "to": change.get("to") if moved else None,
            "text": _text(kind, change, assignee),
            # The command that made the change (``board.task.close``, ``staff.report``, …); null for the system's.
            "operation": row["operation"],
            "backfilled": bool(row["backfilled"]),
        }
        if kind in _FIELDS:
            item["field"] = _FIELDS[kind]
            if kind != "dated":
                item["change"] = {"from": change.get("from"), "to": change.get("to")}
        if kind == "assigned":
            item["assignee"] = assignee
        if kind == "dated" or (kind == "created" and change.get("due_at")):
            item["due_at"] = change.get("to") if kind == "dated" else change["due_at"]
        if row["cause_task_id"]:
            # A dependent that moved because the card it waited on was closed or accepted.
            item["cause"] = {"task_id": str(row["cause_task_id"]), "title": cards.get(str(row["cause_task_id"]))}
        out.append(item)
    return out


async def _in(db: Database, select: str, ids: set[str]) -> list[Any]:
    if not ids:
        return []
    return await db.fetchall(f"{select} WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))  # noqa: S608 — placeholders only
