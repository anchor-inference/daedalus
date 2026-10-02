"""The board exposes eligible CLI history and sends the chosen conversation on assignment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, TeamStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "9f3c2a1b7d40"


def check(lang: str, width: int, browser: object, unhandled: Unhandled) -> None:
    project = {"id": PID, "name": "Bakery", "folders": folders("/work/bakery"), "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}
    member = {"id": "st-ada", "name": "Ada", "color": "blue", "harness": "cursor", "isolation": "shared"}
    task = BoardStub.task("t-menu", "Menu", status="todo", project_id=PID, assignee=BoardStub.assignee("st-ada", "Ada", harness="cursor"), brief={"objective": "Make a menu", "deliverable": "menu.md", "boundaries": "Only menu.md", "done_when": "The menu is readable"})
    board = BoardStub(project, staff=[member], tasks=[task])
    team = TeamStub(project)
    context = browser.new_context(viewport={"width": width, "height": 850})
    page = context.new_page()

    def route(request_route) -> None:  # type: ignore[no-untyped-def]
        request = request_route.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):] if "/api/" in path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        if path == "/api/staff/st-ada/resume-sessions":
            response = [
                {"id": "ss-old", "started_at": "2026-09-20T09:00:00Z", "owner_name": "Ada", "task_title": "Menu", "can_resume": True, "resume_reason": ""},
                {"id": "ss-other", "started_at": "2026-09-19T09:00:00Z", "owner_name": "Ada", "task_title": "Other branch", "can_resume": False, "resume_reason": "folder"},
            ]
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps(response))
        answered = board.answer(request.method, path, urlsplit(request.url).query, body) or team.answer(request.method, path, urlsplit(request.url).query, body)
        if answered is not None:
            status, payload = answered
            return request_route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if path == "/api/projects":
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps([project]))
        if path == "/api/sessions":
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
        if path == "/api/settings":
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if fulfil_shared(request_route):
            return None
        unhandled.record(path)
        return request_route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", route)
    page.goto(f"{BASE}/project/{PID}/board?task=t-menu&token=t&lang={lang}")
    chooser = page.locator("#ptask-resume")
    expect(chooser).to_be_visible()
    expect(chooser.locator('option[value="ss-old"]')).to_be_enabled()
    expect(chooser.locator('option[value="ss-other"]')).to_be_disabled()
    chooser.select_option("ss-old")
    page.locator(".sheet-foot .btn.primary").click()
    expect(page.locator(".sheet")).to_have_count(0)
    assert board.updated[-1][1]["resume_from"] == "ss-old"
    page.get_by_role("button", name="New task" if lang == "en" else "Новая задача").first.click()
    page.locator("#ptask-title").fill("Another menu")
    page.locator("#ptask-assignee").select_option("st-ada")
    chooser = page.locator("#ptask-resume")
    expect(chooser).to_be_visible()
    chooser.select_option("ss-old")
    page.locator(".sheet-foot .btn.primary").click()
    expect(page.locator(".sheet")).to_have_count(0)
    assert board.created[-1]["resume_from"] == "ss-old"
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    context.close()


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (1440, 390):
                check(lang, width, browser, unhandled)
        browser.close()
    print("check_board_resume: ok")
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
