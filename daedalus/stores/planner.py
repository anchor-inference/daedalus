"""The operator's personal tasks: lists, due dates, time blocks on the calendar, repeats.

These are the operator's own to-dos, not the agent's Board. Dates are kept as the operator wrote
them: ``due_date`` is a calendar date and ``due_time`` a wall-clock time, both in the calendar's time
zone, because "pay rent on the 5th" must stay on the 5th wherever the operator travels. A time block
(``scheduled_start``/``scheduled_end``) is a real interval, stored as UTC instants like events.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from daedalus.stores import recurrence
from daedalus.stores.calendar import color_of, instant, now, reminders_of
from daedalus.stores.database import Database

VIEWS = ("inbox", "today", "upcoming", "overdue", "done", "all")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DONE_LIMIT = 300


def task_view(row: Any) -> dict[str, Any]:
    view = {key: row[key] for key in ("id", "list_id", "title", "notes", "due_date", "due_time", "scheduled_start", "scheduled_end", "duration", "priority", "done_at", "recurrence", "position", "version", "created_at", "updated_at")}
    view["reminders"] = json.loads(row["reminders"] or "[]")
    view["done"] = row["done_at"] is not None
    return view


def list_view(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "color": row["color"], "position": row["position"], "inbox": bool(row["inbox"]), "open": row["open"] if "open" in row.keys() else 0}


class PlannerStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- lists ---------------------------------------------------------------------------------

    async def lists(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT l.*, (SELECT count(*) FROM planner_tasks t WHERE t.list_id=l.id AND t.done_at IS NULL) AS open FROM planner_lists l ORDER BY l.inbox DESC, l.position, l.created_at"
        )
        return [list_view(row) for row in rows]

    async def inbox(self) -> str:
        row = await self.db.fetchone("SELECT id FROM planner_lists WHERE inbox=1")
        if row is not None:
            return str(row["id"])
        # The migration makes the inbox and deleting it is refused; this is a database edited by hand.
        list_id = uuid.uuid4().hex
        await self.db.execute("INSERT INTO planner_lists(id,name,color,position,inbox,created_at) VALUES(?,?,?,0,1,?)", (list_id, "Inbox", "", now()))
        return list_id

    async def create_list(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ValueError("the list name needs 1 to 100 characters")
        list_id = uuid.uuid4().hex
        position = await self.db.fetchone("SELECT COALESCE(MAX(position), 0) + 1 AS next FROM planner_lists")
        await self.db.execute(
            "INSERT INTO planner_lists(id,name,color,position,inbox,created_at) VALUES(?,?,?,?,0,?)",
            (list_id, name, color_of(body.get("color"), allow_empty=True), position["next"] if position else 1, now()),
        )
        return next(item for item in await self.lists() if item["id"] == list_id)

    async def update_list(self, list_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM planner_lists WHERE id=?", (list_id,))
        if row is None:
            raise KeyError(list_id)
        name = str(body["name"]).strip() if body.get("name") is not None else row["name"]
        if not name or len(name) > 100:
            raise ValueError("the list name needs 1 to 100 characters")
        color = color_of(body["color"], allow_empty=True) if body.get("color") is not None else row["color"]
        position = int(body["position"]) if body.get("position") is not None else row["position"]
        await self.db.execute("UPDATE planner_lists SET name=?,color=?,position=? WHERE id=?", (name, color, position, list_id))
        return next(item for item in await self.lists() if item["id"] == list_id)

    async def delete_list(self, list_id: str) -> None:
        row = await self.db.fetchone("SELECT * FROM planner_lists WHERE id=?", (list_id,))
        if row is None:
            raise KeyError(list_id)
        if row["inbox"]:
            raise ValueError("the inbox cannot be deleted")
        async with self.db.transaction() as conn:
            # Its open tasks move to the inbox rather than disappearing with a list deleted in passing;
            # finished ones go with it.
            cursor = await conn.execute("SELECT id FROM planner_lists WHERE inbox=1")
            inbox = await cursor.fetchone()
            if inbox is not None:
                await conn.execute("UPDATE planner_tasks SET list_id=?, version=version+1, updated_at=? WHERE list_id=? AND done_at IS NULL", (inbox["id"], now(), list_id))
            await conn.execute("DELETE FROM planner_lists WHERE id=?", (list_id,))

    # -- tasks ---------------------------------------------------------------------------------

    async def timezone(self) -> str:
        """The calendar's zone, for a caller that did not name one (the store has no browser to ask)."""
        stored = await self.db.kv_get("calendar_settings", {}) or {}
        return str(stored.get("timezone") or "UTC")

    async def tasks(self, view: str = "all", list_id: str | None = None, start: str | None = None, end: str | None = None, timezone: str | None = None) -> list[dict[str, Any]]:
        """Tasks for one view of the planner, or every task due or scheduled in ``[start, end)``.

        ``inbox`` is the open tasks of the Inbox list; ``today`` those due today or with a time block
        starting today; ``upcoming`` the open tasks dated after today; ``overdue`` the open tasks due
        before today; ``done`` the finished ones, newest first; ``all`` every task, open first.
        "Today" is the operator's today, in the calendar's zone.
        """
        zone = recurrence.zone(timezone or await self.timezone())
        local_now = datetime.now(UTC).astimezone(zone)
        today = local_now.date().isoformat()
        day_start = datetime.combine(local_now.date(), time(), zone).astimezone(UTC).isoformat()
        day_end = datetime.combine(local_now.date() + timedelta(days=1), time(), zone).astimezone(UTC).isoformat()
        where: list[str] = []
        params: list[Any] = []
        order = "COALESCE(due_date, substr(scheduled_start, 1, 10), '9999'), due_time, priority DESC, position, created_at"
        if start or end:
            if not (start and end):
                raise ValueError("a task range needs both start and end")
            first, last = instant(start), instant(end)
            if first >= last:
                raise ValueError("the task range needs an end after its start")
            first_day = datetime.fromisoformat(first).astimezone(zone).date().isoformat()
            last_day = (datetime.fromisoformat(last).astimezone(zone) - timedelta(microseconds=1)).date().isoformat()
            where.append("((scheduled_start IS NOT NULL AND scheduled_start < ? AND COALESCE(scheduled_end, scheduled_start) >= ?) OR (scheduled_start IS NULL AND due_date BETWEEN ? AND ?))")
            params += [last, first, first_day, last_day]
        elif view not in VIEWS:
            raise ValueError("the task view is inbox, today, upcoming, overdue, done or all")
        elif view == "inbox":
            where.append("done_at IS NULL AND list_id=(SELECT id FROM planner_lists WHERE inbox=1)")
            order = "position, created_at"
        elif view == "today":
            where.append("done_at IS NULL AND (due_date = ? OR (scheduled_start >= ? AND scheduled_start < ?))")
            params += [today, day_start, day_end]
        elif view == "upcoming":
            where.append("done_at IS NULL AND (due_date > ? OR (due_date IS NULL AND scheduled_start >= ?))")
            params += [today, day_end]
        elif view == "overdue":
            where.append("done_at IS NULL AND due_date < ?")
            params.append(today)
        elif view == "done":
            where.append("done_at IS NOT NULL")
            order = "done_at DESC"
        else:
            order = "done_at IS NOT NULL, " + order
        if list_id:
            where.append("list_id=?")
            params.append(list_id)
        sql = "SELECT * FROM planner_tasks" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT {DONE_LIMIT if view == 'done' else 2000}"
        return [task_view(row) for row in await self.db.fetchall(sql, params)]

    async def get(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM planner_tasks WHERE id=?", (task_id,))
        return task_view(row) if row else None

    async def _fields(self, body: dict[str, Any], base: Any) -> dict[str, Any]:
        def pick(key: str, default: Any = None) -> Any:
            return body[key] if key in body else (base[key] if base is not None else default)

        title = str(pick("title", "") or "").strip()
        if not title or len(title) > 500:
            raise ValueError("the task title needs 1 to 500 characters")
        list_id = pick("list_id") or await self.inbox()
        if await self.db.fetchone("SELECT 1 FROM planner_lists WHERE id=?", (list_id,)) is None:
            raise ValueError("no such task list")
        due_date = pick("due_date")
        if due_date:
            due_date = date.fromisoformat(str(due_date)[:10]).isoformat()
        due_time = pick("due_time") or None
        if due_time and (not due_date or not _TIME.match(str(due_time))):
            raise ValueError("a due time is HH:MM and needs a due date")
        scheduled_start, scheduled_end = pick("scheduled_start") or None, pick("scheduled_end") or None
        duration = pick("duration")
        if scheduled_start:
            scheduled_start = instant(str(scheduled_start))
            if scheduled_end:
                scheduled_end = instant(str(scheduled_end))
            elif duration:
                scheduled_end = (datetime.fromisoformat(scheduled_start) + timedelta(minutes=int(duration))).isoformat()
            else:
                scheduled_end = (datetime.fromisoformat(scheduled_start) + timedelta(minutes=30)).isoformat()
            if scheduled_end <= scheduled_start:
                raise ValueError("a time block must end after it starts")
            duration = int((datetime.fromisoformat(scheduled_end) - datetime.fromisoformat(scheduled_start)).total_seconds() // 60)
        elif scheduled_end:
            raise ValueError("a time block needs a start")
        if duration is not None and (isinstance(duration, bool) or not 1 <= int(duration) <= 24 * 60 * 7):
            raise ValueError("a task's duration is 1 minute to a week")
        priority = pick("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 3:
            raise ValueError("priority is 0 (none) to 3 (high)")
        rule = recurrence.normalize(str(pick("recurrence", "") or ""))
        if rule and not due_date:
            raise ValueError("a repeating task needs a due date to repeat from")
        reminders = json.dumps(reminders_of(pick("reminders", [])))
        if json.loads(reminders) and not (due_date or scheduled_start):
            raise ValueError("a reminder needs a due date or a time block")
        return {
            "title": title, "notes": str(pick("notes", "") or "")[:20000], "list_id": list_id, "due_date": due_date, "due_time": due_time,
            "scheduled_start": scheduled_start, "scheduled_end": scheduled_end, "duration": int(duration) if duration is not None else None,
            "priority": priority, "recurrence": rule, "reminders": reminders,
            "position": float(pick("position", 0) or 0),
        }

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        fields = await self._fields(body, None)
        if "position" not in body:
            row = await self.db.fetchone("SELECT COALESCE(MAX(position), 0) + 1 AS next FROM planner_tasks WHERE list_id=?", (fields["list_id"],))
            fields["position"] = float(row["next"]) if row else 1.0
        task_id = uuid.uuid4().hex
        stamp = now()
        await self.db.execute(
            "INSERT INTO planner_tasks(id,list_id,title,notes,due_date,due_time,scheduled_start,scheduled_end,duration,priority,reminders,recurrence,position,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, fields["list_id"], fields["title"], fields["notes"], fields["due_date"], fields["due_time"], fields["scheduled_start"], fields["scheduled_end"],
             fields["duration"], fields["priority"], fields["reminders"], fields["recurrence"], fields["position"], stamp, stamp),
        )
        found = await self.get(task_id)
        assert found is not None
        return found

    async def update(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM planner_tasks WHERE id=?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        if int(body.get("version", -1)) != row["version"]:
            raise RuntimeError("the task changed elsewhere; reload it before saving")
        fields = await self._fields(body, row)
        cursor_rows = await self._write(task_id, row["version"], fields)
        if cursor_rows == 0:
            raise RuntimeError("the task changed elsewhere; reload it before saving")
        found = await self.get(task_id)
        assert found is not None
        return found

    async def _write(self, task_id: str, version: int, fields: dict[str, Any]) -> int:
        columns = ["list_id", "title", "notes", "due_date", "due_time", "scheduled_start", "scheduled_end", "duration", "priority", "reminders", "recurrence", "position"]
        values = [fields[key] for key in columns]
        assignments = ",".join(f"{column}=?" for column in columns)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(f"UPDATE planner_tasks SET {assignments}, version=version+1, updated_at=? WHERE id=? AND version=?", (*values, now(), task_id, version))
            return cursor.rowcount

    async def delete(self, task_id: str, version: int | None = None) -> None:
        row = await self.db.fetchone("SELECT version FROM planner_tasks WHERE id=?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        if version is not None and version != row["version"]:
            raise RuntimeError("the task changed elsewhere; reload it before deleting")
        await self.db.execute("DELETE FROM planner_tasks WHERE id=?", (task_id,))

    async def complete(self, task_id: str, done: bool = True, timezone: str | None = None) -> dict[str, Any]:
        """Mark a task done or open again.

        Finishing a repeating task records the finished occurrence as its own done task and moves the
        task to its next due date (its time block, if any, moves by the same amount), so the done view
        keeps the history and the list keeps one open copy. A series with no further date is simply done.
        """
        row = await self.db.fetchone("SELECT * FROM planner_tasks WHERE id=?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        stamp = now()
        if not done:
            await self.db.execute("UPDATE planner_tasks SET done_at=NULL, version=version+1, updated_at=? WHERE id=?", (stamp, task_id))
        elif row["done_at"] is not None:
            pass
        elif row["recurrence"] and row["due_date"]:
            due = datetime.fromisoformat(row["due_date"])
            following = recurrence.next_after(row["recurrence"], due, due)
            if following is None:
                await self.db.execute("UPDATE planner_tasks SET done_at=?, version=version+1, updated_at=? WHERE id=?", (stamp, stamp, task_id))
            else:
                shift = following - due
                # The rule is read from the current due date each time, so a counted series counts
                # down as it advances; otherwise COUNT=3 would repeat forever.
                rule = row["recurrence"]
                found = recurrence.parts(rule)
                if "COUNT" in found:
                    found["COUNT"] = str(max(1, int(found["COUNT"]) - 1))
                    rule = "RRULE:" + ";".join(f"{key}={value}" for key, value in found.items())
                # The block moves by whole days on the operator's wall clock: a 09:00 walk stays at
                # 09:00 when the clocks change, which adding the same number of UTC hours would not.
                zone = recurrence.zone(timezone or await self.timezone())

                def moved(value: str | None) -> str | None:
                    if not value:
                        return None
                    local = datetime.fromisoformat(value).astimezone(zone).replace(tzinfo=None) + timedelta(days=shift.days)
                    return local.replace(tzinfo=zone).astimezone(UTC).isoformat()

                scheduled_start, scheduled_end = moved(row["scheduled_start"]), moved(row["scheduled_end"])
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "INSERT INTO planner_tasks(id,list_id,title,notes,due_date,due_time,scheduled_start,scheduled_end,duration,priority,done_at,reminders,recurrence,position,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,'[]','',?,?,?)",
                        (uuid.uuid4().hex, row["list_id"], row["title"], row["notes"], row["due_date"], row["due_time"], row["scheduled_start"], row["scheduled_end"],
                         row["duration"], row["priority"], stamp, row["position"], row["created_at"], stamp),
                    )
                    await conn.execute(
                        "UPDATE planner_tasks SET due_date=?, scheduled_start=?, scheduled_end=?, recurrence=?, version=version+1, updated_at=? WHERE id=?",
                        (following.date().isoformat(), scheduled_start, scheduled_end, rule, stamp, task_id),
                    )
        else:
            await self.db.execute("UPDATE planner_tasks SET done_at=?, version=version+1, updated_at=? WHERE id=?", (stamp, stamp, task_id))
        found = await self.get(task_id)
        assert found is not None
        return found

    async def reminder_candidates(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM planner_tasks WHERE done_at IS NULL AND reminders != '[]' AND (due_date IS NOT NULL OR scheduled_start IS NOT NULL)")
        return [task_view(row) for row in rows]


__all__ = ["VIEWS", "PlannerStore", "list_view", "task_view"]
