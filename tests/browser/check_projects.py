"""Exercise name-only project creation and the optional directory browser."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> int:
    projects: list[dict] = []
    created: list[dict] = []
    installed: list[dict] = []
    staged: list[dict] = []
    lifecycle_commands: list[dict] = []
    lifecycle_attempts: list[dict] = []
    lifecycle_fingerprint = ["b" * 64]
    unhandled = Unhandled()
    extension = {"id": "project_status", "version": "1.0.0", "display_name": "Project status", "description": "Read task counts by status for one selected project.", "capabilities": ["board.read"], "tools": [{"name": "inspect_project"}], "ui_extensions": [{"id": "project_status", "slot": "project.settings", "schema_version": 1, "component": "status"}]}

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/projects" and request.method == "POST":
            payload = request.post_data_json
            created.append(payload)
            project = {"id": f"p{len(projects) + 1}", "name": payload["name"], "folders": folders((payload.get("folders") or [{"path": f"/managed/p{len(projects) + 1}"}])[0]["path"]), "created_at": "2026-09-19T00:00:00Z", "settings": {"snapshots": True, "system": ""}, "system": "", "sessions": []}
            projects.append(project)
            return answer(route, project)
        if path == "/api/projects":
            return answer(route, projects)
        if path == "/api/sessions":
            listed = [{**project, "total": 0, "active": 0, "loops": 0, "last_message_at": ""} for project in projects]
            return answer(route, {"sessions": [], "projects": listed})
        if path == "/api/settings":
            # The start canvas reads the default model before a chat exists.
            return answer(route, {"presets": {}, "model": {}})
        if path == "/api/plugins/catalog":
            return answer(route, [{"manifest": extension, "valid": True, "digest": "a" * 64, "required_capabilities": ["board.read"], "ui_extensions": extension["ui_extensions"]}])
        if path == "/api/runtime/hosts" and request.method == "GET":
            return answer(route, {"items": [], "collection_revision": 1})
        if path == "/api/plugins" and request.method == "GET":
            return answer(route, {"items": installed, "collection_revision": len(installed) + 1})
        if path == "/api/plugins/safe-mode" and request.method == "GET":
            return answer(route, {"enabled": False})
        if path == "/api/plugins/install" and request.method == "POST":
            payload = request.post_data_json
            staged.append(payload)
            installed.append({"id": "project_status", "version": "1.0.0", "digest": "a" * 64, "status": "staged", "health": "inactive", "created_at": "2026-10-03T00:00:00Z"})
            return answer(route, {"id": "project_status", "status": "staged", "receipt_id": "receipt", "entity_revision": 1})
        if path == "/api/plugins/project_status/health":
            return answer(route, {"id": "project_status", "state": "inactive"})
        if path == "/api/projects/p1/board" and request.method == "GET":
            return answer(route, {"tasks": [{"id": "t-owned", "title": "Owned task"}]})
        if path == "/api/lifecycle/project_goal/p1" and request.method == "GET":
            task = {"parent_kind": "project_goal", "parent_id": "p1", "child_kind": "task", "child_id": "t-owned", "generation": 1, "source_revision": 1, "cancel_state": "active"}
            attempt = {"parent_kind": "task", "parent_id": "t-owned", "child_kind": "execution_attempt", "child_id": "a-owned", "generation": 1, "source_revision": 1, "cancel_state": "active"}
            return answer(route, {"parent_kind": "project_goal", "parent_id": "p1", "project_id": "p1", "entity_revision": 1,
                                  "source_revision": 1, "generation": 1, "cancel_state": "requested" if lifecycle_commands else "active",
                                  "children": [task], "owned_descendants": [task, attempt], "preview_fingerprint": lifecycle_fingerprint[0]})
        if path == "/api/lifecycle/project_goal/p1/cancel" and request.method == "POST":
            payload = request.post_data_json
            lifecycle_attempts.append(payload)
            if len(lifecycle_attempts) == 1:
                lifecycle_fingerprint[0] = "c" * 64
                return answer(route, {"detail": "owned work changed since preview"}, status=409)
            if payload["preview_fingerprint"] != lifecycle_fingerprint[0]:
                return answer(route, {"detail": "owned work changed since preview"}, status=409)
            lifecycle_commands.append(payload)
            return answer(route, {"parent_kind": "project_goal", "parent_id": "p1", "cancel_state": "requested", "generation": 1, "children": [], "receipt_id": "lifecycle-receipt", "entity_revision": 2})
        if path == "/api/project-directories":
            query = parse_qs(url.query)
            if not query:
                return answer(route, {"roots": [{"name": "work", "path": "/work", "readable": True, "writable": True, "project_id": None}], "docker": True})
            selected = query.get("path", ["/work"])[0]
            entries = [{"name": "existing", "path": "/work/existing", "readable": True, "writable": True, "project_id": None}] if selected == "/work" else []
            parents = [{"name": "work", "path": "/work"}] + ([{"name": "existing", "path": "/work/existing"}] if selected != "/work" else [])
            return answer(route, {"root": "/work", "path": selected, "parents": parents, "entries": entries, "truncated": False})
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
        name = page.locator("#project-name")
        expect(name).to_be_visible()
        name.fill("Plain")
        page.get_by_role("button", name="Add", exact=True).click()
        expect(page.locator(".sheet")).to_have_count(0)
        assert created[0] == {"name": "Plain"}, created[0]

        page.reload()
        page.locator(".project-chip").click()
        expect(page.locator(".project-row")).to_have_count(1)
        page.get_by_role("button", name="Add a project", exact=True).click()
        page.locator("#project-name").fill("Existing")
        page.get_by_role("button", name="Use an existing folder").click()
        expect(page.get_by_text("the bot sees a folder only once it is mounted", exact=False)).to_be_visible()
        page.get_by_role("button", name="work", exact=True).click()
        page.get_by_role("button", name="existing", exact=True).click()
        expect(page.locator("#project-root")).to_have_value("/work/existing")
        page.get_by_role("button", name="Add", exact=True).click()
        assert created[1] == {"name": "Existing", "folders": [{"path": "/work/existing"}]}, created[1]
        page.locator(".project-chip").click()
        page.locator(".project-row", has_text="Plain").get_by_role("button", name="Settings for Plain").click()
        extensions = page.locator(".project-extensions")
        expect(extensions).to_be_visible()
        hosts = page.locator(".project-runtime-hosts")
        expect(hosts).to_be_visible()
        expect(hosts).not_to_contain_text("No remote host identities recorded")
        hosts.locator("summary").first.click()
        expect(hosts).to_contain_text("No remote host identities recorded")
        page.set_viewport_size({"width": 320, "height": 560})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "host settings overflow the phone"
        lifecycle = page.locator("details.sheet-section", has_text="Stop work owned by this goal")
        lifecycle.locator("summary").first.click()
        expect(lifecycle).to_contain_text("Owned task")
        expect(lifecycle).to_contain_text("Owned run")
        lifecycle.get_by_text("Technical identity").first.click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "ownership identity overflows the phone"
        lifecycle.locator("textarea").fill("Stop the owned work")
        lifecycle.get_by_role("button", name="Request stop").click()
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/lifecycle/project_goal/p1" and response.request.method == "GET"):
            page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        expect(lifecycle.locator("textarea")).to_have_value("Stop the owned work")
        expect(lifecycle).to_contain_text("Active")
        lifecycle.get_by_role("button", name="Request stop").click()
        page.locator(".sheet-backdrop.confirm .dialog button").last.click()
        assert lifecycle_attempts[0]["preview_fingerprint"] == "b" * 64
        assert lifecycle_commands and lifecycle_commands[-1]["preview_fingerprint"] == "c" * 64
        assert lifecycle_commands[-1]["expected_entity_revision"] == 1 and lifecycle_commands[-1]["expected_source_revision"] == 1
        expect(lifecycle).to_contain_text("Stop requested")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "lifecycle settings overflow the phone"
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(extensions.locator(".project-extension")).to_have_count(0)
        extensions.locator("summary").first.click()
        expect(extensions.locator(".project-extension")).to_have_count(1)
        expect(extensions).to_contain_text("ordinary project work needs no extension")
        extensions.get_by_role("button", name="Install").click()
        expect(extensions).to_contain_text("Activating")
        expect(extensions.get_by_role("button", name="Test on this project")).to_be_disabled()
        assert staged and staged[0]["expected_digest"] == "a" * 64 and staged[0]["client_operation_id"], staged
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
