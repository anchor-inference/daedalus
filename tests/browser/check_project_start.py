"""The orchestration project form preserves a typed goal and exact command through a lost reply.

The goal, the first task and its checks live in orchestration mode's "New orchestration project",
opened from that mode's empty list; the plain new-project dialog no longer holds them.
"""

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


def open_form(page, language: str) -> None:  # type: ignore[no-untyped-def]
    """Orchestration mode with no orchestrated project offers the form under its empty list."""
    page.goto(f"{BASE}/orchestration?token=t&lang={language}")
    page.get_by_role("button", name="New orchestration project" if language == "en" else "Новый проект оркестрации").first.click()


def scenario(language: str, width: int) -> None:
    projects: list[dict] = []
    requests: list[dict] = []
    receipt = None
    lost = True

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def answer(route, value, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

        def stub(route):
            nonlocal receipt, lost
            request = route.request
            url = urlsplit(request.url)
            path = url.path
            body = request.post_data_json if request.post_data and request.method == "POST" else None
            if path == "/api/control/revisions":
                return answer(route, {"scope": {"kind": "global", "id": "global"},
                                      "collection_revision": 1 if receipt is None else 2, "entity_revision": None})
            if path == "/api/project-start" and request.method == "POST":
                requests.append(body)
                assert body["name"] == "Bakery" and body["goal"] == "Publish a clear menu"
                assert body["constraints"] == "Keep current prices" and body["task_title"] == "Prepare menu"
                assert body["checks"] == ["All items listed", "Wording checked"]
                assert body["owner_intent"] == "manual" and body["expected_collection_revision"] == 1
                if receipt is None:
                    receipt = {"project_id": "p1", "task_id": "task-one", "goal_revision": 1,
                               "contract_revision": 1, "owner_intent": "manual", "receipt_id": "guided-receipt", "entity_revision": 2}
                    projects.append({"id": "p1", "name": "Bakery", "entity_revision": 1,
                                     "folders": folders("/managed/p1"), "created_at": "2026-10-03T00:00:00Z",
                                     "settings": {"snapshots": True}, "system": "", "sessions": []})
                else:
                    assert body == requests[0]
                if lost:
                    lost = False
                    return answer(route, {"detail": "unconfirmed response"}, 503)
                return answer(route, receipt)
            if path == "/api/projects" and request.method == "GET":
                return answer(route, projects)
            if path == "/api/sessions":
                return answer(route, {"sessions": [], "projects": []})
            if path == "/api/settings":
                return answer(route, {"presets": {}, "model": {}})
            if path == "/api/project-environments":
                return answer(route, {"local": "container", "available": ["container"]})
            if fulfil_shared(route):
                return None
            # The project screen can request its other independent, empty sections after navigation.
            answer(route, [])

        page.route("**/api/**", stub)
        open_form(page, language)
        page.set_viewport_size({"width": width, "height": 560 if width == 320 else 900})
        sheet = page.locator(".sheet.np-orch")
        expect(sheet).to_be_visible()
        sheet.locator("#project-name").fill("Bakery")
        sheet.locator("#project-start-goal").fill("Publish a clear menu")
        sheet.locator("#project-start-constraints").fill("Keep current prices")
        sheet.locator("#project-start-task").fill("Prepare menu")
        sheet.locator("#project-start-checks").fill("\n".join(f"Check {index}" for index in range(13)))
        expect(sheet.get_by_role("button", name="Create project and first task" if language == "en" else "Создать проект и первую задачу")).to_be_disabled()
        page.set_viewport_size({"width": 1440, "height": 900})
        open_form(page, language)
        page.set_viewport_size({"width": width, "height": 560 if width == 320 else 900})
        sheet = page.locator(".sheet.np-orch")
        expect(sheet.locator("#project-start-checks")).to_have_value("\n".join(f"Check {index}" for index in range(13)))
        sheet.locator("#project-start-checks").fill("All items listed\nWording checked")
        sheet.get_by_role("button", name="Create project and first task" if language == "en" else "Создать проект и первую задачу").click()
        expect(sheet).to_contain_text("Couldn't confirm the project was created" if language == "en" else "Не удалось подтвердить создание проекта")
        page.set_viewport_size({"width": 1440, "height": 900})
        open_form(page, language)
        page.set_viewport_size({"width": width, "height": 560 if width == 320 else 900})
        sheet = page.locator(".sheet.np-orch")
        sheet.get_by_role("button", name="Try again" if language == "en" else "Ещё раз").click()
        expect(sheet).to_have_count(0)
        page.wait_for_url("**/project/p1/board?task=task-one*")
        assert len(requests) == 2 and requests[0] == requests[1]
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()


def conflict_scenario(language: str) -> None:
    requests: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def answer(route, value, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

        def stub(route):
            request = route.request
            path = urlsplit(request.url).path
            if path == "/api/control/revisions":
                return answer(route, {"collection_revision": 1 if not requests else 2})
            if path == "/api/project-start" and request.method == "POST":
                requests.append(request.post_data_json)
                return answer(route, {"detail": {"reason": "the entity has changed", "current_revision": 2}}, 409)
            if path == "/api/projects":
                return answer(route, [])
            if path == "/api/sessions":
                return answer(route, {"sessions": [], "projects": []})
            if path == "/api/settings":
                return answer(route, {"presets": {}, "model": {}})
            if path == "/api/project-environments":
                return answer(route, {"local": "container", "available": ["container"]})
            if fulfil_shared(route):
                return None
            answer(route, [])

        page.route("**/api/**", stub)
        open_form(page, language)
        sheet = page.locator(".sheet.np-orch")
        sheet.locator("#project-name").fill("Bakery")
        sheet.locator("#project-start-goal").fill("Publish a clear menu")
        sheet.locator("#project-start-constraints").fill("Keep current prices")
        sheet.locator("#project-start-task").fill("Prepare menu")
        sheet.locator("#project-start-checks").fill("All items listed")
        sheet.get_by_role("button", name="Create project and first task" if language == "en" else "Создать проект и первую задачу").click()
        expect(sheet).to_contain_text("The project list changed" if language == "en" else "Список проектов изменился")
        expect(sheet.locator("#project-start-goal")).to_have_value("Publish a clear menu")
        expect(sheet.get_by_role("button", name="Create project and first task" if language == "en" else "Создать проект и первую задачу")).to_be_disabled()
        sheet.get_by_role("button", name="Review current projects" if language == "en" else "Проверить проекты").click()
        sheet.get_by_role("button", name="Create project and first task" if language == "en" else "Создать проект и первую задачу").click()
        expect(sheet).to_contain_text("The project list changed" if language == "en" else "Список проектов изменился")
        assert len(requests) == 2
        assert requests[0]["client_operation_id"] != requests[1]["client_operation_id"]
        assert requests[1]["expected_collection_revision"] == 2
        assert requests[0]["goal"] == requests[1]["goal"]
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (320, 390, 1440):
            scenario(lang, viewport)
            print(f"project start {lang} {viewport}: PASS")
        conflict_scenario(lang)
        print(f"project start {lang} conflict: PASS")
