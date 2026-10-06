"""A phone and desktop review one exact contender and retry a lost selection receipt."""

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
    "en": {"pair": "Compare two approaches", "original": "Show original report", "review": "Review checks and evidence",
           "observe": "Record observation", "verdict": "Approve reviewed result", "choose": "Choose this result",
           "observation": "What did you observe?", "reason": "Review conclusion",
           "unknown": "Couldn't confirm your choice was saved"},
    "ru": {"pair": "Сравнить два подхода", "original": "Показать исходный отчёт", "review": "Проверка критериев и доказательств",
           "observe": "Записать наблюдение", "verdict": "Одобрить проверенный результат", "choose": "Выбрать этот результат",
           "observation": "Что вы наблюдали?", "reason": "Вывод проверки",
           "unknown": "Не удалось подтвердить, что выбор сохранён"},
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
                page.set_default_timeout(8000)
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
    task["folder_id"] = PROJECT["folders"][0]["id"]
    board = BoardStub(PROJECT, staff=staff, tasks=[task])
    state: dict[str, object] = {"evidence": [], "verdict": False, "choice": None, "lost": True, "requests": [], "over": False}
    base = "/api/board/task"

    def bump() -> None:
        task["entity_revision"] += 1
        board.tasks[0]["entity_revision"] = task["entity_revision"]

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def group() -> dict:
        chosen = state["choice"] is not None
        return {"group_id": "group-1", "task_id": "task", "contract_revision": 1, "state": "chosen" if chosen else "ready",
            "budget_cap_microusd": 2_000_000, "reserved_microusd": 2_000_000, "selected_result_id": "result-1" if chosen else None,
            "created_at": "2026-10-03T10:00:00Z", "blockers": [],
            "alternatives": [{"slot": 1, "attempt_id": "attempt-1", "state": "completed", "reserved_microusd": 1_000_000,
                              "observed_cost_microusd": 2_000_000 if state["over"] else 400_000, "physical_exit_verified": True,
                              "results": [{"result_id": "result-1", "outcome": "complete", "original_digest": "a" * 64, "checks": []}]},
                             {"slot": 2, "attempt_id": "attempt-2", "state": "failed", "reserved_microusd": 1_000_000,
                              "observed_cost_microusd": 200_000, "physical_exit_verified": True,
                              "results": [{"result_id": "result-2", "outcome": "failed", "original_digest": "b" * 64, "checks": []}]}],
            "slots": [{"slot_id": "slot-1", "slot": 1, "staff_id": "one", "attempt_id": "attempt-1", "funding_state": "observed", "launch_started_at": "2026-10-03T10:00:00Z", "launch_effect_id": "effect-1", "launch_state": "completed"},
                      {"slot_id": "slot-2", "slot": 2, "staff_id": "two", "attempt_id": "attempt-2", "funding_state": "observed", "launch_started_at": "2026-10-03T10:00:00Z", "launch_effect_id": "effect-2", "launch_state": "completed"}]}

    def result(result_id: str) -> dict:
        first = result_id == "result-1"
        return {"result_id": result_id, "task_id": "task", "attempt_id": "attempt-1" if first else "attempt-2",
                "contract_revision": 1, "outcome": "complete" if first else "failed", "original_preview": "Short report",
                "original_digest": "a" * 64, "original_size_bytes": 15, "artifacts": [{"id": "manifest-1", "artifact_kind": "file", "artifact_key": "report.txt", "artifact_revision": 1, "digest": "c" * 64, "size_bytes": 15}] if first else [],
                "checks": [], "limitations": [], "verification": "verified" if state["verdict"] and first else "unverified",
                "verdict_id": "verdict-1" if state["verdict"] and first else None,
                "verdict_accepted": bool(state["verdict"] and first), "acceptance_state": "handed_in", "accepted": False,
                "created_at": "2026-10-03T10:00:00Z"}

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method == "POST" and request.post_data else None
        if path == base + "/contract" and request.method == "GET":
            return answer(route, {"task_id": "task", "contract_revision": 1, "entity_revision": task["entity_revision"],
                                  "folder_id": task["folder_id"], "checklist": [{"id": "C1", "text": "Check report"}]})
        if path == base + "/comparisons" and request.method == "GET":
            return answer(route, {"task_id": "task", "groups": [group()], "next_before": None})
        if path == base + "/comparisons/group-1" and request.method == "GET":
            return answer(route, group())
        if path.endswith("/slots/1/review") and request.method == "GET":
            return answer(route, {"group_id": "group-1", "slot": 1, "attempt_id": "attempt-1", "result_id": "result-1",
                                  "verdict_id": "verdict-1" if state["verdict"] else None, "head_sha": "a" * 40,
                                  "base_sha": "b" * 40, "verification": "verified" if state["verdict"] else "unverified",
                                  "verdict_accepted": bool(state["verdict"]), "source_current": not bool(state["choice"]),
                                  "can_choose": bool(state["verdict"] and not state["choice"]),
                                  "blockers": [] if state["verdict"] else [{"code": "verdict_missing", "text": "review required"}],
                                  "self_review_waiver_required": False, "artifacts": [{"manifest_id": "manifest-1", "artifact_key": "report.txt", "file_id": "file-1", "digest": "c" * 64}],
                                  "physical_exit_verified": True, "observed_cost_microusd": 2_000_000 if state["over"] else 400_000,
                                  "patch": "+report", "patch_complete": True})
        if path.endswith("/slots/2/review") and request.method == "GET":
            return answer(route, {"detail": "failed contender has no current branch"}, 409)
        if path == base + "/results" and request.method == "GET":
            return answer(route, [result("result-1"), result("result-2")])
        if path == base + "/results/result-1/original" and request.method == "GET":
            return answer(route, {"original_text": "Complete original report"})
        if path == base + "/results/result-1/evidence" and request.method == "GET":
            return answer(route, state["evidence"])
        if path == base + "/results/result-2/evidence" and request.method == "GET":
            return answer(route, [])
        if path in (base + "/results/result-1/comments", base + "/results/result-2/comments") and request.method == "GET":
            return answer(route, [])
        if path == base + "/results/result-1/evidence" and request.method == "POST":
            assert isinstance(body, dict) and body["criterion_id"] == "C1" and body["manifest_id"] == "manifest-1"
            assert body["client_operation_id"] and body["expected_entity_revision"] == task["entity_revision"]
            state["evidence"] = [{"evidence_id": "evidence-1", "criterion_id": "C1", "observation": body["observation"],
                                  "verification": "verified", "manifest_digest_before": "c" * 64, "manifest_digest_after": "c" * 64,
                                  "observed_at": "2026-10-03T10:00:00Z"}]
            bump()
            return answer(route, {"evidence_id": "evidence-1", "receipt_id": "receipt-evidence", "entity_revision": task["entity_revision"]})
        if path.endswith("/slots/1/verdicts") and request.method == "POST":
            assert isinstance(body, dict) and set(body) == {"client_operation_id", "expected_entity_revision", "result_id", "verification", "accepted", "evidence_ids", "reason"}
            assert body["result_id"] == "result-1" and body["evidence_ids"] == ["evidence-1"]
            assert body["expected_entity_revision"] == task["entity_revision"] and "head" not in body and "base" not in body
            state["verdict"] = True
            bump()
            return answer(route, {"verdict_id": "verdict-1", "receipt_id": "receipt-verdict", "entity_revision": task["entity_revision"]})
        if path == base + "/comparisons/group-1/choose" and request.method == "POST":
            assert isinstance(body, dict) and set(body) == {"client_operation_id", "expected_entity_revision", "result_id", "verdict_id"}
            assert body["result_id"] == "result-1" and body["verdict_id"] == "verdict-1"
            state["requests"].append(body)
            state["choice"] = {"selection_receipt_id": "selection-1", "selected_result_id": "result-1", "receipt_id": "receipt-choice"}
            if state["lost"]:
                state["lost"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, state["choice"])
        if path == "/api/projects" and request.method == "GET":
            return answer(route, [PROJECT])
        if path == "/api/projects/p1/staff" and request.method == "GET":
            return answer(route, staff)
        if path == "/api/projects/p1/wakeups" and request.method == "GET":
            return answer(route, [])
        if path == "/api/projects/p1/watches" and request.method == "GET":
            return answer(route, {"watches": [], "max": 20, "min_cooldown_minutes": 1, "providers": [],
                                  "collection_revision": 1, "project_entity_revision": 1})
        known = board.answer(request.method, path, url.query, body)
        if known is not None:
            return answer(route, known[1], known[0])
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/project/p1/board?task=task&token=t&lang={language}")
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["pair"])).first
    section.locator("summary").first.click()
    first = section.locator("details.result-details", has=page.get_by_text("Ira"))
    first.locator("summary").first.click()
    first.get_by_role("button", name=words["original"]).click()
    expect(first.get_by_text("Complete original report")).to_be_visible()
    first.get_by_text(words["review"]).click()
    first.get_by_label(words["observation"]).fill("Read the attached report" if language == "en" else "Изучен приложенный отчёт")
    first.get_by_role("button", name=words["observe"]).click()
    first.get_by_label(words["reason"]).fill("Report satisfies the task" if language == "en" else "Отчёт соответствует задаче")
    first.get_by_role("button", name=words["verdict"]).click()
    expect(first.get_by_role("button", name=words["choose"])).to_be_enabled()
    state["over"] = True
    page.reload()
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["pair"])).first
    section.locator("summary").first.click()
    first = section.locator("details.result-details", has=page.get_by_text("Ira"))
    first.locator("summary").first.click()
    expect(first.get_by_role("button", name=words["choose"])).to_be_disabled()
    expect(section.get_by_text("Observed costs exceed" if language == "en" else "Фактические затраты превышают", exact=False)).to_be_visible()
    state["over"] = False
    page.reload()
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["pair"])).first
    section.locator("summary").first.click()
    first = section.locator("details.result-details", has=page.get_by_text("Ira"))
    first.locator("summary").first.click()
    expect(first.get_by_role("button", name=words["choose"])).to_be_enabled()
    first.get_by_role("button", name=words["choose"]).click()
    page.locator(".dialog[role='alertdialog']").get_by_role("button", name=words["choose"]).click()
    expect(first.get_by_text(words["unknown"], exact=False)).to_be_visible()
    page.reload()
    section = page.locator(".sheet.pboard-sheet details.result-details", has=page.get_by_text(words["pair"])).first
    section.locator("summary").first.click()
    first = section.locator("details.result-details", has=page.get_by_text("Ira"))
    first.locator("summary").first.click()
    first.get_by_role("status").filter(has_text=words["unknown"]).get_by_role("button").first.click()
    expect(first.get_by_role("status").filter(has_text=words["unknown"])).to_have_count(0)
    assert len(state["requests"]) == 2 and state["requests"][0] == state["requests"][1]
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, overflow


if __name__ == "__main__":
    raise SystemExit(run())
