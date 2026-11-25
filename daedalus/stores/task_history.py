"""A board card's own history: one row for each change a reader of the card cares about.

The rows are written by triggers on ``board_tasks``, so they land in the transaction of the change
itself and cover every path that changes a card: the board's commands, a member's report into
review, a verdict, the dependents a close or an acceptance unblocks, a CI or contract change that
sends a card back, the legacy board's sweeps. The card's history used to be read back from the
bus's event ring instead. That ring keeps a week, and a change made without an event (most of the
paths above) was never in it, so an older card had no history and a recent one had gaps.

A trigger cannot know who acted. A command that runs through :meth:`ControlStore.mutate` names its
principal and its operation on the rows its transaction wrote (:func:`attribute`); a row no command
claims is the system's.

The facets, one row each (``change_json`` holds the values):

- ``created``: ``{"to": status, "due_at", "priority"}``, with an ``assigned`` row after it when the
  card was made with an owner;
- ``moved``: the status, ``{"from", "to"}``;
- ``assigned``: ``"operator"``, ``"staff:<id>"`` or null on each side;
- ``accepted``: the accepted result, ``{"result_id"}``;
- ``titled``, ``prioritised``, ``dated``: ``{"from", "to"}``;
- ``removed`` and ``restored``: ``removed_at`` set or cleared.

A migration that rebuilds ``board_tasks`` drops these triggers with the table and must create them
again (:data:`TRIGGERS`).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import aiosqlite

AT = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
"""An instant as the bus writes one, so a backfilled line and a live one sort and read alike."""

_ASSIGNEE = "CASE WHEN {row}.assignee_operator THEN 'operator' WHEN {row}.assignee_staff_id IS NOT NULL THEN 'staff:' || {row}.assignee_staff_id END"

TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS board_task_history_created AFTER INSERT ON board_tasks BEGIN
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        VALUES (NEW.id, {AT}, 'created', json_object('to', NEW.status, 'due_at', NEW.due_at, 'priority', NEW.priority));
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'assigned', json_object('from', NULL, 'to', {_ASSIGNEE.format(row="NEW")})
        WHERE NEW.assignee_operator OR NEW.assignee_staff_id IS NOT NULL;
END;
CREATE TRIGGER IF NOT EXISTS board_task_history_changed
AFTER UPDATE OF status, assignee_staff_id, assignee_operator, priority, due_at, title, accepted_result_id, removed_at ON board_tasks
BEGIN
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'accepted', json_object('result_id', NEW.accepted_result_id)
        WHERE NEW.accepted_result_id IS NOT NULL AND OLD.accepted_result_id IS NOT NEW.accepted_result_id;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'moved', json_object('from', OLD.status, 'to', NEW.status)
        WHERE OLD.status IS NOT NEW.status;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'assigned', json_object('from', {_ASSIGNEE.format(row="OLD")}, 'to', {_ASSIGNEE.format(row="NEW")})
        WHERE OLD.assignee_staff_id IS NOT NEW.assignee_staff_id OR OLD.assignee_operator IS NOT NEW.assignee_operator;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'titled', json_object('from', OLD.title, 'to', NEW.title)
        WHERE OLD.title IS NOT NEW.title;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'prioritised', json_object('from', OLD.priority, 'to', NEW.priority)
        WHERE OLD.priority IS NOT NEW.priority;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'dated', json_object('from', OLD.due_at, 'to', NEW.due_at)
        WHERE OLD.due_at IS NOT NEW.due_at;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'removed', '{{}}' WHERE OLD.removed_at IS NULL AND NEW.removed_at IS NOT NULL;
    INSERT INTO board_task_history(task_id, at, kind, change_json)
        SELECT NEW.id, {AT}, 'restored', '{{}}' WHERE OLD.removed_at IS NOT NULL AND NEW.removed_at IS NULL;
END;
"""

# ``seq`` is AUTOINCREMENT so that :func:`attribute` can claim "the rows written since" by number:
# a reused rowid would let a command claim a row that is not its own. The foreign key takes a card's
# history with it on the one path that deletes a card row (the legacy board's delete).
SQL = f"""
CREATE TABLE IF NOT EXISTS board_task_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('created', 'moved', 'assigned', 'accepted', 'titled', 'prioritised', 'dated', 'removed', 'restored')),
    change_json TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(change_json)),
    actor_kind TEXT,
    actor_id TEXT,
    operation TEXT,
    cause_task_id TEXT,
    backfilled INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1))
);
CREATE INDEX IF NOT EXISTS board_task_history_by_task ON board_task_history(task_id, seq);
{TRIGGERS}
DROP INDEX IF EXISTS app_events_by_task;
"""
"""The table, its triggers, and the end of the index the ring-read history needed."""

_RING_KINDS = {"task.created": "created", "task.moved": "moved", "task.assigned": "assigned", "task.accepted": "accepted"}


def instant(text: str) -> str:
    """``text`` as the triggers write an instant; a value that is not one is kept as it is."""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return text
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    # An instant that could not be read goes first: it is a creation from a row whose date is unusable.
    return (0, "") if not entry["at"].endswith("Z") else (1, entry["at"])


