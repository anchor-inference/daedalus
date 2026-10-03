"""Approve, recover, and withdraw a bounded coordinator grant in the real app shell."""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folder, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = {
    "id": "p1", "entity_revision": 1, "name": "Bakery", "created_at": "2026-09-19T00:00:00Z", "system": "",
    "settings": {"snapshots": False, "system": "", "ephemeral": False, "default_env": "container"},
    "folders": [folder("/work/site", label="Site")], "sessions": [],
}
RIGHTS = {
    "planning": {"scope_kind": "project", "operations": ["board.task.create", "board.task.update", "contract.require", "contract.apply", "contract.withdraw"], "effects": []},
    "execution": {"scope_kind": "task", "operations": ["task.launch", "task.stop", "staff.release"], "effects": ["execution.start", "execution.stop"]},
    "execution_project": {"scope_kind": "project", "operations": ["task.launch", "task.stop", "staff.release"], "effects": ["execution.start", "execution.stop"]},
    "review": {"scope_kind": "project", "operations": ["review.verdict", "review.return"], "effects": []},
    "watch": {"scope_kind": "project", "operations": ["watch.create", "watch.change", "watch.remove", "watch.deliver"], "effects": ["watch.wake", "watch.tell", "watch.notify"]},
}
WORDS = {
    "en": {"projects": "Projects", "settings": "Settings for Bakery", "title": "Coordinator permissions", "add": "Approve an action", "watch": "Manage project watches", "approve": "Approve", "retry": "Retry original request", "withdraw": "Withdraw approval", "reason": "Reason for withdrawal", "active": "Active approvals: 1", "wake": "may wake the coordinator"},
    "ru": {"projects": "Проекты", "settings": "Настройки: Bakery", "title": "Полномочия координатора", "add": "Разрешить действие", "watch": "Управлять наблюдениями проекта", "approve": "Разрешить", "retry": "Повторить исходный запрос", "withdraw": "Отозвать разрешение", "reason": "Причина отзыва", "active": "Действующих разрешений: 1", "wake": "могут будить координатора"},
}


