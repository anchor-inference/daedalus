"""A phone and desktop show provenance and retry the exact approved continuation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = {"id": "p1", "name": "Bakery", "entity_revision": 1, "folders": folders("/home/operator/work/bakery"),
           "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": False, "default_env": "container"}, "system": "", "sessions": []}


def scenario(page: Page, language: str, unhandled: Unhandled) -> None:
    source = BoardStub.assignee("source", "Source", harness="daedalus")
    alternate = BoardStub.assignee("alternate", "Alternate", harness="claude", permission_mode="default")
    task = BoardStub.task("task", "Prepare catalog", status="blocked", project_id="p1", assignee=source,
                          current_attempt_id="attempt-1", source_released=True, source_harness="daedalus",
                          source_result={"id": "result-1", "outcome": "partial", "original_digest": "b" * 64},
                          brief={"objective": "Catalog", "deliverable": "Report", "boundaries": "One folder", "done_when": "Evidence"})
    board = BoardStub(PROJECT, staff=[source, alternate], tasks=[task])
    state: dict[str, object] = {"lost": True, "commands": []}

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method == "POST" and request.post_data else None
        if path == "/api/board/task/continue-elsewhere" and request.method == "POST":
            assert isinstance(body, dict) and set(body) == {"source_attempt_id", "target_staff_id", "preview_digest",
                                                       "expected_entity_revision", "client_operation_id"}
            assert body["source_attempt_id"] == "attempt-1" and body["target_staff_id"] == "alternate"
            assert body["expected_entity_revision"] == 1 and body["preview_digest"] == "a" * 64
            state["commands"].append(body)
            if state["lost"]:
                state["lost"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, {"task_id": "task", "effect_id": "effect-1", "handoff_id": "handoff-1",
                                  "receipt_id": "receipt-1", "state": "queued", "entity_revision": 2})
        if path == "/api/control/effects/effect-1" and request.method == "GET":
            return answer(route, {"state": "pending", "wait_reason": "capacity"})
        if path == "/api/projects" and request.method == "GET":
            return answer(route, [PROJECT])
        if path == "/api/projects/p1/staff" and request.method == "GET":
            return answer(route, [source, alternate])
        if path == "/api/projects/p1/wakeups" and request.method == "GET":
            return answer(route, [])
        if path == "/api/projects/p1/watches" and request.method == "GET":
            return answer(route, {"watches": [], "max": 20, "min_cooldown_minutes": 1, "providers": [],
                                  "collection_revision": 1, "project_entity_revision": 1})
        if path == "/api/sessions" and request.method == "GET":
            return answer(route, {"sessions": [], "projects": []})
        if path == "/api/settings" and request.method == "GET":
            return answer(route, {"presets": {}, "model": {}})
        known = board.answer(request.method, path, url.query, body)
        if known is not None:
            return answer(route, known[1], known[0])
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/project/p1/board?task=task&token=t&lang={language}")
    title = "Continue with another runtime" if language == "en" else "Продолжить в другом рантайме"
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(title)).first
    section.locator("summary").click()
    section.get_by_label("New worker" if language == "en" else "Новый исполнитель").select_option("alternate")
    expect(section).to_contain_text("provider session" if language == "en" else "сессия провайдера")
    action = "Approve continuation" if language == "en" else "Подтвердить продолжение"
    section.get_by_role("button", name=action).click()
    dialog = page.locator(".dialog[role='alertdialog']")
    expect(dialog).to_contain_text("Alternate")
    dialog.get_by_role("button", name=action).click()
    expect(section.get_by_role("status")).to_contain_text("uncertain" if language == "en" else "не подтверждён")
    page.reload()
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(title)).first
    section.locator("summary").click()
    section.get_by_role("status").get_by_role("button").click()
    expect(page.get_by_text("Continuation queued" if language == "en" else "Продолжение поставлено в очередь")).to_be_visible()
    assert len(state["commands"]) == 2 and state["commands"][0] == state["commands"][1]


def run() -> int:
    unhandled = Unhandled()
    failures = []
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile in ((320, 560, True), (390, 844, True), (1440, 900, False)):
                context = browser.new_context(viewport={"width": width, "height": height},
                                              is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(6000)
                try:
                    scenario(page, language, unhandled)
                    print(f"ok {language} {width}")
                except Exception as exc:  # noqa: BLE001 — retain every viewport result
                    failures.append(f"{language} {width}: {exc}")
                    print(f"FAILED {language} {width}: {exc}")
                context.close()
        browser.close()
    return 1 if failures else unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
