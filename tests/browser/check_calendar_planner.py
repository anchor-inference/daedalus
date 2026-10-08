"""Drive the planner in a browser the way it is used: on a desktop with a mouse and keys, on a phone with a thumb.

What it holds the screen to:

- Today really moves the view back to today and scrolls the hours to now; Previous and Next move by
  the view's own length; the view chosen is remembered across a reload.
- A drag on empty time opens the quick card with that slot, and Enter makes the event; a drag on
  the event moves it, a drag on its lower edge resizes it, and Undo puts it back. A repeating event
  asks "this event or all events" before anything is sent, and sends the answer.
- A task is ticked off in the list, and a task dragged onto the grid is given that time.
- Search finds an event and opens it; `/app/calendar?event=` and `?task=` open the item they name.
- Connections offer the two versions of a conflicted event as buttons; the settings change the week.
- On a 390 px phone nothing scrolls sideways, the header's targets are 44 px, the add button makes
  an event through a bottom sheet, a swipe changes the period, and day, three days, month and agenda
  all show words, not just marks.
- Both languages, and with SHOTS set, pictures of every view in both themes.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402
from calendar_stub import ZONE, CalendarStub  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
HOUR = 48
"""The desktop grid's hour, in pixels (TimeGrid's hourHeight)."""


PAGE: list[Page] = []
"""The page being driven, for `wait_for` to wait on."""


def wait_for(what: str, test, timeout: float = 6.0):  # type: ignore[no-untyped-def]
    """Polls until ``test()`` is truthy and returns it; fails with ``what`` when it never is.

    It waits through the page, not with a sleep: the sync API runs a route's handler only while a
    call into Playwright is in progress, so a sleeping loop never let the stub see the request it
    was waiting for."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = test()
        if found:
            return found
        PAGE[-1].wait_for_timeout(50)
    raise AssertionError(f"never happened: {what}")


def writes(stub: CalendarStub, method: str, prefix: str) -> list[dict]:
    return [body for m, path, body in stub.writes if m == method and path.startswith(prefix)]


def no_sideways(page: Page, where: str) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"{where}: the page scrolls sideways by {wide}px"


def shot(page: Page, name: str) -> None:
    if SHOTS:
        page.wait_for_timeout(350)  # a sheet's entrance, or the picture is of it half faded in
        page.screenshot(path=f"{SHOTS}/{name}.png")


def open_calendar(page: Page, lang: str, view: str | None = None, query: str = "") -> None:
    page.goto(f"{BASE}/calendar?token=t&lang={lang}{query}")
    expect(page.locator(".cal-screen")).to_be_visible()
    if view is not None:
        phone = page.viewport_size["width"] < 640  # type: ignore[index]
        page.evaluate(f"localStorage.setItem('daedalus.calendar.view{'.phone' if phone else ''}', '{view}')")
        page.reload()
        expect(page.locator(".cal-screen")).to_be_visible()


def scroll_hours(page: Page, hour: float) -> None:
    page.locator(".cal-tg-scroll").evaluate(f"el => {{ el.scrollTop = {hour * HOUR}; }}")
    page.wait_for_timeout(50)


def slot_y(page: Page, day: str, minutes: int) -> tuple[float, float]:
    """The page coordinates of a minute in a day's column."""
    box = page.locator(f".cal-tg-col[data-day='{day}']").bounding_box()
    assert box is not None, f"no column for {day}"
    return box["x"] + box["width"] / 2, box["y"] + minutes / 60 * HOUR


def drag(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0], start[1] + 6, steps=2)
    page.mouse.move(*end, steps=8)
    page.mouse.up()


