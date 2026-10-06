"""Open the calendar in a browser, save an event and reach every provider's connection form.

The diagram editor has its own, fuller check (check_diagrams.py); this one keeps the calendar's
smoke path short: an event saved from the editor lands on the grid, the connections sheet offers
each provider's form, and a phone draws the grid without a sideways scroll.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402
from calendar_stub import CalendarStub  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    expect_app(BASE)
    planner = CalendarStub()
    errors: list[str] = []

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        body = request.post_data_json if request.post_data and method in ("POST", "PUT", "PATCH") else {}
        calendar = planner.answer(method, path, urlsplit(request.url).query, body)
        if calendar is not None:
            return route.fulfill(status=calendar[0], content_type="application/json", body=json.dumps(calendar[1]))
        return shots.stub(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/calendar?token=t&lang=en")
        expect(page.locator(".cal-tg")).to_be_visible()
        page.get_by_role("button", name="Create", exact=True).click()
        page.get_by_role("dialog", name="New event").get_by_label("Title").fill("Design sync")
        page.get_by_role("dialog", name="New event").get_by_role("button", name="Save").click()
        expect(page.locator(".cal-block", has_text="Design sync")).to_be_visible()
        page.get_by_role("button", name="Calendar menu").click()
        page.get_by_role("menuitem", name="Connected calendars").click()
        page.get_by_role("dialog").get_by_role("button", name="Google Calendar").click()
        expect(page.get_by_label("Client ID")).to_be_visible()
        page.get_by_role("button", name="All providers").click()
        page.get_by_role("dialog").get_by_role("button", name="Yandex Calendar").click()
        expect(page.get_by_label("App password")).to_be_visible()
        page.get_by_role("button", name="All providers").click()
        page.get_by_role("dialog").get_by_role("button", name="Outlook").click()
        expect(page.get_by_text("Microsoft Entra", exact=False).first).to_be_visible()
        page.keyboard.press("Escape")
        assert not errors, errors
        context.close()
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile = phone.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.route("**/api/**", stub)
        mobile.goto(f"{BASE}/calendar?token=t&lang=ru")
        expect(mobile.locator(".cal-tg")).to_be_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        phone.close()
        browser.close()
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
