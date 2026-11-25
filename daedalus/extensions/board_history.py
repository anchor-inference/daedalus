"""A card's history, read back from the stored ``task.*`` events.

The bus writes every task event to ``app_events`` before anyone hears it, with the task's id inside
the payload and no column of its own; ``app_events_by_task`` indexes that id, so one card's history is
a lookup and not a scan of the event table. What is stored is the bus's: it reaches back as far as the
table's retention does, and a change made without an event (a few system paths move a card quietly)
has no line here.
"""

from __future__ import annotations

import json
from typing import Any

from daedalus.stores.database import Database

_GROUPED = ("task.moved", "task.assigned")
"""Events announced in the same step as a ``task.changed``, which then adds nothing to the list."""


def _actor(payload: dict[str, Any], titles: dict[str, str]) -> dict[str, Any]:
    """Who acted, as far as the event says: its kind, its id, and the chat's title when it was a chat.

    A system move (a dependent made ready, a stale card handed back) names no id."""
    ident = str(payload.get("actor_id") or "")
    prefix, _, session = ident.partition(":")
    return {"kind": str(payload.get("actor") or ""), "id": ident or None,
            "name": titles.get(session) if prefix in ("session", "orchestrator") else None}


def _text(kind: str, payload: dict[str, Any], assignee: dict[str, Any] | None) -> str:
    """One line for the list; the app may draw its own from the fields and ignore this."""
    if kind == "task.created":
        return f"Created in {payload.get('to') or 'todo'}"
    if kind == "task.moved":
        return f"{payload.get('from') or '?'} → {payload.get('to') or '?'}"
    if kind == "task.assigned":
        if assignee is None:
            return "Unassigned"
        return "Kept by the operator" if assignee["kind"] == "operator" else f"Assigned to {assignee.get('name') or 'a member'}"
    if kind == "task.accepted":
        return "Result accepted"
    if kind == "task.merge_failed":
        return "Merge failed" + (f": {payload['error']}" if payload.get("error") else "")
    if kind == "task.ci_failed":
        failed = ", ".join(str(name) for name in payload.get("failed_checks") or [payload.get("check_name")] if name)
        return "CI failed" + (f": {failed}" if failed else "")
    if kind == "task.launch_waiting":
        return "Waiting to start" + (f": {payload['error']}" if payload.get("error") else "")
    if "due_at" in payload:
        return f"Due {payload['due_at']}" if payload["due_at"] else "Due date cleared"
    return "Edited"


async def task_history(db: Database, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """The card's stored ``task.*`` events, oldest first, at most ``limit`` of the newest."""
    rows = await db.fetchall(
        "SELECT seq, at, type, payload_json FROM app_events"
        " WHERE json_extract(payload_json, '$.task_id') = ? AND type LIKE 'task.%'"
        " ORDER BY seq DESC LIMIT ?", (task_id, limit))
    events = [(int(row["seq"]), str(row["at"]), str(row["type"]), json.loads(row["payload_json"])) for row in reversed(rows)]
    sessions: set[str] = set()
    for _, _, _, payload in events:
        prefix, _, session = str(payload.get("actor_id") or "").partition(":")
        if prefix in ("session", "orchestrator") and session:
            sessions.add(session)
    staff_ids = {str(payload["assignee_staff_id"]) for _, _, _, payload in events if payload.get("assignee_staff_id")}
    titles = {r["id"]: r["title"] for r in await _in(db, "SELECT id, title FROM sessions", sessions)}
    names = {r["id"]: r["name"] for r in await _in(db, "SELECT id, name FROM staff", staff_ids)}
    out: list[dict[str, Any]] = []
    for index, (seq, at, kind, payload) in enumerate(events):
        following = events[index + 1] if index + 1 < len(events) else None
        # The bare "changed" that opens a step also announced as a move or a reassignment says it twice.
        if (kind == "task.changed" and "due_at" not in payload and following is not None and following[2] in _GROUPED
                and following[3].get("actor_id") == payload.get("actor_id") and following[0] - seq <= 2):
            continue
        assignee: dict[str, Any] | None = None
        if kind == "task.assigned":
            if payload.get("assignee_operator"):
                assignee = {"kind": "operator"}
            elif payload.get("assignee_staff_id"):
                assignee = {"kind": "staff", "id": str(payload["assignee_staff_id"]),
                            "name": names.get(str(payload["assignee_staff_id"]))}
        item: dict[str, Any] = {"seq": seq, "type": kind, "at": at, "actor": _actor(payload, titles),
                                "from": payload.get("from"), "to": payload.get("to"), "text": _text(kind, payload, assignee)}
        if kind == "task.assigned":
            item["assignee"] = assignee
        if "due_at" in payload:
            item["due_at"] = payload["due_at"] or None
        out.append(item)
    return out


async def _in(db: Database, select: str, ids: set[str]) -> list[Any]:
    if not ids:
        return []
    return await db.fetchall(f"{select} WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))  # noqa: S608 — placeholders only
