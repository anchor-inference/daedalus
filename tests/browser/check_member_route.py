"""Edit a project's member route in its settings: runtimes in order, each with an optional model.

What the operator relies on: the folded section names the saved route in its summary; adding a
runtime offers the next one not yet in the route, a row moves up and down, a runtime named twice is
refused before saving, and Save sends the whole ordered route with the project's revision. The rows
fit a phone's width.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    projects = [{"id": "p1", "name": "Bakery", "entity_revision": 1, "folders": folders("/work/p1"), "created_at": "2026-09-19T00:00:00Z",
                 "settings": {"snapshots": False, "system": "", "member_route": [{"runtime": "claude", "model": ""}]}, "system": "", "sessions": []}]
    patches: list[dict] = []
    unhandled = Unhandled()

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/projects" and request.method == "GET":
            return answer(route, projects)
        if path == "/api/sessions":
            return answer(route, {"sessions": [], "projects": [{**p, "total": 0, "active": 0, "loops": 0, "last_message_at": ""} for p in projects]})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if path == "/api/projects/p1" and request.method == "PATCH":
            payload = request.post_data_json
            patches.append(payload)
            found = projects[0]
            if payload["expected_entity_revision"] != found["entity_revision"]:
                return answer(route, {"detail": "project changed", "current_revision": found["entity_revision"]}, status=409)
            if "member_route" in payload:
                found["settings"]["member_route"] = payload["member_route"]
            found["entity_revision"] += 1
            return answer(route, {"receipt_id": f"r-{len(patches)}", "entity_revision": found["entity_revision"], "project_id": "p1"})
        if path == "/api/projects/p1/worktrees" and request.method == "GET":
            return answer(route, {"worktrees": [], "problems": []})
        if path.endswith("/orchestrator/authority") and request.method == "GET":
            return answer(route, {"project_id": "p1", "entity_revision": 1, "current_coordinator_session_id": "",
                                  "available_bundles": [], "grants": [], "readiness_blockers": []})
        if path.startswith("/api/lifecycle/project_goal/") and request.method == "GET":
            return answer(route, {"parent_kind": "project_goal", "parent_id": "p1", "project_id": "p1", "entity_revision": 1,
                                  "source_revision": 1, "generation": 1, "cancel_state": "active", "children": [],
                                  "owned_descendants": [], "preview_fingerprint": "a" * 64})
        if path == "/api/plugins/catalog":
            return answer(route, [])
        if path == "/api/plugins" and request.method == "GET":
            return answer(route, {"items": [], "collection_revision": 1})
        if path == "/api/plugins/safe-mode":
            return answer(route, {"enabled": False})
        if path == "/api/runtime/hosts" and request.method == "GET":
            return answer(route, {"items": [], "collection_revision": 1})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents?token=t&lang=en")
        page.locator(".project-chip").click()
        page.locator(".projects-row", has_text="Bakery").get_by_role("button", name="Settings for Bakery").click()

        section = page.locator("details[data-member-route]")
        expect(section.locator("summary")).to_contain_text("Member runtimes")
        expect(section.locator("summary")).to_contain_text("Claude Code")
        section.locator("summary").click()
        rows = section.locator(".project-route-row")
        expect(rows).to_have_count(1)
        save = section.get_by_role("button", name="Save route")
        expect(save).to_be_disabled()

        # The next runtime offered is the first one the route does not name.
        section.get_by_role("button", name="Add runtime").click()
        expect(rows).to_have_count(2)
        expect(rows.nth(1).get_by_role("combobox")).to_have_value("codex")
        rows.nth(0).get_by_role("textbox").fill("opus")
        rows.nth(1).get_by_role("button", name="Move up").click()
        expect(rows.nth(0).get_by_role("combobox")).to_have_value("codex")
        expect(rows.nth(1).get_by_role("textbox")).to_have_value("opus")

        # A runtime twice cannot be saved.
        rows.nth(0).get_by_role("combobox").select_option("claude")
        expect(section.get_by_role("status")).to_contain_text("Each runtime may appear once")
        expect(save).to_be_disabled()
        rows.nth(0).get_by_role("combobox").select_option("codex")
        expect(save).to_be_enabled()

        for width in (320, 390):
            page.set_viewport_size({"width": width, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"route rows overflow at {width}px"
        page.set_viewport_size({"width": 1440, "height": 900})
        if not section.count() or not section.evaluate("el => el.open"):
            page.locator(".projects-row", has_text="Bakery").get_by_role("button", name="Settings for Bakery").click()
            section.locator("summary").click()
        save.click()
        expect(section.locator("summary")).to_contain_text("Codex → Claude Code")
        assert patches[-1]["member_route"] == [{"runtime": "codex", "model": ""}, {"runtime": "claude", "model": "opus"}], patches[-1]
        assert patches[-1]["expected_entity_revision"] == 1, patches[-1]
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