def desktop(browser, lang: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    en = lang == "en"
    stub = CalendarStub(lang=lang)
    zone = ZoneInfo(ZONE)
    today = datetime.now(zone).date()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, timezone_id=ZONE, locale="en-GB" if en else "ru-RU", color_scheme="dark")
    page = context.new_page()
    PAGE.append(page)
    page.on("pageerror", lambda error: errors.append(f"{lang} desktop: {error}"))
    page.route("**/api/**", lambda route: stub.fulfil(route) or shots.stub(route))
    open_calendar(page, lang)

    # The settings say "week", and nothing is remembered yet: the week, seven days, with today in it.
    heads = page.locator(".cal-tg-head")
    expect(heads).to_have_count(7)
    expect(page.locator(".cal-tg-head.today")).to_have_count(1)
    title = page.locator(".cal-title")
    first_title = title.inner_text()

    # Today, from two weeks away, comes back to this week and scrolls the hours to now.
    next_week = "Next week" if en else "Следующая неделя"
    page.get_by_role("button", name=next_week).click()
    page.get_by_role("button", name=next_week).click()
    expect(page.locator(".cal-tg-head.today")).to_have_count(0)
    assert title.inner_text() != first_title
    scroll_hours(page, 0)
    page.get_by_role("button", name="Today" if en else "Сегодня", exact=True).click()
    expect(page.locator(".cal-tg-head.today")).to_have_count(1)
    expect(title).to_have_text(first_title)
    now = datetime.now(zone)
    minutes = now.hour * 60 + now.minute
    top, limit = page.locator(".cal-tg-scroll").evaluate("el => [el.scrollTop, el.scrollHeight - el.clientHeight]")
    # Late in the day the grid cannot scroll as far as an hour and a half before now; it stops at its end.
    want = min(limit, max(0, minutes - 90) / 60 * HOUR)
    assert abs(top - want) < 6, f"Today scrolled to {top}px, not to now ({minutes} min, {want}px)"
    line = page.locator(".cal-now").bounding_box()
    scroller = page.locator(".cal-tg-scroll").bounding_box()
    assert line and scroller and scroller["y"] < line["y"] < scroller["y"] + scroller["height"], "the current-time line is not on screen after Today"
    shot(page, f"desk-{lang}-dark-week")

    # The keys switch views, and the view chosen survives a reload.
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("m")
    expect(page.locator(".cal-month")).to_be_visible()
    expect(page.locator(".cal-month-week").first).to_be_visible()
    shot(page, f"desk-{lang}-dark-month")
    page.keyboard.press("a")
    expect(page.locator(".cal-agenda")).to_be_visible()
    expect(page.locator(".cal-agenda-day.today")).to_be_visible()
    shot(page, f"desk-{lang}-dark-agenda")
    page.keyboard.press("d")
    expect(heads).to_have_count(1)
    shot(page, f"desk-{lang}-dark-day")
    page.keyboard.press("j")
    expect(page.locator(".cal-tg-head.today")).to_have_count(0)
    page.keyboard.press("t")
    expect(page.locator(".cal-tg-head.today")).to_have_count(1)
    page.get_by_role("radio", name="Month" if en else "Месяц").click()
    page.reload()
    expect(page.locator(".cal-month")).to_be_visible()
    page.keyboard.press("w")
    expect(heads).to_have_count(7)

    # Work week: Monday to Friday.
    page.get_by_role("button", name="Calendar menu" if en else "Меню календаря").click()
    page.get_by_role("menuitemcheckbox", name="Work week only (Mon–Fri)" if en else "Только рабочие дни (пн–пт)").click()
    page.keyboard.press("Escape")
    expect(heads).to_have_count(5)
    page.get_by_role("button", name="Calendar menu" if en else "Меню календаря").click()
    page.get_by_role("menuitemcheckbox", name="Work week only (Mon–Fri)" if en else "Только рабочие дни (пн–пт)").click()
    page.keyboard.press("Escape")
    expect(heads).to_have_count(7)

    if not en:
        context.close()
        return

    # Drag on empty time: 08:00 to 09:30 today, and the quick card holds that slot.
    day = str(today)
    scroll_hours(page, 6)
    drag(page, slot_y(page, day, 8 * 60 + 3), slot_y(page, day, 9 * 60 + 27))
    card = page.locator(".cal-quick")
    expect(card).to_be_visible()
    expect(card.get_by_label("Start time")).to_have_value(str(8 * 60))
    expect(card.get_by_label("End time")).to_have_value(str(9 * 60 + 30))
    card.get_by_label("Title").fill("Planning poker")
    card.get_by_label("Title").press("Enter")
    expect(card).to_be_hidden()
    made = wait_for("the new event posted", lambda: writes(stub, "POST", "/api/calendar/events"))[0]
    assert made["title"] == "Planning poker" and made["calendar_id"] == "cal-personal", made
    assert parse(made["start_at"]) == datetime(today.year, today.month, today.day, 8, tzinfo=zone), made
    block = page.locator(".cal-block", has_text="Planning poker")
    expect(block).to_be_visible()
    shot(page, "desk-en-dark-created")

    # Move it an hour earlier, then take it back with Undo.
    page.wait_for_timeout(300)
    box = block.bounding_box()
    assert box
    drag(page, (box["x"] + box["width"] / 2, box["y"] + 10), (box["x"] + box["width"] / 2, box["y"] + 10 - HOUR))
    moved = wait_for("the move sent", lambda: writes(stub, "PUT", "/api/calendar/events/ev-new"))
    assert parse(moved[-1]["start_at"]) == datetime(today.year, today.month, today.day, 7, tzinfo=zone), moved[-1]
    toast = page.locator(".toast")
    expect(toast).to_contain_text("Event moved")
    toast.get_by_role("button", name="Undo").click()
    wait_for("the undo sent", lambda: len(writes(stub, "PUT", "/api/calendar/events/ev-new")) == 2)
    assert parse(writes(stub, "PUT", "/api/calendar/events/ev-new")[-1]["start_at"]) == parse(made["start_at"])
    page.wait_for_timeout(300)

    # Resize it by its lower edge to end an hour later.
    block = page.locator(".cal-block", has_text="Planning poker")
    block.hover()
    edge = block.locator(".cal-resize").bounding_box()
    assert edge
    drag(page, (edge["x"] + edge["width"] / 2, edge["y"] + 3), (edge["x"] + edge["width"] / 2, edge["y"] + 3 + HOUR))
    resized = wait_for("the resize sent", lambda: len(writes(stub, "PUT", "/api/calendar/events/ev-new")) == 3 and writes(stub, "PUT", "/api/calendar/events/ev-new")[-1])
    assert parse(resized["end_at"]) - parse(resized["start_at"]) == timedelta(minutes=150), resized

    # A repeating event asks which ones before anything is sent.
    sync = page.locator(".cal-block[data-key^='e:ev-sync:']").first
    occurrence = (sync.get_attribute("data-key") or "")[len("e:ev-sync:"):]
    scroll_hours(page, 8)
    box = sync.bounding_box()
    assert box
    before = len(writes(stub, "PUT", "/api/calendar/events/ev-sync"))
    drag(page, (box["x"] + box["width"] / 2, box["y"] + 8), (box["x"] + box["width"] / 2, box["y"] + 8 + HOUR / 2))
    ask = page.get_by_role("alertdialog")
    expect(ask).to_contain_text("Change a repeating event")
    assert len(writes(stub, "PUT", "/api/calendar/events/ev-sync")) == before, "a repeating event was changed before the question was answered"
    ask.get_by_role("button", name="This event").click()
    sent = wait_for("the occurrence's change", lambda: writes(stub, "PUT", "/api/calendar/events/ev-sync")[before:])[0]
    assert sent["scope"] == "this" and sent["occurrence_start"] == occurrence, sent

    # Tasks: tick one off, and drop one from the inbox onto 11:00 today.
    tasks = page.locator(".cal-tasks")
    tasks.get_by_role("tab", name="Today").click()
    tasks.get_by_role("checkbox", name="Done: Pay the electricity bill").click()
    wait_for("the task completed", lambda: writes(stub, "POST", "/api/planner/tasks/task-bill/complete"))
    expect(page.locator(".toast")).to_contain_text("Task done")
    tasks.get_by_role("tab", name="Inbox").click()
    row = tasks.locator(".cal-task", has_text="Read the design doc")
    expect(row).to_be_visible()
    scroll_hours(page, 9)
    column = page.locator(f".cal-tg-col[data-day='{day}']")
    row.drag_to(column, target_position={"x": 30, "y": 11 * HOUR + 6})
    blocked = wait_for("the task scheduled", lambda: writes(stub, "PUT", "/api/planner/tasks/task-doc"))[0]
    assert parse(blocked["scheduled_start"]) == datetime(today.year, today.month, today.day, 11, tzinfo=zone), blocked
    assert parse(blocked["scheduled_end"]) - parse(blocked["scheduled_start"]) == timedelta(minutes=45), blocked
    expect(page.locator(".cal-block.task", has_text="Read the design doc")).to_be_visible()
    expect(page.locator(".cal-block.task", has_text="Read the design doc").get_by_role("checkbox")).to_be_visible()
    shot(page, "desk-en-dark-tasks")

    # Search from the keyboard; Enter opens the event.
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    search = page.get_by_role("combobox", name="Search events")
    expect(search).to_be_focused()
    search.fill("dentist")
    expect(page.locator(".cal-search-item").first).to_contain_text("Dentist")
    search.press("Enter")
    editor = page.get_by_role("dialog", name="Edit event")
    expect(editor.get_by_label("Title")).to_have_value("Dentist")
    expect(editor.get_by_label("Add a place")).to_have_value("12 Garden Street")
    shot(page, "desk-en-dark-editor")
    page.keyboard.press("Escape")
    expect(editor).to_be_hidden()

    # The editor sends a repeat preset as an RRULE. The search took the view to the dentist's day.
    page.keyboard.press("t")
    page.keyboard.press("d")
    scroll_hours(page, 9)
    page.locator(".cal-block", has_text="Focus time").click()
    editor = page.get_by_role("dialog", name="Edit event")
    editor.get_by_label("Repeat").select_option("weekdays")
    editor.get_by_role("button", name="Save").click()
    saved = wait_for("the focus block saved", lambda: writes(stub, "PUT", "/api/calendar/events/ev-focus"))[0]
    assert saved["recurrence"] == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" and saved["scope"] == "all", saved

    # The conflicted event says so in its editor and offers both versions.
    page.goto(f"{BASE}/calendar?token=t&lang=en&event=ev-planning")
    editor = page.get_by_role("dialog", name="Edit event")
    expect(editor.get_by_label("Title")).to_have_value("Quarterly planning")
    expect(editor.get_by_role("alert")).to_contain_text("Changed in two places")
    expect(editor.get_by_role("button", name="Use theirs")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)

    # Deep links: an occurrence of a series, and a task.
    page.goto(f"{BASE}/calendar?token=t&lang=en&event=ev-sync:{occurrence}")
    expect(page.get_by_role("dialog", name="Edit event").get_by_label("Title")).to_have_value("Weekly team sync")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.goto(f"{BASE}/calendar?token=t&lang=en&task=task-grandma")
    expect(page.get_by_role("dialog", name="Task").get_by_label("Title")).to_have_value("Call grandma")
    assert "task=" not in page.url, "the deep link stays in the address after it was opened"
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)

    # Connections: the failed account's conflict is resolved by its buttons, and a provider shows its steps.
    page.get_by_role("button", name="Calendar menu").click()
    page.get_by_role("menuitem", name="Connected calendars").click()
    sheet = page.get_by_role("dialog", name="Connected calendars")
    yandex = sheet.locator(".cal-account", has_text="Yandex")
    expect(yandex).to_contain_text("Sync failed")
    expect(yandex).to_contain_text("412 Precondition Failed")
    shot(page, "desk-en-dark-connections")
    yandex.get_by_role("button", name="Keep mine").click()
    resolved = wait_for("the conflict resolved", lambda: writes(stub, "POST", "/api/calendar/events/ev-planning/resolve"))[0]
    assert resolved == {"choice": "local"}, resolved
    sheet.get_by_role("button", name="iCloud").click()
    expect(page.locator(".cal-steps li")).to_have_count(2)
    expect(page.get_by_label("App password")).to_be_visible()
    page.get_by_role("button", name="All providers").click()
    sheet = page.get_by_role("dialog", name="Connected calendars")
    sheet.get_by_role("button", name="Subscription (ICS)").click()
    page.get_by_label("Calendar address (.ics)").fill("https://example.org/holidays.ics")
    page.get_by_role("button", name="Subscribe").click()
    subscribed = wait_for("the subscription", lambda: writes(stub, "POST", "/api/calendar/subscriptions"))[0]
    assert subscribed["url"] == "https://example.org/holidays.ics", subscribed
    page.keyboard.press("Escape")

    # Settings: weekends off makes the week five days.
    page.keyboard.press("w")
    page.get_by_role("button", name="Calendar settings").click()
    settings = page.get_by_role("dialog", name="Calendar settings")
    settings.get_by_role("switch", name="Show weekends").click()
    settings.get_by_role("button", name="Save").click()
    wait_for("the settings saved", lambda: writes(stub, "PUT", "/api/calendar/settings"))
    expect(heads).to_have_count(5)

    # Keyboard help is one key away.
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("?")
    expect(page.get_by_role("dialog", name="Keyboard shortcuts")).to_be_visible()
    page.keyboard.press("Escape")
    no_sideways(page, "desktop")
    context.close()


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def swipe(page: Page, selector: str, dx: int) -> None:
    """A finger sliding sideways across the view, as touch events."""
    page.locator(selector).evaluate(
        """(el, dx) => {
            const r = el.getBoundingClientRect();
            const y = r.top + r.height / 2, x = r.left + r.width / 2;
            const touch = (cx) => new Touch({ identifier: 1, target: el, clientX: cx, clientY: y });
            el.dispatchEvent(new TouchEvent("touchstart", { bubbles: true, touches: [touch(x)], changedTouches: [touch(x)] }));
            el.dispatchEvent(new TouchEvent("touchend", { bubbles: true, touches: [], changedTouches: [touch(x + dx)] }));
        }""",
        dx,
    )


