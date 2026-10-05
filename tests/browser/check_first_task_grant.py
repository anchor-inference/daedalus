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


def scenario(language: str, width: int, coordinator: bool, staff_state: str, previous_coordinator: bool = False) -> None:
    assigned = staff_state == "assigned"
    bundle_id = "execution" if assigned else "assignment_execution"
    operations = ["task.launch", "task.stop", "staff.release"] if assigned else ["board.task.assign", "task.launch", "task.stop", "staff.release"]
    grant_name = ("Let the coordinator run this task" if assigned else "Let the coordinator assign and run this task") if language == "en" else (
        "Разрешить координатору запустить задачу" if assigned else "Разрешить координатору назначить и запустить задачу")
    assignee = BoardStub.assignee("first-worker", "First worker", harness="daedalus") if assigned else None
    task = BoardStub.task("first-task", "Prepare menu", project_id=PID, entity_revision=1,
                          assignee=assignee, checklist=[{"text": "All items listed", "done": False}])
    staff = [{"id": "first-worker", "name": "First worker", "color": "blue", "harness": "daedalus"}] if staff_state != "empty" else []
    stub = BoardStub(project(), staff=staff, tasks=[task])
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
            grants = [{"grant_id": "grant-one", "generation": 1, "session_id": "coordinator-one",
                       "scope": {"kind": "task", "id": "first-task"}, "operations": operations,
                       "effects": ["execution.start", "execution.stop"], "expires_at": approvals[-1]["expires_at"],
                       "revoked_at": None, "state": "active", "receipt_id": "receipt-one",
                       "parent_grant_id": None, "parent_grant_generation": None}] if approvals else []
            if previous_coordinator:
                grants.append({"grant_id": "grant-previous", "generation": 1, "session_id": "coordinator-previous",
                               "scope": {"kind": "task", "id": "first-task"}, "operations": operations,
                               "effects": ["execution.start", "execution.stop"],
                               "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                               "revoked_at": None, "state": "active", "receipt_id": "receipt-previous",
                               "parent_grant_id": None, "parent_grant_generation": None})
            if coordinator:
                grants.append({"grant_id": "grant-other", "generation": 1, "session_id": "coordinator-one",
                               "scope": {"kind": "task", "id": "another-task"}, "operations": operations,
                               "effects": ["execution.start", "execution.stop"],
                               "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                               "revoked_at": None, "state": "active", "receipt_id": "receipt-other",
                               "parent_grant_id": None, "parent_grant_generation": None})
            payload = {"project_id": PID, "entity_revision": 1,
                       "current_coordinator_session_id": "coordinator-one" if coordinator else None,
                       "readiness_blockers": [], "grants": grants, "available_bundles": [{
                           "id": bundle_id, "scope_kind": "task", "operations": operations,
                           "effects": ["execution.start", "execution.stop"],
                           "max_expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                           "blockers": [],
                       }]}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        page.route("**/api/**", authority)
        page.goto(f"{BASE}/project/{PID}/board?task=first-task&token=t&lang={language}")
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        if staff_state != "empty":
            expect(sheet.get_by_text("The project has no team yet." if language == "en" else "У проекта пока нет команды.")).to_have_count(0)
        else:
            expect(sheet.get_by_role("button", name="Hire someone" if language == "en" else "Нанять сотрудника")).to_be_visible()
        grant = sheet.get_by_role("button", name=grant_name)
        assert grant.evaluate("(node) => !!(node.compareDocumentPosition(document.querySelector('.pboard-sheet .task-workflow')) & Node.DOCUMENT_POSITION_FOLLOWING)")
        grant.click()
        expect(page).to_have_url(f"{BASE}/orchestration/project/{PID}/board?task=first-task&grant={bundle_id}")
        expect(sheet).to_contain_text("Prepare menu")
        authority_section = sheet.locator(".sheet-section", has_text="Coordinator permissions" if language == "en" else "Полномочия координатора")
        expect(authority_section).to_be_visible()
        expect(authority_section.get_by_label("Allowed actions" if language == "en" else "Разрешённые действия")).to_have_count(0)
        expect(authority_section.get_by_text("Prepare menu").first).to_be_visible()
        expect(authority_section.locator("select.field")).to_have_count(1)
        approve = authority_section.get_by_role("button", name="Approve" if language == "en" else "Разрешить")
        continue_label = "Continue in project conversation" if language == "en" else "Продолжить в разговоре проекта"
        expect(authority_section.get_by_role("button", name=continue_label)).to_have_count(0)
        if coordinator:
            if previous_coordinator:
                expect(authority_section.locator(".project-extension")).to_have_count(1)
                expect(authority_section.get_by_role("button", name=continue_label)).to_have_count(0)
            expect(approve).to_be_enabled()
            approve.click()
            dialog = page.locator(".dialog[role='alertdialog']")
            if assigned:
                assert "board.task.assign" not in dialog.inner_text()
            else:
                expect(dialog).to_contain_text("board.task.assign")
            expect(dialog).to_contain_text("task.launch")
            expect(dialog).to_contain_text("execution.start")
            assert "board.task.update" not in dialog.inner_text()
            dialog.get_by_role("button", name="Approve" if language == "en" else "Разрешить").click()
            expect(page.locator(".toast")).to_contain_text("Approval recorded" if language == "en" else "Разрешение записано")
            expect(authority_section.get_by_role("button", name="Approve" if language == "en" else "Разрешить")).to_have_count(0)
            expect(authority_section.get_by_text("Prepare menu").first).to_be_visible()
            expect(authority_section.get_by_role("button", name=continue_label)).to_be_visible()
            expect(authority_section).to_contain_text("permission alone does not start work" if language == "en" else "само разрешение не начинает работу")
            assert len(approvals) == 1
            assert approvals[0]["bundle_id"] == bundle_id and approvals[0]["task_id"] == "first-task"
        else:
            expect(approve).to_be_disabled()
            expect(authority_section.get_by_role("button", name="Enable the coordinator" if language == "en" else "Включить координатора")).to_be_visible()
        assert len(approvals) == (1 if coordinator else 0)
        sheet.get_by_role("button", name="Back to task" if language == "en" else "Назад к задаче").click()
        expect(page).to_have_url(f"{BASE}/orchestration/project/{PID}/board?task=first-task")
        expect(sheet.get_by_role("button", name=grant_name)).to_be_visible()
        if coordinator:
            sheet.get_by_role("button", name=grant_name).click()
            expect(authority_section.get_by_role("button", name=continue_label)).to_be_visible()
            authority_section.get_by_role("button", name=continue_label).click()
            expect(page).to_have_url(f"{BASE}/orchestration/project/{PID}")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    assert unhandled.report() == 0


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (320, 1440):
            for enabled in (False, True):
                for staff_state in ("empty", "hired", "assigned"):
                    scenario(lang, viewport, enabled, staff_state)
                    print(f"first task grant {lang} {viewport} coordinator={enabled} staff={staff_state}: PASS")
        scenario(lang, 390, True, "empty", previous_coordinator=True)
        print(f"first task grant {lang} previous coordinator: PASS")
