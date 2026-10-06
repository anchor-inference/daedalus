"""Calendar synchronization: Google, Microsoft Graph, any CalDAV server, and read-only ICS feeds.

The local write queue is sent before remote changes are read. Conditional writes never replace a
provider edit made since the last read: such a write is marked as a conflict on its event (the
``conflict`` column, which the app shows and :meth:`CalendarSync.resolve` settles) and the rest of the
queue still goes out, so one disputed event does not hold every other change back.

Every provider is mapped onto the same model the store expands: a series is one row with its rule,
an occurrence that differs is an override row naming the occurrence it replaces, an occurrence that
was removed is an exception date or a cancelled override. Reading occurrences as separate events was
what duplicated a series created here (Google returned each instance next to the series it was made
from), so occurrences are never imported as events of their own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from defusedxml import ElementTree as SafeXML
from icalendar import Calendar, Event
from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON

from daedalus.stores import recurrence
from daedalus.stores.calendar import SYNCING, CalendarStore, instant, now

logger = logging.getLogger(__name__)
GOOGLE = "https://www.googleapis.com/calendar/v3/calendars/"
GRAPH = "https://graph.microsoft.com/v1.0/me/"
DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
_ACCOUNT_LOCKS: dict[str, asyncio.Lock] = {}

SYNC_SECONDS = 5 * 60
"""How often a healthy two-way account is read."""
ICS_SECONDS = 30 * 60
"""How often a subscription is polled. Feeds are holidays and shared schedules; the conditional
request makes a poll that finds nothing new cost one round trip."""
LOOP_SECONDS = 60
BACKOFF_MAX_SECONDS = 6 * 60 * 60
"""The longest a failing account waits between attempts. Doubling from two minutes, an account with
a revoked token is tried about a dozen times on its first day instead of 288 times."""
ICS_MAX_BYTES = 5 * 1024 * 1024
ICS_MAX_EVENTS = 5000


def backoff(failures: int) -> int:
    return min(60 * 2 ** min(failures, 20), BACKOFF_MAX_SECONDS)


def _time(value: Any) -> str:
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        value = date.fromisoformat(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), UTC)
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
    text = str(value)
    if "T" in text and not text.endswith("Z") and "+" not in text[10:] and "-" not in text[10:]:
        # Graph, asked for UTC, writes its times without an offset.
        text += "Z"
    return instant(text)


def _iana(name: str | None) -> str:
    """An IANA zone for whatever a provider named: IANA itself, or a Windows name from Outlook."""
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        pass
    mapped = WINDOWS_TO_OLSON.get(name)
    return mapped if mapped else "UTC"


def _rule(text: str) -> str:
    """A provider's rule as the store keeps it. One the store would refuse is kept as written: the
    series then shows its first occurrence instead of failing the whole read."""
    try:
        return recurrence.normalize(text)
    except (ValueError, TypeError):
        return text[:500]


def _series_end(data: dict[str, Any]) -> str | None:
    try:
        return recurrence.series_end(data["recurrence"], data["start_at"], data["end_at"], data["timezone"], bool(data["all_day"]))
    except (ValueError, TypeError, OverflowError):
        return None


def _exdate_keys(lines: list[str], timezone: str) -> list[str]:
    """Occurrence keys from EXDATE lines in a Google ``recurrence`` list."""
    found: list[str] = []
    for line in lines:
        head, _, values = line.partition(":")
        params = dict(part.split("=", 1) for part in head.split(";")[1:] if "=" in part)
        zone = ZoneInfo(_iana(params.get("TZID") or timezone))
        for value in values.split(","):
            value = value.strip()
            if len(value) == 8:
                found.append(datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC).isoformat())
            elif value.endswith("Z"):
                found.append(datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).isoformat())
            elif value:
                found.append(datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone).astimezone(UTC).isoformat())
    return found


def _exdate_lines(exdates: list[str], all_day: bool) -> list[str]:
    if not exdates:
        return []
    if all_day:
        return ["EXDATE;VALUE=DATE:" + ",".join(datetime.fromisoformat(item).strftime("%Y%m%d") for item in exdates)]
    return ["EXDATE:" + ",".join(datetime.fromisoformat(item).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") for item in exdates)]


def _google_event(item: dict[str, Any]) -> dict[str, Any]:
    start, end = item.get("start") or {}, item.get("end") or {}
    all_day = "date" in start
    timezone = _iana(start.get("timeZone")) if not all_day else "UTC"
    lines = item.get("recurrence") or []
    rules = [line for line in lines if line.upper().startswith("RRULE:")]
    exdates = _exdate_keys([line for line in lines if line.upper().startswith("EXDATE")], timezone)
    return {
        "title": item.get("summary") or "(untitled)", "description": item.get("description") or "",
        "start_at": _time(start.get("dateTime") or start.get("date")), "end_at": _time(end.get("dateTime") or end.get("date")),
        "timezone": timezone, "all_day": int(all_day), "location": item.get("location") or "",
        "recurrence": _rule(rules[0]) if rules else "", "exdates": exdates,
        "etag": item.get("etag") or "", "remote_uid": item.get("iCalUID") or "",
    }


def _outlook_event(item: dict[str, Any]) -> dict[str, Any]:
    start, end = item.get("start") or {}, item.get("end") or {}
    data = {
        "title": item.get("subject") or "(untitled)", "description": (item.get("body") or {}).get("content") or "",
        "start_at": _time(start.get("dateTime")), "end_at": _time(end.get("dateTime")),
        "timezone": "UTC", "all_day": int(bool(item.get("isAllDay"))), "location": (item.get("location") or {}).get("displayName") or "",
        "recurrence": "", "exdates": [], "etag": item.get("@odata.etag") or "", "remote_uid": item.get("iCalUId") or "",
    }
    if item.get("recurrence"):
        span = item["recurrence"].get("range") or {}
        data["timezone"] = "UTC" if data["all_day"] else _iana(span.get("recurrenceTimeZone") or item.get("originalStartTimeZone"))
        try:
            data["recurrence"] = recurrence.from_graph(item["recurrence"])
        except ValueError:
            logger.warning("an Outlook series has a repeat pattern this calendar cannot read; only its first occurrence is shown")
    return data


def _google_stamp(original: str, all_day: bool) -> str:
    """The suffix Google gives an occurrence's id: ``<series>_<UTC start>`` or ``<series>_<date>``."""
    moment = datetime.fromisoformat(original).astimezone(UTC)
    return moment.strftime("%Y%m%d") if all_day else moment.strftime("%Y%m%dT%H%M%SZ")