def phone(browser, lang: str, scheme: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    en = lang == "en"
    stub = CalendarStub(lang=lang)
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, timezone_id=ZONE, locale="en-GB" if en else "ru-RU", color_scheme=scheme)
    page = context.new_page()
    PAGE.append(page)
    page.on("pageerror", lambda error: errors.append(f"{lang} phone: {error}"))
    page.route("**/api/**", lambda route: stub.fulfil(route) or shots.stub(route))
    open_calendar(page, lang)
    tag = f"phone-{lang}-{scheme}"

    # A week does not fit; the phone shows three days, with the week strip above.
    expect(page.locator(".cal-tg-head")).to_have_count(3)
    expect(page.locator(".cal-strip")).to_be_visible()
    no_sideways(page, f"{lang} phone three days")
    for name in ("search", "today"):
        button = page.get_by_role("button", name={"search": "Search events" if en else "Поиск событий", "today": "Today" if en else "Сегодня"}[name], exact=True)
        box = button.bounding_box()
        assert box and box["width"] >= 44 and box["height"] >= 44, f"{lang} phone: the {name} button is {box}"
    fab = page.locator(".cal-fab")
    box = fab.bounding_box()
    assert box and box["width"] >= 44, f"the add button is {box}"
    # No bottom bar on a phone outside a project: the add button sits above the screen's edge, and the
    # app's drawer is one tap away from the planner's header.
    expect(page.locator("nav.tabbar")).to_have_count(0)
    assert box["y"] + box["height"] <= page.viewport_size["height"] - 8, f"the add button runs off the screen: {box}"
    expect(page.locator(".cal-head .ph-menu")).to_be_visible()
    shot(page, f"{tag}-3day")

    # The views, each readable: words on screen, not only coloured marks.
    def pick(view: str) -> None:
        page.locator(".cal-view-pick").click()
        page.get_by_role("menuitem", name=view, exact=True).click()

    pick("Day" if en else "День")
    expect(page.locator(".cal-tg-col")).to_have_count(1)
    expect(page.locator(".cal-block", has_text="Lunch with Anna" if en else "Обед с Анной")).to_be_visible()
    size = page.locator(".cal-block-title").first.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    assert size >= 12, f"event titles are {size}px on a phone"
    no_sideways(page, f"{lang} phone day")
    shot(page, f"{tag}-day")

    # A swipe left is the next day, right the one before.
    before = page.locator(".cal-title").inner_text()
    swipe(page, ".cal-main", -140)
    expect(page.locator(".cal-title")).not_to_have_text(before)
    swipe(page, ".cal-main", 140)
    expect(page.locator(".cal-title")).to_have_text(before)

    pick("Month" if en else "Месяц")
    expect(page.locator(".cal-month.compact")).to_be_visible()
    expect(page.locator(".cal-month-day .cal-row").first).to_be_visible()
    expect(page.locator(".cal-month-day")).to_contain_text("Focus time" if en else "Глубокая работа")
    no_sideways(page, f"{lang} phone month")
    shot(page, f"{tag}-month")

    pick("Agenda" if en else "Список")
    expect(page.locator(".cal-agenda-day").first).to_be_visible()
    expect(page.locator(".cal-agenda")).to_contain_text("Dentist" if en else "Стоматолог")
    no_sideways(page, f"{lang} phone agenda")
    shot(page, f"{tag}-agenda")

    # The add button: a bottom sheet, then the full editor, also from the bottom.
    fab.click()
    sheet = page.locator(".sheet.cal-quick-sheet")
    expect(sheet).to_be_visible()
    sheet.get_by_label("Title" if en else "Название").fill("Pick up the bike" if en else "Забрать велосипед")
    sheet.get_by_role("button", name="More options" if en else "Подробнее").click()
    editor = page.locator(".sheet.cal-editor")
    expect(editor).to_be_visible()
    expect(editor.get_by_label("Title" if en else "Название")).to_have_value("Pick up the bike" if en else "Забрать велосипед")
    page.wait_for_timeout(400)  # the sheet's rise
    box = editor.bounding_box()
    assert box and abs(box["y"] + box["height"] - 844) < 2, f"the phone editor is not a bottom sheet: {box}"
    assert box["width"] <= 390, box
    no_sideways(page, f"{lang} phone editor")
    shot(page, f"{tag}-editor")
    editor.get_by_role("button", name="Save" if en else "Сохранить").click()
    wait_for("the phone's event", lambda: writes(stub, "POST", "/api/calendar/events"))

    # Today from another month on the phone's own button.
    pick("Month" if en else "Месяц")
    swipe(page, ".cal-main", -140)
    page.get_by_role("button", name="Today" if en else "Сегодня", exact=True).click()
    expect(page.locator(".cal-month.compact .today.selected")).to_have_count(1)
    context.close()


