"""A phone and desktop keep one bounded pair intent through a lost launch response."""

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
WORDS = {
    "en": {"title": "Compare two approaches", "prepare": "Prepare a comparison", "start": "Start comparison", "retry": "Retry", "queued": "Comparison queued", "unknown": "Observed cost is unknown", "capacity": "Project capacity: 1 of 10 simultaneous workers.", "raise": "Set capacity to 2"},
    "ru": {"title": "Сравнить два подхода", "prepare": "Подготовить сравнение", "start": "Начать сравнение", "retry": "Ещё раз", "queued": "Сравнение поставлено в очередь", "unknown": "Фактические затраты одного из вариантов неизвестны", "capacity": "Одновременно в проекте: 1 из 10 исполнителей.", "raise": "Установить лимит 2"},
}


def run() -> int:
    unhandled = Unhandled()
    failures = []
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile in ((320, 560, True), (390, 844, True), (1440, 900, False)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
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


def scenario(page: Page, language: str, unhandled: Unhandled) -> None:
    words = WORDS[language]
    staff = [{"id": "one", "name": "Ira", "color": "orange", "harness": "daedalus", "isolation": "worktree"},
             {"id": "two", "name": "Max", "color": "blue", "harness": "daedalus", "isolation": "worktree"}]
    task = BoardStub.task("task", "Prepare catalog", project_id="p1", brief={"objective": "Catalog", "deliverable": "Report", "boundaries": "One folder", "done_when": "Evidence"})
    board = BoardStub(PROJECT, staff=staff, tasks=[task])
    state: dict[str, object] = {"lost": True, "commands": [], "group": None, "stops": [], "capacity": 1,
                                "capacity_changes": [], "capacity_lost": True, "orchestrator": True}
    base = "/api/board/task"

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def group() -> dict:
        return {"group_id": "group-1", "task_id": "task", "contract_revision": 1, "state": "planned",
            "budget_cap_microusd": 2_000_000, "reserved_microusd": 2_000_000, "selected_result_id": None,
            "created_at": "2026-10-03T10:00:00Z", "alternatives": [], "blockers": ["alternatives_missing", "cost_unknown:attempt-1", "exit_unknown:attempt-1"],
            "slots": [{"slot_id": "slot-1", "slot": 1, "staff_id": "one", "attempt_id": "attempt-1", "funding_state": "reserved", "launch_started_at": None, "launch_effect_id": "effect-1", "launch_state": "pending"},
                      {"slot_id": "slot-2", "slot": 2, "staff_id": "two", "attempt_id": "attempt-2", "funding_state": "reserved", "launch_started_at": None, "launch_effect_id": "effect-2", "launch_state": "pending"}]}

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PATCH") and request.post_data else None
        if path == base + "/contract" and request.method == "GET":
            return answer(route, {"task_id": "task", "contract_revision": 1, "entity_revision": task["entity_revision"],
                                  "folder_id": PROJECT["folders"][0]["id"], "checklist": []})
        if path == base + "/comparisons" and request.method == "GET":
            return answer(route, {"task_id": "task", "groups": [state["group"]] if state["group"] else [], "next_before": None})
        if path == base + "/comparisons/group-1" and request.method == "GET":
            return answer(route, state["group"])
        if path == base + "/comparisons/group-1/slots/1/stop" and request.method == "POST":
            assert isinstance(body, dict) and set(body) == {"client_operation_id", "expected_entity_revision", "reason"}
            assert body["expected_entity_revision"] == 2 and body["client_operation_id"]
            state["stops"].append(body)
            state["group"]["slots"][0]["launch_state"] = "cancelled"
            return answer(route, {"group_id": "group-1", "slot": 1, "state": "cancelled_pending", "launch_effect_id": "effect-1", "receipt_id": "stop-receipt", "entity_revision": 3})
        if path == base + "/comparisons" and request.method == "POST":
            assert isinstance(body, dict)
            assert set(body) == {"client_operation_id", "expected_entity_revision", "contract_revision", "budget_cap_usd", "alternatives"}
            assert body["expected_entity_revision"] == 1 and body["contract_revision"] == 1
            assert body["budget_cap_usd"] == "2" and body["alternatives"] == [
                {"staff_id": "one", "allowance_usd": "1"}, {"staff_id": "two", "allowance_usd": "1"}]
            state["commands"].append(body)
            if state["group"] is None:
                state["group"] = group()
                task["entity_revision"] = 2
                board.tasks[0]["entity_revision"] = 2
            if state["lost"]:
                state["lost"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, {"group_id": "group-1", "receipt_id": "pair-receipt", "state": "queued",
                                  "task_id": "task", "entity_revision": 2})
        if path == "/api/projects" and request.method == "GET":
            return answer(route, [PROJECT])
        if path == "/api/projects/p1/staff" and request.method == "GET":
            return answer(route, {"project": {"id": "p1", "concurrency": state["capacity"], "concurrency_cap": 10,
                                               "orchestrator": state["orchestrator"]}, "staff": staff})
        if path == "/api/projects/p1/orchestrator" and request.method == "PATCH":
            assert body == {"concurrency": 2, "concurrency_cap": 10}, body
            state["capacity_changes"].append(body)
            state["capacity"] = 2
            if state["capacity_lost"]:
                state["capacity_lost"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, {"project_id": "p1", "concurrency": 2, "concurrency_cap": 10})
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
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["title"])).first
    assert page.get_by_role("button", name=words["raise"]).count() == 0, "capacity control escaped collapsed comparison"
    section.locator("summary").first.click()
    assert section.get_by_role("button", name=words["raise"]).count() == 0, "capacity control escaped collapsed preparation"
    section.get_by_text(words["prepare"]).click()
    expect(section.get_by_text(words["capacity"], exact=False)).to_be_visible()
    expect(section.get_by_role("button", name=words["start"])).to_be_disabled()
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, overflow
    section.get_by_role("button", name=words["raise"]).click()
    capacity_dialog = page.locator(".dialog[role='alertdialog']")
    expect(capacity_dialog).to_contain_text("Already queued work may start" if language == "en" else "Уже ожидающая работа может начаться")
    capacity_dialog.get_by_role("button", name="Cancel" if language == "en" else "Отмена").click()
    assert not state["capacity_changes"] and not state["commands"], "cancelling capacity change must do nothing"
    section.get_by_role("button", name=words["raise"]).click()
    page.locator(".dialog[role='alertdialog']").get_by_role("button", name=words["raise"]).click()
    expect(section.get_by_text("Project capacity: 2" if language == "en" else "Одновременно в проекте: 2", exact=False)).to_be_visible()
    expect(section.get_by_text("Capacity change is unconfirmed" if language == "en" else "Изменение лимита не подтверждено", exact=False)).to_have_count(0)
    assert state["capacity_changes"] == [{"concurrency": 2, "concurrency_cap": 10}]
    assert not state["commands"], "saving capacity must not launch a comparison"
    selects = section.locator("select.field")
    selects.nth(0).select_option("one")
    selects.nth(1).select_option("two")
    section.get_by_label("First allowance (USD)" if language == "en" else "Лимит первого (USD)").fill("1")
    section.get_by_label("Second allowance (USD)" if language == "en" else "Лимит второго (USD)").fill("1")
    section.get_by_label("Total ceiling (USD)" if language == "en" else "Общий потолок (USD)").fill("2")
    section.get_by_role("button", name=words["start"]).click()
    dialog = page.locator(".dialog[role='alertdialog']")
    expect(dialog).to_contain_text("Ira")
    expect(dialog).to_contain_text("Max")
    dialog.get_by_role("button", name=words["start"]).click()
    expect(section.get_by_text("The launch outcome is unknown" if language == "en" else "Исход запуска неизвестен", exact=False)).to_be_visible()
    page.reload()
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["title"])).first
    section.locator("summary").first.click()
    pending_status = section.get_by_role("status").filter(has_text="The launch outcome is unknown" if language == "en" else "Исход запуска неизвестен")
    pending_status.get_by_role("button").first.click()
    expect(page.get_by_text(words["queued"], exact=False)).to_be_visible()
    assert len(state["commands"]) == 2 and state["commands"][0] == state["commands"][1]
    expect(section.get_by_text("Both alternatives have not been admitted" if language == "en" else "Ещё не приняты оба варианта", exact=False)).to_be_visible()
    assert section.get_by_role("button", name="Choose" if language == "en" else "Выбрать").count() == 0
    first = section.locator("details.result-details", has=page.get_by_text("Ira"))
    first.locator("summary").first.click()
    first.get_by_role("button", name="Stop this worker" if language == "en" else "Остановить этого исполнителя").click()
    page.locator(".dialog[role='alertdialog']").get_by_role("button", name="Stop this worker" if language == "en" else "Остановить этого исполнителя").click()
    assert len(state["stops"]) == 1 and state["group"]["slots"][1]["launch_state"] == "pending"
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, overflow
    state["capacity"] = 1
    state["orchestrator"] = False
    page.reload()
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["title"])).first
    section.locator("summary").first.click()
    section.get_by_text(words["prepare"]).click()
    expect(section.get_by_text(words["capacity"], exact=False)).to_be_visible()
    assert section.get_by_role("button", name=words["raise"]).count() == 0, "disabled orchestrator cannot accept a PATCH"
    expect(section.get_by_role("button", name="Open project setup" if language == "en" else "Открыть настройку проекта")).to_be_visible()
    expect(section.get_by_role("button", name=words["start"])).to_be_disabled()


if __name__ == "__main__":
    raise SystemExit(run())
