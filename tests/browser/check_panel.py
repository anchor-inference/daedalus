"""Drive the right panel in a real browser and refuse what does not behave.

The panel's state has a unit test (miniapp/src/panel.test.ts); this is the part that only exists
once it is drawn: a file opened from Files lands in Preview with its breadcrumb, Back and Forward
walk the history, the left edge drags and the width survives a reload, the expansion covers the
conversation and Escape restores it, the close button closes, the link reproduces the view, the
keyboard toggles it, and a phone gets the same tabs as a sheet.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_panel.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, S2, UNHANDLED, phone_tool, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def open_page(context, route: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang=en")
    page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
    page.wait_for_timeout(500)
    return page


def width(page: Page, sel: str) -> int:
    return page.evaluate(f"() => {{ const el = document.querySelector('{sel}'); return el ? Math.round(el.getBoundingClientRect().width) : 0; }}")


def query(page: Page) -> str:
    return page.evaluate("() => location.search")


def step(page: Page, label: str) -> None:
    """One line per step, so a failure names where the panel was when it stopped behaving."""
    print(f"{label}: panel={page.locator('.panel').count()} full={page.locator('.panel.full').count()} query={query(page)!r}")


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
    page = open_page(context, f"agents/{S1}")

    # Open by default on a wide window, on Details, beside a conversation that keeps ≥ 800 px.
    if not page.locator(".panel .panel-tab.on[data-tab='details']").count():
        problems.append("the panel did not open on Details")
    w0 = width(page, ".panel")
    chat0 = width(page, ".chat-main")
    print(f"panel {w0}px, conversation {chat0}px")
    if w0 < 360 or chat0 < 600:
        problems.append(f"panel {w0}px / conversation {chat0}px at 1440")
    if page.locator(".chat-head").evaluate("(el) => Math.round(el.getBoundingClientRect().height)") != 48:
        problems.append("the header is not 48px")

    # Files → a file → Preview, with the breadcrumb and the route.
    page.locator(".panel-tab[data-tab='files']").click()
    page.wait_for_selector(".panel-files .filerow", timeout=5000)
    if "panel=files" not in query(page):
        problems.append(f"the route does not say Files: {query(page)}")
    page.locator(".panel-files .filerow .title", has_text="NOTES.md").click()
    page.wait_for_selector(".panel-body.tab-preview .preview-doc", timeout=5000)
    crumbs = page.locator(".panel-crumbs").inner_text()
    print("crumbs:", crumbs.replace("\n", " "))
    if "NOTES.md" not in crumbs or "Bakery site" not in crumbs:
        problems.append(f"the breadcrumb does not name the project and the file: {crumbs!r}")
    if "panel=preview" not in query(page) or "path=NOTES.md" not in query(page):
        problems.append(f"the route does not carry the previewed file: {query(page)}")
    if not page.locator(".panel-toolbar button[aria-label='Back'][disabled]").count():
        problems.append("Back is enabled with one file in the history")

    # A second file: Back returns to the first, Forward to the second.
    page.locator(".panel-tab[data-tab='files']").click()
    page.wait_for_selector(".panel-files .filerow", timeout=5000)
    page.locator(".panel-files .filerow .title", has_text="PLAN.md").click()
    page.wait_for_selector(".panel-body.tab-preview .preview-doc", timeout=5000)
    page.wait_for_timeout(300)
    page.locator(".panel-toolbar button[aria-label='Back']").click()
    page.wait_for_timeout(500)
    if "NOTES.md" not in page.locator(".panel-crumbs").inner_text():
        problems.append("Back did not return to the first file")
    if "path=NOTES.md" not in query(page):
        problems.append(f"Back did not update the route: {query(page)}")
    page.locator(".panel-toolbar button[aria-label='Forward']").click()
    page.wait_for_timeout(500)
    if "PLAN.md" not in page.locator(".panel-crumbs").inner_text():
        problems.append("Forward did not return to the second file")
    page.locator(".panel-toolbar button[aria-label='Reload']").click()
    page.wait_for_selector(".panel-body.tab-preview .preview-doc", timeout=5000)

    # The browser's Back closes the panel (opening pushed one entry).
    page.go_back()
    page.wait_for_timeout(500)
    print("after history back:", query(page), "panel:", page.locator(".panel").count())
    if page.locator(".panel").count():
        problems.append("the browser's Back did not close the panel")

    # The link reproduces the view.
    page = open_page(context, f"agents/{S1}?panel=preview&path=reports/menu-check.md")
    if not page.locator(".panel-tab.on[data-tab='preview']").count():
        problems.append("a link with ?panel=preview did not open the Preview tab")
    if "menu-check.md" not in page.locator(".panel-crumbs").inner_text():
        problems.append("a link with ?path= did not open that file")

    # Drag the left edge: wider, then the width survives a reload.
    page = open_page(context, f"agents/{S1}")
    handle = page.locator(".panel .pane-handle")
    box = handle.bounding_box()
    assert box
    before = width(page, ".panel")
    x = box["x"] + box["width"] / 2
    y = box["y"] + 200
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 120, y, steps=8)
    page.mouse.up()
    page.wait_for_timeout(300)
    after = width(page, ".panel")
    print(f"drag: {before} → {after}")
    if after < before + 100:
        problems.append(f"dragging the edge 120px left grew the panel from {before} to {after}")
    page.reload()
    page.wait_for_selector(".panel", timeout=15000)
    page.wait_for_timeout(400)
    kept = width(page, ".panel")
    if abs(kept - after) > 3:
        problems.append(f"the width did not survive a reload: {after} → {kept}")
    stored = page.evaluate("() => localStorage.getItem('daedalus.width.panel')")
    print("stored width:", stored)
    if not stored or not stored.replace(".", "").isdigit() or float(stored) > 65:
        problems.append(f"the width is not stored as a percentage: {stored!r}")
    # Dragging far past the ceiling stops at 65 %.
    box = page.locator(".panel .pane-handle").bounding_box()
    assert box
    x = box["x"] + box["width"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 900, y, steps=10)
    page.mouse.up()
    page.wait_for_timeout(300)
    area = width(page, ".chat-body")
    capped = width(page, ".panel")
    print(f"capped: {capped} of {area}")
    if capped > 0.66 * area:
        problems.append(f"the panel grew past 65%: {capped} of {area}")

    # Expand covers the conversation; Escape restores; Escape again closes.
    page.locator(".panel-actions button[aria-label*='Expand']").click()
    page.wait_for_timeout(300)
    step(page, "expanded")
    full = width(page, ".panel.full")
    print(f"expanded: {full} of {area}")
    if abs(full - area) > 2:
        problems.append(f"expanded panel is {full}px, the area {area}px")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    step(page, "escape")
    if page.locator(".panel.full").count():
        problems.append("Escape did not restore the expanded panel")
    if not page.locator(".panel").count():
        problems.append("Escape closed the panel instead of restoring it")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    step(page, "escape again")
    if page.locator(".panel").count():
        problems.append("a second Escape did not close the panel")

    # Ctrl+. toggles, Ctrl+Shift+. expands, the header button closes, the close button closes.
    page.keyboard.press("Control+.")
    page.wait_for_timeout(300)
    step(page, "ctrl+.")
    if not page.locator(".panel").count():
        problems.append("Ctrl+. did not open the panel")
    page.keyboard.press("Control+Shift+.")
    page.wait_for_timeout(300)
    step(page, "ctrl+shift+.")
    if not page.locator(".panel.full").count():
        problems.append("Ctrl+Shift+. did not expand the panel")
    page.locator(".panel-actions button[aria-label*='Back beside']").click()
    page.wait_for_timeout(300)
    step(page, "restored")
    page.locator(".panel-actions button[aria-label='Close the panel']").click()
    page.wait_for_timeout(300)
    if page.locator(".panel").count():
        problems.append("the close button did not close the panel")
    page.locator(".chat-head button[aria-label='Panel']").click()
    page.wait_for_timeout(300)
    if not page.locator(".panel").count():
        problems.append("the header button did not open the panel")
    # A sheet over the panel takes Escape first.
    page.get_by_role("button", name="Session actions", exact=True).click()
    page.wait_for_selector(".menu[role='menu']", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if page.locator(".menu[role='menu']").count():
        problems.append("Escape did not close the title menu")
    if not page.locator(".panel").count():
        problems.append("Escape on the title menu closed the panel underneath it")
    # Closed is remembered (the init script above would reopen it on a reload, so the memory is read).
    page.locator(".panel-actions button[aria-label='Close the panel']").click()
    page.wait_for_timeout(200)
    remembered = page.evaluate("() => localStorage.getItem('daedalus.session.panel')")
    if remembered != "0":
        problems.append(f"closing the panel is remembered as {remembered!r}, not '0'")
    context.close()
    return problems


def dual(browser) -> list[str]:  # type: ignore[no-untyped-def]
    """Two panes, each with its own panel; below 1920 only one is open at a time."""
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1600, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
    page = open_page(context, f"agents/{S1}?with={S2}")
    n = page.locator(".panel").count()
    print("dual at 1600: panels open", n)
    if n != 1:
        problems.append(f"dual view at 1600 opened {n} panels, not one")
    page.locator(".pane-right .chat-head button[aria-label='Panel']").click()
    page.wait_for_timeout(400)
    left = page.locator(".pane-left .panel").count()
    right = page.locator(".pane-right .panel").count()
    print("after opening the right pane's:", left, right)
    if right != 1 or left != 0:
        problems.append(f"opening the right pane's panel left {left} left / {right} right")
    context.close()
    context = browser.new_context(viewport={"width": 2560, "height": 1400}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
    page = open_page(context, f"agents/{S1}?with={S2}")
    n = page.locator(".panel").count()
    print("dual at 2560: panels open", n)
    if n != 2:
        problems.append(f"dual view at 2560 holds {n} panels, not two")
    sections = page.locator(".panel .dt-section")
    section_ids = sections.evaluate_all("els => els.map(el => el.id)")
    assert len(section_ids) == len(set(section_ids)), "Details sections share ids across panels"
    for side in ("right", "left"):
        for section in page.locator(".panel [id$='-info-context']").all():
            if section.get_attribute("open") is not None:
                section.locator("summary").click()
            expect(section).not_to_have_attribute("open", "")
        pane = page.locator(f".pane-{side}")
        target = pane.locator(".panel [id$='-info-context']")
        other = page.locator(f".pane-{'left' if side == 'right' else 'right'} .panel [id$='-info-context']")
        pane.get_by_role("button", name="Context in use").click()
        expect(target).to_have_attribute("open", "")
        expect(other).not_to_have_attribute("open", "")
        prefix = pane.locator(".panel [role='tabpanel']").get_attribute("id").removesuffix("-body")
        assert target.get_attribute("id") == f"{prefix}-info-context"
        print(f"{side} Context in use opens its own section; the other stays closed")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, f"agents/{S1}")
    if page.locator(".panel").count():
        problems.append("a phone drew the panel as a column")
    phone_tool(page, "details")
    page.wait_for_selector(".panel-sheet", timeout=5000)
    # The sheet rises to just under the session's 56 px bar, which stays in view above its corners.
    h = page.evaluate("() => Math.round(document.querySelector('.panel-sheet').getBoundingClientRect().height)")
    print("phone sheet height:", h)
    if h < 844 - 56 - 2:
        problems.append(f"the phone sheet is {h}px, not the height under the bar")
    page.locator(".panel-sheet .panel-tab[data-tab='files']").click()
    page.wait_for_selector(".panel-sheet .filerow", timeout=5000)
    page.locator(".panel-sheet .filerow .title", has_text="NOTES.md").click()
    page.wait_for_selector(".panel-sheet .tab-preview .preview-doc", timeout=5000)
    if "path=NOTES.md" not in query(page):
        problems.append(f"the phone route does not carry the file: {query(page)}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if page.locator(".panel-sheet").count():
        problems.append("Escape did not close the phone sheet")
    if "panel=" in query(page):
        problems.append(f"closing the sheet left the panel in the route: {query(page)}")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += desktop(browser)
        problems += dual(browser)
        problems += phone(browser)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