def run() -> int:
    expect_app(BASE)
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        desktop(browser, "en", errors)
        desktop(browser, "ru", errors)
        phone(browser, "en", "dark", errors)
        phone(browser, "ru", "light", errors)
        if SHOTS:
            light_pictures(browser)
        browser.close()
    assert not errors, errors
    return shots.UNHANDLED.report()


def answer(stub: CalendarStub):  # type: ignore[no-untyped-def]
    """A route handler for one stub. Not a default argument: Playwright hands a two-argument handler
    the request as its second, which then stood in for the stub."""
    return lambda route: stub.fulfil(route) or shots.stub(route)


def light_pictures(browser) -> None:  # type: ignore[no-untyped-def]
    """The desktop views in the light theme and in Russian, for looking at."""
    for lang in ("en", "ru"):
        stub = CalendarStub(lang=lang)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, timezone_id=ZONE, locale="en-GB" if lang == "en" else "ru-RU", color_scheme="light")
        page = context.new_page()
        page.route("**/api/**", answer(stub))
        for view in ("week", "day", "month", "agenda"):
            open_calendar(page, lang, view)
            page.wait_for_timeout(400)
            shot(page, f"desk-{lang}-light-{view}")
        context.close()
    for lang, scheme in (("en", "light"), ("ru", "dark")):
        stub = CalendarStub(lang=lang)
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, timezone_id=ZONE, locale="en-GB", color_scheme=scheme)
        page = context.new_page()
        page.route("**/api/**", answer(stub))
        for view in ("day", "month", "agenda"):
            open_calendar(page, lang, view)
            page.wait_for_timeout(400)
            shot(page, f"phone-{lang}-{scheme}-{view}")
        context.close()


if __name__ == "__main__":
    raise SystemExit(run())
