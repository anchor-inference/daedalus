"""An installed descriptor drives the project form and reaches the scoped read endpoint."""

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
    unhandled = Unhandled()
    calls: list[dict] = []
    custom: list[dict] = []
    project = {"id": "p1", "name": "Plain", "folders": folders("/work/plain"),
               "created_at": "2026-10-03T00:00:00Z", "settings": {"snapshots": True, "system": ""},
               "system": "", "sessions": []}
    manifest = {
        "id": "project_status", "version": "1.0.0", "display_name": "Project status",
        "description": "Read task counts by status for one selected project.", "capabilities": ["board.read"],
        "tools": [{"name": "inspect_project", "input_schema": {"type": "object", "properties": {
            "project_id": {"type": "string"}}, "required": ["project_id"]}}],
        "ui_extensions": [{"id": "project_status", "slot": "project.settings", "schema_version": 1,
                           "component": "status", "tool": "inspect_project"}],
    }

    def answer(route, body: object) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/projects":
            return answer(route, [project])
        if path == "/api/sessions":
            return answer(route, {"sessions": [], "projects": [{**project, "total": 0, "active": 0,
                                                                     "loops": 0, "last_message_at": ""}]})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if path == "/api/control/revisions":
            return answer(route, {"scope": {"kind": "global", "id": "global"},
                                  "collection_revision": 2, "entity_revision": None})
        if path == "/api/projects/p1/workspace-archive":
            return answer(route, {"latest": None, "available": False})
        if path == "/api/plugins/catalog":
            return answer(route, [{"manifest": manifest, "valid": True, "digest": "a" * 64,
                                   "required_capabilities": ["board.read"],
                                   "ui_extensions": manifest["ui_extensions"]}])
        if path == "/api/plugins/read-models":
            return answer(route, [{"id": "task_counts", "input_schema": {
                "type": "object", "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"], "additionalProperties": False}},
                {"id": "accepted_results", "input_schema": {
                    "type": "object", "properties": {"project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                    "required": ["project_id"], "additionalProperties": False}}])
        if path == "/api/plugins" and request.method == "GET":
            return answer(route, {"items": [{"id": "project_status", "version": "1.0.0",
                                               "digest": "a" * 64, "status": "active",
                                               "health": "unknown", "created_at": "2026-10-03T00:00:00Z"}, *custom],
                                  "collection_revision": 2})
        if path == "/api/plugins/validate":
            assert request.post_data_json["manifest"]["tools"][0]["read_model"] == "task_counts"
            return answer(route, {"valid": True, "digest": "b" * 64})
        if path == "/api/projects/p1/plugins/preview-read":
            assert request.post_data_json["expected_digest"] == "b" * 64
            return answer(route, {"preview_only": True, "digest": "b" * 64,
                                  "result": {"project_id": "p1", "tasks": {"todo": 2}}})
        if path == "/api/plugins/install":
            payload = request.post_data_json
            assert payload["expected_digest"] == "b" * 64
            assert payload["manifest"]["id"] == "my_counts"
            custom.append({"id": "my_counts", "version": "1.0.0", "digest": "b" * 64,
                           "manifest": payload["manifest"], "status": "staged",
                           "health": "unknown", "created_at": "2026-10-03T00:00:00Z"})
            return answer(route, {"status": "staged", "receipt_id": "receipt"})
        if path == "/api/plugins/my_counts/1.0.0/retry":
            assert request.post_data_json["client_operation_id"]
            assert custom[0]["activation_state"] == "failed"
            custom[0].update({"status": "staged", "activation_state": "pending"})
            return answer(route, {"status": "staged", "receipt_id": "retry-receipt"})
        if path == "/api/plugins/my_counts/health":
            return answer(route, {"id": "my_counts", "state": "inactive"})
        if path == "/api/plugins/safe-mode":
            return answer(route, {"enabled": False})
        if path == "/api/plugins/project_status/health":
            return answer(route, {"id": "project_status", "state": "configured_unverified"})
        if path == "/api/projects/p1/plugins/project_status/invoke-read":
            calls.append(request.post_data_json)
            return answer(route, {"result": {"project_id": "p1", "name": "Plain", "tasks": {"todo": 2}}})
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
        page.locator(".project-row", has_text="Plain").get_by_role("button", name="Settings for Plain").click()
        extensions = page.locator(".project-extensions")
        extensions.locator("summary").first.click()
        expect(extensions).to_contain_text("Project status")
        extensions.get_by_role("button", name="Test on this project").click()
        expect(extensions.locator(".project-extension pre")).to_contain_text('"todo": 2')
        assert calls == [{"tool": "inspect_project", "arguments": {}}], calls
        builder = extensions.get_by_text("Create a read card")
        builder.click()
        fields = extensions.locator("details.project-extension label.field input")
        fields.nth(0).fill("my_counts")
        fields.nth(1).fill("My counts")
        fields.nth(2).fill("A read-only count for this project")
        extensions.get_by_role("button", name="Preview on this project").click()
        expect(extensions).to_contain_text("Preview only")
        extensions.get_by_role("button", name="Install", exact=True).last.click()
        expect(extensions).to_contain_text("My counts")
        assert custom and custom[0]["status"] == "staged"
        custom[0].update({"status": "failed", "activation_state": "failed"})
        page.reload()
        page.locator(".project-chip").click()
        page.locator(".project-row", has_text="Plain").get_by_role("button", name="Settings for Plain").click()
        extensions = page.locator(".project-extensions")
        extensions.locator("summary").first.click()
        expect(extensions).to_contain_text("Activation failed")
        extensions.get_by_role("button", name="Retry activation").click()
        assert custom[0]["activation_state"] == "pending"
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
