"""The Voice project in the sidebar: a project like the others, and still one step from the voice page.

The sidebar draws every project as one row with a square tile, and Voice with a mic on its tile. The
row opens the list of voice conversations where it stands, as every project row does; the voice page
itself is the first thing the row's menu (and its right click) offers after a new chat, and the rail's
mic is the other way there. With one voice conversation the row still opens to show it.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8203 /tmp/app-root &
    APP_URL=http://127.0.0.1:8203/app python3 tests/browser/check_voice_entry.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE


def open_list(browser, viewport: dict, route: str, single: bool = False):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport=viewport, color_scheme="dark")
    page = context.new_page()

    def stub(request):  # type: ignore[no-untyped-def]
        if single and request.request.url.split("?", 1)[0].endswith("/api/sessions"):
            listing = shots.listing()
            listing["sessions"] = [s for s in listing["sessions"] if s["project_id"] != shots.PV or s["id"] == shots.S9]
            listing["projects"] = [{**p, "total": 1, "members": 1, "active": 0} if p["id"] == shots.PV else p for p in listing["projects"]]
            return shots.respond(request, listing)
        return shots.stub(request)

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".sb-project.system", timeout=15000)
    return context, page


def check_the_row(browser, check, where: str, scope: str, viewport: dict, route: str, single: bool = False) -> None:  # type: ignore[no-untyped-def]
    context, page = open_list(browser, viewport, route, single)
    voice = page.locator(f"{scope} .sb-project.system").first
    head = voice.locator(".sb-prow")
    check(head.locator(".sb-tile .ic-mic").count() == 1, f"{where}: the Voice row carries the mic on its square tile")
    check(head.get_attribute("aria-expanded") is not None and "Voice" in (head.get_attribute("aria-label") or ""), f"{where}: the row says what it opens and whether it is open")
    plain = page.locator(f"{scope} .sb-project:not(.system) .sb-prow").first
    check(plain.get_attribute("aria-expanded") is not None, f"{where}: an ordinary project's row is the same kind of control")

    # The row opens the project where it stands, without leaving the screen.
    was = head.get_attribute("aria-expanded")
    head.click()
    page.wait_for_timeout(300)
    check(head.get_attribute("aria-expanded") != was, f"{where}: the row opens and closes the project")
    check(page.evaluate("location.pathname").endswith(route), f"{where}: and does not navigate anywhere ({page.evaluate('location.pathname')})")

    if single:
        if head.get_attribute("aria-expanded") != "true":
            head.click()
        rows = voice.locator("[data-session]")
        check(rows.count() == 1 and rows.first.is_visible(), f"{where}: opening it exposes the sole Voice conversation")
        rows.first.click()
        page.wait_for_url(f"**/agents/{shots.S9}")
        check(page.evaluate("location.pathname").endswith(f"/agents/{shots.S9}"), f"{where}: the conversation row opens its conversation")
        page.goto(f"{BASE}/{route}?token=t&scheme=dark&lang=en")
        page.wait_for_selector(f"{scope} .sb-project.system")

    page.locator(f"{scope} .sb-project.system .sb-prow").first.click(button="right")
    item = page.locator(".context-menu").get_by_role("menuitem", name="Open the voice screen")
    # The menu draws a frame after the click; counting at once raced it.
    try:
        item.wait_for(timeout=5000)
    except Exception:  # noqa: BLE001 — the check below reports it
        pass
    check(item.count() == 1, f"{where}: the row's menu offers the voice page")
    item.click()
    page.wait_for_selector(".voice-stage, .voice-grid", timeout=15000)
    check(page.evaluate("location.pathname").endswith("/voice"), f"{where}: and it opens the voice page ({page.evaluate('location.pathname')})")
    context.close()


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        check_the_row(browser, check, "the Agents screen", ".sidebar", {"width": 1440, "height": 900}, "agents")
        # The start canvas has no list of its own. The sidebar beside it is the list, on Agents
        # and on every other screen.
        check_the_row(browser, check, "the sidebar", ".sidebar", {"width": 1680, "height": 1000}, "inbox")
        check_the_row(browser, check, "one Voice agent on Agents", ".sidebar", {"width": 1440, "height": 900}, "agents", single=True)
        check_the_row(browser, check, "one Voice agent in the sidebar", ".sidebar", {"width": 1680, "height": 1000}, "inbox", single=True)
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
