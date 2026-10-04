"""The operator's calendar and its pending remote changes.

Local edits stay in SQLite until a connector acknowledges them. A remote refresh never overwrites
an unacknowledged edit; this is what lets a disconnected calendar remain editable.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daedalus.stores.database import Database


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


def event_view(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "account_id", "remote_id", "title", "description", "start_at", "end_at", "timezone", "all_day", "location", "recurrence", "dirty", "version", "updated_at")}


def account_view(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "provider", "name", "remote_calendar_id", "last_sync_at", "sync_error", "created_at")}


class CalendarStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def events(self, start: str, end: str) -> list[dict[str, Any]]:
        first, last = instant(start), instant(end)
        if first >= last:
            raise ValueError("the calendar range needs an end after its start")
        rows = await self.db.fetchall(
            "SELECT * FROM calendar_events WHERE start_at < ? AND end_at > ? AND dirty != 'delete' ORDER BY start_at, id LIMIT 2000",
            (last, first),
        )
        return [event_view(row) for row in rows]

    async def get(self, event_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id = ? AND dirty != 'delete'", (event_id,))
        return event_view(row) if row else None

    async def save(self, body: dict[str, Any], event_id: str | None = None) -> dict[str, Any]:
        old = await self.db.fetchone("SELECT * FROM calendar_events WHERE id = ? AND dirty != 'delete'", (event_id,)) if event_id else None
        if event_id and old is None:
            raise KeyError(event_id)
        if old and int(body.get("version", -1)) != old["version"]:
            raise RuntimeError("the event changed elsewhere; reload it before saving")
        account_id = body.get("account_id", old["account_id"] if old else None) or None
        if old and account_id != old["account_id"]:
            raise ValueError("an event cannot move between calendars; create it in the other calendar")
        if account_id and await self.db.fetchone("SELECT 1 FROM calendar_accounts WHERE id = ?", (account_id,)) is None:
            raise ValueError("no such connected calendar")
        title = str(body.get("title", old["title"] if old else "")).strip()
        if not title or len(title) > 240:
            raise ValueError("the event title needs 1 to 240 characters")
        all_day = int(bool(body.get("all_day", old["all_day"] if old else False)))
        start_value = str(body.get("start_at", old["start_at"] if old else ""))
        end_value = str(body.get("end_at", old["end_at"] if old else ""))
        start = instant(start_value)
        end = instant(end_value)
        if all_day:
            # Calendar dates have no UTC offset. Keep the dates the caller named, even when their
            # midnight has an offset that would fall on the previous UTC day.
            start = datetime.fromisoformat(start_value.replace("Z", "+00:00")).date().isoformat() + "T00:00:00+00:00"
            end = datetime.fromisoformat(end_value.replace("Z", "+00:00")).date().isoformat() + "T00:00:00+00:00"
        if end <= start:
            raise ValueError("the event must end after it starts")
        zone = str(body.get("timezone", old["timezone"] if old else "UTC"))
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("use an IANA time zone, such as Europe/London") from exc
        description = str(body.get("description", old["description"] if old else ""))[:10000]
        location = str(body.get("location", old["location"] if old else ""))[:500]
        recurrence = str(body.get("recurrence", old["recurrence"] if old else ""))[:500]
        if recurrence and not recurrence.startswith("RRULE:"):
            recurrence = "RRULE:" + recurrence
        changed_at = now()
        if old:
            dirty = ("create" if old["dirty"] == "create" else "update") if account_id else ""
            async with self.db.transaction() as conn:
                cursor = await conn.execute(
                    "UPDATE calendar_events SET title=?, description=?, start_at=?, end_at=?, timezone=?, all_day=?, location=?, recurrence=?, dirty=?, version=version+1, updated_at=? WHERE id=? AND version=? AND dirty!='delete'",
                    (title, description, start, end, zone, all_day, location, recurrence, dirty, changed_at, event_id, old["version"]),
                )
                if cursor.rowcount == 0:
                    raise RuntimeError("the event changed elsewhere; reload it before saving")
        else:
            event_id = uuid.uuid4().hex
            await self.db.execute(
                "INSERT INTO calendar_events(id,account_id,title,description,start_at,end_at,timezone,all_day,location,recurrence,dirty,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, account_id, title, description, start, end, zone, all_day, location, recurrence, "create" if account_id else "", changed_at),
            )
        result = await self.get(event_id)
        assert result is not None
        return result

    async def delete(self, event_id: str, version: int | None = None) -> None:
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id = ? AND dirty != 'delete'", (event_id,))
        if row is None:
            raise KeyError(event_id)
        if version is not None and version != row["version"]:
            raise RuntimeError("the event changed elsewhere; reload it before deleting")
        async with self.db.transaction() as conn:
            if not row["account_id"] or row["dirty"] == "create":
                cursor = await conn.execute("DELETE FROM calendar_events WHERE id=? AND version=? AND dirty!='delete'", (event_id, row["version"]))
            else:
                cursor = await conn.execute("UPDATE calendar_events SET dirty='delete',version=version+1,updated_at=? WHERE id=? AND version=? AND dirty!='delete'", (now(), event_id, row["version"]))
            if cursor.rowcount == 0:
                raise RuntimeError("the event changed elsewhere; reload it before deleting")

    async def accounts(self) -> list[dict[str, Any]]:
        return [account_view(row) for row in await self.db.fetchall("SELECT * FROM calendar_accounts ORDER BY created_at")]

    async def account(self, account_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM calendar_accounts WHERE id = ?", (account_id,))

    async def connect(self, provider: str, name: str, credentials: dict[str, str], remote_calendar_id: str = "primary") -> dict[str, Any]:
        if provider not in ("google", "outlook", "yandex"):
            raise ValueError("the calendar provider must be google, outlook or yandex")
        needed = ("client_id", "client_secret", "refresh_token") if provider != "yandex" else ("username", "app_password")
        if any(not credentials.get(key) for key in needed):
            raise ValueError("missing calendar credentials: " + ", ".join(key for key in needed if not credentials.get(key)))
        if len(name.strip()) > 100 or not name.strip():
            raise ValueError("the calendar name needs 1 to 100 characters")
        account_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO calendar_accounts(id,provider,name,credentials_json,remote_calendar_id,created_at) VALUES(?,?,?,?,?,?)",
            (account_id, provider, name.strip(), json.dumps({key: credentials[key] for key in needed}), remote_calendar_id.strip() or "primary", now()),
        )
        row = await self.account(account_id)
        return account_view(row)

    async def disconnect(self, account_id: str) -> None:
        if await self.account(account_id) is None:
            raise KeyError(account_id)
        await self.db.execute("DELETE FROM calendar_accounts WHERE id = ?", (account_id,))


__all__ = ["CalendarStore", "account_view", "event_view", "instant", "now"]
