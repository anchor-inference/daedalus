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
    unhandled = Unhandled()

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
        # The sheet closes once the request has been answered; read what was posted only then, as
        # for the first project. Read at once, it raced the request on a loaded machine.
        expect(page.locator(".sheet")).to_have_count(0)
        assert created[1] == {"name": "Existing", "folders": [{"path": "/work/existing"}]}, created[1]
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
