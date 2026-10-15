"""A project's environment in its settings: edited, saved whole, prepared, and its hold shown."""

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
SHOTS = os.environ.get("SHOTS_DIR", "")

HOLD = "environment not ready: preparation failed: git clone /srv/api.git exited with 128. The operator fixes it in the project's settings and presses Prepare"


def run() -> int:
    unhandled = Unhandled()
    project = {"id": "p1", "name": "Plain", "folders": folders("/work/plain"),
               "created_at": "2026-10-03T00:00:00Z", "settings": {"snapshots": True, "system": ""},
               "system": "", "sessions": []}
    spec = {"repos": [], "seeds": [], "mcp": [], "skills": [], "goal": "", "goal_minutes": 30, "goal_usd": 2.0}
    state = {"spec": spec, "state": "unprepared", "hold": "", "reason": "", "log": "", "attempt": 0}
    writes: list[dict] = []
    prepares: list[str] = []

    def view() -> dict:
        current = state["spec"]
        needs = bool(current["repos"] or current["seeds"] or current["goal"])
        return {"project_id": "p1", "spec": current, "configured": needs or bool(current["mcp"] or current["skills"]),
                "needs_preparation": needs, "state": state["state"], "stale": False, "reason": state["reason"],
                "log": state["log"], "hold": state["hold"], "attempt": state["attempt"], "setup_session_id": "",
                "started_at": "", "finished_at": "2026-10-15T08:00:00Z" if state["attempt"] else "", "updated_at": "",
                "choices": {"mcp": ["github", "notion"], "skills": ["review"]}}

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
        if path == "/api/projects/p1/environment" and request.method == "PUT":
            writes.append(request.post_data_json)
            state["spec"] = request.post_data_json["spec"]
            return answer(route, view())
        if path == "/api/projects/p1/environment":
            return answer(route, view())
        if path == "/api/projects/p1/environment/prepare":
            prepares.append(request.method)
            state.update({"state": "failed", "hold": HOLD, "reason": "git clone failed", "attempt": 1,
                          "log": "failed: git clone /srv/api.git exited with 128"})
            return answer(route, view())
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
        page.locator(".projects-row", has_text="Plain").get_by_role("button", name="Settings for Plain").click()
        section = page.locator(".project-environment")
        section.locator("summary").first.click()
        expect(section).to_contain_text("Prepared once")
        prepare = section.get_by_role("button", name="Prepare", exact=True)
        expect(prepare).to_be_disabled()
        section.get_by_role("button", name="Add a repository").click()
        section.get_by_label("Repository URL").fill("/srv/api.git")
        section.get_by_label("Folder name").fill("api")
        section.get_by_role("button", name="Add a file").click()
        section.get_by_label("File content").fill("Run the tests before you hand in.")
        section.get_by_label("github").check()
        section.get_by_role("button", name="Save", exact=True).click()
        expect(prepare).to_be_enabled()
        sent = writes[-1]["spec"]
        assert sent["repos"] == [{"url": "/srv/api.git", "branch": "", "folder": "api"}], sent
        assert sent["seeds"][0]["path"] == "AGENTS.md" and sent["mcp"] == ["github"], sent
        prepare.click()
        expect(section.locator(".result-warning")).to_contain_text("environment not ready: preparation failed")
        expect(section.locator("summary").first).to_contain_text("Preparation failed")
        assert prepares == ["POST"], prepares
        if SHOTS:
            section.screenshot(path=f"{SHOTS}/project-environment-wide.png")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if SHOTS:
            section.screenshot(path=f"{SHOTS}/project-environment-phone.png")
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
