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
from calendar_stub import CalendarStub  # noqa: E402

from daedalus.stores.diagrams import shape_label  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    expect_app(BASE)
    planner = CalendarStub()
    diagrams: list[dict] = []
    versions: list[dict] = []
    errors: list[str] = []

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        body = request.post_data_json if request.post_data and method in ("POST", "PUT") else {}

        def reply(value: object, status: int = 200) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

        calendar = planner.answer(method, path, urlsplit(request.url).query, body)
        if calendar is not None:
            return reply(calendar[1], calendar[0])
        if path == "/api/diagrams":
            if method == "GET":
                return reply([{k: v for k, v in row.items() if k != "scene"} for row in diagrams])
            if method == "POST":
                made = {"id": "diagram-1", "title": body["title"], "scene": body.get("scene", {"elements": [], "appState": {}, "files": {}}), "version": 1, "created_at": "2026-10-04T00:00:00Z", "updated_at": "2026-10-04T00:00:00Z"}
                diagrams.append(made)
                versions.append({"version": 1, "title": made["title"], "saved_at": made["updated_at"], "scene": made["scene"]})
                return reply(made, 201)
        if path == "/api/diagrams/diagram-1/versions" and method == "GET":
            return reply([{k: v for k, v in item.items() if k != "scene"} for item in reversed(versions)])
        if path.startswith("/api/diagrams/diagram-1/versions/") and method == "GET":
            version = int(path.rsplit("/", 1)[1])
            return reply(next(item for item in versions if item["version"] == version))
        if path == "/api/diagrams/diagram-1/share":
            if method == "POST":
                diagrams[0]["share_token"] = "sample-token"
                return reply({"url": "/app/d/sample-token"})
            if method == "DELETE":
                diagrams[0]["share_token"] = ""
                return reply({"ok": True})
        if path == "/api/public/diagrams/sample-token" and method == "GET":
            return reply({"title": diagrams[0]["title"], "version": diagrams[0]["version"], "scene": diagrams[0]["scene"]})
        if path == "/api/diagrams/diagram-1":
            if method == "GET":
                return reply(diagrams[0])
            if method == "PUT":
                if body["version"] != diagrams[0]["version"]:
                    return reply({"detail": "Diagram changed since it was opened"}, 409)
                diagrams[0].update(title=body["title"], scene=body["scene"], version=diagrams[0]["version"] + 1)
                versions.append({"version": diagrams[0]["version"], "title": body["title"], "saved_at": "2026-10-04T00:01:00Z", "scene": body["scene"]})
                return reply(diagrams[0])
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
        page.goto(f"{BASE}/diagrams?token=t&lang=en")
        page.get_by_role("button", name="New diagram").click()
        expect(page.locator(".excalidraw")).to_be_visible(timeout=20000)
        expect(page.locator(".diagram-side")).to_be_visible()
        expect(page.locator(".diagram-version")).to_contain_text("Version 1")
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/diagram-editor.png")
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
        rectangle = next(element for element in diagrams[0]["scene"]["elements"] if element.get("type") == "rectangle")
        diagrams[0]["scene"]["elements"].append(shape_label(rectangle, "Agent note"))
        diagrams[0]["version"] += 1
        agent_version = diagrams[0]["version"]
        versions.append({"version": diagrams[0]["version"], "title": diagrams[0]["title"], "saved_at": "2026-10-04T00:02:00Z", "scene": diagrams[0]["scene"]})
        expect(page.locator(".diagram-version")).to_contain_text(f"Version {diagrams[0]['version']}", timeout=10000)
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/diagram-agent-label.png")
        page.wait_for_timeout(1400)
        assert diagrams[0]["version"] == agent_version, "loading an agent revision must not trigger an automatic save"
        page.get_by_role("button", name="Version 1").click()
        expect(page.locator(".diagram-preview-note")).to_contain_text("Viewing version 1")
        expect(page.locator(".diagram-diff")).to_contain_text("Added 2")
        expect(page.locator(".diagram-diff")).to_contain_text("Title: Untitled diagram → Agent flow")
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/diagram-history.png")
        page.get_by_role("button", name="Back to current").click()
        page.get_by_role("button", name="Share", exact=True).click()
        expect(page.locator(".diagram-share-state")).to_contain_text("Public read-only link active")
        with page.expect_download() as saved_file:
            page.get_by_label("Export", exact=True).select_option("excalidraw")
        assert saved_file.value.suggested_filename.endswith(".excalidraw")
        assert any(element.get("type") == "rectangle" for element in json.loads(saved_file.value.path().read_text())["elements"])
        with page.expect_download() as saved_svg:
            page.get_by_label("Export", exact=True).select_option("svg")
        assert saved_svg.value.suggested_filename.endswith(".svg")
        assert "<svg" in saved_svg.value.path().read_text()
        with page.expect_download() as saved_png:
            page.get_by_label("Export", exact=True).select_option("png")
        assert saved_png.value.suggested_filename.endswith(".png")
        assert saved_png.value.path().stat().st_size > 100
        page.goto(f"{BASE}/d/sample-token?lang=en")
        expect(page.locator(".diagram-shared .excalidraw")).to_be_visible(timeout=20000)
        page.goto(f"{BASE}/diagrams/diagram-1?token=t&lang=en")
        expect(page.locator(".excalidraw")).to_be_visible(timeout=20000)
        page.get_by_role("textbox", name="Diagram title").fill("Unsaved local title")
        diagrams[0]["title"] = "Agent changed title"
        diagrams[0]["version"] += 1
        expect(page.locator(".diagram-status")).to_have_text("Save failed", timeout=10000)
        expect(page.get_by_role("textbox", name="Diagram title")).to_have_value("Unsaved local title")
        page.get_by_role("button", name="Load latest version").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Load latest version").click()
        expect(page.get_by_role("textbox", name="Diagram title")).to_have_value("Agent changed title")
        expect(page.locator(".diagram-status")).to_have_text("Saved")
        assert not errors, errors
        context.close()
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile = phone.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.route("**/api/**", stub)
        mobile.goto(f"{BASE}/calendar?token=t&lang=ru")
        expect(mobile.locator(".cal-tg")).to_be_visible()
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
