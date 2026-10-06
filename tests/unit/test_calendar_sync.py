"""Calendar synchronization against fake providers: no request leaves the process."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from icalendar import Calendar, Event

from daedalus.extensions import calendar_sync
from daedalus.extensions.calendar_sync import CalendarSync, sync_due
from daedalus.stores.calendar import CalendarStore
from daedalus.stores.database import Database

OAUTH = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "sync.sqlite")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


def _client(reply):
    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


def _fake_network(monkeypatch, reply):
    """``CalendarSync.sync`` opens its own client; this hands it one that answers from ``reply``."""
    real = httpx.AsyncClient
    monkeypatch.setattr(calendar_sync.httpx, "AsyncClient", lambda **_: real(transport=httpx.MockTransport(reply)))


def _titles_and_starts(items):
    return [(item["title"], item["start_at"]) for item in items]


@pytest.mark.asyncio
async def test_google_reads_a_series_once_with_its_exceptions(db):
    store = CalendarStore(db)
    account = await store.connect("google", "Work", OAUTH)
    made = await store.create({"calendar_id": account["calendar_id"], "title": "Weekly", "start_at": "2026-10-05T08:00:00Z", "end_at": "2026-10-05T09:00:00Z", "timezone": "Europe/Berlin", "recurrence": "FREQ=WEEKLY;COUNT=4"})
    assert made["pending_sync"]
    reads = []

    def reply(request):
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=4"] and body["start"]["timeZone"] == "Europe/Berlin"
            return httpx.Response(200, json={"id": "series-1", "etag": '"1"'})
        reads.append(request.url.params)
        return httpx.Response(200, json={"nextSyncToken": "t1", "items": [
            {"id": "series-1", "etag": '"1"', "summary": "Weekly", "start": {"dateTime": "2026-10-05T10:00:00+02:00", "timeZone": "Europe/Berlin"}, "end": {"dateTime": "2026-10-05T11:00:00+02:00", "timeZone": "Europe/Berlin"},
             "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE;TZID=Europe/Berlin:20261019T100000"]},
            {"id": "series-1_20261012T080000Z", "recurringEventId": "series-1", "originalStartTime": {"dateTime": "2026-10-12T10:00:00+02:00", "timeZone": "Europe/Berlin"},
             "summary": "Weekly (moved)", "start": {"dateTime": "2026-10-12T15:00:00+02:00", "timeZone": "Europe/Berlin"}, "end": {"dateTime": "2026-10-12T16:00:00+02:00", "timeZone": "Europe/Berlin"}},
            {"id": "series-1_20261026T090000Z", "recurringEventId": "series-1", "status": "cancelled", "originalStartTime": {"dateTime": "2026-10-26T10:00:00+01:00", "timeZone": "Europe/Berlin"}},
            {"id": "lunch", "summary": "Lunch", "start": {"dateTime": "2026-10-06T12:00:00Z"}, "end": {"dateTime": "2026-10-06T13:00:00Z"}},
        ]})

    async with _client(reply) as client:
        await CalendarSync(store)._json_provider(client, await store.account(account["id"]), "token")
    assert reads[0]["singleEvents"] == "false" and reads[0]["showDeleted"] == "true"
    found = await store.occurrences("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert _titles_and_starts(found) == [("Weekly", "2026-10-05T08:00:00+00:00"), ("Lunch", "2026-10-06T12:00:00+00:00"), ("Weekly (moved)", "2026-10-12T13:00:00+00:00")]
    assert not any(item["pending_sync"] for item in found)
    rows = await db.fetchall("SELECT remote_id, series_id IS NOT NULL AS exception, cancelled FROM calendar_events WHERE account_id=? ORDER BY remote_id", (account["id"],))
    # The series is one row (the one made here, now acknowledged); occurrences are not imported as events.
    assert [(row["remote_id"], row["exception"], row["cancelled"]) for row in rows] == [("lunch", 0, 0), ("series-1", 0, 0), ("series-1_20261012T080000Z", 1, 0), ("series-1_20261026T090000Z", 1, 1)]
    assert (await store.get(made["event_id"]))["exdates"] == ["2026-10-19T08:00:00+00:00"]


@pytest.mark.asyncio
async def test_google_sends_one_occurrence_as_its_instance(db):
    store = CalendarStore(db)
    account = await store.connect("google", "Work", OAUTH)
    made = await store.create({"calendar_id": account["calendar_id"], "title": "Sync", "start_at": "2026-10-05T08:00:00Z", "end_at": "2026-10-05T09:00:00Z", "recurrence": "FREQ=WEEKLY"})
    await db.execute("UPDATE calendar_events SET remote_id='series-1', dirty='', etag='\"1\"' WHERE id=?", (made["event_id"],))
    # Past the first read: an incremental one that finds nothing new must leave both rows alone.
    await db.execute("UPDATE calendar_accounts SET cursor='t1' WHERE id=?", (account["id"],))
    version = (await store.get(made["event_id"]))["version"]
    await store.update(made["event_id"], {"scope": "this", "occurrence_start": "2026-10-12T08:00:00Z", "version": version, "title": "Kickoff"})
    await store.delete(made["event_id"], version, "this", "2026-10-19T08:00:00Z")
    sent = []

    def reply(request):
        sent.append((request.method, request.url.path))
        if request.method == "PATCH":
            assert json.loads(request.content)["summary"] == "Kickoff" and "recurrence" not in json.loads(request.content)
            return httpx.Response(200, json={"id": "series-1_20261012T080000Z", "etag": '"2"'})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"items": [], "nextSyncToken": "t2"})

    async with _client(reply) as client:
        await CalendarSync(store)._json_provider(client, await store.account(account["id"]), "token")
    assert ("PATCH", "/calendar/v3/calendars/primary/events/series-1_20261012T080000Z") in sent
    assert ("DELETE", "/calendar/v3/calendars/primary/events/series-1_20261019T080000Z") in sent
    assert await db.fetchone("SELECT 1 FROM calendar_events WHERE dirty!=''") is None
    found = await store.occurrences("2026-10-10T00:00:00Z", "2026-10-25T00:00:00Z")
    assert _titles_and_starts(found) == [("Kickoff", "2026-10-12T08:00:00+00:00")]


@pytest.mark.asyncio
async def test_a_conflict_is_kept_on_its_event_and_the_rest_still_syncs(db, monkeypatch):
    store = CalendarStore(db)
    account = await store.connect("google", "Work", OAUTH)
    first = await store.create({"calendar_id": account["calendar_id"], "title": "Review", "start_at": "2026-10-05T08:00:00Z", "end_at": "2026-10-05T09:00:00Z"})
    second = await store.create({"calendar_id": account["calendar_id"], "title": "Retro", "start_at": "2026-10-05T10:00:00Z", "end_at": "2026-10-05T11:00:00Z"})
    await db.execute("UPDATE calendar_events SET remote_id='r1', etag='\"old\"', dirty='update' WHERE id=?", (first["event_id"],))
    await db.execute("UPDATE calendar_events SET remote_id='r2', etag='\"two\"', dirty='update' WHERE id=?", (second["event_id"],))
    await db.execute("UPDATE calendar_accounts SET cursor='t0' WHERE id=?", (account["id"],))
    matches = []

    def reply(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access"})
        if request.method == "PATCH":
            matches.append((request.url.path.rsplit("/", 1)[-1], request.headers.get("If-Match")))
            if request.url.path.endswith("/r1") and request.headers.get("If-Match"):
                return httpx.Response(412)
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1], "etag": '"new"'})
        return httpx.Response(200, json={"items": [], "nextSyncToken": "t1"})

    _fake_network(monkeypatch, reply)
    sync = CalendarSync(store)
    result = await sync.sync(account["id"])
    assert result["conflicts"] == 1
    assert (await store.get(second["event_id"]))["pending_sync"] is False
    disputed = await store.get(first["event_id"])
    assert disputed["conflict"] and disputed["conflict_kind"] == "changed" and disputed["dirty"] == "update"
    calendar = next(item for item in await store.calendars() if item["account_id"] == account["id"])
    assert calendar["sync"]["status"] == "ok" and calendar["sync"]["conflicts"] == 1 and calendar["sync"]["error"] is None
    assert [item["conflict"] for item in await store.occurrences("2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")] == [True, False]
    assert (await store.accounts())[0]["conflicts"] == 1
    # A conflicted event is not sent again until the operator chooses.
    await sync.sync(account["id"])
    assert [name for name, _ in matches].count("r1") == 1
    await sync.resolve(first["event_id"], "local")
    assert matches[-1] == ("r1", None)
    assert (await store.get(first["event_id"]))["conflict"] is False and (await store.get(first["event_id"]))["dirty"] == ""


@pytest.mark.asyncio
async def test_a_failing_account_backs_off_and_waits_its_turn(db, monkeypatch):
    store = CalendarStore(db)
    account = await store.connect("google", "Work", OAUTH)
    attempts = []

    def reply(request):
        attempts.append(request.url.host)
        return httpx.Response(500)

    _fake_network(monkeypatch, reply)
    sync = CalendarSync(store)
    before = datetime.now(UTC)
    await sync_due(sync)
    row = await store.account(account["id"])
    assert row["failures"] == 1 and row["sync_error"]
    assert timedelta(seconds=110) < datetime.fromisoformat(row["next_sync_at"]) - before < timedelta(seconds=130)
    calendar = next(item for item in await store.calendars() if item["account_id"] == account["id"])
    assert calendar["sync"]["status"] == "error" and calendar["sync"]["failures"] == 1
    await sync_due(sync)
    assert len(attempts) == 1
    await db.execute("UPDATE calendar_accounts SET next_sync_at=? WHERE id=?", ((before - timedelta(seconds=1)).isoformat(), account["id"]))
    await sync_due(sync)
    row = await store.account(account["id"])
    assert len(attempts) == 2 and row["failures"] == 2
    assert timedelta(seconds=230) < datetime.fromisoformat(row["next_sync_at"]) - before < timedelta(seconds=250)
    assert calendar_sync.backoff(30) == calendar_sync.BACKOFF_MAX_SECONDS


@pytest.mark.asyncio
async def test_outlook_series_come_from_their_master_and_occurrences_are_not_duplicated(db):
    store = CalendarStore(db)
    account = await store.connect("outlook", "Work", OAUTH)
    masters = []

    def occurrence(remote_id, start, kind="occurrence", subject="Sync", moved=None):
        begins = moved or start
        return {"id": remote_id, "type": kind, "seriesMasterId": "master-1", "originalStart": start + "Z", "subject": subject,
                "start": {"dateTime": begins + ".0000000", "timeZone": "UTC"}, "end": {"dateTime": begins[:11] + str(int(begins[11:13]) + 1).zfill(2) + begins[13:] + ".0000000", "timeZone": "UTC"}}

    def reply(request):
        url = str(request.url)
        if url.endswith("/me/events/master-1"):
            masters.append(url)
            return httpx.Response(200, json={
                "id": "master-1", "type": "seriesMaster", "subject": "Sync", "start": {"dateTime": "2026-10-05T08:00:00.0000000", "timeZone": "UTC"}, "end": {"dateTime": "2026-10-05T09:00:00.0000000", "timeZone": "UTC"},
                "recurrence": {"pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday"], "firstDayOfWeek": "monday"},
                               "range": {"type": "numbered", "numberOfOccurrences": 4, "startDate": "2026-10-05", "recurrenceTimeZone": "W. Europe Standard Time"}},
            })
        if "$deltatoken=next" in url:
            return httpx.Response(200, json={"value": [{"id": "occ-2", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=after"})
        if "calendarView/delta" in url:
            return httpx.Response(200, json={"value": [
                occurrence("occ-1", "2026-10-05T08:00:00"),
                occurrence("occ-2", "2026-10-12T08:00:00"),
                occurrence("exc-3", "2026-10-19T08:00:00", "exception", "Moved", "2026-10-19T12:00:00"),
                {"id": "single-1", "type": "singleInstance", "subject": "Lunch", "start": {"dateTime": "2026-10-06T12:00:00.0000000"}, "end": {"dateTime": "2026-10-06T13:00:00.0000000"}},
            ], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=next"})
        raise AssertionError(url)

    sync = CalendarSync(store)
    async with _client(reply) as client:
        await sync._json_provider(client, await store.account(account["id"]), "token")
        found = await store.occurrences("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
        # Berlin's clocks go back on 25 October: the series' last occurrence is an hour later in UTC.
        assert _titles_and_starts(found) == [
            ("Sync", "2026-10-05T08:00:00+00:00"), ("Lunch", "2026-10-06T12:00:00+00:00"), ("Sync", "2026-10-12T08:00:00+00:00"),
            ("Moved", "2026-10-19T12:00:00+00:00"), ("Sync", "2026-10-26T09:00:00+00:00"),
        ]
        assert len(masters) == 1
        assert (await db.fetchone("SELECT count(*) FROM calendar_events WHERE account_id=?", (account["id"],)))[0] == 3
        await sync._json_provider(client, await store.account(account["id"]), "token")
    master = await db.fetchone("SELECT * FROM calendar_events WHERE remote_id='master-1'")
    assert json.loads(master["exdates"]) == ["2026-10-12T08:00:00+00:00"] and master["timezone"] == "Europe/Berlin"
    assert len(masters) == 1


@pytest.mark.asyncio
async def test_outlook_receives_a_pattern_in_the_events_zone_and_refuses_what_it_cannot_hold(db):
    store = CalendarStore(db)
    account = await store.connect("outlook", "Work", OAUTH)
    with pytest.raises(ValueError, match="Outlook"):
        await store.create({"calendar_id": account["calendar_id"], "title": "Hourly", "start_at": "2026-10-05T08:00:00Z", "end_at": "2026-10-05T08:30:00Z", "recurrence": "FREQ=HOURLY"})
    await store.create({"calendar_id": account["calendar_id"], "title": "Planning", "start_at": "2026-10-05T08:00:00Z", "end_at": "2026-10-05T09:00:00Z", "timezone": "Europe/Berlin", "recurrence": "FREQ=MONTHLY;BYDAY=1MO;UNTIL=20261231T000000Z"})
    sent = []

    def reply(request):
        if request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "m", "@odata.etag": "e"})
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=x"})

    async with _client(reply) as client:
        await CalendarSync(store)._json_provider(client, await store.account(account["id"]), "token")
    body = sent[0]
    assert body["start"] == {"dateTime": "2026-10-05T10:00:00", "timeZone": "Europe/Berlin"}
    assert body["recurrence"]["pattern"] == {"interval": 1, "type": "relativeMonthly", "daysOfWeek": ["monday"], "index": "first"}
    assert body["recurrence"]["range"] == {"type": "endDate", "startDate": "2026-10-05", "recurrenceTimeZone": "Europe/Berlin", "endDate": "2026-12-31"}


def _ics(*events) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//test//EN")
    calendar.add("version", "2.0")
    for fields in events:
        event = Event()
        for key, value in fields.items():
            event.add(key, value)
        calendar.add_component(event)
    return calendar.to_ical()


@pytest.mark.asyncio
async def test_caldav_discovers_the_calendar_and_keeps_series_in_one_resource(db):
    store = CalendarStore(db)
    account = await store.connect("caldav", "Home", {"server_url": "https://dav.example.invalid", "username": "someone", "password": "secret"})
    series = await store.create({"calendar_id": account["calendar_id"], "title": "Piano", "start_at": "2026-10-05T16:00:00Z", "end_at": "2026-10-05T17:00:00Z", "timezone": "Europe/Berlin", "recurrence": "FREQ=WEEKLY;COUNT=3"})
    await store.update(series["event_id"], {"scope": "this", "occurrence_start": "2026-10-12T16:00:00Z", "version": series["version"], "title": "Piano recital"})
    await store.delete(series["event_id"], None, "this", "2026-10-19T16:00:00Z")
    moscow = ZoneInfo("Europe/Moscow")
    remote = _ics(
        {"uid": "remote@example.invalid", "summary": "Remote", "dtstart": datetime(2026, 10, 6, 10, tzinfo=moscow), "dtend": datetime(2026, 10, 6, 11, tzinfo=moscow), "rrule": {"freq": "daily", "count": 3}, "exdate": datetime(2026, 10, 8, 10, tzinfo=moscow)},
        {"uid": "remote@example.invalid", "summary": "Remote moved", "recurrence-id": datetime(2026, 10, 7, 10, tzinfo=moscow), "dtstart": datetime(2026, 10, 7, 15, tzinfo=moscow), "dtend": datetime(2026, 10, 7, 16, tzinfo=moscow)},
    )
    put = []

    def multistatus(*responses):
        return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(responses) + "</d:multistatus>")

    def reply(request):
        path = request.url.path
        if request.method == "PROPFIND" and path == "/":
            return multistatus("<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/principals/someone/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>")
        if request.method == "PROPFIND" and path == "/principals/someone/":
            return multistatus("<d:response><d:href>/principals/someone/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/calendars/someone/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>")
        if request.method == "PROPFIND" and path == "/calendars/someone/":
            return multistatus(
                "<d:response><d:href>/calendars/someone/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>",
                '<d:response><d:href>/calendars/someone/todo/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>',
                '<d:response><d:href>/calendars/someone/work/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>',
            )
        if request.method == "PUT":
            put.append((path, request.headers.get("If-None-Match"), request.content))
            return httpx.Response(201, headers={"etag": '"local"'})
        if request.method == "PROPFIND" and path == "/calendars/someone/work/":
            return multistatus(
                "<d:response><d:href>/calendars/someone/work/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype></d:prop></d:propstat></d:response>",
                f'<d:response><d:href>/calendars/someone/work/{series["event_id"]}.ics</d:href><d:propstat><d:prop><d:getetag>"local"</d:getetag></d:prop></d:propstat></d:response>',
                '<d:response><d:href>/calendars/someone/work/remote.ics</d:href><d:propstat><d:prop><d:getetag>"remote"</d:getetag></d:prop></d:propstat></d:response>',
            )
        if request.method == "GET" and path == "/calendars/someone/work/remote.ics":
            return httpx.Response(200, content=remote)
        raise AssertionError((request.method, path))

    async with _client(reply) as client:
        assert await CalendarSync(store)._caldav(client, await store.account(account["id"])) == 1
    assert (await store.account(account["id"]))["remote_calendar_id"] == "https://dav.example.invalid/calendars/someone/work/"
    assert len(put) == 1 and put[0][0] == f"/calendars/someone/work/{series['event_id']}.ics" and put[0][1] == "*"
    sent = put[0][2].decode()
    assert "RRULE:FREQ=WEEKLY;COUNT=3" in sent and "EXDATE;TZID=Europe/Berlin:20261019T180000" in sent
    assert "RECURRENCE-ID;TZID=Europe/Berlin:20261012T180000" in sent and "SUMMARY:Piano recital" in sent and "BEGIN:VTIMEZONE" in sent
    assert await db.fetchone("SELECT 1 FROM calendar_events WHERE dirty!=''") is None
    found = await store.occurrences("2026-10-05T00:00:00Z", "2026-10-20T00:00:00Z")
    assert _titles_and_starts(found) == [
        ("Piano", "2026-10-05T16:00:00+00:00"), ("Remote", "2026-10-06T07:00:00+00:00"), ("Remote moved", "2026-10-07T12:00:00+00:00"), ("Piano recital", "2026-10-12T16:00:00+00:00"),
    ]
    remote_row = await db.fetchone("SELECT timezone FROM calendar_events WHERE remote_id='/calendars/someone/work/remote.ics'")
    assert remote_row["timezone"] == "Europe/Moscow"


@pytest.mark.asyncio
async def test_a_subscription_is_polled_conditionally_over_https_only(db, monkeypatch):
    store = CalendarStore(db)
    feed = await store.subscribe("Holidays", "https://feeds.example.invalid/holidays.ics")
    body = _ics(
        {"uid": "new-year@example.invalid", "summary": "New Year", "dtstart": date(2026, 1, 1), "dtend": date(2026, 1, 2), "rrule": {"freq": "yearly"}},
        {"uid": "talk@example.invalid", "summary": "Talk", "dtstart": datetime(2026, 10, 7, 9, tzinfo=UTC)},
    )
    seen_headers = []
    answers = iter([
        httpx.Response(302, headers={"location": "https://cdn.example.invalid/holidays.ics"}),
        httpx.Response(200, headers={"etag": '"v1"'}, content=body),
        httpx.Response(304),
        httpx.Response(302, headers={"location": "http://cdn.example.invalid/holidays.ics"}),
    ])

    def reply(request):
        seen_headers.append(dict(request.headers))
        return next(answers)

    sync = CalendarSync(store)
    async with _client(reply) as client:
        assert await sync._ics(client, await store.account(feed["id"])) == 2
        assert await sync._ics(client, await store.account(feed["id"])) == 0
        assert seen_headers[2]["if-none-match"] == '"v1"'
        with pytest.raises(ValueError, match="https"):
            await sync._ics(client, await store.account(feed["id"]))
    found = await store.occurrences("2026-10-01T00:00:00Z", "2027-01-05T00:00:00Z", [feed["calendar_id"]])
    # A date-time without an end is an instant; it still shows in its range.
    assert [(item["title"], item["start_at"], item["writable"]) for item in found] == [("Talk", "2026-10-07T09:00:00+00:00", False), ("New Year", "2027-01-01T00:00:00+00:00", False)]
    with pytest.raises(ValueError, match="read-only"):
        await store.update(found[0]["event_id"], {"version": found[0]["version"], "title": "Mine"})
    monkeypatch.setattr(calendar_sync, "ICS_MAX_BYTES", 100)
    await db.execute("UPDATE calendar_accounts SET cursor='' WHERE id=?", (feed["id"],))
    async with _client(lambda request: httpx.Response(200, content=body)) as client:
        with pytest.raises(ValueError, match="larger"):
            await sync._ics(client, await store.account(feed["id"]))
