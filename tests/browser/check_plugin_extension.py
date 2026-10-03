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
        if path == "/api/plugins" and request.method == "GET":
            return answer(route, {"items": [{"id": "project_status", "version": "1.0.0",
                                               "digest": "a" * 64, "status": "active",
                                               "health": "unknown", "created_at": "2026-10-03T00:00:00Z"}],
                                  "collection_revision": 2})
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
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