def _from_ring(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The facet a stored ``task.*`` event recorded, or ``None`` for one that says nothing a row could hold."""
    if kind == "task.changed":
        # A bare "changed" names no field; only a date change says what it was.
        return {"kind": "dated", "change": {"from": None, "to": payload["due_at"] or None}} if "due_at" in payload else None
    facet = _RING_KINDS.get(kind)
    if facet is None:
        return None
    if facet == "created":
        return {"kind": facet, "change": {"to": payload.get("to"), "due_at": payload.get("due_at") or None}}
    if facet == "moved":
        return {"kind": facet, "change": {"from": payload.get("from"), "to": payload.get("to")}}
    if facet == "assigned":
        owner = "operator" if payload.get("assignee_operator") else f"staff:{payload['assignee_staff_id']}" if payload.get("assignee_staff_id") else None
        return {"kind": facet, "change": {"from": None, "to": owner}}
    return {"kind": facet, "change": {}}


async def backfill(conn: aiosqlite.Connection) -> None:
    """Give every card that exists the history that can still be told truthfully, marked backfilled.

    What is known: the card's creation (``created_at``; the column it was made in is not recorded, so
    it is null unless the event ring still holds the creation), the ``task.*`` events the ring still
    holds (a week of them), the verdict that accepted the result a done card carries, and the card's
    removal. What is not: every change older than the ring and every change made without an event.
    A card that already has rows (a second run) is left alone.
    """
    async with conn.execute("SELECT id, created_at, accepted_result_id, removed_at FROM board_tasks") as cursor:
        tasks = {row["id"]: row for row in await cursor.fetchall()}
    async with conn.execute("SELECT DISTINCT task_id FROM board_task_history") as cursor:
        done = {row[0] for row in await cursor.fetchall()}
    ring: dict[str, list[dict[str, Any]]] = defaultdict(list)
    async with conn.execute("SELECT at, type, payload_json FROM app_events WHERE type LIKE 'task.%' ORDER BY seq") as cursor:
        for row in await cursor.fetchall():
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            task_id = str(payload.get("task_id") or "") if isinstance(payload, dict) else ""
            facet = _from_ring(row["type"], payload) if task_id in tasks else None
            if facet is None:
                continue
            ring[task_id].append({**facet, "at": instant(str(row["at"])), "actor_kind": payload.get("actor") or None,
                                  "actor_id": payload.get("actor_id") or None})
    for task_id, task in tasks.items():
        if task_id in done:
            continue
        entries = list(ring.get(task_id, []))
        if not any(entry["kind"] == "created" for entry in entries):
            entries.insert(0, {"kind": "created", "change": {"to": None}, "at": instant(str(task["created_at"])),
                               "actor_kind": None, "actor_id": None})
        if task["accepted_result_id"] and not any(entry["kind"] == "accepted" for entry in entries):
            async with conn.execute("SELECT created_at, reviewer_actor_id FROM review_verdicts WHERE result_id = ? AND accepted = 1"
                                    " ORDER BY created_at DESC LIMIT 1", (task["accepted_result_id"],)) as cursor:
                verdict = await cursor.fetchone()
            if verdict is not None:
                reviewer = str(verdict["reviewer_actor_id"] or "")
                entries.append({"kind": "accepted", "change": {"result_id": task["accepted_result_id"]},
                                "at": instant(str(verdict["created_at"])),
                                "actor_kind": "operator" if reviewer.startswith("operator:") else "agent" if reviewer else None,
                                "actor_id": reviewer or None})
        if task["removed_at"]:
            entries.append({"kind": "removed", "change": {}, "at": instant(str(task["removed_at"])), "actor_kind": None, "actor_id": None})
        # Stable: the ring's own order holds among events of the same instant.
        entries.sort(key=_sort_key)
        await conn.executemany(
            "INSERT INTO board_task_history(task_id, at, kind, change_json, actor_kind, actor_id, backfilled)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            [(task_id, entry["at"], entry["kind"], json.dumps(entry["change"], ensure_ascii=False), entry["actor_kind"], entry["actor_id"])
             for entry in entries])


async def last_seq(conn: aiosqlite.Connection) -> int:
    """The newest history row's number, read before a command's effect so it can claim what follows."""
    async with conn.execute("SELECT coalesce(max(seq), 0) FROM board_task_history") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def attribute(conn: aiosqlite.Connection, since: int, *, actor_kind: str, actor_id: str, operation: str,
                    task_id: str | None) -> None:
    """Name who acted, and in which operation, on the rows the current transaction wrote after ``since``.

    Run inside the command's own transaction, which holds the database's only connection, so no
    other writer's rows can be among them. A row about another card than the command's own was
    written in consequence of it (a dependent a close unblocked); it names the command's card as its
    cause. A command on a collection (a create) has the card it made as its own."""
    await conn.execute(
        "UPDATE board_task_history SET actor_kind = ?, actor_id = ?, operation = ?,"
        " cause_task_id = CASE WHEN ? IS NULL OR task_id = ? THEN NULL ELSE ? END"
        " WHERE seq > ? AND actor_kind IS NULL AND actor_id IS NULL",
        (actor_kind, actor_id, operation, task_id, task_id, task_id, since))


__all__ = ["SQL", "TRIGGERS", "attribute", "backfill", "instant", "last_seq"]
