"""The Board explains shared host admission where an operator starts a task."""

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
    project = {"id": PID, "name": "Bakery", "folders": folders("/work/bakery"), "created_at": "2026-09-20T00:00:00Z",
               "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}
    member = {"id": "st-ada", "name": "Ada", "color": "blue", "harness": "cursor", "isolation": "shared"}
    task = BoardStub.task("t-menu", "Menu", status="blocked", project_id=PID,
                          assignee=BoardStub.assignee("st-ada", "Ada", harness="cursor"))
    board = BoardStub(project, staff=[member], tasks=[task])
    team = TeamStub(project)
    context = browser.new_context(viewport={"width": width, "height": 850})
    page = context.new_page()

    def route(request_route) -> None:  # type: ignore[no-untyped-def]
        request = request_route.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):] if "/api/" in path else ""
        if path == "/api/admission/capacity":
            body = {"host_id": "local", "cap": 4, "active": 3, "reserved": 1, "available": 0}
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        if path == "/api/admission/queue":
            body = {"entries": [{"project_id": "another", "role_class": "reviewer", "position": 1, "reason": "capacity"},
                                {"project_id": PID, "role_class": "worker", "position": 2, "reason": "capacity"}]}
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        answered = board.answer(request.method, path, urlsplit(request.url).query, None) or team.answer(request.method, path, urlsplit(request.url).query, None)
        if answered is not None:
            status, body = answered
            return request_route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if path == "/api/projects":
            return request_route.fulfill(status=200, content_type="application/json", body=json.dumps([project]))
        if fulfil_shared(request_route):
            return None
        unhandled.record(path)
        return request_route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", route)
    page.goto(f"{BASE}/project/{PID}/board?task=t-menu&token=t&lang={lang}")
    panel = page.locator(".host-capacity")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Host slots: 0 of 4 free" if lang == "en" else "Места на хосте: свободно 0 из 4")
    expect(panel).to_contain_text("#2 in line" if lang == "en" else "очереди №2")
    expect(panel.locator("ol")).to_have_count(0)
    panel.get_by_role("button", name="Details" if lang == "en" else "Подробнее").click()
    expect(panel).to_contain_text("Running: 3 · starting: 1" if lang == "en" else "Работают: 3 · запускаются: 1")
    expect(panel.locator("ol li")).to_have_count(2)
    expect(panel.locator("ol li").nth(1)).to_contain_text("This project" if lang == "en" else "Этот проект")
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    context.close()


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (320, 390, 1440):
                check(lang, width, browser, unhandled)
        browser.close()
    print("check_board_capacity: ok")
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
