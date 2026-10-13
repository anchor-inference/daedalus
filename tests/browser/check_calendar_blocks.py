"""Structured calendar entries in a real browser: the view sheet, its blocks, the agenda's totals.

What is checked, at 390x844 and at 1440x900, in the light and the dark theme:

- A tap on an event opens it to be read, not the editor. A fifty-item checklist on a phone is one
  full-height sheet with one scroll, no text field, every row a 44 px checkbox; one tap ticks an item,
  the head's "n/50" follows at once, the tick is sent as a point patch, the toast takes it back, and
  the agenda's row shows the new count once the list is read again.
- A basket of 44 rows with a money column summed and a checkbox column counted shows its totals; on
  a phone it is cards that fit the screen, on a desktop a table with its first column held. A block
  of a kind the app does not know is shown folded, not dropped.
- An old event with only a description reads as Markdown, and deleting it can be undone.
- The agenda shows each day's totals with their target marks; a phone's month shows a dot per kind
  of thing and the day's list with its totals under it; a recipe's steps offer their timer; the
  plan's page lists its days. A swipe on a phone's row marks it done.

    cd miniapp && npm run build && cd ..
    python3 tests/browser/serve_app.py 8230 /tmp/app-root &
    APP_URL=http://127.0.0.1:8230/app python3 tests/browser/check_calendar_blocks.py

With SHOTS=<dir>, the Russian pictures of every screen named above in both themes and sizes.
Exit 0 when everything holds.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402
from calendar_stub import ZONE, CalendarStub  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
PAGE: list[Page] = []

WORDS = {
    "ru": {"trip": "Собрать вещи в поездку", "shop": "Заказ Wolt №1", "breakfast": "Завтрак · 518", "dentist": "Стоматолог", "undo": "Вернуть", "delete": "Удалить", "menu": "Действия с событием", "edit": "Изменить событие"},
    "en": {"trip": "Pack for the trip", "shop": "Grocery order #1", "breakfast": "Breakfast · 518", "dentist": "Dentist", "undo": "Undo", "delete": "Delete", "menu": "Event actions", "edit": "Edit event"},
}


def wait_for(what: str, test, timeout: float = 6.0):  # type: ignore[no-untyped-def]
    """Polls through the page (a sleeping loop never lets the stub's route handler run)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = test()
        if found:
            return found
        PAGE[-1].wait_for_timeout(50)
    raise AssertionError(f"never happened: {what}")


def writes(stub: CalendarStub, method: str, needle: str) -> list[dict]:
    return [body or {} for m, path, body in stub.writes if m == method and needle in path]


def shot(page: Page, name: str) -> None:
    if SHOTS:
        page.wait_for_timeout(350)  # a sheet's entrance
        # The toast of the tick just made would stand over the picture; it is hidden for the
        # picture only, since the check presses its Undo afterwards.
        page.evaluate("document.querySelectorAll('.toast').forEach((t) => { t.style.visibility = 'hidden'; })")
        page.screenshot(path=f"{SHOTS}/{name}.png")
        page.evaluate("document.querySelectorAll('.toast').forEach((t) => { t.style.visibility = ''; })")


def no_sideways(page: Page, where: str) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"{where}: the page scrolls sideways by {wide}px"


TARGET = """el => {
    // The box a finger can hit: the element and any absolutely placed ::before or ::after that
    // reaches past it, which is how a 24 px pill or a 22 px square gets a 44 px target.
    const r = el.getBoundingClientRect();
    let [top, left, bottom, right] = [r.top, r.left, r.bottom, r.right];
    for (const which of ["::before", "::after"]) {
        const s = getComputedStyle(el, which);
        if (s.content === "none" || s.position !== "absolute") continue;
        const px = (v) => (v.endsWith("px") ? parseFloat(v) : 0);
        top = Math.min(top, r.top + px(s.top)); left = Math.min(left, r.left + px(s.left));
        bottom = Math.max(bottom, r.bottom - px(s.bottom)); right = Math.max(right, r.right - px(s.right));
    }
    return [right - left, bottom - top];
}"""


def targets_at_least(locator: Locator, where: str, size: int = 44) -> None:
    for i in range(min(locator.count(), 60)):
        width, height = locator.nth(i).evaluate(TARGET)
        assert width >= size - 0.5 and height >= size - 0.5, f"{where}: target {i} is {width:.0f}x{height:.0f}"


def one_scroll(sheet: Locator, where: str) -> None:
    """Nothing in the sheet scrolls up and down on its own but its body: a list inside a list was the
    wall of text this replaces."""
    nested = sheet.evaluate("""el => [...el.querySelectorAll('*')].filter((n) => {
        if (n.classList.contains('sheet-body')) return false;
        const s = getComputedStyle(n);
        return (s.overflowY === 'auto' || s.overflowY === 'scroll') && n.scrollHeight > n.clientHeight + 1;
    }).map((n) => n.className)""")
    assert not nested, f"{where}: scrolls of their own inside the sheet: {nested}"
    assert sheet.locator("textarea").count() == 0, f"{where}: the view sheet has a text field"


def open_row(page: Page, text: str) -> Locator:
    page.locator(".cal-row-open", has_text=text).first.click()
    sheet = page.locator(".sheet.cal-view").last
    expect(sheet).to_be_visible()
    return sheet


def close(page: Page) -> None:
    page.keyboard.press("Escape")
    expect(page.locator(".sheet.cal-view")).to_have_count(0)


def scenario(browser, lang: str, phone: bool, scheme: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    w = WORDS[lang]
    stub = CalendarStub(lang=lang)
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, timezone_id=ZONE, locale="ru-RU" if lang == "ru" else "en-GB", color_scheme=scheme)
    page = context.new_page()
    PAGE.append(page)
    where = f"{lang} {'phone' if phone else 'desktop'} {scheme}"
    page.on("pageerror", lambda error: errors.append(f"{where}: {error}"))
    page.route("**/api/**", lambda route: stub.fulfil(route) or shots.stub(route))
    tag = f"{'phone' if phone else 'desk'}-{lang}-{scheme}"

    page.goto(f"{BASE}/calendar?token=t&lang={lang}")
    expect(page.locator(".cal-screen")).to_be_visible()
    if not phone:
        page.evaluate("localStorage.setItem('daedalus.calendar.view', 'agenda')")
        page.reload()
    # A phone opens on the agenda; every day of the plan carries its totals, one inside the calorie
    # target and one outside it.
    expect(page.locator(".cal-agenda-day").first).to_be_visible()
    expect(page.locator(".cal-totals-day .cal-agg").first).to_be_visible()
    assert page.locator(".cal-agg.ok").count() >= 1, f"{where}: no day total met its target"
    assert page.locator(".cal-agg.low, .cal-agg.high").count() >= 1, f"{where}: no day total missed its target"
    assert "Σ" in page.locator(".cal-agg").first.inner_text()
    no_sideways(page, f"{where} agenda")
    shot(page, f"{tag}-agenda")

    # Criterion 1: fifty items, one scroll, one tap.
    sheet = open_row(page, w["trip"])
    boxes = sheet.locator(".cal-checklist [role=checkbox]")
    expect(boxes).to_have_count(50)
    if phone:
        page.wait_for_timeout(300)  # the sheet's rise
        box = sheet.bounding_box()
        assert box and box["height"] >= 844 - 1 and box["width"] <= 390, f"{where}: the view is not the whole screen: {box}"
    one_scroll(sheet, where)
    targets_at_least(boxes, f"{where} checklist rows")
    targets_at_least(sheet.locator(".cal-blk-head"), f"{where} block heads")
    head = sheet.locator(".sheet-head .cal-progress-text")
    expect(head).to_have_text("7/50")
    first_open = sheet.locator(".cal-checklist [role=checkbox][aria-checked=false]").first
    name = first_open.inner_text()
    first_open.click()
    expect(sheet.locator(".cal-checklist [role=checkbox]", has_text=name)).to_have_attribute("aria-checked", "true")
    expect(head).to_have_text("8/50")
    sent = wait_for("the tick sent as a patch", lambda: writes(stub, "PATCH", "/trip-pack/blocks"))
    assert sent[-1]["ops"][0]["op"] == "set_checked" and sent[-1]["ops"][0]["checked"] is True and "version" not in sent[-1], sent[-1]
    page.locator(".toast").get_by_role("button", name=w["undo"]).click()
    expect(head).to_have_text("7/50")
    wait_for("the undo sent", lambda: len(writes(stub, "PATCH", "/trip-pack/blocks")) >= 2 and writes(stub, "PATCH", "/trip-pack/blocks")[-1]["ops"][0]["checked"] is False)
    sheet.locator(".cal-checklist [role=checkbox]", has_text=name).click()
    expect(head).to_have_text("8/50")
    wait_for("the second tick sent", lambda: len(writes(stub, "PATCH", "/trip-pack/blocks")) >= 3)
    shot(page, f"{tag}-checklist")
    close(page)
    expect(page.locator(".cal-row-wrap", has_text=w["trip"]).locator(".cal-progress-text")).to_have_text("8/50")

    # Criterion 2: the basket's totals, and nothing cut off on a phone.
    sheet = open_row(page, w["shop"])
    expect(sheet.locator(".cal-totals")).to_be_visible()
    totals = sheet.locator(".cal-totals").inner_text()
    assert "₸" in totals and "15/44" in totals, f"{where}: the basket's totals read {totals!r}"
    if phone:
        expect(sheet.locator(".cal-cards .cal-card")).to_have_count(44)
        assert sheet.locator("table").count() == 0, f"{where}: a table on a phone"
        cut = sheet.evaluate("""el => [...el.querySelectorAll('.cal-card')].filter((c) => c.getBoundingClientRect().right > window.innerWidth + 0.5 || c.scrollWidth > c.clientWidth + 1).length""")
        assert cut == 0, f"{where}: {cut} cards are cut off"
        targets_at_least(sheet.locator(".cal-card-hit"), f"{where} basket cards")
        sheet.locator(".cal-card:not(.on) .cal-card-hit").first.click()
    else:
        expect(sheet.locator("table.cal-table tbody tr")).to_have_count(44)
        sticky = sheet.locator("table.cal-table tbody th").first.evaluate("el => getComputedStyle(el).position")
        assert sticky == "sticky", f"{where}: the basket's first column is not held: {sticky}"
        targets_at_least(sheet.locator("table.cal-table [role=checkbox]"), f"{where} basket checkboxes")
        sheet.locator("table.cal-table tr:not(.on) [role=checkbox]").first.click()
    expect(sheet.locator(".cal-totals")).to_contain_text("16/44")
    wait_for("the cell's tick sent", lambda: any(op.get("column") for body in writes(stub, "PATCH", "/shop-1/blocks") for op in body["ops"]))
    unknown = sheet.locator(".cal-blk.kind-unknown")
    expect(unknown).to_have_count(1)
    expect(unknown.locator(".cal-blk-head")).to_have_attribute("aria-expanded", "false")
    one_scroll(sheet, f"{where} basket")
    no_sideways(page, f"{where} basket")
    shot(page, f"{tag}-shopping")
    close(page)

    # A recipe's steps: big numbers that tick, and a timer where a step takes time.
    sheet = open_row(page, w["breakfast"])
    expect(sheet.locator(".cal-step").first).to_be_visible()
    assert sheet.locator(".cal-timer").count() >= 1, f"{where}: no step offers a timer"
    targets_at_least(sheet.locator(".cal-step-n"), f"{where} step numbers")
    targets_at_least(sheet.locator(".cal-status"), f"{where} status")
    sheet.locator(".cal-steps-view").scroll_into_view_if_needed()
    shot(page, f"{tag}-meal")
    # The plan's page from the event's group.
    sheet.locator(".cal-chipbtn.group").click()
    plan = page.locator(".sheet.cal-group-page")
    expect(plan).to_be_visible()
    expect(plan.locator(".cal-agenda-day")).to_have_count(7)
    expect(plan.locator(".cal-totals-day").first).to_be_visible()
    no_sideways(page, f"{where} plan")
    shot(page, f"{tag}-group")
    page.keyboard.press("Escape")
    expect(plan).to_have_count(0)

    # Criterion 7: an old event of a description only reads as Markdown; criterion 8: its delete
    # can be undone, and nothing is sent until the undo has had its time.
    sheet = open_row(page, w["dentist"])
    md = sheet.locator(".cal-md")
    expect(md).to_be_visible()
    assert md.locator("b, strong").count() >= 1 and md.locator("li").count() >= 1 and md.locator("a").count() >= 1, f"{where}: the description is not drawn as Markdown"
    assert sheet.locator(".cal-blk").count() == 0
    shot(page, f"{tag}-description")
    sheet.get_by_role("button", name=w["menu"]).click()
    page.get_by_role("menuitem", name=w["delete"]).click()
    expect(page.locator(".cal-row-open", has_text=w["dentist"])).to_have_count(0)
    page.locator(".toast").get_by_role("button", name=w["undo"]).click()
    expect(page.locator(".cal-row-open", has_text=w["dentist"]).first).to_be_visible()
    assert not writes(stub, "DELETE", "/api/calendar/events/ev-dentist"), f"{where}: the undone delete was sent"

    if phone:
        # A swipe right on a planned meal marks it done.
        row = page.locator(".cal-row-wrap", has_text="Обед · 720" if lang == "ru" else "Lunch · 720").first.locator(".cal-row")
        row.evaluate(
            """el => {
                const r = el.getBoundingClientRect();
                const y = r.top + r.height / 2, x = r.left + 40;
                const touch = (cx) => new Touch({ identifier: 1, target: el, clientX: cx, clientY: y });
                el.dispatchEvent(new TouchEvent("touchstart", { bubbles: true, touches: [touch(x)], changedTouches: [touch(x)] }));
                for (const dx of [20, 60, 110]) el.dispatchEvent(new TouchEvent("touchmove", { bubbles: true, touches: [touch(x + dx)], changedTouches: [touch(x + dx)] }));
                el.dispatchEvent(new TouchEvent("touchend", { bubbles: true, touches: [], changedTouches: [touch(x + 110)] }));
            }"""
        )
        sent_status = wait_for("the swipe's status", lambda: writes(stub, "POST", "/status"))
        assert sent_status[-1] == {"status": "done"}, sent_status

        # The phone's month: a dot per kind of thing, and the picked day's list with its totals.
        page.locator(".cal-view-pick").click()
        page.get_by_role("menuitem", name="Месяц" if lang == "ru" else "Month", exact=True).click()
        expect(page.locator(".cal-month.compact")).to_be_visible()
        assert page.locator(".cal-month.compact .cal-mark.typed").count() >= 7, f"{where}: the month draws no type dots"
        widest = page.evaluate("Math.max(...[...document.querySelectorAll('.cal-month.compact .cal-month-marks')].map((m) => m.scrollWidth - m.parentElement.clientWidth))")
        assert widest <= 0, f"{where}: a day's dots run out of their cell by {widest}px"
        expect(page.locator(".cal-month-day .cal-totals-day")).to_be_visible()
        no_sideways(page, f"{where} month")
        shot(page, f"{tag}-month")
    context.close()


def run() -> int:
    expect_app(BASE)
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for phone in (True, False):
            for scheme in ("dark", "light"):
                scenario(browser, "ru", phone, scheme, errors)
        scenario(browser, "en", True, "dark", errors)
        scenario(browser, "en", False, "light", errors)
        browser.close()
    assert not errors, errors
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