def _payload(provider: str, row: Any) -> dict[str, Any]:
    start = datetime.fromisoformat(row["start_at"])
    end = datetime.fromisoformat(row["end_at"])
    rule = row["recurrence"] if "series_id" not in row.keys() or not row["series_id"] else ""
    exdates = json.loads(row["exdates"] or "[]")
    if provider == "google":
        dates = ({"date": start.date().isoformat()}, {"date": end.date().isoformat()}) if row["all_day"] else (
            {"dateTime": start.isoformat(), "timeZone": row["timezone"]}, {"dateTime": end.isoformat(), "timeZone": row["timezone"]},
        )
        data = {"summary": row["title"], "description": row["description"], "start": dates[0], "end": dates[1], "location": row["location"]}
        if rule:
            data["recurrence"] = [rule, *_exdate_lines(exdates, bool(row["all_day"]))]
        return data
    data = {
        "subject": row["title"], "body": {"contentType": "text", "content": row["description"]},
        "start": {"dateTime": start.replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        "isAllDay": bool(row["all_day"]), "location": {"displayName": row["location"]},
    }
    if rule:
        # A pattern repeats in the zone its start is written in; sent in UTC, a weekly 10:00 meeting
        # would move by an hour at every daylight-saving change.
        zone = ZoneInfo(row["timezone"]) if not row["all_day"] else UTC
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        timezone = row["timezone"] if not row["all_day"] else "UTC"
        data["start"] = {"dateTime": local_start.replace(tzinfo=None).isoformat(), "timeZone": timezone}
        data["end"] = {"dateTime": local_end.replace(tzinfo=None).isoformat(), "timeZone": timezone}
        data["recurrence"] = recurrence.to_graph(rule, recurrence.series_start(row["start_at"], row["timezone"], bool(row["all_day"])), timezone, bool(row["all_day"]))
    return data


def _component_zone(prop: Any, value: Any) -> str:
    tzid = prop.params.get("TZID") if hasattr(prop, "params") else None
    if tzid:
        return _iana(str(tzid))
    key = getattr(getattr(value, "tzinfo", None), "key", None)
    return _iana(key) if key else "UTC"


def _as_key(value: Any, timezone: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(timezone))
        return value.astimezone(UTC).isoformat()
    return datetime.combine(value, datetime.min.time(), UTC).isoformat()


def _vevent(event: Any) -> dict[str, Any]:
    start = event.decoded("DTSTART")
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    timezone = "UTC" if all_day else _component_zone(event["DTSTART"], start)
    if "DTEND" in event:
        end = event.decoded("DTEND")
    elif "DURATION" in event:
        end = start + event.decoded("DURATION")
    else:
        # RFC 5545: a date lasts the day, a date-time without an end is an instant.
        end = start + timedelta(days=1) if all_day else start
    exdates: list[str] = []
    found = event.get("EXDATE")
    for item in found if isinstance(found, list) else ([found] if found is not None else []):
        exdates.extend(_as_key(entry.dt, timezone) for entry in item.dts)
    rule = event.get("RRULE")
    return {
        "title": str(event.get("SUMMARY") or "(untitled)"), "description": str(event.get("DESCRIPTION") or ""),
        "start_at": _as_key(start, timezone), "end_at": _as_key(end, timezone), "timezone": timezone, "all_day": int(all_day),
        "location": str(event.get("LOCATION") or ""), "recurrence": _rule("RRULE:" + rule.to_ical().decode()) if rule else "",
        "exdates": exdates, "remote_uid": str(event.get("UID") or ""), "etag": "",
    }


def _ical(master: Any, overrides: list[Any]) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Daedalus//Calendar//EN")
    calendar.add("version", "2.0")
    uid = master["remote_uid"] or master["id"] + "@daedalus.local"
    all_day = bool(master["all_day"])
    zone = ZoneInfo(master["timezone"]) if not all_day else UTC

    def local(value: str) -> Any:
        moment = datetime.fromisoformat(value)
        return moment.date() if all_day else moment.astimezone(zone)

    def component(row: Any) -> Event:
        event = Event()
        event.add("uid", uid)
        event.add("summary", row["title"])
        event.add("description", row["description"])
        event.add("location", row["location"])
        event.add("dtstart", local(row["start_at"]))
        event.add("dtend", local(row["end_at"]))
        event.add("dtstamp", datetime.now(UTC))
        event.add("sequence", row["version"])
        return event

    event = component(master)
    if master["recurrence"]:
        event.add("rrule", master["recurrence"].removeprefix("RRULE:"))
        exdates = list(json.loads(master["exdates"] or "[]")) + [row["original_start"] for row in overrides if row["cancelled"]]
        if exdates:
            event.add("exdate", [local(item) for item in sorted(set(exdates))])
    calendar.add_component(event)
    for row in overrides:
        if row["cancelled"]:
            continue
        exception = component(row)
        exception.add("recurrence-id", local(row["original_start"]))
        calendar.add_component(exception)
    calendar.add_missing_timezones()
    return calendar.to_ical()


class CalendarSync:
    def __init__(self, store: CalendarStore) -> None:
        self.store = store

    @property
    def db(self) -> Any:
        return self.store.db

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
            SYNCING.add(account_id)
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                    if account["provider"] == "caldav":
                        changed = await self._caldav(client, account)
                    elif account["provider"] == "ics":
                        changed = await self._ics(client, account)
                    else:
                        token = await self._token(client, account)
                        changed = await self._json_provider(client, account, token)
                at = datetime.now(UTC)
                interval = ICS_SECONDS if account["provider"] == "ics" else SYNC_SECONDS
                await self.db.execute(
                    "UPDATE calendar_accounts SET last_sync_at=?,sync_error='',failures=0,next_sync_at=? WHERE id=?",
                    (at.isoformat(), (at + timedelta(seconds=interval)).isoformat(), account_id),
                )
                conflicts = await self.db.fetchone("SELECT count(*) AS n FROM calendar_events WHERE account_id=? AND conflict!=''", (account_id,))
                return {"changed": changed, "last_sync_at": at.isoformat(), "conflicts": conflicts["n"] if conflicts else 0}
            except Exception as exc:
                failures = int(account["failures"] or 0) + 1
                retry = datetime.now(UTC) + timedelta(seconds=backoff(failures))
                await self.db.execute(
                    "UPDATE calendar_accounts SET sync_error=?,failures=?,next_sync_at=? WHERE id=?",
                    (str(exc)[:500] or type(exc).__name__, failures, retry.isoformat(), account_id),
                )
                raise
            finally:
                SYNCING.discard(account_id)

    async def resolve(self, event_id: str, choice: str) -> dict[str, Any]:
        if choice not in ("local", "remote"):
            raise ValueError("choose local or remote")
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND account_id IS NOT NULL AND (conflict!='' OR dirty!='')", (event_id,))
        if row is None:
            raise KeyError(event_id)
        if choice == "remote":
            if row["conflict"] == "deleted" or row["series_id"]:
                # Nothing of ours is worth keeping: the provider's copy (or its absence) comes back
                # with the full read below.
                await self.db.execute("DELETE FROM calendar_events WHERE id=?", (event_id,))
            else:
                await self.db.execute("UPDATE calendar_events SET dirty='',conflict='',etag='' WHERE id=?", (event_id,))
            # A full refresh is needed because the provider's cursor may already have passed the
            # change that conflicted with this local write.
            await self.db.execute("UPDATE calendar_accounts SET cursor='' WHERE id=?", (row["account_id"],))
        elif row["conflict"] == "deleted":
            # The provider no longer has it and the operator wants it: it is created again.
            await self.db.execute("UPDATE calendar_events SET remote_id=NULL,etag='',dirty='create',conflict='' WHERE id=?", (event_id,))
        else:
            # The operator explicitly chose to replace the provider's newer version. The next
            # outbound write omits If-Match for this one event; other writes keep their checks.
            await self.db.execute("UPDATE calendar_events SET etag='',conflict='' WHERE id=?", (event_id,))
        return await self.sync(row["account_id"])

    async def _conflict(self, row: Any, kind: str) -> None:
        await self.db.execute("UPDATE calendar_events SET conflict=? WHERE id=?", (kind, row["id"]))

    async def _calendar_id(self, account_id: str) -> str:
        row = await self.db.fetchone("SELECT id FROM calendars WHERE account_id=?", (account_id,))
        if row is None:
            raise KeyError(account_id)
        return str(row["id"])

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
            await self.db.execute("UPDATE calendar_accounts SET credentials_json=? WHERE id=?", (json.dumps(credentials), account["id"]))
        return body["access_token"]

    # -- storing what a provider said ----------------------------------------------------------

    async def _apply(self, account_id: str, remote_id: str, data: dict[str, Any] | None) -> Any:
        """Store one series or single event a provider sent; returns its row. A pending local edit wins."""
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE account_id=? AND remote_id=? AND series_id IS NULL", (account_id, remote_id))
        if row is not None and row["dirty"]:
            return row
        if data is None:
            if row is not None:
                await self.db.execute("DELETE FROM calendar_events WHERE id=? AND version=? AND dirty=''", (row["id"], row["version"]))
            return None
        exdates = json.dumps(sorted(set(data.get("exdates") or [])))
        columns = ("title", "description", "start_at", "end_at", "timezone", "all_day", "location", "recurrence", "etag")
        values = (data["title"][:240], data["description"][:10000], data["start_at"], data["end_at"], data["timezone"], data["all_day"], data["location"][:500], data["recurrence"][:500], data["etag"])
        if row is not None:
            if all(row[key] == value for key, value in zip(columns, values, strict=True)) and row["exdates"] == exdates and row["remote_uid"] == (data.get("remote_uid") or ""):
                return row
            await self.db.execute(
                "UPDATE calendar_events SET title=?,description=?,start_at=?,end_at=?,timezone=?,all_day=?,location=?,recurrence=?,etag=?,exdates=?,remote_uid=?,recurrence_end=?,updated_at=?,version=version+1"
                " WHERE id=? AND version=? AND dirty=''",
                (*values, exdates, data.get("remote_uid") or "", _series_end(data), now(), row["id"], row["version"]),
            )
            event_id = row["id"]
        else:
            event_id = uuid.uuid4().hex
            await self.db.execute(
                "INSERT INTO calendar_events(id,calendar_id,account_id,remote_id,title,description,start_at,end_at,timezone,all_day,location,recurrence,etag,exdates,remote_uid,recurrence_end,dirty,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',?)",
                (event_id, await self._calendar_id(account_id), account_id, remote_id, *values, exdates, data.get("remote_uid") or "", _series_end(data), now()),
            )
        return await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (event_id,))

    async def _apply_override(self, master: Any, original: str, remote_id: str | None, data: dict[str, Any] | None) -> None:
        """Store one occurrence a provider changed (``data``) or cancelled (``None``)."""
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE series_id=? AND original_start=?", (master["id"], original))
        if row is None and remote_id:
            row = await self.db.fetchone("SELECT * FROM calendar_events WHERE account_id=? AND remote_id=?", (master["account_id"], remote_id))
        if row is not None and row["dirty"]:
            return
        if data is None:
            data = {"title": master["title"], "description": "", "start_at": original, "end_at": original, "timezone": master["timezone"], "all_day": master["all_day"], "location": "", "etag": ""}
            cancelled = 1
        else:
            cancelled = 0
        values = (data["title"][:240], data["description"][:10000], data["start_at"], data["end_at"], data["timezone"], data["all_day"], data["location"][:500], data.get("etag") or "", cancelled)
        if row is not None:
            await self.db.execute(
                "UPDATE calendar_events SET title=?,description=?,start_at=?,end_at=?,timezone=?,all_day=?,location=?,etag=?,cancelled=?,remote_id=?,original_start=?,series_id=?,updated_at=?,version=version+1"
                " WHERE id=? AND dirty=''",
                (*values, remote_id, original, master["id"], now(), row["id"]),
            )
            return
        await self.db.execute(
            "INSERT INTO calendar_events(id,calendar_id,account_id,remote_id,series_id,original_start,title,description,start_at,end_at,timezone,all_day,location,etag,cancelled,recurrence,dirty,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'','',?)",
            (uuid.uuid4().hex, master["calendar_id"], master["account_id"], remote_id, master["id"], original, *values, now()),
        )

    async def _apply_resource(self, account_id: str, remote_id: str, etag: str, components: list[Any]) -> bool:
        """Store one iCalendar object (CalDAV resource or one UID of a feed): its series and exceptions."""
        master_component = next((item for item in components if "RECURRENCE-ID" not in item), None)
        if master_component is None:
            return False
        data = _vevent(master_component)
        data["etag"] = etag
        exceptions: dict[str, dict[str, Any] | None] = {}
        for item in components:
            if "RECURRENCE-ID" not in item:
                continue
            original = _as_key(item.decoded("RECURRENCE-ID"), data["timezone"])
            if str(item.get("STATUS") or "").upper() == "CANCELLED":
                data["exdates"].append(original)
            else:
                exceptions[original] = _vevent(item)
        master = await self._apply(account_id, remote_id, data)
        if master is None or master["dirty"]:
            return True
        for original, override in exceptions.items():
            await self._apply_override(master, original, None, override)
        # An exception the resource no longer carries is gone at the provider.
        for row in await self.db.fetchall("SELECT id, original_start FROM calendar_events WHERE series_id=? AND dirty=''", (master["id"],)):
            if row["original_start"] not in exceptions:
                await self.db.execute("DELETE FROM calendar_events WHERE id=?", (row["id"],))
        return True

    async def _forget_unseen(self, account_id: str, seen: set[str]) -> int:
        """After a complete read, remove clean rows the provider no longer has. A cancelled occurrence
        stays: some providers leave cancelled instances out of a full listing, and dropping the row
        would bring the occurrence back."""
        gone = 0
        for row in await self.db.fetchall("SELECT id,remote_id,cancelled FROM calendar_events WHERE account_id=? AND remote_id IS NOT NULL AND dirty=''", (account_id,)):
            if row["remote_id"] not in seen and not row["cancelled"]:
                await self.db.execute("DELETE FROM calendar_events WHERE id=? AND dirty=''", (row["id"],))
                gone += 1
        return gone

    # -- Google and Outlook --------------------------------------------------------------------

    async def _json_provider(self, client: httpx.AsyncClient, account: Any, token: str) -> int:
        provider, account_id = account["provider"], account["id"]
        headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'}
        base = GOOGLE + quote(account["remote_calendar_id"], safe="") + "/events" if provider == "google" else GRAPH + "calendars/" + quote(account["remote_calendar_id"], safe="") + "/events"
        if provider == "outlook" and account["remote_calendar_id"] == "primary":
            base = GRAPH + "events"
        # Series before their exceptions: an exception is addressed through its series' remote id.
        pending = await self.db.fetchall(
            "SELECT * FROM calendar_events WHERE account_id=? AND dirty!='' AND conflict='' ORDER BY series_id IS NOT NULL, updated_at", (account_id,),
        )
        for row in pending:
            if row["series_id"]:
                await self._push_instance(client, provider, base, headers, row)
            else:
                await self._push_event(client, provider, base, headers, row)
        if provider == "google":
            return await self._google_read(client, account, base, headers)
        return await self._outlook_read(client, account, base, headers)

    async def _push_event(self, client: httpx.AsyncClient, provider: str, base: str, headers: dict[str, str], row: Any) -> None:
        remote_id = row["remote_id"]
        url = base + ("/" + quote(remote_id, safe="") if remote_id else "")
        conditional = {**headers, **({"If-Match": row["etag"]} if row["etag"] else {})}
        if row["dirty"] == "delete":
            if remote_id:
                response = await client.delete(url, headers=conditional)
                if response.status_code in (409, 412):
                    await self._conflict(row, "changed")
                    return
                if response.status_code not in (404, 410):
                    response.raise_for_status()
            await self.db.execute("DELETE FROM calendar_events WHERE id=? AND version=?", (row["id"], row["version"]))
            return
        method = client.patch if remote_id else client.post
        response = await method(url, headers=conditional, json=_payload(provider, row))
        if response.status_code in (409, 412):
            await self._conflict(row, "changed")
            return
        if remote_id and response.status_code in (404, 410):
            await self._conflict(row, "deleted")
            return
        response.raise_for_status()
        saved = response.json()
        await self.db.execute(
            "UPDATE calendar_events SET remote_id=?,etag=?,dirty='' WHERE id=? AND version=?",
            (saved["id"], saved.get("etag") or saved.get("@odata.etag") or "", row["id"], row["version"]),
        )

    async def _push_instance(self, client: httpx.AsyncClient, provider: str, base: str, headers: dict[str, str], row: Any) -> None:
        master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (row["series_id"],))
        if master is None or not master["remote_id"]:
            return  # The series has not reached the provider yet; this goes with the next sync.
        instance = row["remote_id"]
        if provider == "google":
            instance = instance or master["remote_id"] + "_" + _google_stamp(row["original_start"], bool(master["all_day"]))
            url = base + "/" + quote(instance, safe="")
        else:
            instance = instance or await self._outlook_instance(client, headers, master["remote_id"], row["original_start"])
            if instance is None:
                await self._conflict(row, "deleted")
                return
            url = GRAPH + "events/" + quote(instance, safe="")
        conditional = {**headers, **({"If-Match": row["etag"]} if row["etag"] else {})}
        if row["cancelled"]:
            response = await client.delete(url, headers=conditional)
            if response.status_code in (409, 412):
                await self._conflict(row, "changed")
                return
            if response.status_code not in (404, 410):
                response.raise_for_status()
            await self.db.execute("UPDATE calendar_events SET remote_id=?,etag='',dirty='' WHERE id=? AND version=?", (instance, row["id"], row["version"]))
            return
        response = await client.patch(url, headers=conditional, json=_payload(provider, row))
        if response.status_code in (409, 412):
            await self._conflict(row, "changed")
            return
        if response.status_code in (404, 410):
            await self._conflict(row, "deleted")
            return
        response.raise_for_status()
        saved = response.json()
        await self.db.execute(
            "UPDATE calendar_events SET remote_id=?,etag=?,dirty='' WHERE id=? AND version=?",
            (saved.get("id") or instance, saved.get("etag") or saved.get("@odata.etag") or "", row["id"], row["version"]),
        )

    async def _google_read(self, client: httpx.AsyncClient, account: Any, base: str, headers: dict[str, str]) -> int:
        account_id = account["id"]
        full = not account["cursor"]
        seen: set[str] = set()
        changed = 0
        # singleEvents=false: a series comes as one item with its rule, and only the occurrences that
        # differ come separately (with recurringEventId). With singleEvents=true Google expands every
        # series into instances, and a series pushed from here came back as its master plus every
        # instance — the duplicates this replaced.
        params: dict[str, str] = {"maxResults": "250", "showDeleted": "true", "singleEvents": "false"}
        if account["cursor"]:
            params["syncToken"] = account["cursor"]
        exceptions: list[dict[str, Any]] = []
        while True:
            response = await client.get(base, headers=headers, params=params)
            if response.status_code == 410:
                await self.db.execute("UPDATE calendar_accounts SET cursor='' WHERE id=?", (account_id,))
                return changed + await self._google_read(client, await self.store.account(account_id), base, headers)
            response.raise_for_status()
            data = response.json()
            for item in data.get("items", []):
                remote_id = str(item["id"])
                seen.add(remote_id)
                changed += 1
                if item.get("recurringEventId"):
                    exceptions.append(item)
                    continue
                await self._apply(account_id, remote_id, None if item.get("status") == "cancelled" else _google_event(item))
            page = data.get("nextPageToken")
            if not page:
                break
            params["pageToken"] = page
        # Exceptions after every series is stored: a page can carry an exception before its series.
        for item in exceptions:
            master = await self.db.fetchone("SELECT * FROM calendar_events WHERE account_id=? AND remote_id=? AND series_id IS NULL", (account_id, item["recurringEventId"]))
            if master is None:
                continue
            original_time = item.get("originalStartTime") or {}
            original = _time(original_time.get("dateTime") or original_time.get("date"))
            mapped = None if item.get("status") == "cancelled" else _google_event(item)
            await self._apply_override(master, original, str(item["id"]), mapped)
        if full:
            changed += await self._forget_unseen(account_id, seen)
        await self.db.execute("UPDATE calendar_accounts SET cursor=? WHERE id=?", (data.get("nextSyncToken") or "", account_id))
        return changed

    async def _outlook_instance(self, client: httpx.AsyncClient, headers: dict[str, str], master_remote_id: str, original: str) -> str | None:
        """The id Graph gives one occurrence of a series, found by its original start."""
        moment = datetime.fromisoformat(original)
        params = {"startDateTime": (moment - timedelta(days=1)).isoformat(), "endDateTime": (moment + timedelta(days=2)).isoformat()}
        response = await client.get(GRAPH + "events/" + quote(master_remote_id, safe="") + "/instances", headers=headers, params=params)
        if response.status_code in (404, 410):
            return None
        response.raise_for_status()
        for item in response.json().get("value", []):
            began = item.get("originalStart") or (item.get("start") or {}).get("dateTime")
            if began and _time(began) == original:
                return str(item["id"])
        return None

    async def _outlook_master(self, client: httpx.AsyncClient, headers: dict[str, str], account_id: str, master_remote_id: str, cache: dict[str, Any]) -> Any:
        """The local series for a Graph series id, read from Graph once per sync. calendarView lists
        occurrences only; their series carries the pattern and is fetched by id."""
        if master_remote_id in cache:
            return cache[master_remote_id]
        response = await client.get(GRAPH + "events/" + quote(master_remote_id, safe=""), headers=headers)
        if response.status_code in (404, 410):
            await self._apply(account_id, master_remote_id, None)
            cache[master_remote_id] = None
            return None
        response.raise_for_status()
        cache[master_remote_id] = await self._apply(account_id, master_remote_id, _outlook_event(response.json()))
        return cache[master_remote_id]

    async def _outlook_read(self, client: httpx.AsyncClient, account: Any, base: str, headers: dict[str, str]) -> int:
        account_id = account["id"]
        cursor = json.loads(account["cursor"] or "{}")
        window = datetime.now(UTC)
        full = not cursor or datetime.fromisoformat(cursor["end"]) < window + timedelta(days=90)
        if full:
            cursor = {"end": (window + timedelta(days=730)).isoformat()}
            url = base.rsplit("/events", 1)[0] + "/calendarView/delta" if base != GRAPH + "events" else GRAPH + "calendarView/delta"
            params: dict[str, str] | None = {"startDateTime": (window - timedelta(days=365)).isoformat(), "endDateTime": cursor["end"]}
        else:
            url, params = cursor["url"], None
        seen: set[str] = set()
        masters: dict[str, Any] = {}
        touched: set[str] = set()
        changed = 0
        while url:
            if not url.startswith("https://graph.microsoft.com/v1.0/"):
                raise ValueError("unexpected calendar delta URL")
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            for item in data.get("value", []):
                changed += 1
                remote_id = str(item["id"])
                if "@removed" in item or item.get("isCancelled"):
                    touched |= await self._outlook_removed(account_id, remote_id)
                    continue
                master_remote = item.get("seriesMasterId")
                kind = item.get("type")
                if master_remote and kind in ("occurrence", "exception"):
                    master = await self._outlook_master(client, headers, account_id, master_remote, masters)
                    seen.add(master_remote)
                    if master is None or master["dirty"]:
                        continue
                    original = _time(item.get("originalStart") or (item.get("start") or {}).get("dateTime"))
                    if kind == "occurrence":
                        # A plain occurrence is what the series already expands to; only its id is
                        # kept, so that its removal can be read as a removed occurrence later.
                        await self.db.execute(
                            "INSERT INTO calendar_remote_instances(account_id,remote_id,event_id,original_start) VALUES(?,?,?,?)"
                            " ON CONFLICT(account_id,remote_id) DO UPDATE SET event_id=excluded.event_id, original_start=excluded.original_start",
                            (account_id, remote_id, master["id"], original),
                        )
                        await self.db.execute("DELETE FROM calendar_events WHERE series_id=? AND original_start=? AND dirty='' AND cancelled=0", (master["id"], original))
                    else:
                        seen.add(remote_id)
                        await self._apply_override(master, original, remote_id, _outlook_event(item))
                    continue
                seen.add(remote_id)
                if kind == "seriesMaster":
                    masters[remote_id] = await self._apply(account_id, remote_id, _outlook_event(item))
                else:
                    await self._apply(account_id, remote_id, _outlook_event(item))
            url = data.get("@odata.nextLink") or ""
            if not url and data.get("@odata.deltaLink"):
                cursor["url"] = data["@odata.deltaLink"]
                await self.db.execute("UPDATE calendar_accounts SET cursor=? WHERE id=?", (json.dumps(cursor), account_id))
            params = None
        # A series whose every occurrence was removed may be gone itself; calendarView never lists a
        # series, so ask for it once and drop it when Graph no longer has it.
        for event_id in touched:
            left = await self.db.fetchone("SELECT count(*) AS n FROM calendar_remote_instances WHERE event_id=?", (event_id,))
            master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty=''", (event_id,))
            if master is not None and left is not None and not left["n"] and master["remote_id"] not in masters:
                await self._outlook_master(client, headers, account_id, master["remote_id"], masters)
        if full:
            changed += await self._forget_unseen(account_id, seen)
        return changed

    async def _outlook_removed(self, account_id: str, remote_id: str) -> set[str]:
        row = await self.db.fetchone("SELECT * FROM calendar_events WHERE account_id=? AND remote_id=?", (account_id, remote_id))
        if row is not None:
            if row["dirty"]:
                return set()
            if row["series_id"]:
                await self.db.execute("UPDATE calendar_events SET cancelled=1, version=version+1 WHERE id=?", (row["id"],))
                return {row["series_id"]}
            await self.db.execute("DELETE FROM calendar_events WHERE id=?", (row["id"],))
            return set()
        mapped = await self.db.fetchone("SELECT * FROM calendar_remote_instances WHERE account_id=? AND remote_id=?", (account_id, remote_id))
        if mapped is None:
            return set()
        await self.db.execute("DELETE FROM calendar_remote_instances WHERE account_id=? AND remote_id=?", (account_id, remote_id))
        master = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=? AND dirty=''", (mapped["event_id"],))
        if master is not None:
            exdates = sorted(set(json.loads(master["exdates"] or "[]")) | {mapped["original_start"]})
            await self.db.execute("UPDATE calendar_events SET exdates=?, version=version+1 WHERE id=? AND dirty=''", (json.dumps(exdates), master["id"]))
        return {mapped["event_id"]}

    # -- CalDAV --------------------------------------------------------------------------------

    @staticmethod
    def _dav_url(base: str, href: str) -> str:
        url = urljoin(base, href)
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError("CalDAV is only spoken over https")
        if ".." in parsed.path.split("/"):
            raise ValueError("invalid CalDAV path")
        return url

    @staticmethod
    async def _propfind(client: httpx.AsyncClient, url: str, auth: tuple[str, str], depth: str, props: str) -> Any:
        body = f'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>{props}</d:prop></d:propfind>'
        response = await client.request("PROPFIND", url, auth=auth, headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}, content=body)
        response.raise_for_status()
        return SafeXML.fromstring(response.content)

    async def _discover(self, client: httpx.AsyncClient, server: str, auth: tuple[str, str]) -> str:
        """The address of the account's first event calendar: principal, then its calendar home, then
        the first collection there that holds events (RFC 4791 and 5397)."""
        root = await self._propfind(client, server, auth, "0", "<d:current-user-principal/>")
        principal = root.findtext(f".//{{{DAV}}}current-user-principal/{{{DAV}}}href")
        if not principal and urlparse(server).netloc == "caldav.yandex.ru":
            # Yandex has answered the root without naming a principal; its principals are at a fixed path.
            principal = "/principals/users/" + quote(auth[0], safe="@") + "/"
        if not principal:
            raise ValueError("the CalDAV server did not name the account's principal; give the calendar's address instead")
        principal_url = self._dav_url(server, principal)
        root = await self._propfind(client, principal_url, auth, "0", "<c:calendar-home-set/>")
        home = root.findtext(f".//{{{CAL}}}calendar-home-set/{{{DAV}}}href")
        if not home:
            raise ValueError("the CalDAV server did not return a calendar home")
        home_url = self._dav_url(principal_url, home)
        root = await self._propfind(client, home_url, auth, "1", "<d:resourcetype/><c:supported-calendar-component-set/>")
        for node in root.findall(f"{{{DAV}}}response"):
            if node.find(f".//{{{CAL}}}calendar") is None:
                continue
            components = [item.get("name", "").upper() for item in node.findall(f".//{{{CAL}}}comp")]
            if components and "VEVENT" not in components:
                continue  # A task list (VTODO only) is a calendar collection too.
            href = node.findtext(f"{{{DAV}}}href")
            if href:
                return self._dav_url(home_url, href)
        raise ValueError("the CalDAV account has no event calendar")

    async def _caldav(self, client: httpx.AsyncClient, account: Any) -> int:
        credentials = json.loads(account["credentials_json"])
        auth = (credentials["username"], credentials["password"])
        server = credentials["server_url"].rstrip("/") + "/"
        collection = account["remote_calendar_id"]
        if collection == "primary":
            collection = await self._discover(client, server, auth)
            await self.db.execute("UPDATE calendar_accounts SET remote_calendar_id=? WHERE id=?", (collection, account["id"]))
        url = self._dav_url(server, collection)
        if not url.endswith("/"):
            url += "/"
        await self._caldav_push(client, account["id"], url, auth)
        root = await self._propfind(client, url, auth, "1", "<d:getetag/><d:resourcetype/>")
        seen: set[str] = set()
        changed = 0
        collection_path = urlparse(url).path
        for node in root.findall(f"{{{DAV}}}response"):
            href = node.findtext(f"{{{DAV}}}href") or ""
            if node.find(f".//{{{DAV}}}collection") is not None:
                continue
            entry_url = self._dav_url(url, href)
            remote_id = urlparse(entry_url).path
            if remote_id.rstrip("/") == collection_path.rstrip("/"):
                continue
            seen.add(remote_id)
            etag = node.findtext(f".//{{{DAV}}}getetag") or ""
            old = await self.db.fetchone("SELECT etag FROM calendar_events WHERE account_id=? AND remote_id=? AND series_id IS NULL", (account["id"], remote_id))
            if old is not None and old["etag"] == etag and etag:
                continue
            entry = await client.get(entry_url, auth=auth)
            entry.raise_for_status()
            parsed = Calendar.from_ical(entry.content)
            if await self._apply_resource(account["id"], remote_id, etag, [child for child in parsed.walk() if child.name == "VEVENT"]):
                changed += 1
        for row in await self.db.fetchall("SELECT id,remote_id FROM calendar_events WHERE account_id=? AND remote_id IS NOT NULL AND series_id IS NULL AND dirty=''", (account["id"],)):
            if row["remote_id"] not in seen:
                await self.db.execute("DELETE FROM calendar_events WHERE id=?", (row["id"],))
                changed += 1
        return changed

    async def _caldav_push(self, client: httpx.AsyncClient, account_id: str, url: str, auth: tuple[str, str]) -> None:
        # A series and its exceptions are one resource, so a change to either sends the series.
        pending = await self.db.fetchall("SELECT id, series_id FROM calendar_events WHERE account_id=? AND dirty!='' AND conflict='' ORDER BY updated_at", (account_id,))
        roots = list(dict.fromkeys(row["series_id"] or row["id"] for row in pending))
        for root_id in roots:
            row = await self.db.fetchone("SELECT * FROM calendar_events WHERE id=?", (root_id,))
            if row is None or row["conflict"]:
                continue
            overrides = await self.db.fetchall("SELECT * FROM calendar_events WHERE series_id=?", (root_id,))
            event_url = self._dav_url(url, row["remote_id"]) if row["remote_id"] else url + row["id"] + ".ics"
            headers = {"If-Match": row["etag"]} if row["etag"] else ({"If-None-Match": "*"} if not row["remote_id"] else {})
            if row["dirty"] == "delete":
                response = await client.delete(event_url, auth=auth, headers=headers)
                if response.status_code in (409, 412):
                    await self._conflict(row, "changed")
                    continue
                if response.status_code not in (404, 410):
                    response.raise_for_status()
                await self.db.execute("DELETE FROM calendar_events WHERE id=? AND version=?", (row["id"], row["version"]))
                continue
            response = await client.put(event_url, auth=auth, headers={**headers, "Content-Type": "text/calendar; charset=utf-8"}, content=_ical(row, list(overrides)))
            if response.status_code in (409, 412):
                await self._conflict(row, "changed")
                continue
            if row["remote_id"] and response.status_code in (404, 410):
                await self._conflict(row, "deleted")
                continue
            response.raise_for_status()
            await self.db.execute(
                "UPDATE calendar_events SET remote_id=?,etag=?,dirty='' WHERE id=? AND version=?",
                (urlparse(event_url).path, response.headers.get("etag", ""), row["id"], row["version"]),
            )
            for override in overrides:
                await self.db.execute("UPDATE calendar_events SET dirty='' WHERE id=? AND version=?", (override["id"], override["version"]))

    # -- ICS subscriptions ---------------------------------------------------------------------

    async def _fetch_feed(self, client: httpx.AsyncClient, url: str, cursor: dict[str, str]) -> tuple[bytes | None, dict[str, str]]:
        """The feed's bytes, or ``None`` when it has not changed since ``cursor``. Redirects are
        followed by hand so that each hop is checked for https, and the body is read with a cap
        so that a feed address pointing at something enormous costs five megabytes, not the memory."""
        headers = {"Accept": "text/calendar"}
        if cursor.get("etag"):
            headers["If-None-Match"] = cursor["etag"]
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = cursor["last_modified"]
        for _ in range(4):
            if urlparse(url).scheme != "https":
                raise ValueError("a calendar subscription is only read over https")
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code == 304:
                    return None, cursor
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > ICS_MAX_BYTES:
                        raise ValueError("the calendar feed is larger than five megabytes")
                return bytes(body), {"etag": response.headers.get("etag", ""), "last_modified": response.headers.get("last-modified", "")}
        raise ValueError("the calendar feed redirects too many times")

    async def _ics(self, client: httpx.AsyncClient, account: Any) -> int:
        credentials = json.loads(account["credentials_json"])
        cursor = json.loads(account["cursor"] or "{}")
        body, cursor = await self._fetch_feed(client, credentials["url"], cursor)
        if body is None:
            return 0
        parsed = Calendar.from_ical(body)
        groups: dict[str, list[Any]] = {}
        for child in parsed.walk():
            if child.name != "VEVENT":
                continue
            uid = str(child.get("UID") or "") or hashlib.sha256((str(child.get("SUMMARY")) + str(child.get("DTSTART").to_ical() if child.get("DTSTART") else "")).encode()).hexdigest()
            groups.setdefault(uid, []).append(child)
            if len(groups) > ICS_MAX_EVENTS:
                raise ValueError(f"the calendar feed has more than {ICS_MAX_EVENTS} events")
        changed = 0
        for uid, components in groups.items():
            if await self._apply_resource(account["id"], uid, "", components):
                changed += 1
        changed += await self._forget_unseen(account["id"], set(groups))
        await self.db.execute("UPDATE calendar_accounts SET cursor=? WHERE id=?", (json.dumps(cursor), account["id"]))
        return changed


async def sync_due(sync: CalendarSync) -> None:
    """Sync every account whose turn has come; a failing account waits longer each time."""
    for row in await sync.db.fetchall("SELECT id FROM calendar_accounts WHERE next_sync_at IS NULL OR next_sync_at <= ?", (now(),)):
        try:
            await sync.sync(row["id"])
        except Exception:
            logger.warning("calendar sync failed for %s", row["id"], exc_info=True)


async def sync_loop(store: CalendarStore, stopping: asyncio.Event) -> None:
    sync = CalendarSync(store)
    while not stopping.is_set():
        await store.db.execute("DELETE FROM kv WHERE key LIKE 'calendar_oauth:%' AND json_extract(value, '$.expires_at') < ?", (now(),))
        await sync_due(sync)
        try:
            await asyncio.wait_for(stopping.wait(), LOOP_SECONDS)
        except TimeoutError:
            pass


__all__ = ["CalendarSync", "backoff", "sync_due", "sync_loop"]
