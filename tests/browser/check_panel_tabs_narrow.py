"""The right panel's tabs stay reachable at the panel's narrowest, on a desktop and on a phone.

A narrow panel draws its tabs without their words, and a strip of more than four tabs (a session with
subagents, a project's orchestrator) drew them without their icons as well, so at the minimum width
the strip showed nothing but the Details tab's count and no tab could be switched to. Here the panel
is set to its narrowest and every tab must be drawn with something to see, carry its name, lie inside
the strip once scrolled to, and open when clicked.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_panel_tabs_narrow.py

Exit 0 when every tab is reachable everywhere.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, focus_stub, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PHONE = {"width": 320, "height": 700}
NARROWEST = "try { localStorage.setItem('daedalus.width.panel', '1'); } catch (e) {}"
"""A share far under the floor: the grid clamps the column to ``--panel-w-min``, the narrowest the
panel is ever drawn beside a conversation."""

DRAWN = """tab => {
  const box = tab.getBoundingClientRect();
  const strip = tab.closest('.panel-tablist').getBoundingClientRect();
  // Something in the tab other than its count: the icon or the word, whichever the width keeps.
  const marks = [...tab.querySelectorAll('svg.ic, span:not(.count)')].filter(el => {
    const r = el.getBoundingClientRect();
    return r.width >= 8 && r.height >= 8 && getComputedStyle(el).visibility !== 'hidden';
  });
  return { width: box.width, inside: box.left >= strip.left - 1 && box.right <= strip.right + 1, marks: marks.length,
           name: tab.getAttribute('aria-label') || '', title: tab.getAttribute('title') || '' };
}"""


def walk(page: Page, scope: str, where: str, problems: list[str], least: int) -> None:
    """Every tab in ``scope`` drawn, named, scrolled into the strip and opened by a click."""
    tabs = page.locator(f"{scope} .panel-tablist .panel-tab")
    names = tabs.evaluate_all("els => els.map(e => e.dataset.tab)")
    strip = page.locator(f"{scope} .panel-tablist").evaluate("el => Math.round(el.getBoundingClientRect().width)")
    print(f"{where}: strip {strip}px, tabs {names}")
    if len(names) < least:
        problems.append(f"{where}: only {len(names)} tabs, expected at least {least}: {names}")
    for name in names:
        tab = page.locator(f"{scope} .panel-tab[data-tab='{name}']")
        tab.scroll_into_view_if_needed()
        page.wait_for_timeout(100)
        seen = tab.evaluate(DRAWN)
        if seen["width"] < 24 or not seen["marks"]:
            problems.append(f"{where}: the {name} tab draws nothing to see ({seen})")
        if not seen["inside"]:
            problems.append(f"{where}: the {name} tab stays outside the strip after scrolling to it ({seen})")
        if not seen["name"] or not seen["title"]:
            problems.append(f"{where}: the {name} tab carries no name of its own ({seen})")
        tab.click()
        page.wait_for_timeout(150)
        if tab.get_attribute("aria-selected") != "true":
            problems.append(f"{where}: clicking the {name} tab did not open it")


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            # A session with subagents: its four tabs and Subagents, the strip that went blank.
            for viewport in ({"width": 1440, "height": 900}, {"width": 1024, "height": 768}):
                context = browser.new_context(viewport=viewport, color_scheme="dark")
                context.add_init_script(NARROWEST)
                page = context.new_page()
                page.route("**/api/**", stub)
                page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={lang}")
                page.wait_for_selector(".panel .panel-tab[data-tab='subagents']", timeout=15000)
                page.wait_for_timeout(400)
                width = page.locator(".panel").evaluate("el => Math.round(el.getBoundingClientRect().width)")
                floor = page.evaluate("parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--panel-w-min'))")
                if width > floor + 2:
                    problems.append(f"{lang} {viewport['width']}: the panel is {width}px, not at its floor of {floor}px")
                walk(page, ".panel", f"{lang} session {viewport['width']} ({width}px)", problems, 5)
                context.close()

            # A project's orchestrator: the longest strip there is.
            focus = FocusStub.bakery(lang)
            pid = focus.projects[0]["id"]
            context = browser.new_context(viewport={"width": 1024, "height": 768}, color_scheme="dark")
            context.add_init_script(NARROWEST)
            page = context.new_page()
            page.route("**/api/**", focus_stub(focus))
            page.goto(f"{BASE}/project/{pid}?panel=details&token=t&lang={lang}")
            page.wait_for_selector(".chat.in-project .panel .panel-tab", timeout=15000)
            page.wait_for_timeout(400)
            walk(page, ".panel", f"{lang} orchestrator 1024", problems, 6)
            context.close()

            # A phone's sheet at the narrowest phone there is.
            context = browser.new_context(viewport=PHONE, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={lang}")
            page.wait_for_selector(".panel-sheet .panel-tab[data-tab='subagents']", timeout=15000)
            page.wait_for_timeout(400)
            walk(page, ".panel-sheet", f"{lang} phone session {PHONE['width']}", problems, 5)
            context.close()
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in problems:
        print("FAIL", problem)
    if not problems and not unhandled:
        print("every panel tab is reachable at the narrowest width")
    return 1 if problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
