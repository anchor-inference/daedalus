"""The column beside a staff member's terminal, dragged wider and narrower, and kept per browser.

At 1440 px, in English and in Russian: its edge drags it wider (the terminal narrower) and the width
is kept across a reload; it never leaves the terminal less than 480 px or itself less than 260; the
arrow keys move the focused edge; a double click gives the stylesheet's width back and forgets the
kept one; the terminal is told its new size a handful of times over a whole drag, not once per frame.
On a phone the column is a sheet, as before, and has no edge to drag.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_staff_aside.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_staff_view import DESK, PHONE, Check, open_page, sideways, stand  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from terminal_stub import DEBUG, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
KEY = "daedalus.staff.asideWidth"

def box(page: Page, selector: str) -> dict:
    found = page.locator(selector).bounding_box()
    assert found is not None, selector
    return found


def drag(page: Page, dx: float, steps: int = 24) -> None:
    """The aside's edge, pressed at its middle and moved by ``dx`` in ``steps`` moves."""
    edge = box(page, ".staff-aside > .pane-handle")
    x, y = edge["x"] + edge["width"] / 2, edge["y"] + edge["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(x + dx * i / steps, y)
        page.wait_for_timeout(16)
    page.mouse.up()
    page.wait_for_timeout(300)


def panel(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    focus, term, pid = stand(lang)
    feed = EventFeed()
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    context.add_init_script(DEBUG)
    page = open_page(context, focus, term, feed, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-term .term-view[data-terminal-view='tm-ira']", timeout=15000)
    wait_live(page, "tm-ira")
    page.wait_for_timeout(800)
    start = box(page, ".staff-aside")["width"]
    term_start = box(page, ".staff-term")["width"]
    check.that(280 <= start <= 360, f"{lang}: the column starts {start} px wide, not the stylesheet's 280–360")
    before = len(term.resizes("tm-ira"))

    # Wider by 200 px: the terminal gives up the same, and is told its size a few times, not per frame.
    drag(page, -200)
    page.wait_for_timeout(400)
    wide = box(page, ".staff-aside")["width"]
    check.that(abs(wide - (start + 200)) <= 2, f"{lang}: dragged 200 px, the column went {start} → {wide}")
    check.that(abs(box(page, ".staff-term")["width"] - (term_start - (wide - start))) <= 2, f"{lang}: the terminal did not give up the column's gain")
    sent = term.resizes("tm-ira")[before:]
    check.that(1 <= len(sent) <= 6, f"{lang}: a 24-step drag sent {len(sent)} RESIZE frames: {sent}")
    stored = page.evaluate(f"localStorage.getItem('{KEY}')")
    check.that(stored == str(round(wide)), f"{lang}: the kept width is {stored!r}, not {round(wide)}")

    # Kept across a reload.
    page.reload()
    page.wait_for_selector(".staff-aside", timeout=15000)
    page.wait_for_timeout(500)
    check.that(abs(box(page, ".staff-aside")["width"] - wide) <= 1, f"{lang}: after a reload the column is {box(page, '.staff-aside')['width']} px, not {wide}")

    # Held: never past what leaves the terminal 480 px, never under 260.
    drag(page, -2000)
    body = box(page, ".chat-body")["width"]
    held = box(page, ".staff-aside")["width"]
    check.that(held <= min(760, body - 480) + 1, f"{lang}: dragged far left the column is {held} px of a {body} px row")
    drag(page, 2000)
    check.that(abs(box(page, ".staff-aside")["width"] - 260) <= 1, f"{lang}: dragged far right the column is {box(page, '.staff-aside')['width']} px, not 260")

    # The keys: ← widens the column by one step (it sits on the right), → narrows it.
    handle = page.locator(".staff-aside > .pane-handle")
    handle.focus()
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(200)
    check.that(abs(box(page, ".staff-aside")["width"] - 292) <= 1, f"{lang}: two ← made the column {box(page, '.staff-aside')['width']} px, not 292")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(200)
    check.that(abs(box(page, ".staff-aside")["width"] - 276) <= 1, f"{lang}: → made the column {box(page, '.staff-aside')['width']} px, not 276")

    # A double click gives the stylesheet its width back and forgets the kept one.
    handle.dblclick()
    page.wait_for_timeout(300)
    back = box(page, ".staff-aside")["width"]
    check.that(abs(back - start) <= 1, f"{lang}: a double click left the column at {back} px, not {start}")
    check.that(page.evaluate(f"localStorage.getItem('{KEY}')") is None, f"{lang}: the kept width outlived the reset")
    check.that(sideways(page) <= 0, f"{lang}: the staff view scrolls sideways")
    context.close()
    feed.close()


def phone(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    focus, term, pid = stand(lang)
    feed = EventFeed()
    context = browser.new_context(viewport=PHONE, color_scheme="dark", is_mobile=True, has_touch=True)
    context.add_init_script(DEBUG)
    context.add_init_script(f"try {{ localStorage.setItem('{KEY}', '600'); }} catch (e) {{}}")
    page = open_page(context, focus, term, feed, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-cli .feed-turn", timeout=15000)
    check.that(page.locator(".staff-aside").count() == 0, f"{lang} phone: a column is drawn beside the Feed")
    page.locator(".staff-cli .ph-top .ph-ib").last.tap()
    expect(page.locator(".sheet .staff-panel")).to_be_visible(timeout=5000)
    check.that(page.locator(".sheet .pane-handle").count() == 0, f"{lang} phone: the sheet has an edge to drag")
    check.that(sideways(page) <= 0, f"{lang} phone: the sheet scrolls sideways by {sideways(page)} px")
    context.close()
    feed.close()


def run() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            panel(browser, lang, check)
            phone(browser, lang, check)
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL", problem)
    if not check.problems and not unhandled:
        print("the column beside the terminal holds")
    return 1 if check.problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
