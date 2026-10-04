"""An empty project's first goal is a saved contract, never an implicit run."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def scenario(language: str, width: int) -> None:
    project = {"id": "p-guided", "name": "Bakery", "entity_revision": 1,
               "folders": folders("/managed/bakery"), "created_at": "2026-10-03T00:00:00Z",
               "settings": {"snapshots": True}, "system": "", "sessions": []}
    goal = {"project_id": project["id"], "goal_revision": 1, "entity_revision": 1,
            "body": "", "checks": None}
    writes: list[dict] = []
    posts: list[str] = []
    fail = True

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 700})

        def answer(route, value, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

        def stub(route):
            nonlocal fail
            request = route.request
            path = urlsplit(request.url).path
            if request.method == "POST":
                posts.append(path)
            if path == "/api/projects" and request.method == "GET":
                return answer(route, [project])
            if path == "/api/sessions":
                return answer(route, {"sessions": [], "projects": []})
            if path == "/api/settings":
                return answer(route, {"presets": {}, "model": {}})
            if path == "/api/projects/p-guided/scope-revisions/current" and request.method == "GET":
                return answer(route, goal)
            if path == "/api/projects/p-guided/scope-revisions" and request.method == "POST":
                body = request.post_data_json
                writes.append(body)
                assert body["root_task_ids"] == [] and body["body"] == "Publish a clear menu"
                assert body["checks"] == ["All items listed", "Prices checked"]
                if fail:
                    fail = False
                    return answer(route, {"detail": "the entity has changed"}, 409)
                assert body["expected_goal_revision"] == goal["goal_revision"]
                goal.update(goal_revision=goal["goal_revision"] + 1,
                            entity_revision=goal["entity_revision"] + 1,
                            body=body["body"], checks=body["checks"])
                return answer(route, {"project_id": project["id"], "goal_revision": goal["goal_revision"],
                                      "entity_revision": goal["entity_revision"], "receipt_id": "goal-receipt"})
            if fulfil_shared(route):
                return None
            return answer(route, [])

        page.route("**/api/**", stub)
        page.goto(f"{BASE}/orchestration/project/p-guided?token=t&lang={language}")
        goal_input = page.locator("#guided-goal-body")
        checks_input = page.locator("#guided-goal-checks")
        expect(goal_input).to_be_visible()
        goal_input.fill("Publish a clear menu")
        checks_input.fill("All items listed\nPrices checked")
        page.get_by_role("button", name="Save" if language == "en" else "Сохранить", exact=True).click()
        expect(page.get_by_role("button", name="Check current goal" if language == "en" else "Проверить текущую цель")).to_be_visible()
        expect(goal_input).to_have_value("Publish a clear menu")
        page.get_by_role("button", name="Check current goal" if language == "en" else "Проверить текущую цель").click()
        page.get_by_role("button", name="Save" if language == "en" else "Сохранить", exact=True).click()
        expect(page.get_by_text("Goal and criteria saved" if language == "en" else "Цель и критерии сохранены")).to_be_visible()
        assert len(writes) == 2 and writes[0]["client_operation_id"] != writes[1]["client_operation_id"]
        assert [path for path in posts if "scope-revisions" in path] == ["/api/projects/p-guided/scope-revisions"] * 2, posts
        assert not any("/run" in path or "/attempt" in path for path in posts), posts
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        browser.close()


def failed_reply_keeps_the_exact_request() -> None:
    project = {"id": "p-guided", "name": "Bakery", "entity_revision": 1,
               "folders": folders("/managed/bakery"), "created_at": "2026-10-03T00:00:00Z",
               "settings": {"snapshots": True}, "system": "", "sessions": []}
    goal = {"project_id": project["id"], "goal_revision": 1, "entity_revision": 1,
            "body": "", "checks": None}
    writes: list[dict] = []
    read_fails = True

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 390, "height": 700})

        def answer(route, value, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

        def stub(route):
            nonlocal read_fails
            request = route.request
            path = urlsplit(request.url).path
            if path == "/api/projects" and request.method == "GET":
                return answer(route, [project])
            if path == "/api/sessions":
                return answer(route, {"sessions": [], "projects": []})
            if path == "/api/settings":
                return answer(route, {"presets": {}, "model": {}})
            if path == "/api/projects/p-guided/scope-revisions/current" and request.method == "GET":
                if read_fails:
                    return answer(route, {"detail": "unavailable"}, 503)
                return answer(route, goal)
            if path == "/api/projects/p-guided/scope-revisions" and request.method == "POST":
                writes.append(request.post_data_json)
                if len(writes) == 1:
                    return answer(route, {"detail": "response unavailable"}, 503)
                goal.update(goal_revision=2, entity_revision=2, body=writes[-1]["body"], checks=writes[-1]["checks"])
                return answer(route, {"project_id": project["id"], "goal_revision": 2,
                                      "entity_revision": 2, "receipt_id": "replayed-receipt"})
            if fulfil_shared(route):
                return None
            return answer(route, [])

        page.route("**/api/**", stub)
        page.goto(f"{BASE}/orchestration/project/p-guided?token=t&lang=en")
        expect(page.get_by_text("The current goal could not be checked.", exact=False)).to_be_visible()
        assert page.locator("#guided-goal-body").count() == 0
        read_fails = False
        page.get_by_role("button", name="Try again").click()
        page.locator("#guided-goal-body").fill("Publish a clear menu")
        page.locator("#guided-goal-checks").fill("All items listed")
        page.reload()
        expect(page.locator("#guided-goal-body")).to_have_value("Publish a clear menu")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("The save outcome is unconfirmed.", exact=False)).to_be_visible()
        page.reload()
        expect(page.locator("#guided-goal-body")).to_have_value("Publish a clear menu")
        page.get_by_role("button", name="Try again").click()
        expect(page.get_by_text("Goal and criteria saved")).to_be_visible()
        assert len(writes) == 2 and writes[0] == writes[1]
        browser.close()


def remembered_view_respects_scope_and_direct_links() -> None:
    project = {"id": "p-guided", "name": "Bakery", "entity_revision": 2,
               "folders": folders("/managed/bakery"), "created_at": "2026-10-03T00:00:00Z",
               "settings": {"snapshots": True, "orchestrator": {"enabled": True, "session_id": "coordinator"}},
               "system": "", "sessions": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 390, "height": 700})

        def stub(route):
            request = route.request
            path = urlsplit(request.url).path
            if path == "/api/projects":
                return route.fulfill(status=200, content_type="application/json", body=json.dumps([project]))
            if path == "/api/projects/p-guided/scope-revisions/current":
                goal = {"project_id": project["id"], "goal_revision": 2, "entity_revision": 2,
                        "body": "Publish a clear menu", "checks": ["All items listed"]}
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(goal))
            if path == "/api/projects/p-guided/brief":
                return route.fulfill(status=200, content_type="application/json", body='{"sections":[]}')
            if path == "/api/projects/p-guided/journal":
                return route.fulfill(status=200, content_type="application/json", body='{"entries":[],"rules":[],"next_before":null}')
            if path == "/api/sessions":
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
            if path == "/api/settings":
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
            if fulfil_shared(route):
                return None
            route.fulfill(status=200, content_type="application/json", body="[]")

        page.route("**/api/**", stub)
        page.add_init_script("""localStorage.setItem('daedalus.mode', 'orchestration');
            localStorage.setItem('daedalus.project.last_view', JSON.stringify({version:1,
              project_id:'p-guided', page:'brief', coordinator_session_id:'coordinator'}));""")
        page.goto(f"{BASE}/?token=t&lang=en")
        expect(page).to_have_url(f"{BASE}/orchestration/project/p-guided/brief")
        page.goto(f"{BASE}/orchestration/project/p-guided/journal?token=t&lang=en")
        expect(page).to_have_url(f"{BASE}/orchestration/project/p-guided/journal")
        page.close()

        stale = browser.new_page(viewport={"width": 390, "height": 700})
        stale.route("**/api/**", stub)
        stale.add_init_script("""localStorage.setItem('daedalus.mode', 'orchestration');
            localStorage.setItem('daedalus.project.last_view', JSON.stringify({version:1,
              project_id:'p-guided', page:'brief', coordinator_session_id:'deleted'}));""")
        stale.goto(f"{BASE}/?token=t&lang=en")
        expect(stale).to_have_url(f"{BASE}/orchestration/project/p-guided")
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for language in ("en", "ru"):
        for width in (320, 390, 1440):
            scenario(language, width)
    failed_reply_keeps_the_exact_request()
    remembered_view_respects_scope_and_direct_links()
    print("Guided goal: RU/EN at 320/390/1440, stale draft, read failure, replay, direct links and scoped view")
