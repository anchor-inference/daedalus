"""The calendar as a planner: series expansion, occurrence edits, calendars, tasks, reminders, routes."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.tools import ToolContext

from daedalus.extensions import calendar_reminders
from daedalus.extensions.api_calendar import register
from daedalus.stores import database as database_module
from daedalus.stores import recurrence
from daedalus.stores.calendar import CalendarStore
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.planner import PlannerStore


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "calendar.sqlite")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def _berlin(store: CalendarStore) -> None:
    await store.save_settings({"timezone": "Europe/Berlin"})


def _starts(items):
    return [item["start_at"] for item in items]


@pytest.mark.asyncio
async def test_weekly_series_keeps_its_wall_clock_across_daylight_saving(db):
    store = CalendarStore(db)
    made = await store.create({"title": "Standup", "start_at": "2026-10-19T10:00:00+02:00", "end_at": "2026-10-19T10:30:00+02:00", "timezone": "Europe/Berlin", "recurrence": "FREQ=WEEKLY;BYDAY=MO"})
    assert made["recurring"] and made["occurrence_start"] == "2026-10-19T08:00:00+00:00"
    found = await store.occurrences("2026-10-18T00:00:00Z", "2026-11-03T00:00:00Z")
    # Berlin leaves summer time on 25 October: the meeting stays at 10:00 local, one hour later in UTC.
    assert _starts(found) == ["2026-10-19T08:00:00+00:00", "2026-10-26T09:00:00+00:00", "2026-11-02T09:00:00+00:00"]
    assert len({item["id"] for item in found}) == 3
    assert all(item["event_id"] == made["event_id"] and item["calendar_name"] == "Personal" for item in found)
    assert found[0]["reminders"] == [10] and found[0]["color"] == "#4f7cff"


@pytest.mark.asyncio
async def test_one_occurrence_is_moved_or_removed_without_touching_the_series(db):
    store = CalendarStore(db)
    series = await store.create({"title": "Gym", "start_at": "2026-10-05T07:00:00Z", "end_at": "2026-10-05T08:00:00Z", "recurrence": "RRULE:FREQ=DAILY;COUNT=5"})
    event_id = series["event_id"]
    await store.delete(event_id, series["version"], "this", "2026-10-06T07:00:00Z")
    master = await store.get(event_id)
    assert master["exdates"] == ["2026-10-06T07:00:00+00:00"]
    with pytest.raises(RuntimeError, match="changed elsewhere"):
        await store.update(event_id, {"scope": "this", "occurrence_start": "2026-10-07T07:00:00+00:00", "version": series["version"], "title": "Stale"})
    moved = await store.update(event_id, {"scope": "this", "occurrence_start": "2026-10-07T07:00:00+00:00", "version": master["version"], "start_at": "2026-10-12T18:00:00Z", "end_at": "2026-10-12T19:00:00Z", "title": "Gym (evening)"})
    assert moved["exception"] and moved["id"] == f"{event_id}:2026-10-07T07:00:00+00:00"
    week = await store.occurrences("2026-10-05T00:00:00Z", "2026-10-10T00:00:00Z")
    assert _starts(week) == ["2026-10-05T07:00:00+00:00", "2026-10-08T07:00:00+00:00", "2026-10-09T07:00:00+00:00"]
    # The moved occurrence shows where it went, a week past the end of its series.
    later = await store.occurrences("2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z")
    assert [(item["title"], item["occurrence_start"]) for item in later] == [("Gym (evening)", "2026-10-07T07:00:00+00:00")]
    # Editing the override again is checked against the override's own version.
    again = await store.update(later[0]["id"], {"scope": "this", "version": later[0]["version"], "location": "Park"})
    assert again["location"] == "Park" and again["title"] == "Gym (evening)"
    await store.delete(event_id, again["version"], "this", "2026-10-07T07:00:00Z")
    assert await store.occurrences("2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z") == []
    with pytest.raises(ValueError, match="not an occurrence"):
        await store.update(event_id, {"scope": "this", "occurrence_start": "2026-10-05T09:00:00Z", "version": 99, "title": "Nowhere"})


@pytest.mark.asyncio
async def test_editing_the_whole_series_from_an_occurrence_moves_it_and_drops_stale_exceptions(db):
    store = CalendarStore(db)
    series = await store.create({"title": "Review", "start_at": "2026-10-05T09:00:00Z", "end_at": "2026-10-05T10:00:00Z", "recurrence": "FREQ=DAILY;COUNT=3"})
    override = await store.update(series["event_id"], {"scope": "this", "occurrence_start": "2026-10-06T09:00:00Z", "version": series["version"], "title": "Special"})
    master = await store.get(series["event_id"])
    assert len(master["overrides"]) == 1 and override["title"] == "Special"
    # Dragged on the 7th from 09:00 to 11:00 and lengthened to 90 minutes, for all occurrences.
    await store.update(series["event_id"], {"scope": "all", "occurrence_start": "2026-10-07T09:00:00Z", "version": master["version"], "start_at": "2026-10-07T11:00:00Z", "end_at": "2026-10-07T12:30:00Z"})
    found = await store.occurrences("2026-10-05T00:00:00Z", "2026-10-08T00:00:00Z")
    assert [(item["start_at"], item["end_at"], item["title"]) for item in found] == [
        ("2026-10-05T11:00:00+00:00", "2026-10-05T12:30:00+00:00", "Review"),
        ("2026-10-06T11:00:00+00:00", "2026-10-06T12:30:00+00:00", "Review"),
        ("2026-10-07T11:00:00+00:00", "2026-10-07T12:30:00+00:00", "Review"),
    ]
    assert (await store.get(series["event_id"]))["overrides"] == []


@pytest.mark.asyncio
async def test_all_day_series_repeats_over_dates_and_ends(db):
    store = CalendarStore(db)
    birthday = await store.create({"title": "Birthday", "all_day": True, "start_date": "2026-03-01", "end_date": "2026-03-02", "timezone": "Asia/Almaty", "recurrence": "FREQ=YEARLY;UNTIL=20280301"})
    assert birthday["start_date"] == "2026-03-01" and birthday["end_date"] == "2026-03-02"
    found = await store.occurrences("2026-01-01T00:00:00Z", "2027-01-30T00:00:00Z")
    assert [item["start_date"] for item in found] == ["2026-03-01"]
    stored = await store.get(birthday["event_id"])
    row = await db.fetchone("SELECT recurrence_end FROM calendar_events WHERE id=?", (stored["id"],))
    assert row["recurrence_end"] == "2028-03-02T00:00:00+00:00"
    assert await store.occurrences("2028-06-01T00:00:00Z", "2029-06-01T00:00:00Z") == []


@pytest.mark.asyncio
async def test_hidden_calendars_ranges_and_the_last_local_calendar(db):
    store = CalendarStore(db)
    work = await store.create_calendar({"name": "Work", "color": "#AA3300"})
    assert work["color"] == "#aa3300" and work["kind"] == "local" and work["sync"] is None
    await store.create({"calendar_id": work["id"], "title": "Sprint", "start_at": "2026-10-05T09:00:00Z", "end_at": "2026-10-05T10:00:00Z", "color": "#00ff00"})
    await store.create({"title": "Dentist", "start_at": "2026-10-05T12:00:00Z", "end_at": "2026-10-05T13:00:00Z"})
    await store.update_calendar(work["id"], {"visible": False})
    assert [item["title"] for item in await store.occurrences("2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")] == ["Dentist"]
    named = await store.occurrences("2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z", [work["id"]])
    assert [(item["title"], item["color"]) for item in named] == [("Sprint", "#00ff00")]
    with pytest.raises(ValueError, match="at most"):
        await store.occurrences("2026-01-01T00:00:00Z", "2027-06-01T00:00:00Z")
    with pytest.raises(ValueError, match="#rrggbb"):
        await store.update_calendar(work["id"], {"color": "red"})
    await store.delete_calendar(work["id"])
    assert await db.fetchone("SELECT 1 FROM calendar_events WHERE title='Sprint'") is None
    personal = (await store.calendars())[0]
    with pytest.raises(ValueError, match="last local calendar"):
        await store.delete_calendar(personal["id"])


@pytest.mark.asyncio
async def test_search_finds_series_and_events_nearest_first(db):
    store = CalendarStore(db)
    today = datetime.now(UTC).replace(microsecond=0)
    await store.create({"title": "Yoga far away", "start_at": (today + timedelta(days=200)).isoformat(), "end_at": (today + timedelta(days=200, hours=1)).isoformat()})
    await store.create({"title": "Weekly yoga", "start_at": (today - timedelta(days=60)).isoformat(), "end_at": (today - timedelta(days=60) + timedelta(hours=1)).isoformat(), "recurrence": "FREQ=WEEKLY"})
    await store.create({"title": "Lunch", "location": "Yoga studio cafe", "start_at": (today + timedelta(days=30)).isoformat(), "end_at": (today + timedelta(days=30, hours=1)).isoformat()})
    found = await store.search("yoga")
    assert [item["title"] for item in found] == ["Weekly yoga", "Lunch", "Yoga far away"]
    assert found[0]["start_at"] >= (today - timedelta(seconds=1)).isoformat()
    assert await store.search("100%_") == []


@pytest.mark.asyncio
async def test_settings_take_the_browsers_zone_once_and_check_their_values(db):
    store = CalendarStore(db, lambda: "Europe/Moscow")
    settings = await store.settings()
    assert settings["timezone"] == "Europe/Moscow" and settings["week_start"] == 1 and settings["default_reminders"] == [10]
    assert (await CalendarStore(db).settings())["timezone"] == "Europe/Moscow"
    saved = await store.save_settings({"work_start": "08:30", "default_view": "day"})
    assert saved["work_start"] == "08:30" and saved["default_view"] == "day"
    for bad in ({"work_end": "07:00"}, {"timezone": "Mars/Base"}, {"default_view": "year"}, {"week_start": 3}):
        with pytest.raises(ValueError):
            await store.save_settings(bad)


@pytest.mark.asyncio
async def test_task_views_follow_the_operators_day(db):
    calendar = CalendarStore(db)
    await _berlin(calendar)
    planner = PlannerStore(db)
    zone = recurrence.zone("Europe/Berlin")
    today = datetime.now(UTC).astimezone(zone).date()
    inbox = await planner.create({"title": "Sort mail"})
    due_today = await planner.create({"title": "Pay rent", "due_date": today.isoformat(), "due_time": "18:00", "priority": 3})
    overdue = await planner.create({"title": "Call bank", "due_date": (today - timedelta(days=2)).isoformat()})
    errands = await planner.create_list({"name": "Errands", "color": "#123456"})
    later = await planner.create({"title": "Buy gift", "list_id": errands["id"], "due_date": (today + timedelta(days=3)).isoformat()})
    blocked_start = datetime.combine(today + timedelta(days=1), datetime.min.time(), zone).replace(hour=10).astimezone(UTC)
    block = await planner.create({"title": "Write report", "list_id": errands["id"], "scheduled_start": blocked_start.isoformat(), "duration": 90})
    assert block["scheduled_end"] == (blocked_start + timedelta(minutes=90)).isoformat() and block["duration"] == 90

    def titles(found):
        return [item["title"] for item in found]

    assert titles(await planner.tasks("inbox")) == ["Sort mail", "Pay rent", "Call bank"]
    assert titles(await planner.tasks("today", timezone="Europe/Berlin")) == ["Pay rent"]
    assert titles(await planner.tasks("overdue", timezone="Europe/Berlin")) == ["Call bank"]
    assert set(titles(await planner.tasks("upcoming", timezone="Europe/Berlin"))) == {"Buy gift", "Write report"}
    assert set(titles(await planner.tasks("all", list_id=errands["id"]))) == {"Buy gift", "Write report"}
    window = await planner.tasks(start=blocked_start.isoformat(), end=(blocked_start + timedelta(hours=1)).isoformat(), timezone="Europe/Berlin")
    assert titles(window) == ["Write report"]
    await planner.complete(inbox["id"])
    assert titles(await planner.tasks("done")) == ["Sort mail"]
    assert titles(await planner.tasks("all"))[-1] == "Sort mail"
    with pytest.raises(RuntimeError, match="changed elsewhere"):
        await planner.update(due_today["id"], {"version": 0, "title": "x"})
    with pytest.raises(ValueError, match="due date"):
        await planner.create({"title": "Bad", "due_time": "10:00"})
    await planner.delete_list(errands["id"])
    moved = await planner.get(later["id"])
    assert moved["list_id"] == (await planner.lists())[0]["id"]
    with pytest.raises(ValueError, match="inbox"):
        await planner.delete_list((await planner.lists())[0]["id"])
    assert overdue["priority"] == 0 and due_today["priority"] == 3


@pytest.mark.asyncio
async def test_completing_a_repeating_task_keeps_history_and_moves_it_on(db):
    planner = PlannerStore(db)
    task = await planner.create({"title": "Water plants", "due_date": "2026-10-05", "recurrence": "FREQ=WEEKLY;COUNT=2", "scheduled_start": "2026-10-05T16:00:00Z", "scheduled_end": "2026-10-05T16:15:00Z", "reminders": [5]})
    advanced = await planner.complete(task["id"])
    assert advanced["done_at"] is None and advanced["due_date"] == "2026-10-12" and advanced["recurrence"] == "RRULE:FREQ=WEEKLY;COUNT=1"
    assert advanced["scheduled_start"] == "2026-10-12T16:00:00+00:00" and advanced["scheduled_end"] == "2026-10-12T16:15:00+00:00"
    done = await planner.tasks("done")
    assert [(item["title"], item["due_date"], item["recurrence"], item["reminders"]) for item in done] == [("Water plants", "2026-10-05", "", [])]
    # The series had two dates: finishing the second finishes the task.
    final = await planner.complete(task["id"])
    assert final["done_at"] is not None and final["due_date"] == "2026-10-12"
    reopened = await planner.complete(task["id"], False)
    assert reopened["done_at"] is None


@pytest.mark.asyncio
async def test_reminders_fire_once_per_occurrence_and_never_late(db):
    store, planner = CalendarStore(db), PlannerStore(db)
    await _berlin(store)
    await store.create({"title": "Standup", "start_at": "2026-10-05T07:00:00Z", "end_at": "2026-10-05T07:15:00Z", "recurrence": "FREQ=DAILY", "reminders": [10], "location": "Room 4"})
    await store.create({"title": "Holiday", "all_day": True, "start_date": "2026-10-06", "end_date": "2026-10-07", "reminders": [0]})
    task = await planner.create({"title": "Send invoice", "due_date": "2026-10-05", "due_time": "11:00", "reminders": [30]})
    posted = []
    app = SimpleNamespace(notifications=SimpleNamespace(language=lambda: "en", post=lambda draft: _record(posted, draft)))

    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 5, 6, 49, tzinfo=UTC)) == 0
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 5, 6, 50, 10, tzinfo=UTC)) == 1
    # Another pass, as after a restart: nothing is repeated.
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 5, 6, 55, tzinfo=UTC)) == 0
    reminder = posted[0]
    assert (reminder.category, reminder.kind, reminder.title) == ("reminder", "calendar_reminder", "Standup")
    assert reminder.body == "09:00–09:15 (in 10 min) · Room 4"
    assert reminder.link.startswith("/app/calendar?event=") and "%3A2026-10-05T07%3A00%3A00%2B00%3A00" in reminder.link
    # 11:00 Berlin is 09:00 UTC; the task's reminder is half an hour before.
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 5, 8, 31, tzinfo=UTC)) == 1
    assert posted[-1].kind == "task_reminder" and posted[-1].link == f"/app/calendar?task={task['id']}" and posted[-1].body == "Due 11:00"
    # The next morning: the holiday counts from the start of the working day (09:00 Berlin).
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 6, 7, 0, 5, tzinfo=UTC)) == 2
    assert {draft.title for draft in posted[-2:]} == {"Holiday", "Standup"}
    # Down for three hours: the standup reminder of the 7th is stale by then and is skipped.
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 7, 9, 51, tzinfo=UTC)) == 0
    keys = [row["key"] for row in await db.fetchall("SELECT key FROM calendar_reminders_sent ORDER BY key")]
    assert len(keys) == 4 and not any("2026-10-07" in key for key in keys)


async def _record(posted, draft):
    posted.append(draft)


@pytest.mark.asyncio
async def test_migration_backfills_calendars_from_the_previous_schema(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    previous = MIGRATIONS[:-1]
    monkeypatch.setattr(database_module, "MIGRATIONS", previous)
    old = Database(path)
    await old.open()
    try:
        await old.execute("INSERT INTO calendar_accounts(id,provider,name,credentials_json,remote_calendar_id,cursor,created_at) VALUES('g','google','Work','{}','primary','token-1','2026-01-01')")
        await old.execute("INSERT INTO calendar_accounts(id,provider,name,credentials_json,remote_calendar_id,cursor,created_at) VALUES('y','yandex','Home',?,'/calendars/someone/events/','','2026-01-02')", (json.dumps({"username": "someone", "app_password": "secret"}),))
        await old.execute("INSERT INTO calendar_events(id,account_id,remote_id,title,start_at,end_at,dirty,updated_at) VALUES('e1',NULL,NULL,'Local','2026-10-05T09:00:00+00:00','2026-10-05T10:00:00+00:00','','x')")
        await old.execute("INSERT INTO calendar_events(id,account_id,remote_id,title,start_at,end_at,dirty,updated_at) VALUES('e2','g','r2','Remote','2026-10-05T11:00:00+00:00','2026-10-05T12:00:00+00:00','','x')")
        await old.execute("INSERT INTO calendar_events(id,account_id,remote_id,title,start_at,end_at,dirty,updated_at) VALUES('e3','y','/x.ics','Yandex','2026-10-05T13:00:00+00:00','2026-10-05T14:00:00+00:00','','x')")
    finally:
        await old.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    db = Database(path)
    await db.open()
    try:
        calendars = {row["kind"]: row for row in await db.fetchall("SELECT * FROM calendars")}
        assert set(calendars) == {"local", "google", "caldav"} and calendars["local"]["name"] == "Personal"
        events = {row["id"]: row["calendar_id"] for row in await db.fetchall("SELECT id, calendar_id FROM calendar_events")}
        assert events == {"e1": calendars["local"]["id"], "e2": calendars["google"]["id"], "e3": calendars["caldav"]["id"]}
        yandex = await db.fetchone("SELECT * FROM calendar_accounts WHERE id='y'")
        assert yandex["provider"] == "caldav" and json.loads(yandex["credentials_json"]) == {"server_url": "https://caldav.yandex.ru", "username": "someone", "password": "secret"}
        assert (await db.fetchone("SELECT cursor FROM calendar_accounts WHERE id='g'"))["cursor"] == ""
        assert (await db.fetchone("SELECT name, inbox FROM planner_lists"))["inbox"] == 1
        assert (await db.fetchone("PRAGMA integrity_check"))[0] == "ok"
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        assert [item["title"] for item in await CalendarStore(db).occurrences("2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")] == ["Local", "Remote", "Yandex"]
    finally:
        await db.close()
    # Opening again runs nothing twice.
    db = Database(path)
    await db.open()
    try:
        assert (await db.fetchone("SELECT version FROM schema_version"))["version"] == len(MIGRATIONS)
        assert (await db.fetchone("SELECT count(*) FROM calendars"))[0] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_routes_follow_the_contract(db):
    app = FastAPI()
    register(app, SimpleNamespace(db=db, notifications=None), lambda: {"user_id": "operator"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        calendars = (await client.get("/api/calendar/calendars")).json()
        assert [(item["name"], item["kind"], item["writable"], item["sync"]) for item in calendars] == [("Personal", "local", True, None)]
        work = (await client.post("/api/calendar/calendars", json={"name": "Work", "color": "#336699"})).json()
        made = await client.post("/api/calendar/events", json={"calendar_id": work["id"], "title": "Retro", "start_at": "2026-10-05T15:00:00Z", "end_at": "2026-10-05T16:00:00Z", "recurrence": "FREQ=WEEKLY", "reminders": [5, 15], "timezone": "UTC", "all_day": False, "location": "", "description": "", "color": None})
        assert made.status_code == 201, made.text
        event = made.json()
        assert event["color"] == "#336699" and event["reminders"] == [5, 15] and event["writable"] and not event["pending_sync"]
        week = (await client.get("/api/calendar/events", params={"start": "2026-10-05T00:00:00Z", "end": "2026-10-15T00:00:00Z", "calendars": work["id"]})).json()
        assert len(week) == 2 and week[1]["id"] == event["event_id"] + ":2026-10-12T15:00:00+00:00"
        one = await client.put(f"/api/calendar/events/{event['event_id']}", json={"scope": "this", "occurrence_start": week[1]["occurrence_start"], "version": week[1]["version"], "title": "Retro (moved)", "start_at": "2026-10-13T15:00:00Z", "end_at": "2026-10-13T16:00:00Z"})
        assert one.status_code == 200, one.text
        stale = await client.put(f"/api/calendar/events/{event['event_id']}", json={"scope": "all", "version": 0, "title": "x"})
        assert stale.status_code == 409
        gone = await client.delete(f"/api/calendar/events/{event['event_id']}", params={"scope": "this", "occurrence_start": week[0]["occurrence_start"], "version": week[0]["version"]})
        assert gone.status_code == 200, gone.text
        titles = [item["title"] for item in (await client.get("/api/calendar/events", params={"start": "2026-10-05T00:00:00Z", "end": "2026-10-15T00:00:00Z", "calendars": work["id"]})).json()]
        assert titles == ["Retro (moved)"]
        stored = (await client.get(f"/api/calendar/events/{event['event_id']}")).json()
        assert stored["recurrence"] == "RRULE:FREQ=WEEKLY" and len(stored["overrides"]) == 1
        assert (await client.get("/api/calendar/search", params={"q": "retro"})).json()[0]["title"].startswith("Retro")
        assert (await client.put("/api/calendar/settings", json={"week_start": 0, "show_weekends": False})).json()["week_start"] == 0
        assert (await client.put("/api/calendar/settings", json={"work_start": "25:00"})).status_code == 400
        assert (await client.post("/api/calendar/subscriptions", json={"name": "Holidays", "url": "http://example.invalid/h.ics"})).status_code == 400
        feed = (await client.post("/api/calendar/subscriptions", json={"name": "Holidays", "url": "webcal://example.invalid/private-token/h.ics"})).json()
        assert feed["provider"] == "ics" and feed["host"] == "example.invalid" and "private-token" not in json.dumps(feed)
        feed_calendar = next(item for item in (await client.get("/api/calendar/calendars")).json() if item["kind"] == "ics")
        assert feed_calendar["writable"] is False and feed_calendar["sync"]["status"] == "never"
        refused = await client.post("/api/calendar/events", json={"calendar_id": feed_calendar["id"], "title": "x", "start_at": "2026-10-05T15:00:00Z", "end_at": "2026-10-05T16:00:00Z"})
        assert refused.status_code == 400 and "read-only" in refused.text
        icloud = (await client.post("/api/calendar/accounts", json={"provider": "icloud", "name": "Family", "credentials": {"username": "someone", "password": "app-specific"}})).json()
        assert icloud["provider"] == "caldav" and icloud["server_url"] == "https://caldav.icloud.com" and "app-specific" not in json.dumps(icloud)
        assert (await client.delete(f"/api/calendar/calendars/{feed_calendar['id']}")).status_code == 400

        lists = (await client.get("/api/planner/lists")).json()
        assert [(item["name"], item["inbox"]) for item in lists] == [("Inbox", True)]
        task = (await client.post("/api/planner/tasks", json={"title": "Plan trip", "due_date": "2026-10-09", "priority": 2})).json()
        assert task["list_id"] == lists[0]["id"] and task["version"] == 1
        changed = await client.put(f"/api/planner/tasks/{task['id']}", json={"version": 1, "scheduled_start": "2026-10-08T10:00:00Z", "scheduled_end": "2026-10-08T11:00:00Z"})
        assert changed.json()["duration"] == 60
        ranged = (await client.get("/api/planner/tasks", params={"start": "2026-10-08T00:00:00Z", "end": "2026-10-09T00:00:00Z"})).json()
        assert [item["id"] for item in ranged] == [task["id"]]
        done = (await client.post(f"/api/planner/tasks/{task['id']}/complete", json={"done": True})).json()
        assert done["done"] is True
        assert (await client.get("/api/planner/tasks", params={"view": "someday"})).status_code == 400
        assert (await client.delete(f"/api/planner/lists/{lists[0]['id']}")).status_code == 400
        assert (await client.delete(f"/api/planner/tasks/{task['id']}", params={"version": done["version"]})).status_code == 200


@pytest.mark.asyncio
async def test_calendar_oauth_state_is_single_use_and_credentials_stay_off_api(db, monkeypatch):
    app = FastAPI()
    register(app, SimpleNamespace(db=db, settings=SimpleNamespace(miniapp_public_url="https://example.invalid/app")), lambda: {"user_id": "operator"})

    class TokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, data):
            assert data["redirect_uri"] == "https://example.invalid/api/calendar/oauth/callback"
            return httpx.Response(200, json={"refresh_token": "private-refresh-token"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        started = await client.post("/api/calendar/oauth/start", json={"provider": "google", "name": "Work", "client_id": "id", "client_secret": "secret"})
        assert started.status_code == 200
        state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
        monkeypatch.setattr("daedalus.extensions.api_calendar.httpx.AsyncClient", lambda **_: TokenClient())
        linked = await client.get("/api/calendar/oauth/callback", params={"state": state, "code": "one-time-code"}, follow_redirects=False)
        assert linked.status_code == 303
        assert (await client.get("/api/calendar/oauth/callback", params={"state": state, "code": "one-time-code"})).status_code == 400
        accounts = (await client.get("/api/calendar/accounts")).json()
        assert len(accounts) == 1 and "credentials" not in accounts[0] and accounts[0]["calendar_id"]
        assert "private-refresh-token" not in str(accounts)


@pytest.mark.asyncio
async def test_find_time_respects_working_hours_events_and_time_blocks(db, monkeypatch):
    from daedalus.tools import calendar as calendar_tools  # Lazy: the tool module reaches the session services.

    monkeypatch.setattr(calendar_tools, "_db", lambda _context: db)
    store, planner = CalendarStore(db), PlannerStore(db)
    await store.save_settings({"timezone": "Europe/Berlin", "work_start": "09:00", "work_end": "12:00"})
    zone = recurrence.zone("Europe/Berlin")
    # A Monday at least a week ahead, so "now" never trims the day.
    day = (datetime.now(UTC) + timedelta(days=8)).astimezone(zone).date()
    day += timedelta(days=(7 - day.weekday()) % 7)

    def at(hour: int, minute: int = 0) -> str:
        return datetime.combine(day, datetime.min.time(), zone).replace(hour=hour, minute=minute).isoformat()

    await store.create({"title": "Busy", "start_at": at(9, 30), "end_at": at(10, 30), "timezone": "Europe/Berlin"})
    await store.create({"title": "Holiday", "all_day": True, "start_date": day.isoformat(), "end_date": (day + timedelta(days=1)).isoformat()})
    await planner.create({"title": "Focus", "scheduled_start": at(11), "scheduled_end": at(11, 30)})
    context = ToolContext(tenant_id="operator", run_id="run", session_id="session")
    result = await calendar_tools.calendar_find_time().invoke(context, {"duration_minutes": 30, "start": at(0), "end": at(23)})
    assert not result.is_error, result.content
    slots = json.loads(result.content)["slots"]
    assert [(slot["start"], slot["end"]) for slot in slots] == [(at(9), at(9, 30)), (at(10, 30), at(11)), (at(11, 30), at(12))]
    result = await calendar_tools.calendar_find_time().invoke(context, {"duration_minutes": 45, "start": at(0), "end": at(23)})
    assert json.loads(result.content)["slots"] == []
    listed = await calendar_tools.calendar_events().invoke(context, {"start": at(0), "end": at(23)})
    body = json.loads(listed.content)
    assert body["timezone"] == "Europe/Berlin" and {item["calendar_name"] for item in body["events"]} == {"Personal"}
    assert "description" not in body["events"][0]


@pytest.mark.asyncio
async def test_an_all_day_series_moved_from_an_occurrence_by_its_midnights_moves_by_days(db):
    store = CalendarStore(db)
    series = await store.create({"title": "Bins", "all_day": True, "start_date": "2026-10-05", "end_date": "2026-10-06", "recurrence": "FREQ=WEEKLY;COUNT=10"})
    # What the agent's tool sends: the occurrence's new midnights, no dates.
    await store.update(series["event_id"], {"scope": "all", "occurrence_start": "2026-10-26T00:00:00+00:00", "version": series["version"], "all_day": True, "start_at": "2026-10-27T00:00:00Z", "end_at": "2026-10-28T00:00:00Z"})
    found = await store.occurrences("2026-10-01T00:00:00Z", "2027-02-28T00:00:00Z")
    assert len(found) == 10 and [item["start_date"] for item in found[:3]] == ["2026-10-06", "2026-10-13", "2026-10-20"]
    assert all(item["end_date"] > item["start_date"] for item in found)


@pytest.mark.asyncio
async def test_a_series_edited_from_an_occurrence_with_one_bound_keeps_the_other(db):
    store = CalendarStore(db)
    series = await store.create({"title": "Standup", "start_at": "2026-10-05T08:00:00Z", "end_at": "2026-10-05T08:30:00Z", "timezone": "Europe/Berlin", "recurrence": "FREQ=WEEKLY"})
    # Only the end of the 2 November occurrence (10:00 Berlin, after the clocks went back) is moved.
    longer = await store.update(series["event_id"], {"scope": "all", "occurrence_start": "2026-11-02T09:00:00+00:00", "version": series["version"], "end_at": "2026-11-02T10:00:00Z"})
    master = await store.get(series["event_id"])
    assert (master["start_at"], master["end_at"]) == ("2026-10-05T08:00:00+00:00", "2026-10-05T09:00:00+00:00")
    # Only the start: the series moves an hour later on the Berlin clock and keeps its length,
    # although this occurrence and the series' first one sit on different sides of the change.
    await store.update(series["event_id"], {"scope": "all", "occurrence_start": "2026-11-02T09:00:00+00:00", "version": longer["version"], "start_at": "2026-11-02T10:00:00Z"})
    found = await store.occurrences("2026-10-05T00:00:00Z", "2026-11-03T00:00:00Z")
    assert [(item["start_at"], item["end_at"]) for item in found][::4] == [("2026-10-05T09:00:00+00:00", "2026-10-05T10:00:00+00:00"), ("2026-11-02T10:00:00+00:00", "2026-11-02T11:00:00+00:00")]


@pytest.mark.asyncio
async def test_a_repeating_tasks_time_block_keeps_its_wall_clock_across_daylight_saving(db):
    await _berlin(CalendarStore(db))
    planner = PlannerStore(db)
    task = await planner.create({"title": "Walk", "due_date": "2026-10-24", "scheduled_start": "2026-10-24T09:00:00+02:00", "scheduled_end": "2026-10-24T09:30:00+02:00", "recurrence": "FREQ=DAILY"})
    for _ in range(2):
        task = await planner.complete(task["id"])
    # 09:00 Berlin on the 26th is 08:00 UTC, an hour later in UTC than on the 24th.
    assert task["due_date"] == "2026-10-26" and (task["scheduled_start"], task["scheduled_end"]) == ("2026-10-26T08:00:00+00:00", "2026-10-26T08:30:00+00:00")


@pytest.mark.asyncio
async def test_a_moved_occurrence_reminds_again_at_its_new_time(db):
    store, planner = CalendarStore(db), PlannerStore(db)
    series = await store.create({"title": "Sync", "start_at": "2026-10-05T07:00:00Z", "end_at": "2026-10-05T07:30:00Z", "recurrence": "FREQ=DAILY", "reminders": [10]})
    posted = []
    app = SimpleNamespace(notifications=SimpleNamespace(language=lambda: "en", post=lambda draft: _record(posted, draft)))
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 6, 6, 51, tzinfo=UTC)) == 1
    await store.update(series["event_id"], {"scope": "this", "occurrence_start": "2026-10-06T07:00:00Z", "version": series["version"], "start_at": "2026-10-06T07:40:00Z", "end_at": "2026-10-06T08:10:00Z"})
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 6, 7, 31, tzinfo=UTC)) == 1
    assert await calendar_reminders.deliver(app, store, planner, datetime(2026, 10, 6, 7, 35, tzinfo=UTC)) == 0


@pytest.mark.asyncio
async def test_migration_drops_outlooks_per_occurrence_rows_and_its_cursor(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:-1])
    old = Database(path)
    await old.open()
    try:
        await old.execute("INSERT INTO calendar_accounts(id,provider,name,credentials_json,cursor,created_at) VALUES('o','outlook','Work','{}','{\"end\":\"2028-01-01T00:00:00+00:00\",\"url\":\"x\"}','2026-01-01')")
        for event_id, dirty in (("o1", ""), ("o2", ""), ("o3", "update")):
            await old.execute("INSERT INTO calendar_events(id,account_id,remote_id,title,start_at,end_at,dirty,updated_at) VALUES(?,'o',?,'Weekly','2026-10-05T09:00:00+00:00','2026-10-05T10:00:00+00:00',?,'x')", (event_id, "r" + event_id, dirty))
    finally:
        await old.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    db = Database(path)
    await db.open()
    try:
        assert (await db.fetchone("SELECT cursor FROM calendar_accounts WHERE id='o'"))["cursor"] == ""
        assert [row["id"] for row in await db.fetchall("SELECT id FROM calendar_events")] == ["o3"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()
