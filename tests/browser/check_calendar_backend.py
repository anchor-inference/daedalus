"""Drive the planner against the real calendar backend, not the stub.

The planner's own check (check_calendar_planner.py) holds the screen to a stub written from the
contract. This one holds the screen and the server to each other: the calendar and planner routes
are the real ones (daedalus/extensions/api_calendar.py over a fresh SQLite database, served by a
uvicorn of its own on a free port), and only the rest of the app is stubbed. What it holds:

- an event made by dragging on the grid is stored at the wall-clock time it was dragged to, in the
  calendar's time zone;
- a repeat chosen in the editor is stored as a rule, and next week shows the occurrence;
- moving one occurrence with "This event" stores an exception for that day only;
- a task quick-added in the panel is stored, and ticking it off completes it;
- deleting the series with "All events" leaves nothing behind;
- on a phone, the add button and the bottom sheet store an event too.

    python3 tests/browser/check_calendar_backend.py   (APP_URL and CHROMIUM as for the other checks)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ROOT = Path(__file__).resolve().parents[2]
ZONE = "Europe/Berlin"
HOUR = 48
"""The desktop grid's hour, in pixels (TimeGrid's hourHeight)."""

SERVER = textwrap.dedent(
    """
    import asyncio, sys
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import uvicorn
    from fastapi import FastAPI
    from daedalus.extensions import api_calendar
    from daedalus.stores.database import Database

    from pathlib import Path
    db = Database(Path(sys.argv[1]))
    app_state = SimpleNamespace(db=db, settings=SimpleNamespace(miniapp_public_url=""), notifications=None)

    @asynccontextmanager
    async def lifespan(_):
        await db.open()
        yield
        await db.close()

    api = FastAPI(lifespan=lifespan)
    api_calendar.register(api, app_state, lambda: {})
    uvicorn.run(api, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    """
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Backend:
    """The real calendar routes in a process of their own, over a database made for this run."""

    def __init__(self, folder: Path) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        self.process = subprocess.Popen([sys.executable, "-c", SERVER, str(folder / "calendar.sqlite"), str(self.port)], env=env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self.get("/api/calendar/settings")
                return
            except OSError:
                if self.process.poll() is not None:
                    raise RuntimeError("the calendar backend exited before it answered") from None
                time.sleep(0.2)
        raise RuntimeError("the calendar backend never answered")

    def call(self, method: str, path: str, body: object | None = None) -> object:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"null")

    def get(self, path: str) -> object:
        return self.call("GET", path)

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)


def routed(backend: Backend):  # type: ignore[no-untyped-def]
    """Calendar and planner requests go to the real backend; everything else to the shared stub."""

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        url = urlsplit(route.request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else url.path
        if path.startswith(("/api/calendar", "/api/planner")):
            target = backend.base + path + (f"?{url.query}" if url.query else "")
            response = route.fetch(url=target)
            route.fulfill(response=response)
            return
        shots.stub(route)

    return handle


def wait_for(page: Page, what: str, test, timeout: float = 8.0):  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = test()
        if found:
            return found
        page.wait_for_timeout(100)
    raise AssertionError(f"never happened: {what}")


def drag(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0], start[1] + 6, steps=2)
    page.mouse.move(*end, steps=8)
    page.mouse.up()


def slot(page: Page, day: str, minutes: int) -> tuple[float, float]:
    box = page.locator(f".cal-tg-col[data-day='{day}']").bounding_box()
    assert box is not None, f"no column for {day}"
    return box["x"] + box["width"] / 2, box["y"] + minutes / 60 * HOUR


def scroll_hours(page: Page, hour: float) -> None:
    page.locator(".cal-tg-scroll").evaluate(f"el => {{ el.scrollTop = {hour * HOUR}; }}")
    page.wait_for_timeout(80)


def events(backend: Backend, start: datetime, days: int) -> list[dict]:
    first = start.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    last = (start + timedelta(days=days)).astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    return backend.get(f"/api/calendar/events?start={first}&end={last}")  # type: ignore[return-value]


def local(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(ZoneInfo(ZONE))


def desktop(browser, backend: Backend, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    zone = ZoneInfo(ZONE)
    today = datetime.now(zone).date()
    monday = today - timedelta(days=today.weekday())
    week_start = datetime(monday.year, monday.month, monday.day, tzinfo=zone)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, timezone_id=ZONE, locale="en-GB", color_scheme="dark")
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(f"desktop: {error}"))
    page.route("**/api/**", routed(backend))
    page.goto(f"{BASE}/calendar?token=t&lang=en")
    expect(page.locator(".cal-screen")).to_be_visible()
    page.evaluate("localStorage.setItem('daedalus.calendar.view', 'week')")
    page.reload()
    expect(page.locator(".cal-tg-head")).to_have_count(7)
    # The backend made the default local calendar; the sidebar shows it.
    expect(page.locator(".cal-screen")).to_contain_text("Personal")

    # Drag 08:00–09:30 today and name it: stored at those wall-clock times in the calendar's zone.
    day = str(today)
    scroll_hours(page, 6)
    drag(page, slot(page, day, 8 * 60 + 3), slot(page, day, 9 * 60 + 27))
    card = page.locator(".cal-quick")
    expect(card).to_be_visible()
    card.get_by_label("Title").fill("Live standup")
    card.get_by_label("Title").press("Enter")
    expect(card).to_be_hidden()
    stored = wait_for(page, "the event stored", lambda: [e for e in events(backend, week_start, 7) if e["title"] == "Live standup"])[0]
    assert local(stored["start_at"]) == datetime(today.year, today.month, today.day, 8, tzinfo=zone), stored
    assert local(stored["end_at"]) == datetime(today.year, today.month, today.day, 9, 30, tzinfo=zone), stored

    # Make it weekly from the editor: the rule is stored, and next week shows the occurrence.
    block = page.locator(".cal-block", has_text="Live standup")
    expect(block).to_be_visible()
    block.click()
    editor = page.get_by_role("dialog", name="Edit event")
    editor.get_by_label("Repeat").select_option("weekly")
    editor.get_by_role("button", name="Save").click()
    expect(editor).to_be_hidden()
    master = wait_for(page, "the rule stored", lambda: (lambda e: e if "FREQ=WEEKLY" in (e.get("recurrence") or "") else None)(backend.get(f"/api/calendar/events/{stored['event_id']}")))
    assert "FREQ=WEEKLY" in master["recurrence"], master
    next_week = [e for e in events(backend, week_start + timedelta(days=7), 7) if e["title"] == "Live standup"]
    assert len(next_week) == 1 and next_week[0]["recurring"], next_week
    page.get_by_role("button", name="Next week").click()
    occurrence = page.locator(".cal-block", has_text="Live standup")
    expect(occurrence).to_be_visible()

    # Move next week's occurrence an hour later, "This event": only that day changes.
    scroll_hours(page, 6)
    box = occurrence.bounding_box()
    assert box
    drag(page, (box["x"] + box["width"] / 2, box["y"] + 10), (box["x"] + box["width"] / 2, box["y"] + 10 + HOUR))
    ask = page.get_by_role("alertdialog")
    expect(ask).to_be_visible()
    ask.get_by_role("button", name="This event").click()
    moved = wait_for(page, "the exception stored", lambda: [e for e in events(backend, week_start + timedelta(days=7), 7) if e["title"] == "Live standup" and local(e["start_at"]).hour == 9])
    assert len(moved) == 1, moved
    this_week = [e for e in events(backend, week_start, 7) if e["title"] == "Live standup"]
    assert len(this_week) == 1 and local(this_week[0]["start_at"]).hour == 8, this_week
    in_two_weeks = [e for e in events(backend, week_start + timedelta(days=14), 7) if e["title"] == "Live standup"]
    assert len(in_two_weeks) == 1 and local(in_two_weeks[0]["start_at"]).hour == 8, in_two_weeks

    # A task added in the panel is stored, and ticking it off completes it.
    page.get_by_role("button", name="Today", exact=True).click()
    tasks = page.locator(".cal-tasks")
    tasks.get_by_role("tab", name="Inbox").click()
    add = tasks.get_by_role("textbox").first
    add.fill("Live task")
    add.press("Enter")
    task = wait_for(page, "the task stored", lambda: [t for t in backend.get("/api/planner/tasks?view=all") if t["title"] == "Live task"])[0]  # type: ignore[union-attr]
    assert not task["done_at"], task
    tasks.get_by_role("checkbox", name="Done: Live task").click()
    wait_for(page, "the task completed", lambda: [t for t in backend.get("/api/planner/tasks?view=all") if t["title"] == "Live task" and t["done_at"]])  # type: ignore[union-attr]

    # Delete the whole series from this week's event: nothing is left in the next three weeks.
    scroll_hours(page, 6)
    page.locator(".cal-block", has_text="Live standup").click()
    editor = page.get_by_role("dialog", name="Edit event")
    editor.get_by_role("button", name="Delete").click()
    ask = page.get_by_role("alertdialog")
    expect(ask).to_be_visible()
    ask.get_by_role("button", name="All events").click()
    wait_for(page, "the series deleted", lambda: not [e for e in events(backend, week_start, 21) if e["title"] == "Live standup"])
    expect(page.locator(".cal-block", has_text="Live standup")).to_have_count(0)
    context.close()


def phone(browser, backend: Backend, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    zone = ZoneInfo(ZONE)
    today = datetime.now(zone).date()
    start = datetime(today.year, today.month, today.day, tzinfo=zone)
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, timezone_id=ZONE, locale="ru-RU", color_scheme="dark")
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(f"phone: {error}"))
    page.route("**/api/**", routed(backend))
    page.goto(f"{BASE}/calendar?token=t&lang=ru")
    expect(page.locator(".cal-screen")).to_be_visible()
    page.locator(".cal-fab").click()
    sheet = page.get_by_role("dialog")
    expect(sheet).to_be_visible()
    sheet.get_by_label("Название").fill("С телефона")
    sheet.get_by_role("button", name="Сохранить").click()
    wait_for(page, "the phone's event stored", lambda: [e for e in events(backend, start - timedelta(days=1), 3) if e["title"] == "С телефона"])
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the phone scrolls sideways"
    context.close()


def run() -> int:
    expect_app(BASE)
    errors: list[str] = []
    with TemporaryDirectory() as folder:
        backend = Backend(Path(folder))
        try:
            backend.call("PUT", "/api/calendar/settings", {"timezone": ZONE, "week_start": 1, "default_view": "week"})
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=CHROMIUM)
                desktop(browser, backend, errors)
                phone(browser, backend, errors)
                browser.close()
        finally:
            backend.stop()
    assert not errors, errors
    print("planner against the real backend: event, repeat, exception, task, series delete, phone")
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
