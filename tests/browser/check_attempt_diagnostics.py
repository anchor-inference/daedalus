"""A running task reveals only saved, task-bound fault categories when opened."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app
from check_project_board import PID, project, serve
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def scenario(language: str, width: int) -> None:
    task = BoardStub.task("task-one", "Prepare menu", status="doing", project_id=PID,
                          current_attempt_id="attempt-one")
    stub = BoardStub(project(), tasks=[task])
    unhandled = Unhandled()
    reads: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 844 if width < 1024 else 900})
        serve(page, stub, unhandled)

        def diagnostics(route):  # type: ignore[no-untyped-def]
            path = urlsplit(route.request.url).path
            if path != "/api/board/task-one/attempts/attempt-one/diagnostics":
                return route.fallback()
            reads.append(path)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "attempt_id": "attempt-one", "task_id": "task-one", "state": "recovering",
                "faults": [{"kind": "launch_error", "diagnostic_ref": "fault-3", "created_at": "2026-10-04T10:00:00Z"}],
                "truncated": False, "raw_exception": "sk-private-credential",
            }))

        page.route("**/api/**", diagnostics)
        page.goto(f"{BASE}/project/{PID}/board?task=task-one&token=t&lang={language}")
        sheet = page.locator(".sheet.pboard-sheet")
        details = sheet.locator("details.result-details", has_text="Execution details" if language == "en" else "Сведения о выполнении")
        expect(details).to_be_visible()
        assert reads == []
        details.locator("summary").click()
        expect(details).to_contain_text("needs recovery" if language == "en" else "требует восстановления")
        expect(details).to_contain_text("Launch failed" if language == "en" else "Сбой запуска")
        expect(details).to_contain_text("fault-3")
        assert "sk-private-credential" not in page.locator("body").inner_text()
        assert reads == ["/api/board/task-one/attempts/attempt-one/diagnostics"]
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    assert unhandled.report() == 0


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (390, 1440):
            scenario(lang, viewport)
            print(f"attempt diagnostics {lang} {viewport}: PASS")
