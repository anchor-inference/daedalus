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
import logging
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

import aiosqlite

from daedalus.stores import calendar_blocks as entries
from daedalus.stores import recurrence
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

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


ENTRY_KEYS = ("type", "fields", "blocks", "status", "tags", "group_id", "links")
"""What a structured entry adds to an event. They belong to the event (or the whole series): an
occurrence edited on its own keeps its series' type, blocks and status."""
OPERATOR = "operator"
MAX_BULK = 200
"""Events in one bulk create: a month of five meals a day, with room to spare."""
MAX_HISTORY = 500
"""Journal entries kept per event."""

_LISTENERS: WeakKeyDictionary[Database, list[Callable[[dict[str, Any]], None]]] = WeakKeyDictionary()


def listen(db: Database, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Call ``callback`` after every committed calendar change on ``db``; returns the unsubscriber.

    The store is built wherever it is needed (a route, a tool call, the reminder loop) with nothing
    but the database, so whoever wants to hear of a change (the app's live stream, the hook
    deliverer) registers here once, against the database every store shares. The callback runs
    synchronously right after the commit and must only schedule work.
    """
    _LISTENERS.setdefault(db, []).append(callback)

    def stop() -> None:
        listeners = _LISTENERS.get(db, [])
        if callback in listeners:
            listeners.remove(callback)

    return stop


def announce(db: Database, payload: dict[str, Any]) -> None:
    for callback in list(_LISTENERS.get(db, [])):
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 — a listener's failure must not fail the write that already committed
            logger.exception("a calendar change listener failed")


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


def _loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def entry_view(row: Any, types: dict[str, Any], include_blocks: bool, title: str | None = None) -> dict[str, Any]:
    """What a structured entry adds to an event's view: type, fields, status, blocks and their progress.

    ``field_issues`` names each value that does not fit the type's current schema (a field the
    type no longer has, a value of the wrong sort); the value itself is still in ``fields``.
    Without ``include_blocks`` only ``block_summary`` is given, so a month of events stays light.
    """
    blocks = _loads(row["blocks_json"], [])
    fields = _loads(row["fields_json"], {})
    type_id = row["type_id"]
    typedef = types.get(type_id) if type_id else None
    issues: dict[str, str] = {}
    if typedef is not None:
        try:
            _, issues = entries.check_fields(fields, typedef["fields_schema"])
        except ValueError:
            issues = {}
    display = typedef["display"] if typedef is not None else {}
    found = entries.event_progress(blocks, display.get("badge", ""))
    view: dict[str, Any] = {
        "type": type_id,
        "type_version": row["type_version"],
        "type_outdated": typedef is not None and row["type_version"] is not None and row["type_version"] != typedef["version"],
        "fields": fields,
        "field_issues": issues,
        "status": row["status"],
        "tags": _loads(row["tags_json"], []),
        "group_id": row["group_id"],
        "links": _loads(row["links_json"], []),
        "block_summary": entries.block_summary(blocks),
        "progress": found,
        "chip": entries.chip(display.get("chip", ""), title if title is not None else row["title"], fields, row["status"], found),
    }
    if include_blocks:
        view["blocks"] = blocks
    return view


def _spoken(column: str) -> str:
    """A column as the history names it: the API's word for it, not the table's."""
    return {"type_id": "type", "fields_json": "fields", "blocks_json": "blocks", "tags_json": "tags", "links_json": "links", "type_version": "type"}.get(column, column)


def type_view(row: Any) -> dict[str, Any]:
    definition = _loads(row["definition_json"], {})
    return {"id": row["id"], **definition, "name": row["name"], "version": row["version"], "owner": row["owner"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


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

    async def occurrences(
        self, start: str, end: str, calendar_ids: Iterable[str] | None = None, limit: int = recurrence.MAX_OCCURRENCES, *,
        filters: dict[str, Any] | None = None, include_blocks: bool = False, include_hidden_groups: bool = False,
    ) -> list[dict[str, Any]]:
        """Every occurrence overlapping ``[start, end)``, series expanded, in start order.

        Hidden calendars are left out unless named, and so are the events of hidden groups unless
        ``filters`` names the group or ``include_hidden_groups`` asks for them (the reminder loop:
        hiding a plan declutters the view, it does not silence its reminders). ``filters`` narrows
        by ``type``, ``group_id``, ``tag`` and ``status``, which an occurrence takes from its series.
        A series' override is shown where it now is, even when that is outside the range its
        original occurrence fell in.
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
        narrow, narrow_params = self._narrowing(filters or {}, include_hidden_groups)
        types = await self.types()
        singles = await self.db.fetchall(
            f"SELECT * FROM calendar_events WHERE calendar_id IN ({marks}) AND recurrence='' AND series_id IS NULL AND dirty!='delete'"
            f" AND start_at < ? AND (end_at > ? OR (end_at = start_at AND start_at >= ?)){narrow} ORDER BY start_at LIMIT ?",
            (*ids, last_text, first_text, first_text, *narrow_params, limit),
        )
        found = [self._occurrence(row, calendars[row["calendar_id"]], None, types=types, include_blocks=include_blocks) for row in singles]
        # A series is read when its span meets the range, or when one of its occurrences was moved
        # into the range from outside that span.
        masters = await self.db.fetchall(
            f"SELECT * FROM calendar_events WHERE calendar_id IN ({marks}) AND recurrence!='' AND series_id IS NULL AND dirty!='delete'"
            " AND ((start_at < ? AND (recurrence_end IS NULL OR recurrence_end > ?))"
            f" OR EXISTS (SELECT 1 FROM calendar_events moved WHERE moved.series_id = calendar_events.id AND moved.cancelled = 0 AND moved.start_at < ? AND moved.end_at >= ?)){narrow}",
            (*ids, last_text, first_text, last_text, first_text, *narrow_params),
        )
        if masters:
            by_series: dict[str, list[Any]] = {}
            master_ids = [row["id"] for row in masters]
            for chunk in range(0, len(master_ids), 500):
                part = master_ids[chunk:chunk + 500]
                for row in await self.db.fetchall(f"SELECT * FROM calendar_events WHERE series_id IN ({','.join('?' * len(part))})", part):
                    by_series.setdefault(row["series_id"], []).append(row)
            for master in masters:
                found.extend(self._expand(master, by_series.get(master["id"], []), calendars[master["calendar_id"]], first, last, limit, types, include_blocks))
        found.sort(key=lambda item: (item["start_at"], item["title"]))
        return found[:limit]

    @staticmethod
    def _narrowing(filters: dict[str, Any], include_hidden_groups: bool) -> tuple[str, list[Any]]:
        """The SQL that narrows an events query by the entry filters, and its parameters."""
        sql, params = "", []
        if filters.get("type"):
            sql += " AND type_id = ?"
            params.append(filters["type"])
        if filters.get("group_id"):
            sql += " AND group_id = ?"
            params.append(filters["group_id"])
        elif not include_hidden_groups:
            sql += " AND (group_id IS NULL OR group_id NOT IN (SELECT id FROM calendar_groups WHERE hidden = 1))"
        if filters.get("status"):
            sql += " AND status = ?"
            params.append(filters["status"])
        if filters.get("tag"):
            sql += " AND EXISTS (SELECT 1 FROM json_each(calendar_events.tags_json) WHERE json_each.value = ?)"
            params.append(filters["tag"])
        return sql, params

    def _expand(self, master: Any, overrides: list[Any], calendar: Any, first: datetime, last: datetime, limit: int, types: dict[str, Any] | None = None, include_blocks: bool = False) -> list[dict[str, Any]]:
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
            found.append(self._occurrence(master, calendar, original, start.isoformat(), end.isoformat(), types=types, include_blocks=include_blocks))
        for row in overrides:
            if row["cancelled"] or row["dirty"] == "delete":
                continue
            start, end = datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"])
            if start < last and (end > first or (end == start and start >= first)):
                found.append(self._occurrence(row, calendar, row["original_start"], master=master, types=types, include_blocks=include_blocks))
        return found

    def _occurrence(
        self, row: Any, calendar: Any, original: str | None, start_at: str | None = None, end_at: str | None = None, master: Any = None,
        *, types: dict[str, Any] | None = None, include_blocks: bool = False,
    ) -> dict[str, Any]:
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
        view.update(entry_view(series, types or {}, include_blocks, title=row["title"]))
        return view

    async def get(self, event_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id = ? AND dirty != 'delete'", (event_id,))
        if row is None:
            return None
        view = event_view(row)
        view.update(entry_view(row, await self.types(), True))
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
        types = await self.types()
        found: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            calendar = calendars.get(row["calendar_id"])
            if calendar is None:
                continue
            if row["series_id"]:
                master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty!='delete'", (row["series_id"],))
                if master is None:
                    continue
                item = self._occurrence(row, calendar, row["original_start"], master=master, types=types)
            elif row["recurrence"]:
                # The occurrence nearest to now stands for the series: the next one, or the last if it ended.
                item = self._nearest(row, calendar, current, types)
            else:
                item = self._occurrence(row, calendar, None, types=types)
            found.append((abs((datetime.fromisoformat(item["start_at"]) - current).total_seconds()), item))
        found.sort(key=lambda pair: pair[0])
        return [item for _, item in found[:limit]]

    def _nearest(self, row: Any, calendar: Any, current: datetime, types: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            ahead = recurrence.occurrences(row["recurrence"], row["start_at"], row["end_at"], row["timezone"], bool(row["all_day"]), current, current + timedelta(days=recurrence.MAX_RANGE_DAYS), 1)
        except (ValueError, TypeError, OverflowError):
            ahead = []
        if ahead:
            start, end = ahead[0]
            return self._occurrence(row, calendar, recurrence.key(start), start.isoformat(), end.isoformat(), types=types)
        return self._occurrence(row, calendar, row["start_at"], types=types)

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

    async def _prepare(self, body: dict[str, Any], types: dict[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        """(calendar, checked columns, entry columns) for a new event, everything validated."""
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
        entry = await self._entry_columns(body, None, types)
        return calendar, fields, entry

    async def _insert(self, conn: aiosqlite.Connection, calendar: Any, fields: dict[str, Any], entry: dict[str, Any], actor: str, event_id: str | None = None) -> str:
        event_id = event_id or uuid.uuid4().hex
        stamp = now()
        columns = {
            "id": event_id, "calendar_id": calendar["id"], "account_id": calendar["account_id"],
            **{key: fields[key] for key in ("title", "description", "start_at", "end_at", "timezone", "all_day", "location", "recurrence", "color", "reminders", "recurrence_end")},
            "dirty": "create" if calendar["account_id"] else "", "updated_at": stamp, **entry,
        }
        await conn.execute(f"INSERT INTO calendar_events({','.join(columns)}) VALUES({','.join('?' * len(columns))})", tuple(columns.values()))
        await self._journal(conn, event_id, actor, [{"action": "created", "summary": fields["title"]}])
        return event_id

    async def create(self, body: dict[str, Any], actor: str = OPERATOR) -> dict[str, Any]:
        types = await self.types()
        calendar, fields, entry = await self._prepare(body, types)
        async with self.db.transaction() as conn:
            event_id = await self._insert(conn, calendar, fields, entry, actor)
            await self._settle_groups(conn, [entry.get("group_id")])
        announce(self.db, {"change": "created", "event_id": event_id})
        return await self._view_of(event_id, types=types)

    async def bulk_create(self, items: list[dict[str, Any]], actor: str = OPERATOR) -> list[dict[str, Any]]:
        """Create every event or none: all are checked before the first is written, in one transaction."""
        if not isinstance(items, list) or not items:
            raise ValueError("events must be a non-empty list")
        if len(items) > MAX_BULK:
            raise ValueError(f"create at most {MAX_BULK} events at a time")
        types = await self.types()
        prepared = []
        for number, body in enumerate(items, start=1):
            if not isinstance(body, dict):
                raise ValueError(f"event {number} is not an object")
            try:
                prepared.append(await self._prepare(body, types))
            except ValueError as exc:
                raise ValueError(f"event {number} ({str(body.get('title') or '')[:60]}): {exc}") from exc
        made: list[str] = []
        async with self.db.transaction() as conn:
            for calendar, fields, entry in prepared:
                made.append(await self._insert(conn, calendar, fields, entry, actor))
            await self._settle_groups(conn, [entry.get("group_id") for _, _, entry in prepared])
        announce(self.db, {"change": "created", "event_ids": made})
        return [await self._view_of(event_id, types=types) for event_id in made]

    async def _entry_columns(self, body: dict[str, Any], base: Any, types: dict[str, Any]) -> dict[str, Any]:
        """The entry columns a request sets, checked (all of them, with defaults, for a new event).

        A key the request leaves out keeps its stored value; ``type`` or ``group_id`` sent as null
        clears it. Giving a type to a new event without blocks starts it from the type's default
        blocks.
        """
        creating = base is None
        columns: dict[str, Any] = {}
        type_id = None if creating else base["type_id"]
        if "type" in body:
            type_id = body["type"] or None
            if type_id is not None and type_id not in types:
                raise ValueError(f"no calendar type {type_id!r}; CalendarTypeList shows the types")
        typedef = types.get(type_id) if type_id else None
        if creating or "type" in body:
            columns["type_id"] = type_id
            columns["type_version"] = typedef["version"] if typedef else None
        if creating or body.get("fields") is not None or "type" in body:
            raw = body.get("fields") if body.get("fields") is not None else ({} if creating else _loads(base["fields_json"], {}))
            stored, _ = entries.check_fields(raw, typedef["fields_schema"] if typedef else None)
            columns["fields_json"] = json.dumps(stored, ensure_ascii=False)
            if typedef:
                columns["type_version"] = typedef["version"]
        if body.get("blocks") is not None:
            columns["blocks_json"] = json.dumps(entries.normalize_blocks(body["blocks"]), ensure_ascii=False)
        elif creating:
            columns["blocks_json"] = json.dumps(entries.instantiate(typedef["default_blocks"]) if typedef else [], ensure_ascii=False)
        if body.get("status") is not None or creating:
            columns["status"] = entries.check_status(body.get("status") or "planned")
        if body.get("tags") is not None or creating:
            columns["tags_json"] = json.dumps(entries.check_tags(body.get("tags")), ensure_ascii=False)
        if "group_id" in body or creating:
            group_id = body.get("group_id") or None
            if group_id is not None and await self.db.fetchone("SELECT 1 FROM calendar_groups WHERE id=?", (group_id,)) is None:
                raise ValueError(f"no calendar group {group_id!r}")
            columns["group_id"] = group_id
        if body.get("links") is not None or creating:
            columns["links_json"] = json.dumps(entries.check_links(body.get("links")), ensure_ascii=False)
        return columns

    async def _view_of(self, event_id: str, original: str | None = None, types: dict[str, Any] | None = None) -> dict[str, Any]:
        """The event as the list shows it, with its blocks: what a write hands back to its caller."""
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (event_id,))
        assert row is not None
        calendar = await self.db.fetchone("SELECT * FROM calendars WHERE id=?", (row["calendar_id"],))
        types = types if types is not None else await self.types()
        if row["series_id"]:
            master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (row["series_id"],))
            return self._occurrence(row, calendar, row["original_start"], master=master, types=types, include_blocks=True)
        if row["recurrence"]:
            if original:
                start = datetime.fromisoformat(original)
                length = datetime.fromisoformat(row["end_at"]) - datetime.fromisoformat(row["start_at"])
                return self._occurrence(row, calendar, original, start.isoformat(), (start + length).isoformat(), types=types, include_blocks=True)
            return self._occurrence(row, calendar, row["start_at"], types=types, include_blocks=True)
        return self._occurrence(row, calendar, None, types=types, include_blocks=True)

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

    async def update(self, event_id: str, body: dict[str, Any], actor: str = OPERATOR) -> dict[str, Any]:
        scope = body.get("scope") or "all"
        if scope not in ("all", "this"):
            raise ValueError("scope is all or this")
        master, override, original = await self._target(event_id, body.get("occurrence_start"))
        if scope == "this" and original is not None:
            # The entry belongs to the series. The app's editor sends every field on each save, so
            # they are set aside here rather than refused, or no single occurrence could be edited.
            result = await self._update_occurrence(master, override, original, {key: value for key, value in body.items() if key not in ENTRY_KEYS})
            announce(self.db, {"change": "updated", "event_id": master["id"]})
            return result
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
        types = await self.types()
        entry = await self._entry_columns(body, master, types)
        account = master["account_id"]
        dirty = ("create" if master["dirty"] == "create" else "update") if account else ""
        reshaped = fields["recurrence"] != master["recurrence"] or fields["start_at"] != master["start_at"] or fields["all_day"] != master["all_day"]
        changed = [key for key, value in fields.items() if key != "recurrence_end" and master[key] != value] + [key for key, value in entry.items() if master[key] != value]
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
            if entry:
                await conn.execute(f"UPDATE calendar_events SET {','.join(key + '=?' for key in entry)} WHERE id=?", (*entry.values(), master["id"]))
            await self._journal(conn, master["id"], actor, [{"action": "updated", "summary": "changed " + ", ".join(dict.fromkeys(_spoken(key) for key in changed)) if changed else "saved without changes"}])
            await self._transitions(conn, master, {**dict(master), **entry}, actor)
        announce(self.db, {"change": "updated", "event_id": master["id"]})
        return await self._view_of(master["id"], types=types)

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
            await self._remove(conn, master)
            await self._settle_groups(conn, [master["group_id"]])
        announce(self.db, {"change": "deleted", "event_id": master["id"]})

    @staticmethod
    async def _remove(conn: aiosqlite.Connection, master: Any) -> None:
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

    # -- structured entries: journal, transitions ----------------------------------------------

    @staticmethod
    async def _journal(conn: aiosqlite.Connection, event_id: str, actor: str, items: list[dict[str, Any]]) -> None:
        stamp = now()
        for item in items:
            await conn.execute(
                "INSERT INTO calendar_event_history(event_id, at, actor, action, block_id, item_id, summary) VALUES (?,?,?,?,?,?,?)",
                (event_id, stamp, actor, item["action"], item.get("block_id"), item.get("item_id"), str(item.get("summary") or "")[:300]),
            )
        # The journal answers "who ticked this, and when"; an event ticked for years keeps its
        # latest entries rather than every one.
        await conn.execute(
            "DELETE FROM calendar_event_history WHERE event_id=? AND id < (SELECT id FROM calendar_event_history WHERE event_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (event_id, event_id, MAX_HISTORY - 1),
        )

    @staticmethod
    async def _hook(conn: aiosqlite.Connection, kind: str, event_id: str | None, group_id: str | None, detail: dict[str, Any]) -> None:
        await conn.execute(
            "INSERT INTO calendar_hooks(kind, event_id, group_id, detail_json, at) VALUES (?,?,?,?,?)",
            (kind, event_id, group_id, json.dumps(detail, ensure_ascii=False), now()),
        )

    async def _transitions(self, conn: aiosqlite.Connection, before: Any, after: dict[str, Any], actor: str) -> None:
        """Record the hooks a change sets off, comparing the row before it with the row after it.

        Written in the change's own transaction, so a transition is in the outbox exactly when the
        change is in the table: a crash cannot lose one or repeat it.
        """
        for block in entries.completed_blocks(_loads(before["blocks_json"], []), _loads(after["blocks_json"], [])):
            done, total = entries.progress(block) or (0, 0)
            await self._hook(conn, "checklist.completed", before["id"], after["group_id"], {
                "title": after["title"], "block_id": block["id"], "block_title": block.get("title") or "", "block_kind": block["kind"], "done": done, "total": total, "actor": actor,
            })
        if before["status"] != after["status"]:
            await self._hook(conn, "event.status_changed", before["id"], after["group_id"], {"title": after["title"], "from": before["status"], "to": after["status"], "actor": actor})
        await self._settle_groups(conn, [before["group_id"], after["group_id"]])

    async def _settle_groups(self, conn: aiosqlite.Connection, group_ids: Iterable[str | None]) -> None:
        """Mark each group finished or not after a change to its events, firing ``group.completed``
        on the change from unfinished to finished only. Finished is: at least one event counts, and
        every event that counts (all but the cancelled) is done or skipped."""
        for group_id in sorted({item for item in group_ids if item}):
            group = await (await conn.execute("SELECT name, completed_at FROM calendar_groups WHERE id=?", (group_id,))).fetchone()
            if group is None:
                continue
            counts = await (await conn.execute(
                "SELECT count(*) FILTER (WHERE status IN ('done','skipped')) AS done, count(*) FILTER (WHERE status != 'cancelled') AS total"
                " FROM calendar_events WHERE group_id=? AND series_id IS NULL AND dirty!='delete'", (group_id,),
            )).fetchone()
            finished = counts["total"] > 0 and counts["done"] == counts["total"]
            if finished and not group["completed_at"]:
                await conn.execute("UPDATE calendar_groups SET completed_at=? WHERE id=?", (now(), group_id))
                await self._hook(conn, "group.completed", None, group_id, {"name": group["name"], "events": counts["total"]})
            elif not finished and group["completed_at"]:
                await conn.execute("UPDATE calendar_groups SET completed_at=NULL WHERE id=?", (group_id,))

    @staticmethod
    def _dirty(row: Any) -> str:
        return ("create" if row["dirty"] == "create" else "update") if row["account_id"] else ""

    async def _locked_row(self, conn: aiosqlite.Connection, event_id: str) -> Any:
        row = await (await conn.execute("SELECT * FROM calendar_events WHERE id=? AND dirty!='delete'", (event_id,))).fetchone()
        if row is None:
            raise KeyError(event_id)
        return row

    # -- structured entries: blocks, status, history -------------------------------------------

    async def patch_blocks(self, event_id: str, ops: Any, version: int | None = None, actor: str = OPERATOR) -> dict[str, Any]:
        """Apply block ops to an event (a series, for an occurrence), all or nothing.

        A patch of ticks alone ignores ``version``: the row is read and written under one lock, so
        two ticks on different items both land, and two on the same item end as the later one. Any
        other op honours ``version`` when it is given.
        """
        master, _, _ = await self._target(event_id, None)
        await self._writable_calendar(master["calendar_id"])
        versionless = isinstance(ops, list) and bool(ops) and all(isinstance(op, dict) and op.get("op") in entries.VERSIONLESS_OPS for op in ops)
        async with self.db.transaction() as conn:
            row = await self._locked_row(conn, master["id"])
            if version is not None and not versionless and int(version) != row["version"]:
                raise RuntimeError("the event changed elsewhere; reload it before saving")
            blocks, journal = entries.apply_ops(_loads(row["blocks_json"], []), ops)
            text = json.dumps(blocks, ensure_ascii=False)
            await conn.execute("UPDATE calendar_events SET blocks_json=?, dirty=?, version=version+1, updated_at=? WHERE id=?", (text, self._dirty(row), now(), row["id"]))
            await self._journal(conn, row["id"], actor, journal)
            await self._transitions(conn, row, {**dict(row), "blocks_json": text}, actor)
        announce(self.db, {"change": "blocks", "event_id": row["id"]})
        return await self._view_of(row["id"])

    async def set_status(self, event_id: str, status: str, actor: str = OPERATOR) -> dict[str, Any]:
        """Set an event's status without its version: like a tick, it is one value, last write wins."""
        status = entries.check_status(status)
        master, _, _ = await self._target(event_id, None)
        await self._writable_calendar(master["calendar_id"])
        async with self.db.transaction() as conn:
            row = await self._locked_row(conn, master["id"])
            if row["status"] != status:
                await conn.execute("UPDATE calendar_events SET status=?, version=version+1, updated_at=? WHERE id=?", (status, now(), row["id"]))
                await self._journal(conn, row["id"], actor, [{"action": "status", "summary": f"{row['status']} → {status}"}])
                await self._transitions(conn, row, {**dict(row), "status": status}, actor)
        announce(self.db, {"change": "status", "event_id": row["id"]})
        return await self._view_of(row["id"])

    async def history(self, event_id: str, limit: int = 200) -> list[dict[str, Any]]:
        master, _, _ = await self._target(event_id, None)
        rows = await self.db.fetchall("SELECT * FROM calendar_event_history WHERE event_id=? ORDER BY id DESC LIMIT ?", (master["id"], max(1, min(int(limit), MAX_HISTORY))))
        found = []
        for row in rows:
            entry = {"at": row["at"], "actor": row["actor"], "action": row["action"], "summary": row["summary"]}
            if row["block_id"]:
                entry["block_id"] = row["block_id"]
            if row["item_id"]:
                entry["item_id"] = row["item_id"]
            found.append(entry)
        return found

    async def action(self, event_id: str, block_id: str, actor: str = OPERATOR) -> tuple[dict[str, Any], str]:
        """An action block and the context its press hands the agent: (block, message text).

        The press is journalled here; the caller delivers the message. The context is the event as
        a reader of an exported copy would see it, clipped, so the session learns what the operator
        was looking at without the whole JSON.
        """
        master, _, _ = await self._target(event_id, None)
        blocks = _loads(master["blocks_json"], [])
        block = next((item for item in blocks if item.get("id") == block_id), None)
        if block is None:
            raise KeyError(block_id)
        if block.get("kind") != "action" or block.get("action") != "agent":
            raise ValueError("only an action block whose action is agent is run here; a url action opens in the app")
        body = entries.compose(master["description"], [item for item in blocks if item.get("kind") != "action"], _loads(master["fields_json"], {}))
        text = (
            f"[calendar action pressed by the operator: {block.get('label')!r}]\n{block.get('prompt')}\n\n"
            f"Event {master['title']!r} (event_id {master['id']}, block_id {block_id}), starts {master['start_at']}, status {master['status']}"
            + (f", group {master['group_id']}" if master["group_id"] else "") + ".\n" + body[:4000]
        )
        async with self.db.transaction() as conn:
            await self._journal(conn, master["id"], actor, [{"action": "run_action", "block_id": block_id, "summary": f"ran {block.get('label')}"}])
        return block, text

    # -- structured entries: types --------------------------------------------------------------

    async def types(self) -> dict[str, dict[str, Any]]:
        return {row["id"]: type_view(row) for row in await self.db.fetchall("SELECT * FROM calendar_types")}

    async def type_list(self) -> list[dict[str, Any]]:
        return sorted((await self.types()).values(), key=lambda item: (item["name"].lower(), item["id"]))

    async def upsert_type(self, type_id: str, body: dict[str, Any], owner: str = "user") -> dict[str, Any]:
        """Create or replace a type. A definition that differs from the stored one raises its version;
        the same definition sent again changes nothing, so an agent repeating itself is harmless."""
        definition = entries.check_type(body, type_id)
        text = json.dumps(definition, ensure_ascii=False)
        stamp = now()
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT * FROM calendar_types WHERE id=?", (type_id,))).fetchone()
            if row is None:
                count = (await (await conn.execute("SELECT count(*) FROM calendar_types")).fetchone())[0]
                if count >= entries.MAX_TYPES:
                    raise ValueError(f"there are already {entries.MAX_TYPES} calendar types; delete one first")
                await conn.execute(
                    "INSERT INTO calendar_types(id, name, version, owner, definition_json, created_at, updated_at) VALUES (?,?,1,?,?,?,?)",
                    (type_id, definition["name"], owner, text, stamp, stamp),
                )
            elif row["definition_json"] != text:
                await conn.execute("UPDATE calendar_types SET name=?, definition_json=?, version=version+1, updated_at=? WHERE id=?", (definition["name"], text, stamp, type_id))
        announce(self.db, {"change": "type", "type_id": type_id})
        found = await self.db.fetchone("SELECT * FROM calendar_types WHERE id=?", (type_id,))
        return type_view(found)

    async def delete_type(self, type_id: str) -> None:
        """Remove a type; its events become ordinary ones and keep their fields and blocks."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM calendar_types WHERE id=?", (type_id,))
            if cursor.rowcount == 0:
                raise KeyError(type_id)
            await conn.execute("UPDATE calendar_events SET type_id=NULL, type_version=NULL WHERE type_id=?", (type_id,))
        announce(self.db, {"change": "type", "type_id": type_id})

    async def aggregates(self, start: str, end: str, type_id: str | None = None, group_id: str | None = None) -> dict[str, Any]:
        """The types' ``aggregates`` over a range: per day (and per week), in the operator's zone.

        Cancelled and skipped events do not count: a skipped dinner was not eaten.
        """
        settings = await self.settings()
        zone = recurrence.zone(settings["timezone"])
        types = await self.types()

        def bound(value: str) -> str:
            # The app asks by local dates, the end exclusive like the events range: a date is that
            # day's midnight in the operator's zone, not in UTC, or an evening meal lands on the next day.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
                return datetime.combine(date.fromisoformat(value.strip()), time(), zone).isoformat()
            return value

        found = await self.occurrences(bound(start), bound(end), None, filters={"type": type_id, "group_id": group_id})
        buckets: dict[tuple[str, str, str, int], list[Any]] = {}
        for item in found:
            typedef = types.get(item["type"]) if item["type"] else None
            if typedef is None or not typedef["aggregates"] or item["status"] in ("cancelled", "skipped"):
                continue
            day = date.fromisoformat(item["start_date"]) if item["all_day"] else datetime.fromisoformat(item["start_at"]).astimezone(zone).date()
            week = day - timedelta(days=day.weekday() if settings["week_start"] == 1 else (day.weekday() + 1) % 7)
            for index, spec in enumerate(typedef["aggregates"]):
                key = (day if spec["per"] == "day" else week).isoformat()
                buckets.setdefault((spec["per"], key, item["type"], index), []).append(item["fields"].get(spec["field"]) if spec["field"] else None)
        days: dict[str, list[dict[str, Any]]] = {}
        weeks: dict[str, list[dict[str, Any]]] = {}
        for (per, key, found_type, index), values in sorted(buckets.items()):
            spec = types[found_type]["aggregates"][index]
            numbers = [number for number in (entries.amount(value) for value in values) if number is not None]
            value: Any
            if spec["op"] == "count":
                value = len(values)
            elif not numbers:
                continue
            elif spec["op"] == "sum":
                value = sum(numbers)
            elif spec["op"] == "avg":
                value = round(sum(numbers) / len(numbers), 2)
            else:
                value = min(numbers) if spec["op"] == "min" else max(numbers)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            entry: dict[str, Any] = {"type": found_type, "field": spec["field"], "op": spec["op"], "label": spec["label"], "value": value}
            if spec["op"] != "count":
                entry["unit"] = types[found_type]["fields_schema"].get(spec["field"], {}).get("unit", "")
            if spec.get("target"):
                low, high = spec["target"]
                entry["target"] = spec["target"]
                entry["ok"] = (low is None or value >= low) and (high is None or value <= high)
            (days if per == "day" else weeks).setdefault(key, []).append(entry)
        return {"days": [{"date": key, "items": items} for key, items in sorted(days.items())], "weeks": [{"start": key, "items": items} for key, items in sorted(weeks.items())]}

    # -- structured entries: groups -------------------------------------------------------------

    _GROUP_STATS = (
        "SELECT g.*,"
        " (SELECT count(*) FROM calendar_events e WHERE e.group_id=g.id AND e.series_id IS NULL AND e.dirty!='delete') AS event_count,"
        " (SELECT count(*) FROM calendar_events e WHERE e.group_id=g.id AND e.series_id IS NULL AND e.dirty!='delete' AND e.status IN ('done','skipped')) AS done,"
        " (SELECT count(*) FROM calendar_events e WHERE e.group_id=g.id AND e.series_id IS NULL AND e.dirty!='delete' AND e.status!='cancelled') AS total,"
        " (SELECT min(e.start_at) FROM calendar_events e WHERE e.group_id=g.id AND e.series_id IS NULL AND e.dirty!='delete') AS first,"
        " (SELECT max(CASE WHEN e.recurrence!='' THEN COALESCE(e.recurrence_end, e.end_at) ELSE e.end_at END) FROM calendar_events e"
        " WHERE e.group_id=g.id AND e.series_id IS NULL AND e.dirty!='delete') AS last"
        " FROM calendar_groups g"
    )

    @staticmethod
    def _group_view(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "icon": row["icon"], "color": row["color"], "description": row["description"],
            "blocks": _loads(row["blocks_json"], []), "created_by": row["created_by"], "archived": bool(row["archived"]), "hidden": bool(row["hidden"]),
            "completed_at": row["completed_at"], "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            "event_count": row["event_count"], "progress": {"done": row["done"], "total": row["total"]},
            "range": {"from": row["first"], "to": row["last"]} if row["first"] else None,
        }

    async def groups(self) -> list[dict[str, Any]]:
        return [self._group_view(row) for row in await self.db.fetchall(self._GROUP_STATS + " ORDER BY g.archived, g.created_at DESC")]

    async def group(self, group_id: str) -> dict[str, Any]:
        row = await self.db.fetchone(self._GROUP_STATS + " WHERE g.id=?", (group_id,))
        if row is None:
            raise KeyError(group_id)
        return self._group_view(row)

    @staticmethod
    def _group_columns(body: dict[str, Any], creating: bool) -> dict[str, Any]:
        columns: dict[str, Any] = {}
        if creating or body.get("name") is not None:
            columns["name"] = _name(body.get("name"), "group name", 120)
        for key, limit in (("icon", 40), ("description", 20000)):
            if body.get(key) is not None or creating:
                value = body.get(key) or ""
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError(f"a group's {key} is text of at most {limit} characters")
                columns[key] = value
        if body.get("color") is not None or creating:
            columns["color"] = color_of(body.get("color"), allow_empty=True)
        if body.get("blocks") is not None or creating:
            columns["blocks_json"] = json.dumps(entries.normalize_blocks(body.get("blocks") or []), ensure_ascii=False)
        for key in ("archived", "hidden"):
            if body.get(key) is not None:
                columns[key] = int(bool(body[key]))
        return columns

    async def create_group(self, body: dict[str, Any], actor: str = OPERATOR) -> dict[str, Any]:
        columns = self._group_columns(body, True)
        group_id = entries.new_id("grp")
        stamp = now()
        columns.update({"id": group_id, "created_by": actor, "created_at": stamp, "updated_at": stamp})
        await self.db.execute(f"INSERT INTO calendar_groups({','.join(columns)}) VALUES({','.join('?' * len(columns))})", tuple(columns.values()))
        announce(self.db, {"change": "group", "group_id": group_id})
        return await self.group(group_id)

    async def update_group(self, group_id: str, body: dict[str, Any]) -> dict[str, Any]:
        columns = self._group_columns(body, False)
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT version FROM calendar_groups WHERE id=?", (group_id,))).fetchone()
            if row is None:
                raise KeyError(group_id)
            if body.get("version") is not None and int(body["version"]) != row["version"]:
                raise RuntimeError("the group changed elsewhere; reload it before saving")
            if columns:
                await conn.execute(
                    f"UPDATE calendar_groups SET {','.join(key + '=?' for key in columns)}, version=version+1, updated_at=? WHERE id=?",
                    (*columns.values(), now(), group_id),
                )
        announce(self.db, {"change": "group", "group_id": group_id})
        return await self.group(group_id)

    async def _group_events(self, conn: aiosqlite.Connection, group_id: str) -> list[Any]:
        group = await (await conn.execute("SELECT * FROM calendar_groups WHERE id=?", (group_id,))).fetchone()
        if group is None:
            raise KeyError(group_id)
        rows = await (await conn.execute("SELECT * FROM calendar_events WHERE group_id=? AND series_id IS NULL AND dirty!='delete' ORDER BY start_at", (group_id,))).fetchall()
        for row in rows:
            calendar = await (await conn.execute("SELECT writable FROM calendars WHERE id=?", (row["calendar_id"],))).fetchone()
            if calendar is None or not calendar["writable"]:
                raise ValueError(f"event {row['title']!r} is in a read-only calendar")
        return list(rows)

    async def delete_group(self, group_id: str, events: str = "keep", actor: str = OPERATOR) -> None:
        """Remove a group; ``events='keep'`` lets its events go on as ordinary ones, ``delete`` removes them."""
        if events not in ("keep", "delete"):
            raise ValueError("events is keep or delete")
        async with self.db.transaction() as conn:
            rows = await self._group_events(conn, group_id)
            for row in rows:
                if events == "delete":
                    await self._remove(conn, row)
                else:
                    await self._journal(conn, row["id"], actor, [{"action": "ungrouped", "summary": "its group was deleted"}])
            await conn.execute("DELETE FROM calendar_groups WHERE id=?", (group_id,))
        announce(self.db, {"change": "group", "group_id": group_id})

    @staticmethod
    def _days(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value == 0 or abs(value) > 3660:
            raise ValueError("days is a whole number of days, not 0, at most ten years either way")
        return value

    async def shift_group(self, group_id: str, days: int, actor: str = OPERATOR) -> dict[str, Any]:
        """Move every event of a group by whole days, keeping each one's wall-clock time in its zone."""
        days = self._days(days)
        async with self.db.transaction() as conn:
            for row in await self._group_events(conn, group_id):
                moved = _moved(row, days)
                await conn.execute(
                    "UPDATE calendar_events SET start_at=?, end_at=?, recurrence_end=?, exdates=?, dirty=?, version=version+1, updated_at=? WHERE id=?",
                    (moved["start_at"], moved["end_at"], moved["recurrence_end"], moved["exdates"], self._dirty(row), now(), row["id"]),
                )
                for override in await (await conn.execute("SELECT * FROM calendar_events WHERE series_id=?", (row["id"],))).fetchall():
                    shifted = _moved(override, days)
                    await conn.execute(
                        "UPDATE calendar_events SET start_at=?, end_at=?, original_start=?, dirty=?, version=version+1, updated_at=? WHERE id=?",
                        (shifted["start_at"], shifted["end_at"], _shift(override["original_start"], row["timezone"], bool(row["all_day"]), days), self._dirty(override), now(), override["id"]),
                    )
                await self._journal(conn, row["id"], actor, [{"action": "shifted", "summary": f"moved by {days:+d} days with its group"}])
        announce(self.db, {"change": "group", "group_id": group_id})
        return await self.group(group_id)

    async def duplicate_group(self, group_id: str, offset_days: int, actor: str = OPERATOR, name: str | None = None) -> dict[str, Any]:
        """Copy a group and its events ``offset_days`` later: a plan to run again, every tick cleared
        and every status back to planned. A series is copied without its changed occurrences."""
        days = self._days(offset_days)
        new_group = entries.new_id("grp")
        stamp = now()
        async with self.db.transaction() as conn:
            rows = await self._group_events(conn, group_id)
            group = await (await conn.execute("SELECT * FROM calendar_groups WHERE id=?", (group_id,))).fetchone()
            await conn.execute(
                "INSERT INTO calendar_groups(id, name, icon, color, description, blocks_json, created_by, archived, hidden, created_at, updated_at) VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                (new_group, _name(name, "group name", 120) if name else group["name"], group["icon"], group["color"], group["description"],
                 json.dumps(entries.reset_marks(_loads(group["blocks_json"], [])), ensure_ascii=False), actor, group["hidden"], stamp, stamp),
            )
            for row in rows:
                calendar = await (await conn.execute("SELECT * FROM calendars WHERE id=?", (row["calendar_id"],))).fetchone()
                moved = _moved(row, days)
                fields = {key: row[key] for key in ("title", "description", "timezone", "all_day", "location", "recurrence", "color", "reminders")}
                fields.update(start_at=moved["start_at"], end_at=moved["end_at"], recurrence_end=moved["recurrence_end"])
                entry = {
                    "type_id": row["type_id"], "type_version": row["type_version"], "fields_json": row["fields_json"],
                    "blocks_json": json.dumps(entries.reset_marks(_loads(row["blocks_json"], [])), ensure_ascii=False),
                    "status": "planned", "tags_json": row["tags_json"], "group_id": new_group, "links_json": row["links_json"], "exdates": moved["exdates"],
                }
                await self._insert(conn, calendar, fields, entry, actor)
        announce(self.db, {"change": "group", "group_id": new_group})
        return await self.group(new_group)

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


def _shift(value: str, timezone: str, all_day: bool, days: int) -> str:
    """An instant moved by whole days on the wall clock of ``timezone``, so 08:00 stays 08:00
    across a daylight-saving change; an all-day bound is a date and moves by dates."""
    moment_at = datetime.fromisoformat(value)
    if all_day:
        return (moment_at + timedelta(days=days)).isoformat()
    zone = recurrence.zone(timezone)
    local = moment_at.astimezone(zone).replace(tzinfo=None) + timedelta(days=days)
    return local.replace(tzinfo=zone).astimezone(UTC).isoformat()


def _moved(row: Any, days: int) -> dict[str, Any]:
    all_day = bool(row["all_day"])
    start = _shift(row["start_at"], row["timezone"], all_day, days)
    end = _shift(row["end_at"], row["timezone"], all_day, days)
    rule = row["recurrence"] or ""
    try:
        until = recurrence.series_end(rule, start, end, row["timezone"], all_day) if rule else None
    except (ValueError, TypeError, OverflowError):
        until = None
    exdates = json.dumps([_shift(item, row["timezone"], all_day, days) for item in _loads(row["exdates"], [])])
    return {"start_at": start, "end_at": end, "recurrence_end": until, "exdates": exdates}


def day_anchor(day: str, settings: dict[str, Any]) -> datetime:
    """The moment an all-day item's reminder counts back from: the start of the working day, in the
    operator's zone. Counting from midnight would wake them for "0 minutes before" a holiday."""
    hours, minutes = (int(part) for part in str(settings["work_start"]).split(":"))
    local = datetime.combine(datetime.fromisoformat(day[:10]).date(), time(hours, minutes), recurrence.zone(settings["timezone"]))
    return local.astimezone(UTC)


__all__ = ["CALDAV_PRESETS", "ENTRY_KEYS", "OPERATOR", "SYNCING", "CalendarStore", "account_view", "announce", "color_of", "day_anchor", "entry_view", "event_view", "instant", "listen", "moment", "now", "reminders_of", "type_view"]
