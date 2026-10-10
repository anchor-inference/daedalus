"""The Jobs tab lists every job, waiter, service and sub-agent once, and an idle session says what it waits for.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_jobs.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import os
import sys
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import S1, UNHANDLED, detail, respond, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    stops: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(r):  # type: ignore[no-untyped-def]
            url = urlsplit(r.request.url)
            if url.path == f"/api/sessions/{S1}":
                # Idle, with the four jobs the task list holds still running.
                data = copy.deepcopy(detail(S1))
                data["background_count"] = 4
                return respond(r, data)
            if url.path.endswith("/stop") and r.request.method == "POST":
                stops.append(url.path)
            return stub(r)

        page.route("**/api/**", route)
        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=jobs")
        expect(page.locator(".task-list")).to_be_visible(timeout=20000)

        assert page.locator(".jobs .task-list").count() == 1, "one jobs list"
        assert page.get_by_text("Background jobs").count() == 0, "no second list"
        assert page.locator(".task-row").count() == 7, page.locator(".task-row").count()
        expect(page.locator(".task-list .dt-aside")).to_have_text("4 running · 7")
        expect(page.locator(".task-flag")).to_have_text("possibly stuck")
        rows = page.locator(".task-list").inner_text()
        for word in ("failed · exit 3", "lost", "succeeded", "service", "host"):
            assert word in rows, f"{word!r} missing from the list: {rows!r}"
        assert page.locator(".task-row.running .task-stop").count() == 4
        assert page.locator(".task-row.done .task-stop").count() == 0
        assert page.locator('.task-row[data-kind="agent"]').count() == 1
        assert page.locator(".task-open").first.get_attribute("title") == "npm run build -- --watch"

        expect(page.locator(".head-status.jobs-pending")).to_have_text("waiting for 4 jobs")
        page.locator(".task-row.running .task-stop").first.click()
        assert stops and stops[0].endswith("/tasks/job-1/stop"), stops

        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()
        phone.route("**/api/**", route)
        phone.goto(f"{BASE}/agents/{S1}?token=t&lang=en")
        expect(phone.locator(".ph-chat-state")).to_contain_text("waiting for 4 jobs", timeout=20000)
        print("one list, seven rows, labels, stop, and the waiting line on desktop and phone: passed")
        assert not errors, errors
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
