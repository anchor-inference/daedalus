"""Open the calendar in a browser and save a change. The diagrams have a check of their own,
``check_diagrams.py``."""

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

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    expect_app(BASE)
    events: list[dict] = []
    errors: list[str] = []

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        body = request.post_data_json if request.post_data and method in ("POST", "PUT") else {}

        def reply(value: object, status: int = 200) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

        if path == "/api/calendar/events":
            if method == "GET":
                return reply(events)
            if method == "POST":
                made = {**body, "id": "event-1", "version": 1, "dirty": "", "remote_id": None}
                events.append(made)
                return reply(made, 201)
        if path == "/api/calendar/accounts" and method == "GET":
            return reply([])
        return shots.stub(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/calendar?token=t&lang=en")
        expect(page.locator(".calendar-grid")).to_be_visible()
        page.get_by_role("button", name="New event", exact=True).first.click()
        page.get_by_role("dialog").get_by_label("Title").fill("Design review")
        page.get_by_role("dialog").get_by_role("button", name="Save").click()
        expect(page.locator(".calendar-event").first).to_contain_text("Design review")
        page.get_by_role("button", name="Connected calendars").click()
        accounts = page.get_by_role("dialog")
        expect(accounts.get_by_label("OAuth client ID")).to_be_visible()
        accounts.get_by_label("Provider").select_option("yandex")
        expect(accounts.get_by_label("App password")).to_be_visible()
        accounts.get_by_label("Provider").select_option("outlook")
        expect(accounts.get_by_text("Microsoft Entra", exact=False)).to_be_visible()
        assert not errors, errors
        context.close()
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile = phone.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.route("**/api/**", stub)
        mobile.goto(f"{BASE}/calendar?token=t&lang=ru")
        expect(mobile.locator(".calendar-grid")).to_be_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        phone.close()
        browser.close()
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
