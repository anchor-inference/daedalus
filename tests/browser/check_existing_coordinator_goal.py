"""An enabled coordinator keeps its conversation when the project goal is absent or unreadable."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"


def scenario(language: str, width: int, unavailable: bool) -> None:
    focus = FocusStub.bakery(language)
    posts: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 700})

        def stub(route):
            request = route.request
            url = urlsplit(request.url)
            if request.method == "POST" and url.path != "/api/presence":
                posts.append(url.path)
            if url.path == f"/api/projects/{PID}/scope-revisions/current":
                value = {"detail": "temporarily unavailable"} if unavailable else {
                    "project_id": PID, "goal_revision": 1, "entity_revision": 1, "body": "", "checks": None}
                return route.fulfill(status=503 if unavailable else 200, content_type="application/json", body=json.dumps(value))
            answered = focus.answer(request.method, url.path, url.query, request.post_data_json if request.post_data else None)
            if answered is not None:
                status, payload = answered
                return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
            return installation(route)

        page.route("**/api/**", stub)
        page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={language}")
        composer = page.get_by_placeholder("Write to the orchestrator…" if language == "en" else "Напишите оркестратору…")
        expect(composer).to_be_visible()
        composer.fill("Keep the existing conversation available")
        page.locator("[data-goal-setup]").get_by_role("button").click()
        sheet = page.locator(".sheet")
        expect(sheet).to_be_visible()
        if unavailable:
            expect(sheet.get_by_role("button", name="Try again" if language == "en" else "Ещё раз")).to_be_visible()
        else:
            sheet.locator("#guided-goal-body").fill("Publish a clear menu")
            sheet.locator("#guided-goal-checks").fill("All prices checked")
        page.keyboard.press("Escape")
        expect(sheet).to_have_count(0)
        expect(composer).to_have_value("Keep the existing conversation available")
        page.reload()
        expect(composer).to_be_visible()
        page.locator("[data-goal-setup]").get_by_role("button").click()
        if not unavailable:
            expect(page.locator("#guided-goal-body")).to_have_value("Publish a clear menu")
        assert posts == [], posts
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (320, 390, 1440):
            for failed_read in (False, True):
                scenario(lang, viewport, failed_read)
                print(f"existing coordinator goal {lang} {viewport} unavailable={failed_read}: PASS")
    raise SystemExit(UNHANDLED.report())
