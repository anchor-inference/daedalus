"""The operator's calendars, their events and pending remote changes.

Local edits stay in SQLite until a connector acknowledges them. A remote refresh never overwrites
an unacknowledged edit; this is what lets a disconnected calendar remain editable.

A recurring event is stored once, as its series, and expanded into occurrences when it is read (see
:mod:`daedalus.stores.recurrence`). One changed occurrence is an override row pointing at its series
by ``series_id`` and naming the occurrence it replaces by ``original_start``; one removed occurrence
is either that start in the series' ``exdates`` or, for providers that address occurrences by their
own id (Google, Outlook), a cancelled override row the connector can send as an instance delete.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse

from daedalus.stores import recurrence
from daedalus.stores.database import Database

SYNCING: set[str] = set()
"""Accounts whose synchronization is running right now, so a calendar can say "syncing"."""
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_REMINDER_MINUTES = 4 * 7 * 24 * 60
"""Four weeks: the longest lead a reminder may have, and so the window the reminder loop reads ahead."""
SETTINGS_KEY = "calendar_settings"
DEFAULT_SETTINGS: dict[str, Any] = {
    "week_start": 1,
    "work_start": "09:00",
    "work_end": "18:00",
    "default_view": "week",
    "default_duration": 60,
    "default_reminders": [10],
    "timezone": "",
    "show_weekends": True,
}
KINDS = ("local", "google", "outlook", "caldav", "ics")
PROVIDER_COLORS = {"google": "#34a853", "outlook": "#0078d4", "caldav": "#fc3f1d", "ics": "#8e8e93"}
CALDAV_PRESETS = {"yandex": "https://caldav.yandex.ru", "icloud": "https://caldav.icloud.com"}
"""Providers that are plain CalDAV servers at a known address: connecting one is CalDAV with the
address filled in. Yandex wants an app password and iCloud an app-specific password; both are the
``password`` credential."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def instant(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("start and end need ISO 8601 dates with a timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError("start and end need an explicit timezone")
    return parsed.astimezone(UTC).isoformat()


def moment(value: str) -> datetime:
    return datetime.fromisoformat(instant(value))


def reminders_of(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value or "[]")
    if not isinstance(value, list | tuple) or len(value) > 5:
        raise ValueError("reminders are up to five lead times in minutes")
    found: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float) or not 0 <= int(item) <= MAX_REMINDER_MINUTES:
            raise ValueError(f"a reminder is 0 to {MAX_REMINDER_MINUTES} minutes before the start")
        found.add(int(item))
    return sorted(found)


