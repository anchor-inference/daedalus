import json
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from icalendar import Calendar, Event
from protocore.contracts.tools import ToolContext

from daedalus.extensions.api_workspace import register
from daedalus.extensions.calendar_sync import CalendarConflict, CalendarSync, _remote
from daedalus.stores.calendar import CalendarStore
from daedalus.stores.database import Database
from daedalus.stores.diagrams import DiagramStore, shape_label


@pytest.mark.asyncio
async def test_calendar_edit_delete_and_conflict(tmp_path):
    db = Database(tmp_path / "calendar.sqlite")
    await db.open()
    try:
        store = CalendarStore(db)
        body = {"title": "Review", "start_at": "2026-10-04T10:00:00+02:00", "end_at": "2026-10-04T11:00:00+02:00", "timezone": "Europe/Berlin"}
        made = await store.save(body)
        assert made["start_at"] == "2026-10-04T08:00:00+00:00"
        assert made["dirty"] == ""
        assert len(await store.events("2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z")) == 1
        changed = await store.save({**body, "title": "Decision", "version": made["version"]}, made["id"])
        assert changed["version"] == made["version"] + 1
        with pytest.raises(RuntimeError, match="changed elsewhere"):
            await store.save({**body, "version": made["version"]}, made["id"])
        with pytest.raises(RuntimeError, match="changed elsewhere"):
            await store.delete(made["id"], made["version"])
        await store.delete(made["id"], changed["version"])
        assert await store.get(made["id"]) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_google_sync_keeps_local_edits_and_cursor(tmp_path):
    db = Database(tmp_path / "calendar.sqlite")
    await db.open()
    try:
        store = CalendarStore(db)
        account = await store.connect("google", "Work", {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"})
        made = await store.save({"title": "Review", "start_at": "2026-10-04T08:00:00Z", "end_at": "2026-10-04T09:00:00Z", "account_id": account["id"]})
        stale = await store.save({"title": "Removed remotely", "start_at": "2026-10-04T10:00:00Z", "end_at": "2026-10-04T11:00:00Z", "account_id": account["id"]})
        await db.execute("UPDATE calendar_events SET remote_id='remote-old',dirty='' WHERE id=?", (stale["id"],))
        calls = []

        def reply(request):
            calls.append((request.method, str(request.url)))
            if request.method == "POST":
                return httpx.Response(200, json={"id": "remote-1", "etag": '"one"'})
            if request.method == "DELETE":
                assert request.headers["If-Match"] == '"one"'
                return httpx.Response(204)
            if "syncToken=" in str(request.url):
                return httpx.Response(200, json={"items": [], "nextSyncToken": "second"})
            return httpx.Response(200, json={"items": [{"id": "remote-1", "etag": '"one"', "summary": "Review", "start": {"dateTime": "2026-10-04T08:00:00Z"}, "end": {"dateTime": "2026-10-04T09:00:00Z"}}], "nextSyncToken": "first"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            sync = CalendarSync(store)
            assert await sync._json_provider(client, await store.account(account["id"]), "token") == 1
            current = await store.get(made["id"])
            assert current["remote_id"] == "remote-1" and current["dirty"] == ""
            assert await store.get(stale["id"]) is None
            assert len(await store.events("2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z")) == 1
            await sync._json_provider(client, await store.account(account["id"]), "token")
            assert any("syncToken=first" in url for _, url in calls)
            await store.delete(made["id"], current["version"])
            await sync._json_provider(client, await store.account(account["id"]), "token")
            assert await store.get(made["id"]) is None
        assert (await store.account(account["id"]))["cursor"] == "second"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_outlook_delta_imports_changes_and_uses_cursor(tmp_path):
    db = Database(tmp_path / "outlook.sqlite")
    await db.open()
    try:
        store = CalendarStore(db)
        account = await store.connect("outlook", "Work", {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"})
        calls = []

        def reply(request):
            calls.append(str(request.url))
            if "$deltatoken=next" in str(request.url):
                return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=after"})
            if "calendarView/delta" in str(request.url):
                return httpx.Response(200, json={"value": [{"id": "remote-1", "subject": "Review", "start": {"dateTime": "2026-10-04T08:00:00"}, "end": {"dateTime": "2026-10-04T09:00:00"}}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=next"})
            raise AssertionError(request.url)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            sync = CalendarSync(store)
            assert await sync._json_provider(client, await store.account(account["id"]), "token") == 1
            assert (await store.events("2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z"))[0]["title"] == "Review"
            assert await sync._json_provider(client, await store.account(account["id"]), "token") == 0, calls
        assert "$deltatoken=next" in calls[-1]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_yandex_caldav_upload_and_import(tmp_path):
    db = Database(tmp_path / "yandex.sqlite")
    await db.open()
    try:
        store = CalendarStore(db)
        account = await store.connect("yandex", "Work", {"username": "someone@example.invalid", "app_password": "secret"}, "/calendars/someone/work/")
        local = await store.save({"title": "Local", "start_at": "2026-10-04T08:00:00Z", "end_at": "2026-10-04T09:00:00Z", "account_id": account["id"]})
        remote = Calendar()
        remote.add("version", "2.0")
        event = Event()
        event.add("uid", "remote@example.invalid")
        event.add("summary", "Remote")
        event.add("dtstart", datetime(2026, 10, 4, 10, tzinfo=ZoneInfo("Europe/Moscow")))
        event.add("dtend", datetime(2026, 10, 4, 11, tzinfo=ZoneInfo("Europe/Moscow")))
        remote.add_component(event)

        def reply(request):
            if request.method == "PUT":
                assert b"SUMMARY:Local" in request.content
                return httpx.Response(201, headers={"etag": '"local"'})
            if request.method == "PROPFIND":
                body = '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/calendars/someone/work/' + local["id"] + '.ics</d:href><d:propstat><d:prop><d:getetag>"local"</d:getetag></d:prop></d:propstat></d:response><d:response><d:href>/calendars/someone/work/remote.ics</d:href><d:propstat><d:prop><d:getetag>"remote"</d:getetag></d:prop></d:propstat></d:response></d:multistatus>'
                return httpx.Response(207, text=body)
            if request.method == "GET":
                return httpx.Response(200, content=remote.to_ical())
            raise AssertionError(request.method)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            assert await CalendarSync(store)._yandex(client, await store.account(account["id"])) == 1
        found = await store.events("2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z")
        assert {item["title"] for item in found} == {"Local", "Remote"}
        assert next(item for item in found if item["title"] == "Remote")["timezone"] == "Europe/Moscow"
        assert (await store.get(local["id"]))["dirty"] == ""
    finally:
        await db.close()


def test_google_all_day_dates_and_yandex_uid_round_trip():
    imported = _remote("google", {"summary": "Holiday", "start": {"date": "2026-10-04"}, "end": {"date": "2026-10-05"}, "iCalUID": "event@example.invalid"})
    assert imported is not None
    assert imported["all_day"] == 1
    assert imported["start_at"] == "2026-10-04T00:00:00+00:00"
    assert imported["remote_uid"] == "event@example.invalid"


@pytest.mark.asyncio
async def test_all_day_keeps_the_named_date_across_time_zones(tmp_path):
    db = Database(tmp_path / "all-day.sqlite")
    await db.open()
    try:
        event = await CalendarStore(db).save({"title": "Holiday", "all_day": True, "timezone": "Asia/Almaty", "start_at": "2026-10-04T00:00:00+05:00", "end_at": "2026-10-05T00:00:00+05:00"})
        assert event["start_at"] == "2026-10-04T00:00:00+00:00"
        assert event["end_at"] == "2026-10-05T00:00:00+00:00"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_provider_conflict_keeps_local_change_until_operator_resolves(tmp_path, monkeypatch):
    db = Database(tmp_path / "calendar.sqlite")
    await db.open()
    try:
        store = CalendarStore(db)
        account = await store.connect("google", "Work", {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"})
        event = await store.save({"title": "Review", "start_at": "2026-10-04T08:00:00Z", "end_at": "2026-10-04T09:00:00Z", "account_id": account["id"]})
        await db.execute("UPDATE calendar_events SET remote_id=?,etag=?,dirty='update' WHERE id=?", ("remote-1", '"old"', event["id"]))
        sync = CalendarSync(store)

        def reply(request):
            assert request.headers["If-Match"] == '"old"'
            return httpx.Response(412)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(CalendarConflict, match=event["id"]):
                await sync._json_provider(client, await store.account(account["id"]), "token")
        assert (await store.get(event["id"]))["dirty"] == "update"

        async def accepted(account_id):
            assert account_id == account["id"]
            return {"changed": 1}

        monkeypatch.setattr(sync, "sync", accepted)
        await sync.resolve(event["id"], "local")
        row = await db.fetchone("SELECT etag,dirty FROM calendar_events WHERE id=?", (event["id"],))
        assert row["etag"] == "" and row["dirty"] == "update"
        await db.execute("UPDATE calendar_accounts SET cursor='stale' WHERE id=?", (account["id"],))
        await sync.resolve(event["id"], "remote")
        assert (await db.fetchone("SELECT dirty FROM calendar_events WHERE id=?", (event["id"],)))["dirty"] == ""
        assert (await store.account(account["id"]))["cursor"] == ""
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_diagram_version_rejects_concurrent_save(tmp_path):
    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        store = DiagramStore(db)
        made = await store.create("Flow")
        scene = {"elements": [{"id": "box", "type": "rectangle", "x": 20, "y": 20}], "appState": {}, "files": {}}
        saved = await store.save(made["id"], "Flow", scene, made["version"])
        assert saved["version"] == made["version"] + 1
        assert json.loads((await db.fetchone("SELECT scene_json FROM diagrams WHERE id=?", (made["id"],)))["scene_json"])["elements"][0]["id"] == "box"
        with pytest.raises(RuntimeError, match="changed elsewhere"):
            await store.save(made["id"], "Old", scene, made["version"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_calendar_and_diagram_routes_share_the_stores(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        app = FastAPI()
        register(app, SimpleNamespace(db=db), lambda: {"user_id": "operator"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            created = await client.post("/api/calendar/events", json={"title": "Review", "start_at": "2026-10-04T08:00:00Z", "end_at": "2026-10-04T09:00:00Z"})
            assert created.status_code == 201
            event = created.json()
            assert (await client.get("/api/calendar/events", params={"start": "2026-10-04T00:00:00Z", "end": "2026-10-05T00:00:00Z"})).json()[0]["id"] == event["id"]
            stale = await client.put(f"/api/calendar/events/{event['id']}", json={"title": "Changed", "start_at": event["start_at"], "end_at": event["end_at"], "version": 0})
            assert stale.status_code == 409
            made = await client.post("/api/diagrams", json={"title": "Flow"})
            assert made.status_code == 201
            diagram = made.json()
            saved = await client.put(f"/api/diagrams/{diagram['id']}", json={"title": "Flow", "scene": {"elements": [], "appState": {}, "files": {}}, "version": 1})
            assert saved.json()["version"] == 2
            assert (await client.get("/api/diagrams")).json()[0]["title"] == "Flow"
            versions = (await client.get(f"/api/diagrams/{diagram['id']}/versions")).json()
            assert [item["version"] for item in versions] == [2, 1]
            assert (await client.get(f"/api/diagrams/{diagram['id']}/versions/1")).json()["scene"]["elements"] == []
            shared = await client.post(f"/api/diagrams/{diagram['id']}/share")
            assert shared.status_code == 200
            token = shared.json()["url"].split("/")[-1]
            public = await client.get(f"/api/public/diagrams/{token}")
            assert public.json()["title"] == "Flow"
            assert "share_token" not in public.json()
            guest_app = FastAPI()

            def deny() -> None:
                raise HTTPException(401)

            register(guest_app, SimpleNamespace(db=db), deny)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest:
                assert (await guest.get(f"/api/public/diagrams/{token}")).status_code == 200
                assert (await guest.get(f"/api/diagrams/{diagram['id']}")).status_code == 401
            assert (await client.delete(f"/api/diagrams/{diagram['id']}/share")).status_code == 200
            assert (await client.get(f"/api/public/diagrams/{token}")).status_code == 404
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_diagram_session_list_and_legacy_shape_labels(tmp_path):
    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        store = DiagramStore(db)
        made = await store.create("Flow", {"elements": [{"id": "box", "type": "rectangle", "x": 10, "y": 20, "width": 200, "height": 100}], "appState": {}, "files": {}})
        await db.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES('project','Flow','2026-01-01','{}','')")
        await db.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES(?,?,?,?,?,?)", ("session", "operator", "Flow", "2026-01-01", "2026-01-01", "project"))
        event = {"metadata": {"diagram_id": made["id"]}, "content_blocks": [{"type": "text", "text": "created"}]}
        await db.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)", ("session", 1, 0, "tool_result", json.dumps(event), "2026-01-01"))
        edit = {"final_input": {"diagram_id": made["id"], "elements": [{"id": "box", "type": "rectangle", "text": "Inside the box"}]}}
        await db.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)", ("session", 2, 0, "tool_use_stop", json.dumps(edit), "2026-01-02"))
        assert [item["id"] for item in await store.list("session")] == [made["id"]]
        found = await store.get(made["id"])
        assert found is not None
        assert found["scene"]["elements"][1]["text"] == "Inside the box"
        assert found["scene"]["elements"][0]["boundElements"] == [{"id": "box-label", "type": "text"}]
        assert found["scene"]["elements"][1]["containerId"] == "box"
        assert (await store.get(made["id"]))["scene"] == found["scene"]
        assert shape_label(found["scene"]["elements"][0], "New")["type"] == "text"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_agent_edit_keeps_text_inside_shapes(tmp_path, monkeypatch):
    from daedalus.tools import diagrams as diagram_tools  # Lazy: the tool is needed only for this integration check.

    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        store = DiagramStore(db)
        made = await store.create("Flow")
        monkeypatch.setattr(diagram_tools, "_store", lambda _context: store)
        context = ToolContext(tenant_id="operator", run_id="run", session_id="session")
        result = await diagram_tools.diagram_edit().invoke(context, {"diagram_id": made["id"], "version": 1, "elements": [{"id": "box", "type": "rectangle", "x": 20, "y": 30, "width": 240, "height": 120, "text": "The worker writes here"}]})
        assert not result.is_error
        scene = (await store.get(made["id"]))["scene"]
        assert [(item["id"], item.get("text")) for item in scene["elements"]] == [("box", None), ("box-label", "The worker writes here")]
        assert scene["elements"][0]["boundElements"] == [{"id": "box-label", "type": "text"}]
        assert scene["elements"][1]["containerId"] == "box"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_calendar_oauth_state_is_single_use_and_credentials_stay_off_api(tmp_path, monkeypatch):
    db = Database(tmp_path / "oauth.sqlite")
    await db.open()
    try:
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
            monkeypatch.setattr("daedalus.extensions.api_workspace.httpx.AsyncClient", lambda **_: TokenClient())
            linked = await client.get("/api/calendar/oauth/callback", params={"state": state, "code": "one-time-code"}, follow_redirects=False)
            assert linked.status_code == 303
            assert (await client.get("/api/calendar/oauth/callback", params={"state": state, "code": "one-time-code"})).status_code == 400
            accounts = (await client.get("/api/calendar/accounts")).json()
            assert len(accounts) == 1 and "credentials" not in accounts[0]
            assert "private-refresh-token" not in str(accounts)
    finally:
        await db.close()
