"""Progress survives reload; every open page sees the warning before a dependency restart."""
from __future__ import annotations

import copy
import json
import os
import time

from api_stub import GATES
from check_composer import BASE, CHROMIUM, UNHANDLED, stub
from playwright.sync_api import expect, sync_playwright

# The maintenance notice's sheet. Named as one: the page under it is Settings, a dialog itself now.
NOTICE = ".sheet[role='dialog']"


def run() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for width, language in ((390, "ru"), (1440, "en")):
            view = copy.deepcopy(GATES["/api/dependencies"])
            started = time.time() - 15
            view["proposal"] = {"id": "progress", "state": "planning", "request": "Add Pillow", "preset": "default", "started_at": started, "updated_at": time.time(), "progress": [{"at": started, "stage": "model"}, {"at": started + 5, "stage": "inspect"}]}
            maintenance = {"notice": None}
            connection = {"offline": False}

            def route_api(route, *, view=view, maintenance=maintenance, connection=connection):
                path = route.request.url.split("?", 1)[0]
                if path.endswith("/api/maintenance"):
                    route.fulfill(status=503 if connection["offline"] else 200, content_type="application/json", body=json.dumps({**maintenance, "server_time": time.time()}))
                elif path.endswith("/api/dependencies"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(view))
                else:
                    stub(route)

            context = browser.new_context(viewport={"width": width, "height": 900})
            context.route("**/api/**", route_api)
            page = context.new_page()
            other = context.new_page()
            page.goto(f"{BASE}/settings/dependencies?lang={language}")
            other.goto(f"{BASE}/settings?lang={language}")
            expect(other.locator("body")).not_to_contain_text("[settings.sec.dependencies.hint]")
            expect(page.locator(".deps-progress-disclosures summary").first).to_contain_text("2")
            page.locator(".deps-progress-disclosures summary").first.click()
            expect(page.locator(".deps-events li")).to_have_count(2)
            expect(page.locator("#dependency-request")).to_have_value("Add Pillow")
            expect(page.locator("#dependency-request")).to_be_disabled()
            page.reload()
            expect(page.locator(".deps-progress-disclosures summary").first).to_contain_text("2")
            view["proposal"]["state"] = "accepted"
            view["job"] = {"id": "progress", "state": "installing", "stage": "building", "detail_stage": "system", "started_at": started, "updated_at": time.time(), "progress": [{"at": started, "stage": "queued"}, {"at": started + 2, "stage": "building", "detail": "system"}], "log": "Setting up gcc\nSetting up cmake"}
            expect(page.locator(".deps-job .deps-progress-disclosures summary").first).to_contain_text("2", timeout=8000)
            page.locator(".deps-job details").last.locator("summary").click()
            expect(page.locator(".deps-live-log")).to_be_visible()
            expect(page.locator(".deps-live-log")).to_contain_text("Setting up cmake")
            if directory := os.environ.get("DEPENDENCY_SCREENSHOTS"):
                page.screenshot(path=f"{directory}/dependency-progress-{width}-{language}.png")
            deadline = time.time() + 30
            maintenance["notice"] = {"id": "progress", "stage": "restart_pending", "restart_at": deadline}
            view["job"].update(stage="restart_pending", restart_at=deadline)
            for tab in (page, other):
                expect(tab.locator(".maintenance-countdown")).to_be_visible(timeout=8000)
                assert tab.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if directory := os.environ.get("DEPENDENCY_SCREENSHOTS"):
                other.screenshot(path=f"{directory}/dependency-notice-{width}-{language}.png")
            page.locator(NOTICE).locator(".btn.primary").click()
            expect(page.locator(".maintenance-strip")).to_be_visible()
            expect(page.locator(NOTICE)).to_have_count(0)
            # A client-side route change must not reset dismissal or lose the countdown.
            page.evaluate("history.pushState({}, '', location.pathname.replace('/dependencies', '')); window.dispatchEvent(new PopStateEvent('popstate'))")
            expect(page.locator(".maintenance-strip")).to_be_visible()
            expect(page.locator(NOTICE)).to_have_count(0)
            connection["offline"] = True
            page.wait_for_timeout(3500)
            expect(page.locator(".maintenance-strip")).to_be_visible()
            expect(other.locator(".maintenance-countdown")).to_be_visible()
            connection["offline"] = False
            maintenance["notice"] = None
            expect(page.locator(".maintenance-strip")).to_have_count(0, timeout=8000)
            expect(other.locator(NOTICE)).to_have_count(0, timeout=8000)
            context.close()
            print(f"{width} {language}: planner journal, installation output, reload, two-page warning, navigation and outage passed")
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    run()
