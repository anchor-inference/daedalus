"""Two-way calendar synchronization over Google, Microsoft Graph and Yandex CalDAV.

The local write queue is sent before remote changes are read. Conditional writes leave a conflict
pending, instead of silently replacing a provider edit made since the last read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from defusedxml import ElementTree as SafeXML
from icalendar import Calendar, Event

from daedalus.stores.calendar import CalendarStore, instant, now

logger = logging.getLogger(__name__)
GOOGLE = "https://www.googleapis.com/calendar/v3/calendars/"
GRAPH = "https://graph.microsoft.com/v1.0/me/"
CALDAV = "https://caldav.yandex.ru"
DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
_ACCOUNT_LOCKS: dict[str, asyncio.Lock] = {}


class CalendarConflict(RuntimeError):
    def __init__(self, event_id: str) -> None:
        super().__init__(f"event {event_id} changed at the provider; choose which version to keep")
        self.event_id = event_id


def _time(value: Any) -> str:
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        value = date.fromisoformat(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), UTC)
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
    return instant(str(value))


def _remote(provider: str, item: dict[str, Any]) -> dict[str, Any] | None:
    if provider == "google":
        if item.get("status") == "cancelled":
            return None
        start, end = item.get("start") or {}, item.get("end") or {}
        all_day = "date" in start
        return {
            "title": item.get("summary") or "(untitled)", "description": item.get("description") or "",
            "start_at": _time(start.get("dateTime") or start.get("date")), "end_at": _time(end.get("dateTime") or end.get("date")),
            "timezone": start.get("timeZone") or "UTC", "all_day": int(all_day), "location": item.get("location") or "",
            "recurrence": "\n".join(item.get("recurrence") or []), "etag": item.get("etag") or "", "remote_uid": item.get("iCalUID") or "",
        }
    if "@removed" in item or item.get("isCancelled"):
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    return {
        "title": item.get("subject") or "(untitled)", "description": (item.get("body") or {}).get("content") or "",
        "start_at": _time(start.get("dateTime") + "Z" if start.get("dateTime") and "+" not in start["dateTime"] and not start["dateTime"].endswith("Z") else start.get("dateTime")),
        "end_at": _time(end.get("dateTime") + "Z" if end.get("dateTime") and "+" not in end["dateTime"] and not end["dateTime"].endswith("Z") else end.get("dateTime")),
        "timezone": "UTC", "all_day": int(bool(item.get("isAllDay"))), "location": (item.get("location") or {}).get("displayName") or "",
        "recurrence": "", "etag": item.get("@odata.etag") or "", "remote_uid": item.get("iCalUId") or "",
    }


def _payload(provider: str, row: Any) -> dict[str, Any]:
    start = datetime.fromisoformat(row["start_at"])
    end = datetime.fromisoformat(row["end_at"])
    if provider == "google":
        dates = ({"date": start.date().isoformat()}, {"date": end.date().isoformat()}) if row["all_day"] else (
            {"dateTime": start.isoformat(), "timeZone": row["timezone"]}, {"dateTime": end.isoformat(), "timeZone": row["timezone"]},
        )
        data = {"summary": row["title"], "description": row["description"], "start": dates[0], "end": dates[1], "location": row["location"]}
        if row["recurrence"]:
            data["recurrence"] = row["recurrence"].splitlines()
        return data
    return {
        "subject": row["title"], "body": {"contentType": "text", "content": row["description"]},
        "start": {"dateTime": start.replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        "isAllDay": bool(row["all_day"]), "location": {"displayName": row["location"]},
    }


class CalendarSync:
    def __init__(self, store: CalendarStore) -> None:
        self.store = store

    async def disconnect(self, account_id: str) -> None:
        async with _ACCOUNT_LOCKS.setdefault(account_id, asyncio.Lock()):
            await self.store.disconnect(account_id)

    async def sync(self, account_id: str) -> dict[str, Any]:
        # The background loop and API routes own separate CalendarSync instances. Sharing this
        # lock keeps a refresh, remote write and cursor advance in one account from racing itself.
        async with _ACCOUNT_LOCKS.setdefault(account_id, asyncio.Lock()):
            account = await self.store.account(account_id)
            if account is None:
                raise KeyError(account_id)
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                    if account["provider"] == "yandex":
                        changed = await self._yandex(client, account)
                    else:
                        token = await self._token(client, account)
                        changed = await self._json_provider(client, account, token)
                at = now()
                await self.store.db.execute("UPDATE calendar_accounts SET last_sync_at=?,sync_error='' WHERE id=?", (at, account_id))
                return {"changed": changed, "last_sync_at": at}
            except Exception as exc:
                message = str(exc)[:500]
                await self.store.db.execute("UPDATE calendar_accounts SET sync_error=? WHERE id=?", (message, account_id))
                raise

    async def resolve(self, event_id: str, choice: str) -> dict[str, Any]:
        if choice not in ("local", "remote"):
            raise ValueError("choose local or remote")
        row = await self.store.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND account_id IS NOT NULL AND dirty!=''", (event_id,))
        if row is None:
            raise KeyError(event_id)
        if choice == "remote":
            # A full refresh is needed because the provider's cursor may already have passed the
            # change that conflicted with this local write.
            await self.store.db.execute("UPDATE calendar_events SET dirty='' WHERE id=?", (event_id,))
            await self.store.db.execute("UPDATE calendar_accounts SET cursor='' WHERE id=?", (row["account_id"],))
        else:
            # The operator explicitly chose to replace the provider's newer version. The next
            # outbound write omits If-Match for this one event; other writes keep their checks.
            await self.store.db.execute("UPDATE calendar_events SET etag='' WHERE id=?", (event_id,))
        return await self.sync(row["account_id"])

    async def _token(self, client: httpx.AsyncClient, account: Any) -> str:
        credentials = json.loads(account["credentials_json"])
        if account["provider"] == "google":
            url = "https://oauth2.googleapis.com/token"
            payload = {**credentials, "grant_type": "refresh_token"}
        else:
            url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
            payload = {**credentials, "grant_type": "refresh_token", "scope": "offline_access Calendars.ReadWrite"}
        response = await client.post(url, data=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("access_token"):
            raise ValueError("calendar provider did not return an access token")
        if body.get("refresh_token") and body["refresh_token"] != credentials["refresh_token"]:
            credentials["refresh_token"] = body["refresh_token"]
            await self.store.db.execute("UPDATE calendar_accounts SET credentials_json=? WHERE id=?", (json.dumps(credentials), account["id"]))
        return body["access_token"]

    async def _json_provider(self, client: httpx.AsyncClient, account: Any, token: str) -> int:
        provider, account_id = account["provider"], account["id"]
        headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'}
        base = GOOGLE + quote(account["remote_calendar_id"], safe="") + "/events" if provider == "google" else GRAPH + "calendars/" + quote(account["remote_calendar_id"], safe="") + "/events"
        if provider == "outlook" and account["remote_calendar_id"] == "primary":
            base = GRAPH + "events"
        pending = await self.store.db.fetchall("SELECT * FROM calendar_events WHERE account_id=? AND dirty!='' ORDER BY updated_at", (account_id,))
        for row in pending:
            remote_id = row["remote_id"]
            url = base + ("/" + quote(remote_id, safe="") if remote_id else "")
            conditional = {**headers, **({"If-Match": row["etag"]} if row["etag"] else {})}
            if row["dirty"] == "delete":
                response = await client.delete(url, headers=conditional)
                if response.status_code in (409, 412):
                    raise CalendarConflict(row["id"])
                if response.status_code not in (404, 410):
                    response.raise_for_status()
                await self.store.db.execute("DELETE FROM calendar_events WHERE id=? AND version=?", (row["id"], row["version"]))
                continue
            method = client.patch if remote_id else client.post
            response = await method(url, headers=conditional, json=_payload(provider, row))
            if response.status_code in (409, 412) or (remote_id and response.status_code in (404, 410)):
                raise CalendarConflict(row["id"])
            response.raise_for_status()
            saved = response.json()
            await self.store.db.execute(
                "UPDATE calendar_events SET remote_id=?,etag=?,dirty='' WHERE id=? AND version=?",
                (saved["id"], saved.get("etag") or saved.get("@odata.etag") or "", row["id"], row["version"]),
            )
        changed = 0
        full_google_refresh = provider == "google" and not account["cursor"]
        seen: set[str] = set()
        if provider == "google":
            cursor = account["cursor"]
            params: dict[str, str] = {"maxResults": "250", "showDeleted": "true", "singleEvents": "true"}
            if cursor:
                params["syncToken"] = cursor
            url = base
        else:
            cursor = json.loads(account["cursor"] or "{}")
            window = datetime.now(UTC)
            if not cursor or datetime.fromisoformat(cursor["end"]) < window + timedelta(days=90):
                cursor = {"end": (window + timedelta(days=730)).isoformat()}
                url = base.rsplit("/events", 1)[0] + "/calendarView/delta" if base != GRAPH + "events" else GRAPH + "calendarView/delta"
                params = {"startDateTime": (window - timedelta(days=365)).isoformat(), "endDateTime": cursor["end"]}
            else:
                url, params = cursor["url"], None
        while url:
            if provider == "outlook" and not url.startswith("https://graph.microsoft.com/v1.0/"):
                raise ValueError("unexpected calendar delta URL")
            response = await client.get(url, headers=headers, params=params)
            if provider == "google" and response.status_code == 410:
                await self.store.db.execute("UPDATE calendar_accounts SET cursor='' WHERE id=?", (account_id,))
                return changed + await self._json_provider(client, await self.store.account(account_id), token)
            response.raise_for_status()
            data = response.json()
            for item in data.get("items" if provider == "google" else "value", []):
                remote_id = str(item["id"])
                if full_google_refresh:
                    seen.add(remote_id)
                await self._apply(account_id, remote_id, _remote(provider, item))
                changed += 1
            if provider == "google":
                page = data.get("nextPageToken")
                if page:
                    params["pageToken"] = page
                else:
                    await self.store.db.execute("UPDATE calendar_accounts SET cursor=? WHERE id=?", (data.get("nextSyncToken") or "", account_id))
                url = base if page else ""
            else:
                url = data.get("@odata.nextLink") or ""
                if not url and data.get("@odata.deltaLink"):
                    cursor["url"] = data["@odata.deltaLink"]
                    await self.store.db.execute("UPDATE calendar_accounts SET cursor=? WHERE id=?", (json.dumps(cursor), account_id))
                params = None
        if full_google_refresh:
            # A 410 makes the old cursor unusable. Full lists omit events deleted long ago, so
            # remove clean local snapshots absent from this complete read after its last page.
            for row in await self.store.db.fetchall("SELECT id,remote_id FROM calendar_events WHERE account_id=? AND remote_id IS NOT NULL AND dirty=''", (account_id,)):
                if row["remote_id"] not in seen:
                    await self.store.db.execute("DELETE FROM calendar_events WHERE id=? AND dirty=''", (row["id"],))
        return changed

    async def _apply(self, account_id: str, remote_id: str, data: dict[str, Any] | None) -> None:
        row = await self.store.db.fetchone("SELECT * FROM calendar_events WHERE account_id=? AND remote_id=?", (account_id, remote_id))
        if row is not None and row["dirty"]:
            return
        if data is None:
            if row is not None:
                await self.store.db.execute("DELETE FROM calendar_events WHERE id=? AND version=? AND dirty=''", (row["id"], row["version"]))
            return
        values = (data["title"][:240], data["description"][:10000], data["start_at"], data["end_at"], data["timezone"], data["all_day"], data["location"][:500], data["recurrence"][:500], data["etag"], data.get("remote_uid") or "", now())
        if row is not None:
            if all(row[key] == data[key] for key in ("title", "description", "start_at", "end_at", "timezone", "all_day", "location", "recurrence", "etag")) and row["remote_uid"] == (data.get("remote_uid") or ""):
                return
            await self.store.db.execute(
                "UPDATE calendar_events SET title=?,description=?,start_at=?,end_at=?,timezone=?,all_day=?,location=?,recurrence=?,etag=?,remote_uid=?,updated_at=?,version=version+1 WHERE id=? AND version=? AND dirty=''",
                (*values, row["id"], row["version"]),
            )
        else:
            await self.store.db.execute(
                "INSERT INTO calendar_events(id,account_id,remote_id,title,description,start_at,end_at,timezone,all_day,location,recurrence,dirty,etag,remote_uid,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'',?,?,?)",
                (uuid.uuid4().hex, account_id, remote_id, *values),
            )

    async def _yandex(self, client: httpx.AsyncClient, account: Any) -> int:
        credentials = json.loads(account["credentials_json"])
        auth = (credentials["username"], credentials["app_password"])
        principal = "/principals/users/" + quote(credentials["username"], safe="@") + "/"
        path = account["remote_calendar_id"]
        if path == "primary":
            response = await client.request("PROPFIND", CALDAV + principal, auth=auth, headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"}, content='<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>')
            response.raise_for_status()
            home = SafeXML.fromstring(response.content).findtext(f".//{{{CAL}}}calendar-home-set/{{{DAV}}}href")
            if not home:
                raise ValueError("Yandex did not return a calendar home")
            response = await client.request("PROPFIND", self._dav_url(home), auth=auth, headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, content='<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:resourcetype/></d:prop></d:propfind>')
            response.raise_for_status()
            root = SafeXML.fromstring(response.content)
            path = next((node.findtext(f"{{{DAV}}}href") for node in root.findall(f"{{{DAV}}}response") if node.find(f".//{{{CAL}}}calendar") is not None), None)
            if not path:
                raise ValueError("Yandex has no calendar in this account")
            await self.store.db.execute("UPDATE calendar_accounts SET remote_calendar_id=? WHERE id=?", (path, account["id"]))
        url = self._dav_url(path)
        pending = await self.store.db.fetchall("SELECT * FROM calendar_events WHERE account_id=? AND dirty!=''", (account["id"],))
        for row in pending:
            event_url = self._dav_url(row["remote_id"]) if row["remote_id"] else url.rstrip("/") + "/" + row["id"] + ".ics"
            headers = {"If-Match": row["etag"]} if row["etag"] else ({"If-None-Match": "*"} if not row["remote_id"] else {})
            if row["dirty"] == "delete":
                response = await client.delete(event_url, auth=auth, headers=headers)
                if response.status_code in (409, 412):
                    raise CalendarConflict(row["id"])
                if response.status_code not in (404, 410):
                    response.raise_for_status()
                await self.store.db.execute("DELETE FROM calendar_events WHERE id=? AND version=?", (row["id"], row["version"]))
                continue
            response = await client.put(event_url, auth=auth, headers={**headers, "Content-Type": "text/calendar; charset=utf-8"}, content=self._ical(row))
            if response.status_code in (409, 412) or (row["remote_id"] and response.status_code in (404, 410)):
                raise CalendarConflict(row["id"])
            response.raise_for_status()
            remote_id = urlparse(event_url).path
            await self.store.db.execute("UPDATE calendar_events SET remote_id=?,etag=?,dirty='' WHERE id=? AND version=?", (remote_id, response.headers.get("etag", ""), row["id"], row["version"]))
        body = '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
        response = await client.request("PROPFIND", url, auth=auth, headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, content=body)
        response.raise_for_status()
        root = SafeXML.fromstring(response.content)
        seen: set[str] = set()
        changed = 0
        for node in root.findall(f"{{{DAV}}}response"):
            href = node.findtext(f"{{{DAV}}}href") or ""
            if not href.endswith(".ics"):
                continue
            remote_id = urlparse(self._dav_url(href)).path
            seen.add(remote_id)
            etag = node.findtext(f".//{{{DAV}}}getetag") or ""
            old = await self.store.db.fetchone("SELECT etag FROM calendar_events WHERE account_id=? AND remote_id=?", (account["id"], remote_id))
            if old is not None and old["etag"] == etag:
                continue
            entry = await client.get(self._dav_url(href), auth=auth)
            entry.raise_for_status()
            parsed = Calendar.from_ical(entry.content)
            event = next((child for child in parsed.walk() if child.name == "VEVENT"), None)
            if event is None:
                continue
            start = event.decoded("DTSTART")
            end = event.decoded("DTEND")
            timezone = str(event["DTSTART"].params.get("TZID") or getattr(getattr(start, "tzinfo", None), "key", "UTC"))
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                timezone = "UTC"
            await self._apply(account["id"], remote_id, {
                "title": str(event.get("SUMMARY") or "(untitled)"), "description": str(event.get("DESCRIPTION") or ""),
                "start_at": _time(start), "end_at": _time(end), "timezone": timezone, "all_day": int(isinstance(start, date) and not isinstance(start, datetime)),
                "location": str(event.get("LOCATION") or ""), "recurrence": "RRULE:" + event.get("RRULE").to_ical().decode() if event.get("RRULE") else "", "etag": etag,
                "remote_uid": str(event.get("UID") or ""),
            })
            changed += 1
        for row in await self.store.db.fetchall("SELECT id,remote_id FROM calendar_events WHERE account_id=? AND remote_id IS NOT NULL AND dirty=''", (account["id"],)):
            if row["remote_id"] not in seen:
                await self.store.db.execute("DELETE FROM calendar_events WHERE id=?", (row["id"],))
                changed += 1
        return changed

    @staticmethod
    def _dav_url(path: str) -> str:
        parsed = urlparse(path)
        if parsed.scheme and (parsed.scheme != "https" or parsed.netloc != "caldav.yandex.ru"):
            raise ValueError("unexpected CalDAV host")
        if ".." in parsed.path.split("/"):
            raise ValueError("invalid CalDAV path")
        return CALDAV + parsed.path if not parsed.scheme else path

    @staticmethod
    def _ical(row: Any) -> bytes:
        calendar = Calendar()
        calendar.add("prodid", "-//Daedalus//Calendar//EN")
        calendar.add("version", "2.0")
        event = Event()
        event.add("uid", row["remote_uid"] or row["id"] + "@daedalus.local")
        event.add("summary", row["title"])
        event.add("description", row["description"])
        event.add("location", row["location"])
        start, end = datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"])
        event.add("dtstart", start.date() if row["all_day"] else start)
        event.add("dtend", end.date() if row["all_day"] else end)
        event.add("dtstamp", datetime.now(UTC))
        event.add("sequence", row["version"])
        if row["recurrence"]:
            event.add("rrule", row["recurrence"].removeprefix("RRULE:"))
        calendar.add_component(event)
        return calendar.to_ical()


async def sync_loop(store: CalendarStore, stopping: asyncio.Event) -> None:
    sync = CalendarSync(store)
    while not stopping.is_set():
        await store.db.execute("DELETE FROM kv WHERE key LIKE 'calendar_oauth:%' AND json_extract(value, '$.expires_at') < ?", (now(),))
        for account in await store.accounts():
            try:
                await sync.sync(account["id"])
            except Exception:
                logger.warning("calendar sync failed for %s", account["id"], exc_info=True)
        try:
            await asyncio.wait_for(stopping.wait(), 300)
        except TimeoutError:
            pass


__all__ = ["CalendarSync", "sync_loop"]
