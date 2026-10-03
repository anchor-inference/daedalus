"""A pending schedule can be reviewed and a new reminder created on a phone."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, expect_app
from check_nav_rail import go, serve
from playwright.sync_api import expect, sync_playwright
from screenshots import UNHANDLED

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def check(lang: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        serve(page, lang)
        proposal = {"id": "p-suggested", "name": "Check the release", "prompt": "Read the release notes",
                    "cron": None, "run_at": "2026-12-01T09:00:00Z", "kind": "message", "run_in": "new",
                    "next_run_at": "2026-12-01T09:00:00Z", "file_count": 0,
                    "files": [], "legacy_file_review_required": False,
                    "source_session_id": "orch-bakery", "source_project_id": None,
                    "proposal_revision": 1, "request_digest": "reviewed-digest", "status": "pending",
                    "accepted_schedule_id": None, "legacy_schedule_id": None,
                    "created_at": "2026-09-24T10:00:00Z"}
        accepted: list[dict] = []

        def proposals(route) -> None:  # type: ignore[no-untyped-def]
            path = urlsplit(route.request.url).path
            if path.endswith("/api/recurring/proposals") and route.request.method == "GET":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"entries": [proposal], "collection_revisions": {"global:global": 1}}))
                return
            if path.endswith("/api/recurring/proposals/p-suggested/accept") and route.request.method == "POST":
                body = route.request.post_data_json
                assert body["request_digest"] == "reviewed-digest" and body["expected_proposal_revision"] == 1
                assert body["expected_collection_revision"] == 1 and body["client_operation_id"] and body["expires_at"]
                accepted.append(body)
                proposal["status"] = "accepted"
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"proposal_id": proposal["id"], "id": "s-approved",
                                               "proposal_revision": 2, "receipt_id": "r-approved", "entity_revision": 2}))
                return
            route.fallback()

        page.route("**/api/recurring/proposals**", proposals)
        go(page, "/schedules", lang)
        page.get_by_role("button", name="Approve schedule" if lang == "en" else "Одобрить расписание").click()
        expect(page.get_by_role("button", name="Approve schedule" if lang == "en" else "Одобрить расписание")).to_have_count(0)
        assert len(accepted) == 1
        go(page, "/schedules/wk1", lang)
        action = "Approve this action" if lang == "en" else "Одобрить это действие"
        current = "Current" if lang == "en" else "Действует"
        expect(page.get_by_role("button", name=action)).to_be_visible()
        page.get_by_role("button", name=action).click()
        expect(page.get_by_text(current, exact=True)).to_be_visible()

        go(page, "/schedules", lang)
        new = "New schedule" if lang == "en" else "Новое расписание"
        page.get_by_role("button", name=new).first.click()
        page.locator(".sheet input.field").first.fill("Check medicine")
        page.get_by_role("radio", name="Reminder" if lang == "en" else "Напоминание").click()
        page.locator(".sheet textarea.field").fill("Take medicine")
        page.get_by_role("button", name="Create" if lang == "en" else "Создать", exact=True).click()
        expect(page.get_by_text("Check medicine")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for language in ("en", "ru"):
        check(language)
    assert UNHANDLED.report() == 0
    print("recurring schedule mobile: ok")
