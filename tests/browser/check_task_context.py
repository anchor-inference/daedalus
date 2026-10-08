"""The task shows a current preview separately from an immutable supplied packet."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, TeamStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def check(language: str, width: int) -> None:
    unhandled = Unhandled()
    project = {"id": "p1", "name": "Bakery", "entity_revision": 1, "folders": folders("/home/operator/work/bakery"),
               "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": False}, "system": "", "sessions": []}
    staff = [{"id": "one", "name": "Ira", "color": "orange", "harness": "daedalus", "isolation": "worktree"}]
    task = BoardStub.task("task", "Prepare catalog", project_id="p1", brief={"objective": "Catalog", "deliverable": "Report", "boundaries": "One folder", "done_when": "Evidence"})
    board = BoardStub(project, staff=staff, tasks=[task])
    team = TeamStub(project)
    base = "/api/board/task"
    current = {"task_id": "task", "role": "worker", "role_hint": "", "title": "Prepare catalog", "contract_revision": 2,
               "contract": {"brief": {"objective": "Fresh catalog"}, "checklist": [{"id": "C1", "text": "Check prices"}]},
               "dependencies": [], "facts": [{"fact_id": "f1", "version": 1, "claim": "Source changed", "source_kind": "file", "source_id": "file-1"}],
               "artifacts": [], "source_refs": ["task-contract:task@2"], "packet_hash": "sha256:current"}
    historical = {**current, "contract_revision": 1, "contract": {"brief": {"objective": "Original catalog"}},
                  "facts": [], "source_refs": ["task-contract:task@1"], "packet_hash": "sha256:original"}
    entry = {"staff_session_id": "session-one", "staff_id": "one", "role": "worker", "role_hint": "worker", "contract_revision": 1,
             "packet_hash": "sha256:original", "source_refs": ["task-contract:task@1"], "source_current": False,
             "current_packet_hash": "sha256:current", "created_at": "2026-10-03T10:00:00Z", "session_ended_at": None}
    preview_fail = True
    network_down = False

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 560 if width == 320 else 900})

        def answer(route, payload, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

        def stub(route):
            nonlocal preview_fail
            request = route.request
            url = urlsplit(request.url)
            path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
            if path == base + "/context" and request.method == "GET":
                if network_down:
                    return route.abort("internetdisconnected")
                if preview_fail:
                    preview_fail = False
                    return answer(route, {"detail": "temporary read failure"}, 503)
                payload = {**current, "role": "reviewer" if "role=reviewer" in url.query else "worker"}
                if "staff_id=one" in url.query:
                    payload["role_hint"] = "worker"
                return answer(route, payload)
            if path == base + "/context-history" and request.method == "GET":
                return answer(route, {"task_id": "task", "entries": [entry]})
            if path == base + "/context-history/session-one" and request.method == "GET":
                return answer(route, {**entry, "packet": historical})
            body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
            handled = board.answer(request.method, path, url.query, body) or team.answer(request.method, path, url.query, body)
            if handled is not None:
                status, payload = handled
                return answer(route, payload, status)
            if path == "/api/projects":
                return answer(route, [project])
            if path == "/api/sessions":
                return answer(route, {"sessions": [], "projects": []})
            if path == "/api/settings":
                return answer(route, {"presets": {}, "model": {}})
            if fulfil_shared(route):
                return None
            unhandled.record(path)
            answer(route, [])

        page.route("**/api/**", stub)
        page.goto(f"{BASE}/project/p1/board?token=t&lang={language}")
        page.locator(".pcard, .ph-row", has_text="Prepare catalog").first.click()
        sheet = page.locator(".sheet.pboard-sheet")
        control = sheet.get_by_role("button", name="Task context" if language == "en" else "Контекст задачи")
        control.focus()
        page.keyboard.press("Enter")
        expect(sheet).to_contain_text("Current context is unavailable" if language == "en" else "Текущий контекст недоступен")
        sheet.get_by_role("button", name="Try again" if language == "en" else "Ещё раз").first.click()
        expect(sheet).to_contain_text("Fresh catalog")
        sheet.get_by_label("Worker context" if language == "en" else "Контекст исполнителя").select_option("one")
        sheet.get_by_label("Role" if language == "en" else "Роль").select_option("reviewer")
        expect(sheet).to_contain_text("Fresh catalog")
        sheet.get_by_text("Packets supplied to workers" if language == "en" else "Пакеты, выданные исполнителям", exact=True).click()
        sheet.get_by_label("Worker launch" if language == "en" else "Запуск исполнителя").select_option("session-one")
        expect(sheet).to_contain_text("Original catalog")
        expect(sheet).to_contain_text("no longer matches current sources" if language == "en" else "больше не соответствует текущим источникам")
        expect(sheet).to_contain_text("Fresh catalog")
        network_down = True
        page.context.set_offline(True)
        sheet.get_by_role("button", name="Refresh context" if language == "en" else "Обновить контекст").click()
        expect(sheet).to_contain_text("Packet freshness cannot be checked offline" if language == "en" else "Без связи нельзя проверить актуальность пакета")
        page.context.set_offline(False)
        network_down = False
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        assert unhandled.report() == 0
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (320, 390, 1440):
            check(lang, viewport)
            print(f"task context {lang} {viewport}: PASS")
