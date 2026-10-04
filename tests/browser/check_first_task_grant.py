"""A new project's first task leads to its exact coordinator assignment permission."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app
from check_project_board import PID, project, serve
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def scenario(language: str, width: int, coordinator: bool) -> None:
    task = BoardStub.task("first-task", "Prepare menu", project_id=PID, entity_revision=1,
                          checklist=[{"text": "All items listed", "done": False}])
    stub = BoardStub(project(), tasks=[task])
    unhandled = Unhandled()
    approvals: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 844 if width < 1024 else 900})
        serve(page, stub, unhandled)

        def authority(route):  # type: ignore[no-untyped-def]
            request = route.request
            path = urlsplit(request.url).path
            if path != f"/api/projects/{PID}/orchestrator/authority":
                return route.fallback()
            if request.method == "POST":
                approvals.append(request.post_data_json)
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({"grant_id": "grant-one"}))
            payload = {"project_id": PID, "entity_revision": 1,
                       "current_coordinator_session_id": "coordinator-one" if coordinator else None,
                       "readiness_blockers": [], "grants": [], "available_bundles": [{
                           "id": "assignment_execution", "scope_kind": "task",
                           "operations": ["board.task.assign", "task.launch", "task.stop", "staff.release"],
                           "effects": ["execution.start", "execution.stop"],
                           "max_expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                           "blockers": [],
                       }]}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        page.route("**/api/**", authority)
        page.goto(f"{BASE}/project/{PID}/board?task=first-task&token=t&lang={language}")
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        grant = sheet.get_by_role("button", name="Let the coordinator assign and run this task" if language == "en" else "Разрешить координатору назначить и запустить задачу")
        assert grant.evaluate("(node) => !!(node.compareDocumentPosition(document.querySelector('.pboard-sheet .task-workflow')) & Node.DOCUMENT_POSITION_FOLLOWING)")
        grant.click()
        expect(page).to_have_url(f"{BASE}/orchestration/project/{PID}/board?task=first-task&grant=assignment_execution")
        expect(sheet).to_contain_text("Prepare menu")
        authority_section = sheet.locator(".sheet-section", has_text="Coordinator permissions" if language == "en" else "Полномочия координатора")
        expect(authority_section).to_be_visible()
        expect(authority_section.get_by_label("Allowed actions" if language == "en" else "Разрешённые действия")).to_have_value("assignment_execution")
        expect(authority_section.locator("select.field").nth(1)).to_have_value("first-task")
        approve = authority_section.get_by_role("button", name="Approve" if language == "en" else "Разрешить")
        if coordinator:
            expect(approve).to_be_enabled()
            approve.click()
            dialog = page.locator(".dialog[role='alertdialog']")
            expect(dialog).to_contain_text("board.task.assign")
            expect(dialog).to_contain_text("task.launch")
            expect(dialog).to_contain_text("execution.start")
            assert "board.task.update" not in dialog.inner_text()
            dialog.get_by_role("button", name="Approve" if language == "en" else "Разрешить").click()
            expect(page.locator(".toast")).to_contain_text("Approval recorded" if language == "en" else "Разрешение записано")
            assert len(approvals) == 1
            assert approvals[0]["bundle_id"] == "assignment_execution" and approvals[0]["task_id"] == "first-task"
        else:
            expect(approve).to_be_disabled()
            expect(authority_section.get_by_role("button", name="Enable the coordinator" if language == "en" else "Включить координатора")).to_be_visible()
        assert len(approvals) == (1 if coordinator else 0)
        sheet.get_by_role("button", name="Back to task" if language == "en" else "Назад к задаче").click()
        expect(page).to_have_url(f"{BASE}/orchestration/project/{PID}/board?task=first-task")
        expect(sheet.get_by_role("button", name="Let the coordinator assign and run this task" if language == "en" else "Разрешить координатору назначить и запустить задачу")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    assert unhandled.report() == 0


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (390, 1440):
            for enabled in (False, True):
                scenario(lang, viewport, enabled)
                print(f"first task grant {lang} {viewport} coordinator={enabled}: PASS")