def run() -> int:
    unhandled = Unhandled()
    failures = []
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile in ((390, 844, True), (1440, 900, False)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(6000)
                try:
                    scenario(page, language, unhandled)
                    print(f"ok {language} {width}")
                except Exception as exc:  # noqa: BLE001 — retain each language and viewport result
                    failures.append(f"{language} {width}: {exc}")
                    print(f"FAILED {language} {width}: {exc}")
                context.close()
        browser.close()
    return 1 if failures else unhandled.report()


def scenario(page: Page, language: str, unhandled: Unhandled) -> None:
    words = WORDS[language]
    project = copy.deepcopy(PROJECT)
    max_expiry = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    state: dict[str, object] = {"revision": 1, "grant": None, "lost": True, "approvals": [], "withdrawals": []}
    base = "/api/projects/p1/orchestrator/authority"

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        method = request.method
        body = request.post_data_json if method == "POST" else None
        if path == "/api/projects" and method == "GET":
            return answer(route, [project])
        if path == "/api/sessions" and method == "GET":
            return answer(route, {"sessions": [], "projects": [{**project, "total": 0, "active": 0, "loops": 0, "last_message_at": ""}]})
        if path == "/api/project-environments" and method == "GET":
            return answer(route, {"local": "container", "available": ["container"], "host_bridge": False, "docker": True})
        if path == "/api/control/revisions" and method == "GET":
            return answer(route, {"scope": {"kind": "global", "id": "global"}, "collection_revision": 1, "entity_revision": None})
        if path == "/api/projects/p1/workspace-archive" and method == "GET":
            return answer(route, {"latest": None, "available": False})
        if path == "/api/projects/p1/board" and method == "GET":
            return answer(route, {"project": {"id": "p1", "name": "Bakery"}, "tasks": [], "staff": [], "needs_you": [], "counts": {}})
        if path == base and method == "GET":
            return answer(route, {"project_id": "p1", "entity_revision": state["revision"],
                "current_coordinator_session_id": "coordinator-session", "readiness_blockers": [],
                "available_bundles": [{"id": key, **rule, "max_expires_at": max_expiry, "blockers": []} for key, rule in RIGHTS.items()],
                "grants": [state["grant"]] if state["grant"] else []})
        if path == base and method == "POST":
            assert isinstance(body, dict)
            assert set(body) == {"client_operation_id", "expected_entity_revision", "expected_coordinator_session_id", "bundle_id", "task_id", "expires_at"}
            assert body["bundle_id"] == "watch" and body["task_id"] is None
            assert body["expected_coordinator_session_id"] == "coordinator-session"
            assert body["expected_entity_revision"] == 1 and body["client_operation_id"]
            assert datetime.fromisoformat(body["expires_at"]) <= datetime.fromisoformat(max_expiry)
            state["approvals"].append(body)
            if state["grant"] is None:
                state["grant"] = {"grant_id": "grant-1", "generation": 1, "session_id": "coordinator-session",
                    "scope": {"kind": "project", "id": "p1"}, **RIGHTS["watch"], "expires_at": body["expires_at"],
                    "revoked_at": None, "state": "active", "receipt_id": "receipt-approval",
                    "parent_grant_id": None, "parent_grant_generation": None}
                state["revision"] = 2
            if state["lost"]:
                state["lost"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, {"grant_id": "grant-1", "receipt_id": "receipt-approval", "entity_revision": 2})
        if path == base + "/grant-1/revoke" and method == "POST":
            assert isinstance(body, dict)
            assert set(body) == {"client_operation_id", "expected_entity_revision", "expected_coordinator_session_id", "expected_grant_generation", "reason"}
            assert body["expected_entity_revision"] == 2 and body["expected_grant_generation"] == 1
            assert body["expected_coordinator_session_id"] == "coordinator-session" and body["reason"] == "No longer needed"
            state["withdrawals"].append(body)
            grant = state["grant"]
            assert isinstance(grant, dict)
            grant["revoked_at"] = datetime.now(UTC).isoformat()
            grant["state"] = "revoked"
            state["revision"] = 3
            return answer(route, {"grant_id": "grant-1", "receipt_id": "receipt-withdrawal", "entity_revision": 3})
        if path == "/api/settings" and method == "GET":
            return answer(route, {"presets": {}, "model": {}})
        if path == "/api/project-directories" and method == "GET":
            return answer(route, {"roots": [], "docker": True})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)

    def open_authority() -> object:
        page.locator(f".project-chip:visible, .start-list-head .iconbtn[aria-label='{words['projects']}']:visible").first.click()
        page.locator(f".project-row .iconbtn[aria-label='{words['settings']}']").click()
        section = page.locator(".sheet-section", has=page.get_by_text(words["title"])).last
        section.locator("summary").first.click()
        return section

    page.goto(f"{BASE}/agents?token=t&lang={language}")
    section = open_authority()
    section.get_by_text(words["add"]).click()
    section.get_by_label("Allowed actions" if language == "en" else "Разрешённые действия").select_option("watch")
    expect(section.get_by_text(words["wake"], exact=False)).to_be_visible()
    section.get_by_role("button", name=words["approve"]).click()
    dialog = page.locator(".dialog[role='alertdialog']")
    expect(dialog.get_by_text("watch.wake", exact=False)).to_be_visible()
    dialog.get_by_role("button", name=words["approve"]).click()
    expect(section.get_by_role("button", name=words["retry"])).to_be_visible()

    page.reload()
    section = open_authority()
    expect(section.get_by_role("button", name=words["retry"])).to_be_visible()
    section.get_by_role("button", name=words["retry"]).click()
    expect(section.get_by_text(words["active"])).to_be_visible()
    assert state["approvals"][0] == state["approvals"][1]
    section.get_by_role("button", name=words["withdraw"]).first.click()
    section.get_by_label(words["reason"]).fill("No longer needed")
    section.get_by_role("button", name=words["withdraw"]).last.click()
    dialog = page.locator(".dialog[role='alertdialog']")
    expect(dialog.get_by_text("Pending actions" if language == "en" else "Ожидающие действия", exact=False)).to_be_visible()
    dialog.get_by_role("button", name=words["withdraw"]).click()
    section.get_by_text("Earlier approvals (1)" if language == "en" else "Прежние разрешения (1)").click()
    expect(section.get_by_text("withdrawn" if language == "en" else "отозвано", exact=False)).to_be_visible()
    assert len(state["withdrawals"]) == 1
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, overflow


if __name__ == "__main__":
    raise SystemExit(run())