def color_of(value: Any, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if allow_empty and not text:
        return ""
    if not COLOR.match(text):
        raise ValueError("a colour is #rrggbb")
    return text.lower()


def _name(value: Any, what: str, limit: int = 100) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ValueError(f"the {what} needs 1 to {limit} characters")
    return text


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def event_view(row: Any) -> dict[str, Any]:
    """The stored event: a single event or a series as written, without expansion."""
    view = {key: row[key] for key in ("id", "calendar_id", "account_id", "remote_id", "title", "description", "start_at", "end_at", "timezone", "location", "recurrence", "dirty", "version", "updated_at", "color", "series_id", "original_start")}
    view["all_day"] = bool(row["all_day"])
    view["reminders"] = None if row["reminders"] is None else json.loads(row["reminders"])
    view["exdates"] = json.loads(row["exdates"] or "[]")
    view["cancelled"] = bool(row["cancelled"])
    view["conflict"] = bool(row["conflict"])
    view["conflict_kind"] = row["conflict"] or None
    view["pending_sync"] = bool(row["dirty"])
    return view


def account_view(row: Any, calendar_id: str | None = None, conflicts: int = 0) -> dict[str, Any]:
    view = {key: row[key] for key in ("id", "provider", "name", "remote_calendar_id", "last_sync_at", "sync_error", "created_at", "failures", "next_sync_at")}
    credentials = json.loads(row["credentials_json"] or "{}")
    # The server address of a CalDAV account is shown; an ICS address often carries a private token
    # in its path, so only its host is.
    if row["provider"] == "caldav":
        view["server_url"] = credentials.get("server_url", "")
        view["username"] = credentials.get("username", "")
    elif row["provider"] == "ics":
        view["host"] = urlparse(credentials.get("url", "")).hostname or ""
    view["calendar_id"] = calendar_id
    view["status"] = sync_status(row)
    view["conflicts"] = conflicts
    return view


def sync_status(row: Any) -> str:
    if row["id"] in SYNCING:
        return "syncing"
    if row["sync_error"]:
        return "error"
    return "ok" if row["last_sync_at"] else "never"


class CalendarStore:
    def __init__(self, db: Database, zone_hint: Callable[[], str] | None = None) -> None:
        self.db = db
        self.zone_hint = zone_hint
        """Where the operator's browser says they are, used for the calendar's time zone until they
        choose one; without it a fresh install would plan "today" in UTC."""

    # -- settings ------------------------------------------------------------------------------

    async def settings(self) -> dict[str, Any]:
        stored = await self.db.kv_get(SETTINGS_KEY, {}) or {}
        merged = {**DEFAULT_SETTINGS, **{key: value for key, value in stored.items() if key in DEFAULT_SETTINGS}}
        if not merged["timezone"]:
            hint = ""
            if self.zone_hint is not None:
                try:
                    hint = self.zone_hint() or ""
                    recurrence.zone(hint)
                except ValueError:
                    hint = ""
            merged["timezone"] = hint or "UTC"
            if hint:
                # Remembered on first sight, so the agent's tools (which have no browser to ask)
                # plan in the same zone as the app.
                await self.db.kv_set(SETTINGS_KEY, {**stored, "timezone": hint})
        return merged

    async def save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        current = await self.settings()
        merged = {**current, **{key: value for key, value in body.items() if key in DEFAULT_SETTINGS and value is not None}}
        if merged["week_start"] not in (0, 1):
            raise ValueError("the week starts on Sunday (0) or Monday (1)")
        for key in ("work_start", "work_end"):
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(merged[key])):
                raise ValueError("working hours are HH:MM")
        if merged["work_start"] >= merged["work_end"]:
            raise ValueError("the working day must end after it starts")
        if merged["default_view"] not in ("week", "day", "month", "agenda"):
            raise ValueError("the default view is week, day, month or agenda")
        if isinstance(merged["default_duration"], bool) or not isinstance(merged["default_duration"], int) or not 5 <= merged["default_duration"] <= 1440:
            raise ValueError("the default duration is 5 to 1440 minutes")
        merged["default_reminders"] = reminders_of(merged["default_reminders"])
        recurrence.zone(str(merged["timezone"]))
        merged["show_weekends"] = bool(merged["show_weekends"])
        await self.db.kv_set(SETTINGS_KEY, merged)
        return merged

    # -- calendars -----------------------------------------------------------------------------

    async def calendars(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT c.*, a.id AS a_id, a.last_sync_at, a.sync_error, a.next_sync_at, a.failures,"
            " (SELECT count(*) FROM calendar_events e WHERE e.calendar_id = c.id AND e.conflict != '') AS conflicts"
            " FROM calendars c LEFT JOIN calendar_accounts a ON a.id = c.account_id ORDER BY c.position, c.created_at"
        )
        return [self._calendar_view(row) for row in rows]

    @staticmethod
    def _calendar_view(row: Any) -> dict[str, Any]:
        sync = None
        if row["account_id"]:
            sync = {
                "last_sync_at": row["last_sync_at"], "error": row["sync_error"] or None,
                "status": sync_status({"id": row["a_id"], "sync_error": row["sync_error"], "last_sync_at": row["last_sync_at"]}),
                "conflicts": row["conflicts"], "next_sync_at": row["next_sync_at"], "failures": row["failures"],
            }
        return {
            "id": row["id"], "name": row["name"], "color": row["color"], "kind": row["kind"], "account_id": row["account_id"],
            "visible": bool(row["visible"]), "writable": bool(row["writable"]), "position": row["position"],
            "default_reminders": json.loads(row["default_reminders"] or "[]"), "sync": sync,
        }

    async def calendar(self, calendar_id: str) -> dict[str, Any]:
        for found in await self.calendars():
            if found["id"] == calendar_id:
                return found
        raise KeyError(calendar_id)

    async def default_calendar(self) -> Any:
        row = await self.db.fetchone("SELECT * FROM calendars WHERE kind='local' ORDER BY position, created_at LIMIT 1")
        if row is None:
            # The migration makes one and deleting the last is refused; this is a database edited by hand.
            calendar = await self.create_calendar({"name": "Personal", "color": "#4f7cff"})
            row = await self.db.fetchone("SELECT * FROM calendars WHERE id=?", (calendar["id"],))
        return row

    async def create_calendar(self, body: dict[str, Any]) -> dict[str, Any]:
        calendar_id = uuid.uuid4().hex
        settings = await self.settings()
        position = await self.db.fetchone("SELECT COALESCE(MAX(position), -1) + 1 AS next FROM calendars")
        await self.db.execute(
            "INSERT INTO calendars(id,name,color,kind,visible,writable,position,default_reminders,created_at) VALUES(?,?,?,'local',1,1,?,?,?)",
            (calendar_id, _name(body.get("name"), "calendar name"), color_of(body.get("color") or "#4f7cff"), position["next"] if position else 0,
             json.dumps(reminders_of(body["default_reminders"]) if body.get("default_reminders") is not None else settings["default_reminders"]), now()),
        )
        return await self.calendar(calendar_id)

    async def update_calendar(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM calendars WHERE id=?", (calendar_id,))
        if row is None:
            raise KeyError(calendar_id)
        name = _name(body["name"], "calendar name") if body.get("name") is not None else row["name"]
        color = color_of(body["color"]) if body.get("color") is not None else row["color"]
        visible = int(bool(body["visible"])) if body.get("visible") is not None else row["visible"]
        position = int(body["position"]) if body.get("position") is not None else row["position"]
        reminders = json.dumps(reminders_of(body["default_reminders"])) if body.get("default_reminders") is not None else row["default_reminders"]
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE calendars SET name=?,color=?,visible=?,position=?,default_reminders=? WHERE id=?", (name, color, visible, position, reminders, calendar_id))
            if row["account_id"] and name != row["name"]:
                # The connections sheet lists accounts by name; one name for both keeps them recognisable.
                await conn.execute("UPDATE calendar_accounts SET name=? WHERE id=?", (name, row["account_id"]))
        return await self.calendar(calendar_id)

    async def delete_calendar(self, calendar_id: str) -> None:
        row = await self.db.fetchone("SELECT * FROM calendars WHERE id=?", (calendar_id,))
        if row is None:
            raise KeyError(calendar_id)
        if row["kind"] != "local":
            raise ValueError("a connected calendar is removed by disconnecting its account")
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT count(*) FROM calendars WHERE kind='local'")
            count = (await cursor.fetchone())[0]
            if count <= 1:
                raise ValueError("the last local calendar cannot be deleted")
            await conn.execute("DELETE FROM calendar_events WHERE calendar_id=? AND series_id IS NOT NULL", (calendar_id,))
            await conn.execute("DELETE FROM calendar_events WHERE calendar_id=?", (calendar_id,))
            await conn.execute("DELETE FROM calendars WHERE id=?", (calendar_id,))

    async def _calendar_rows(self, ids: Iterable[str] | None) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT * FROM calendars")
        wanted = set(ids) if ids else None
        return {row["id"]: row for row in rows if (row["id"] in wanted if wanted is not None else row["visible"])}

    # -- reading events ------------------------------------------------------------------------

    async def occurrences(self, start: str, end: str, calendar_ids: Iterable[str] | None = None, limit: int = recurrence.MAX_OCCURRENCES) -> list[dict[str, Any]]:
        """Every occurrence overlapping ``[start, end)``, series expanded, in start order.

        Hidden calendars are left out unless named; a series' override is shown where it now is,
        even when that is outside the range its original occurrence fell in.
        """
        first, last = moment(start), moment(end)
        if first >= last:
            raise ValueError("the calendar range needs an end after its start")
        if last - first > timedelta(days=recurrence.MAX_RANGE_DAYS):
            raise ValueError(f"read at most {recurrence.MAX_RANGE_DAYS} days at a time")
        calendars = await self._calendar_rows(calendar_ids)
        if not calendars:
            return []
        marks = ",".join("?" * len(calendars))
        ids = tuple(calendars)
        first_text, last_text = first.isoformat(), last.isoformat()
        singles = await self.db.fetchall(
            f"SELECT * FROM calendar_events WHERE calendar_id IN ({marks}) AND recurrence='' AND series_id IS NULL AND dirty!='delete'"
            " AND start_at < ? AND (end_at > ? OR (end_at = start_at AND start_at >= ?)) ORDER BY start_at LIMIT ?",
            (*ids, last_text, first_text, first_text, limit),
        )
        found = [self._occurrence(row, calendars[row["calendar_id"]], None) for row in singles]
        # A series is read when its span meets the range, or when one of its occurrences was moved
        # into the range from outside that span.
        masters = await self.db.fetchall(
            f"SELECT * FROM calendar_events WHERE calendar_id IN ({marks}) AND recurrence!='' AND series_id IS NULL AND dirty!='delete'"
            " AND ((start_at < ? AND (recurrence_end IS NULL OR recurrence_end > ?))"
            " OR EXISTS (SELECT 1 FROM calendar_events moved WHERE moved.series_id = calendar_events.id AND moved.cancelled = 0 AND moved.start_at < ? AND moved.end_at >= ?))",
            (*ids, last_text, first_text, last_text, first_text),
        )
        if masters:
            by_series: dict[str, list[Any]] = {}
            master_ids = [row["id"] for row in masters]
            for chunk in range(0, len(master_ids), 500):
                part = master_ids[chunk:chunk + 500]
                for row in await self.db.fetchall(f"SELECT * FROM calendar_events WHERE series_id IN ({','.join('?' * len(part))})", part):
                    by_series.setdefault(row["series_id"], []).append(row)
            for master in masters:
                found.extend(self._expand(master, by_series.get(master["id"], []), calendars[master["calendar_id"]], first, last, limit))
        found.sort(key=lambda item: (item["start_at"], item["title"]))
        return found[:limit]

    def _expand(self, master: Any, overrides: list[Any], calendar: Any, first: datetime, last: datetime, limit: int) -> list[dict[str, Any]]:
        skipped = set(json.loads(master["exdates"] or "[]")) | {row["original_start"] for row in overrides}
        found: list[dict[str, Any]] = []
        try:
            spans = recurrence.occurrences(master["recurrence"], master["start_at"], master["end_at"], master["timezone"], bool(master["all_day"]), first, last, limit)
        except (ValueError, TypeError, OverflowError):
            # A rule a provider wrote that dateutil cannot read: the series still shows where it
            # starts rather than vanishing from the calendar.
            start, end = datetime.fromisoformat(master["start_at"]), datetime.fromisoformat(master["end_at"])
            spans = [(start, end)] if start < last and end > first else []
        for start, end in spans:
            original = recurrence.key(start)
            if original in skipped:
                continue
            found.append(self._occurrence(master, calendar, original, start.isoformat(), end.isoformat()))
        for row in overrides:
            if row["cancelled"] or row["dirty"] == "delete":
                continue
            start, end = datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"])
            if start < last and (end > first or (end == start and start >= first)):
                found.append(self._occurrence(row, calendar, row["original_start"], master=master))
        return found

    def _occurrence(self, row: Any, calendar: Any, original: str | None, start_at: str | None = None, end_at: str | None = None, master: Any = None) -> dict[str, Any]:
        series = master if master is not None else row
        event_id = series["id"]
        start_at, end_at = start_at or row["start_at"], end_at or row["end_at"]
        all_day = bool(row["all_day"])
        reminders = row["reminders"] if row["reminders"] is not None else (series["reminders"] if series["reminders"] is not None else calendar["default_reminders"])
        view: dict[str, Any] = {
            "id": event_id if original is None else f"{event_id}:{original}",
            "event_id": event_id,
            "calendar_id": row["calendar_id"],
            "calendar_name": calendar["name"],
            "account_id": row["account_id"],
            "title": row["title"], "description": row["description"], "location": row["location"],
            "start_at": start_at, "end_at": end_at, "all_day": all_day, "timezone": row["timezone"],
            "color": row["color"] or series["color"] or calendar["color"],
            "recurrence": series["recurrence"], "recurring": bool(series["recurrence"]),
            "occurrence_start": original,
            "exception": master is not None,
            "reminders": json.loads(reminders or "[]"),
            "version": row["version"],
            "writable": bool(calendar["writable"]),
            "pending_sync": bool(row["dirty"]) or (master is not None and bool(master["dirty"])),
            "conflict": bool(row["conflict"]) or (master is not None and bool(master["conflict"])),
        }
        if all_day:
            view["start_date"] = start_at[:10]
            view["end_date"] = end_at[:10]
        return view

    async def get(self, event_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id = ? AND dirty != 'delete'", (event_id,))
        if row is None:
            return None
        view = event_view(row)
        if row["recurrence"]:
            overrides = await self.db.fetchall("SELECT * FROM calendar_events WHERE series_id=? ORDER BY original_start", (event_id,))
            view["overrides"] = [event_view(item) for item in overrides]
        return view

    async def search(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        limit = max(1, min(int(limit), 100))
        pattern = f"%{_escape_like(text)}%"
        calendars = {row["id"]: row for row in await self.db.fetchall("SELECT * FROM calendars")}
        rows = await self.db.fetchall(
            "SELECT * FROM calendar_events WHERE dirty!='delete' AND cancelled=0 AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\')"
            " ORDER BY abs(julianday(start_at) - julianday('now')) LIMIT 400",
            (pattern, pattern, pattern),
        )
        current = datetime.now(UTC)
        found: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            calendar = calendars.get(row["calendar_id"])
            if calendar is None:
                continue
            if row["series_id"]:
                master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty!='delete'", (row["series_id"],))
                if master is None:
                    continue
                item = self._occurrence(row, calendar, row["original_start"], master=master)
            elif row["recurrence"]:
                # The occurrence nearest to now stands for the series: the next one, or the last if it ended.
                item = self._nearest(row, calendar, current)
            else:
                item = self._occurrence(row, calendar, None)
            found.append((abs((datetime.fromisoformat(item["start_at"]) - current).total_seconds()), item))
        found.sort(key=lambda pair: pair[0])
        return [item for _, item in found[:limit]]

    def _nearest(self, row: Any, calendar: Any, current: datetime) -> dict[str, Any]:
        try:
            ahead = recurrence.occurrences(row["recurrence"], row["start_at"], row["end_at"], row["timezone"], bool(row["all_day"]), current, current + timedelta(days=recurrence.MAX_RANGE_DAYS), 1)
        except (ValueError, TypeError, OverflowError):
            ahead = []
        if ahead:
            start, end = ahead[0]
            return self._occurrence(row, calendar, recurrence.key(start), start.isoformat(), end.isoformat())
        return self._occurrence(row, calendar, row["start_at"])

    # -- writing events ------------------------------------------------------------------------

    async def _writable_calendar(self, calendar_id: str) -> Any:
        row = await self.db.fetchone("SELECT c.*, a.provider FROM calendars c LEFT JOIN calendar_accounts a ON a.id=c.account_id WHERE c.id=?", (calendar_id,))
        if row is None:
            raise ValueError("no such calendar")
        if not row["writable"]:
            raise ValueError("this calendar is a read-only subscription")
        return row

    def _fields(self, body: dict[str, Any], base: Any, calendar: Any) -> dict[str, Any]:
        """The checked columns of an event from a request over what is stored (``base``, may be None)."""
        def pick(key: str, default: Any) -> Any:
            return body[key] if key in body and body[key] is not None else (base[key] if base is not None else default)

        title = str(pick("title", "")).strip()
        if not title or len(title) > 240:
            raise ValueError("the event title needs 1 to 240 characters")
        all_day = bool(pick("all_day", False))
        if all_day and body.get("start_date") and body.get("end_date"):
            start = str(body["start_date"])[:10] + "T00:00:00+00:00"
            end = str(body["end_date"])[:10] + "T00:00:00+00:00"
            datetime.fromisoformat(start)
            datetime.fromisoformat(end)
        else:
            start_value, end_value = str(pick("start_at", "")), str(pick("end_at", ""))
            start, end = instant(start_value), instant(end_value)
            if all_day:
                # Calendar dates have no UTC offset. Keep the dates the caller named, even when their
                # midnight has an offset that would fall on the previous UTC day.
                start = datetime.fromisoformat(start_value.replace("Z", "+00:00")).date().isoformat() + "T00:00:00+00:00"
                end = datetime.fromisoformat(end_value.replace("Z", "+00:00")).date().isoformat() + "T00:00:00+00:00"
        if end <= start:
            raise ValueError("the event must end after it starts")
        zone = str(pick("timezone", "UTC") or "UTC")
        recurrence.zone(zone)
        rule = recurrence.normalize(str(pick("recurrence", "")))
        if rule and calendar["provider"] == "outlook":
            recurrence.to_graph(rule, recurrence.series_start(start, zone, all_day), zone, all_day)
        reminders: Any
        if "reminders" in body:
            reminders = None if body["reminders"] is None else json.dumps(reminders_of(body["reminders"]))
        else:
            reminders = base["reminders"] if base is not None else None
        return {
            "title": title, "description": str(pick("description", ""))[:10000], "location": str(pick("location", ""))[:500],
            "start_at": start, "end_at": end, "timezone": zone, "all_day": int(all_day), "recurrence": rule,
            "color": color_of(body["color"], allow_empty=True) if "color" in body and body["color"] is not None else (base["color"] if base is not None else ""),
            "reminders": reminders,
            "recurrence_end": recurrence.series_end(rule, start, end, zone, all_day),
        }

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        calendar_id = body.get("calendar_id")
        if not calendar_id and body.get("account_id"):
            found = await self.db.fetchone("SELECT id FROM calendars WHERE account_id=?", (body["account_id"],))
            if found is None:
                raise ValueError("no such connected calendar")
            calendar_id = found["id"]
        if not calendar_id:
            calendar_id = (await self.default_calendar())["id"]
        calendar = await self._writable_calendar(str(calendar_id))
        fields = self._fields(body, None, calendar)
        event_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO calendar_events(id,calendar_id,account_id,title,description,start_at,end_at,timezone,all_day,location,recurrence,color,reminders,recurrence_end,dirty,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, calendar["id"], calendar["account_id"], fields["title"], fields["description"], fields["start_at"], fields["end_at"], fields["timezone"], fields["all_day"],
             fields["location"], fields["recurrence"], fields["color"], fields["reminders"], fields["recurrence_end"], "create" if calendar["account_id"] else "", now()),
        )
        return await self._view_of(event_id)

    async def _view_of(self, event_id: str, original: str | None = None) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (event_id,))
        assert row is not None
        calendar = await self.db.fetchone("SELECT * FROM calendars WHERE id=?", (row["calendar_id"],))
        if row["series_id"]:
            master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (row["series_id"],))
            return self._occurrence(row, calendar, row["original_start"], master=master)
        if row["recurrence"]:
            if original:
                start = datetime.fromisoformat(original)
                length = datetime.fromisoformat(row["end_at"]) - datetime.fromisoformat(row["start_at"])
                return self._occurrence(row, calendar, original, start.isoformat(), (start + length).isoformat())
            return self._occurrence(row, calendar, row["start_at"])
        return self._occurrence(row, calendar, None)

    async def _target(self, event_id: str, occurrence_start: str | None) -> tuple[Any, Any, str | None]:
        """(series or single event, the override row if any, the occurrence key) for an edit.

        The series id with the occurrence's start, the occurrence's id from the events list and an
        override's row id all address the same occurrence, so a client holding any of them can.
        """
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty!='delete'", (event_id,))
        if row is None and ":" in event_id:
            # An occurrence's own id, ``<series>:<original start>``, as the events list gives it.
            event_id, occurrence_start = event_id.split(":", 1)
            row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty!='delete'", (event_id,))
        if row is None:
            raise KeyError(event_id)
        if row["series_id"]:
            master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty!='delete'", (row["series_id"],))
            if master is None:
                raise KeyError(event_id)
            return master, row, row["original_start"]
        if not row["recurrence"] or not occurrence_start:
            return row, None, None
        original = instant(occurrence_start)
        override = await self.db.fetchone("SELECT * FROM calendar_events WHERE series_id=? AND original_start=?", (row["id"], original))
        if override is None and not self._is_occurrence(row, original):
            raise ValueError("that start is not an occurrence of this series")
        return row, override, original

    @staticmethod
    def _is_occurrence(row: Any, original: str) -> bool:
        start = datetime.fromisoformat(original)
        try:
            spans = recurrence.occurrences(row["recurrence"], row["start_at"], row["end_at"], row["timezone"], bool(row["all_day"]), start, start + timedelta(seconds=1), 5)
        except (ValueError, TypeError, OverflowError):
            return original == row["start_at"]
        return any(recurrence.key(begins) == original for begins, _ in spans) and original not in json.loads(row["exdates"] or "[]")

    async def update(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        scope = body.get("scope") or "all"
        if scope not in ("all", "this"):
            raise ValueError("scope is all or this")
        master, override, original = await self._target(event_id, body.get("occurrence_start"))
        if scope == "this" and original is not None:
            return await self._update_occurrence(master, override, original, body)
        if int(body.get("version", -1)) != master["version"]:
            raise RuntimeError("the event changed elsewhere; reload it before saving")
        calendar = await self._writable_calendar(master["calendar_id"])
        target_calendar = calendar
        if body.get("calendar_id") and body["calendar_id"] != master["calendar_id"]:
            target_calendar = await self._writable_calendar(str(body["calendar_id"]))
            if calendar["kind"] != "local" or target_calendar["kind"] != "local":
                raise ValueError("an event cannot move to or from a connected calendar; create it in the other calendar")
        changes = dict(body)
        # Addressed through one occurrence (its override's id, or the series with its start), the new
        # times the client sent are that occurrence's.
        occurrence = body.get("occurrence_start") or original
        if master["recurrence"] and occurrence and any(body.get(key) for key in ("start_at", "end_at", "start_date", "end_date")):
            changes.update(self._series_times(master, occurrence, body))
        fields = self._fields(changes, master, target_calendar)
        account = master["account_id"]
        dirty = ("create" if master["dirty"] == "create" else "update") if account else ""
        reshaped = fields["recurrence"] != master["recurrence"] or fields["start_at"] != master["start_at"] or fields["all_day"] != master["all_day"]
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE calendar_events SET calendar_id=?,title=?,description=?,start_at=?,end_at=?,timezone=?,all_day=?,location=?,recurrence=?,color=?,reminders=?,recurrence_end=?,"
                " dirty=?,version=version+1,updated_at=? WHERE id=? AND version=? AND dirty!='delete'",
                (target_calendar["id"], fields["title"], fields["description"], fields["start_at"], fields["end_at"], fields["timezone"], fields["all_day"], fields["location"],
                 fields["recurrence"], fields["color"], fields["reminders"], fields["recurrence_end"], dirty, now(), master["id"], master["version"]),
            )
            if cursor.rowcount == 0:
                raise RuntimeError("the event changed elsewhere; reload it before saving")
            if not fields["recurrence"]:
                await conn.execute("DELETE FROM calendar_events WHERE series_id=?", (master["id"],))
                await conn.execute("UPDATE calendar_events SET exdates='[]' WHERE id=?", (master["id"],))
            elif reshaped and (not account or calendar["provider"] == "caldav"):
                # A series that moved or changed its rule has different occurrences: the old
                # exceptions would point at starts that no longer exist, and a moved one would show
                # twice. Google and Outlook keep their own exceptions and decide for their copy, which
                # the next read brings; a CalDAV series is sent whole from here, so its exceptions
                # are dropped here too, or the resource would carry RECURRENCE-IDs of no occurrence.
                await conn.execute("DELETE FROM calendar_events WHERE series_id=?", (master["id"],))
                await conn.execute("UPDATE calendar_events SET exdates='[]' WHERE id=?", (master["id"],))
            if target_calendar["id"] != master["calendar_id"]:
                await conn.execute("UPDATE calendar_events SET calendar_id=? WHERE series_id=?", (target_calendar["id"], master["id"]))
        return await self._view_of(master["id"])

    @staticmethod
    def _series_times(master: Any, occurrence: str, body: dict[str, Any]) -> dict[str, Any]:
        """The series' new bounds when the client moved one occurrence with ``scope=all``.

        The client sends that occurrence's new times, possibly only one bound of them; the series
        moves by the same amount and takes the new length. The move is measured on the wall clock of
        the series' zone, so dragging a 10:00 meeting to 11:00 keeps it at 11:00 on both sides of a
        daylight-saving change. An all-day series moves by whole days, whether the client named the
        dates or sent the dates' midnights (the agent's tool can only send the latter).
        """
        zone = recurrence.zone(str(body.get("timezone") or master["timezone"]))
        was_all_day = bool(master["all_day"])
        all_day = bool(body["all_day"]) if body.get("all_day") is not None else was_all_day
        occurrence_at = moment(occurrence)
        master_start = datetime.fromisoformat(master["start_at"])
        length = datetime.fromisoformat(master["end_at"]) - master_start
        if was_all_day:
            occurrence_local = datetime.combine(occurrence_at.date(), time())
            master_local = datetime.combine(master_start.date(), time())
        else:
            occurrence_local = occurrence_at.astimezone(zone).replace(tzinfo=None)
            master_local = master_start.astimezone(zone).replace(tzinfo=None)

        def day_of(date_key: str, time_key: str) -> date | None:
            if body.get(date_key):
                return date.fromisoformat(str(body[date_key])[:10])
            if body.get(time_key):
                # As in _fields: an all-day bound keeps the date the caller wrote, whatever its offset.
                return datetime.fromisoformat(str(body[time_key]).replace("Z", "+00:00")).date()
            return None

        if all_day:
            first_day = day_of("start_date", "start_at")
            last_day = day_of("end_date", "end_at")
            if first_day is None:
                first_day = occurrence_local.date() if last_day is None else last_day - max(timedelta(days=1), timedelta(days=length.days))
            if last_day is None:
                last_day = first_day + max(timedelta(days=1), timedelta(days=length.days))
            begins = master_local.date() + (first_day - occurrence_local.date())
            return {"all_day": True, "start_date": begins.isoformat(), "end_date": (begins + (last_day - first_day)).isoformat()}
        new_start = moment(body["start_at"]) if body.get("start_at") else occurrence_at
        new_end = moment(body["end_at"]) if body.get("end_at") else new_start + length
        begins_local = master_local + (new_start.astimezone(zone).replace(tzinfo=None) - occurrence_local)
        begins = begins_local.replace(tzinfo=zone).astimezone(UTC)
        return {"start_at": begins.isoformat(), "end_at": (begins + (new_end - new_start)).isoformat(), "start_date": None, "end_date": None}

    async def _update_occurrence(self, master: Any, override: Any, original: str, body: dict[str, Any]) -> dict[str, Any]:
        expected = override["version"] if override is not None else master["version"]
        if int(body.get("version", -1)) != expected:
            raise RuntimeError("the event changed elsewhere; reload it before saving")
        calendar = await self._writable_calendar(master["calendar_id"])
        if body.get("calendar_id") and body["calendar_id"] != master["calendar_id"]:
            raise ValueError("one occurrence cannot move to another calendar")
        if override is None:
            length = datetime.fromisoformat(master["end_at"]) - datetime.fromisoformat(master["start_at"])
            base = {**dict(master), "start_at": original, "end_at": (datetime.fromisoformat(original) + length).isoformat(), "recurrence": "", "reminders": master["reminders"]}
        else:
            base = override
        changes = {key: value for key, value in body.items() if key not in ("recurrence", "calendar_id")}
        changes["recurrence"] = ""
        fields = self._fields(changes, base, calendar)
        account = master["account_id"]
        provider = calendar["provider"]
        stamp = now()
        async with self.db.transaction() as conn:
            if override is None:
                override_id = uuid.uuid4().hex
                await conn.execute(
                    "INSERT INTO calendar_events(id,calendar_id,account_id,series_id,original_start,title,description,start_at,end_at,timezone,all_day,location,recurrence,color,reminders,dirty,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,?,?)",
                    (override_id, master["calendar_id"], account, master["id"], original, fields["title"], fields["description"], fields["start_at"], fields["end_at"], fields["timezone"],
                     fields["all_day"], fields["location"], fields["color"], fields["reminders"], "create" if account else "", stamp),
                )
            else:
                override_id = override["id"]
                dirty = ("create" if override["dirty"] == "create" else "update") if account else ""
                cursor = await conn.execute(
                    "UPDATE calendar_events SET title=?,description=?,start_at=?,end_at=?,timezone=?,all_day=?,location=?,color=?,reminders=?,cancelled=0,dirty=?,version=version+1,updated_at=?"
                    " WHERE id=? AND version=?",
                    (fields["title"], fields["description"], fields["start_at"], fields["end_at"], fields["timezone"], fields["all_day"], fields["location"], fields["color"],
                     fields["reminders"], dirty, stamp, override_id, override["version"]),
                )
                if cursor.rowcount == 0:
                    raise RuntimeError("the event changed elsewhere; reload it before saving")
            if provider == "caldav":
                # CalDAV keeps a series and its exceptions in one resource: the series is what is sent.
                await conn.execute("UPDATE calendar_events SET dirty=CASE dirty WHEN 'create' THEN 'create' ELSE 'update' END, updated_at=? WHERE id=?", (stamp, master["id"]))
        return await self._view_of(override_id)

    async def delete(self, event_id: str, version: int | None = None, scope: str = "all", occurrence_start: str | None = None) -> None:
        if scope not in ("all", "this"):
            raise ValueError("scope is all or this")
        master, override, original = await self._target(event_id, occurrence_start)
        if scope == "this" and original is not None:
            await self._delete_occurrence(master, override, original, version)
            return
        if version is not None and version != master["version"]:
            raise RuntimeError("the event changed elsewhere; reload it before deleting")
        await self._writable_calendar(master["calendar_id"])
        async with self.db.transaction() as conn:
            # Only a local calendar's event is removed at once. One of a provider's is marked, even
            # when it was never sent: its create may be in flight right now, and a row removed under
            # it left the provider's new copy with nothing to record its id on — an orphan nobody
            # would delete. Marked, the acknowledgement keeps the id and the next sync deletes it
            # there (or, never sent, simply drops the row).
            if not master["account_id"]:
                cursor = await conn.execute("DELETE FROM calendar_events WHERE id=? AND version=? AND dirty!='delete'", (master["id"], master["version"]))
            else:
                cursor = await conn.execute("UPDATE calendar_events SET dirty='delete',version=version+1,updated_at=? WHERE id=? AND version=? AND dirty!='delete'", (now(), master["id"], master["version"]))
            if cursor.rowcount == 0:
                raise RuntimeError("the event changed elsewhere; reload it before deleting")

    async def _delete_occurrence(self, master: Any, override: Any, original: str, version: int | None) -> None:
        expected = override["version"] if override is not None else master["version"]
        if version is not None and version != expected:
            raise RuntimeError("the event changed elsewhere; reload it before deleting")
        calendar = await self._writable_calendar(master["calendar_id"])
        provider = calendar["provider"]
        stamp = now()
        async with self.db.transaction() as conn:
            if provider in ("google", "outlook"):
                # These providers address an occurrence by its own id: a cancelled override is what
                # the connector turns into that instance's delete, and what a later read confirms.
                if override is None:
                    await conn.execute(
                        "INSERT INTO calendar_events(id,calendar_id,account_id,series_id,original_start,title,start_at,end_at,timezone,all_day,cancelled,dirty,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,1,'create',?)",
                        (uuid.uuid4().hex, master["calendar_id"], master["account_id"], master["id"], original, master["title"], original, original, master["timezone"], master["all_day"], stamp),
                    )
                else:
                    await conn.execute(
                        "UPDATE calendar_events SET cancelled=1, dirty=CASE dirty WHEN 'create' THEN 'create' ELSE 'update' END, version=version+1, updated_at=? WHERE id=?",
                        (stamp, override["id"]),
                    )
                return
            exdates = sorted(set(json.loads(master["exdates"] or "[]")) | {original})
            if override is not None:
                await conn.execute("DELETE FROM calendar_events WHERE id=?", (override["id"],))
            dirty = ("create" if master["dirty"] == "create" else "update") if master["account_id"] else ""
            await conn.execute("UPDATE calendar_events SET exdates=?, dirty=?, version=version+1, updated_at=? WHERE id=?", (json.dumps(exdates), dirty, stamp, master["id"]))

    # -- accounts ------------------------------------------------------------------------------

    async def accounts(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT a.*, c.id AS calendar_id, (SELECT count(*) FROM calendar_events e WHERE e.account_id=a.id AND e.conflict!='') AS conflicts"
            " FROM calendar_accounts a LEFT JOIN calendars c ON c.account_id=a.id ORDER BY a.created_at"
        )
        return [account_view(row, row["calendar_id"], row["conflicts"]) for row in rows]

    async def account(self, account_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM calendar_accounts WHERE id = ?", (account_id,))

    async def connect(self, provider: str, name: str, credentials: dict[str, str], remote_calendar_id: str = "primary", color: str = "") -> dict[str, Any]:
        provider = provider.strip().lower()
        if provider in CALDAV_PRESETS:
            credentials = {**credentials, "server_url": CALDAV_PRESETS[provider], "password": credentials.get("password") or credentials.get("app_password", "")}
            provider = "caldav"
        if provider not in ("google", "outlook", "caldav"):
            raise ValueError("the calendar provider must be google, outlook, caldav, yandex or icloud")
        needed = ("client_id", "client_secret", "refresh_token") if provider != "caldav" else ("server_url", "username", "password")
        if any(not credentials.get(key) for key in needed):
            raise ValueError("missing calendar credentials: " + ", ".join(key for key in needed if not credentials.get(key)))
        if provider == "caldav" and not str(credentials["server_url"]).startswith("https://"):
            raise ValueError("the CalDAV server address must start with https://")
        return await self._add_account(provider, name, {key: str(credentials[key]).strip() for key in needed}, remote_calendar_id.strip() or "primary", color)

    async def subscribe(self, name: str, url: str, color: str = "") -> dict[str, Any]:
        address = url.strip()
        if address.startswith("webcal://"):
            address = "https://" + address.removeprefix("webcal://")
        if not address.startswith("https://") or len(address) > 2000:
            raise ValueError("a calendar subscription needs an https:// (or webcal://) address")
        return await self._add_account("ics", name, {"url": address}, "primary", color)

    async def _add_account(self, provider: str, name: str, credentials: dict[str, str], remote_calendar_id: str, color: str) -> dict[str, Any]:
        title = _name(name, "calendar name")
        account_id = uuid.uuid4().hex
        stamp = now()
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO calendar_accounts(id,provider,name,credentials_json,remote_calendar_id,created_at) VALUES(?,?,?,?,?,?)",
                (account_id, provider, title, json.dumps(credentials), remote_calendar_id, stamp),
            )
            cursor = await conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM calendars")
            position = (await cursor.fetchone())[0]
            await conn.execute(
                "INSERT INTO calendars(id,name,color,kind,account_id,visible,writable,position,default_reminders,created_at) VALUES(?,?,?,?,?,1,?,?,'[]',?)",
                (uuid.uuid4().hex, title, color_of(color) if color else PROVIDER_COLORS[provider], provider, account_id, int(provider != "ics"), position, stamp),
            )
        found = next(item for item in await self.accounts() if item["id"] == account_id)
        return found

    async def disconnect(self, account_id: str) -> None:
        if await self.account(account_id) is None:
            raise KeyError(account_id)
        async with self.db.transaction() as conn:
            # Overrides first: they point at their series, and the series at the account.
            await conn.execute("DELETE FROM calendar_events WHERE account_id=? AND series_id IS NOT NULL", (account_id,))
            await conn.execute("DELETE FROM calendar_events WHERE account_id=?", (account_id,))
            await conn.execute("DELETE FROM calendars WHERE account_id=?", (account_id,))
            await conn.execute("DELETE FROM calendar_accounts WHERE id = ?", (account_id,))

    # -- reminders -----------------------------------------------------------------------------

    async def longest_lead(self) -> int:
        """The longest reminder anyone set, so the reminder loop reads no further ahead than needed."""
        row = await self.db.fetchone(
            "SELECT MAX(lead) AS lead FROM ("
            " SELECT MAX(value) AS lead FROM calendar_events, json_each(COALESCE(calendar_events.reminders, '[]'))"
            " UNION ALL SELECT MAX(value) FROM calendars, json_each(calendars.default_reminders))"
        )
        return int(row["lead"] or 0) if row else 0


def day_anchor(day: str, settings: dict[str, Any]) -> datetime:
    """The moment an all-day item's reminder counts back from: the start of the working day, in the
    operator's zone. Counting from midnight would wake them for "0 minutes before" a holiday."""
    hours, minutes = (int(part) for part in str(settings["work_start"]).split(":"))
    local = datetime.combine(datetime.fromisoformat(day[:10]).date(), time(hours, minutes), recurrence.zone(settings["timezone"]))
    return local.astimezone(UTC)


__all__ = ["CALDAV_PRESETS", "SYNCING", "CalendarStore", "account_view", "color_of", "day_anchor", "event_view", "instant", "moment", "now", "reminders_of"]
