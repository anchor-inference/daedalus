"""Open the calendar and native canvas in a browser and save a change in each."""

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
    diagrams: list[dict] = []
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
        if path == "/api/diagrams":
            if method == "GET":
                return reply([{k: v for k, v in row.items() if k != "scene"} for row in diagrams])
            if method == "POST":
                made = {"id": "diagram-1", "title": body["title"], "scene": body.get("scene", {"elements": [], "appState": {}, "files": {}}), "version": 1, "created_at": "2026-10-04T00:00:00Z", "updated_at": "2026-10-04T00:00:00Z"}
                diagrams.append(made)
                return reply(made, 201)
        if path == "/api/diagrams/diagram-1":
            if method == "GET":
                return reply(diagrams[0])
            if method == "PUT":
                diagrams[0].update(title=body["title"], scene=body["scene"], version=diagrams[0]["version"] + 1)
                return reply(diagrams[0])
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
        page.goto(f"{BASE}/diagrams?token=t&lang=en")
        page.get_by_role("button", name="New diagram").click()
        expect(page.locator(".excalidraw")).to_be_visible(timeout=20000)
        page.get_by_role("textbox", name="Diagram title").fill("Agent flow")
        expect(page.locator(".diagram-status")).to_have_text("Saved", timeout=15000)
        assert diagrams[0]["title"] == "Agent flow"
        canvas = page.locator(".excalidraw canvas").first
        box = canvas.bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.keyboard.press("r")
        page.mouse.move(box["x"] + box["width"] / 2 - 60, box["y"] + box["height"] / 2 - 30)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 60, box["y"] + box["height"] / 2 + 30, steps=10)
        page.mouse.up()
        expect(page.locator(".diagram-status")).to_have_text("Saved", timeout=15000)
        assert any(element.get("type") == "rectangle" for element in diagrams[0]["scene"]["elements"])
        assert not errors, errors
        context.close()
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile = phone.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.route("**/api/**", stub)
        mobile.goto(f"{BASE}/calendar?token=t&lang=ru")
        expect(mobile.locator(".calendar-grid")).to_be_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        mobile.goto(f"{BASE}/diagrams/diagram-1?token=t&lang=ru")
        expect(mobile.locator(".excalidraw")).to_be_visible(timeout=20000)
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        phone.close()
        browser.close()
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
