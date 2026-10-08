"""An operator's branchless result survives a lost response and reaches exact review/reopen."""

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


def reveal(page, locator):  # type: ignore[no-untyped-def]
    """On a phone the task is a page of tabs (Task, Result, Diff) with the others mounted and hidden;
    the control the step needs is on one of them. On a desktop the sheet shows it at once."""
    tabs = page.locator(".ph-taskpage-tabs [role='radio']")
    for i in range(tabs.count() + 1):
        try:
            expect(locator.first).to_be_visible(timeout=2500)
            return locator
        except AssertionError:
            if i < tabs.count():
                tabs.nth(i).click()
    return locator


def scenario(language: str, width: int, file_bound: bool) -> None:
    project = {"id": "p1", "name": "Bakery", "entity_revision": 1, "folders": folders("/home/operator/work/bakery"),
               "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": False}, "system": "", "sessions": []}
    task = BoardStub.task("task", "Update catalog", project_id="p1", status="todo",
                          brief={"objective": "Update catalog", "deliverable": "New catalog", "boundaries": "One file", "done_when": "Prices checked"})
    board = BoardStub(project, tasks=[task])
    task = board.tasks[0]
    team = TeamStub(project)
    unhandled = Unhandled()
    base = "/api/board/task"
    result = None
    evidence = []
    verdict = None
    registered = False
    lost_reply = True
    lost_reopen_reply = True
    reopen_command = None
    reopen_calls = []
    calls = []
    attachments = [{"manifest_id": "manifest-one" if registered else None, "file_id": "file-one", "label": "prices.csv",
                    "digest": "b" * 64, "size_bytes": 20}] if file_bound else []
    checks = [{"id": "C1", "text": "Prices checked"}]
    requirements = [{"id": "R1", "text": "Attach the current prices", "file_id": "file-one"}] if file_bound else []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 560 if width == 320 else 900})

        def answer(route, payload, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

        def current_result():
            return {"result_id": "result-one", "current_result_id": "result-one", "task_id": "task", "contract_revision": 1,
                "origin_kind": "operator_manual", "outcome": "complete", "original_preview": "Updated all catalog prices",
                "original_digest": "a" * 64, "original_size_bytes": 26,
                "artifacts": [{"id": "manifest-one", "file_id": "file-one", "artifact_kind": "other", "artifact_key": "prices.csv",
                               "artifact_revision": 1, "digest": "b" * 64, "size_bytes": 20}] if file_bound else [],
                "checks": [], "limitations": [], "verification": "verified" if verdict else "unverified",
                "verdict_id": "verdict-one" if verdict else None, "verdict_accepted": bool(verdict), "verdict_head": None,
                "verdict_base": None, "self_review_waiver_required": False, "acceptance_state": task["acceptance_state"],
                "accepted": task["acceptance_state"] == "operator_approved", "created_at": "2026-10-03T10:00:00Z"}

        def stub(route):
            nonlocal result, evidence, verdict, registered, lost_reply, lost_reopen_reply, reopen_command, attachments
            request = route.request
            url = urlsplit(request.url)
            path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
            method = request.method
            body = request.post_data_json if method in ("POST", "PUT", "PATCH") and request.post_data else None
            if path == base + "/manual-review" and method == "GET":
                return answer(route, {"eligible": task["status"] in ("todo", "blocked", "review"), "blockers": [],
                                      "entity_revision": task["entity_revision"], "contract_revision": 1, "attached_artifacts": attachments})
            if path == base + "/contract" and method == "GET":
                return answer(route, {"task_id": "task", "contract_revision": 1, "entity_revision": task["entity_revision"],
                                      "checklist": checks, "requirements": requirements})
            if path == base + "/results" and method == "GET":
                return answer(route, [current_result()] if result else [])
            if path == base + "/artifacts" and method == "POST":
                assert file_bound and body["file_id"] == "file-one" and body["digest"] == "b" * 64
                assert body["artifact_kind"] == "other" and body["expected_entity_revision"] == task["entity_revision"]
                registered = True
                attachments = [{**attachments[0], "manifest_id": "manifest-one"}]
                task["entity_revision"] += 1
                return answer(route, {"manifest_id": "manifest-one", "receipt_id": "artifact-receipt", "entity_revision": task["entity_revision"]})
            if path == base + "/results" and method == "POST":
                calls.append(("result", body))
                assert body["outcome"] == "complete" and body["contract_revision"] == 1
                assert body["manifest_ids"] == (["manifest-one"] if file_bound else [])
                if result is None:
                    assert body["expected_entity_revision"] == task["entity_revision"]
                    result = body
                    task["status"] = "review"
                    task["acceptance_state"] = "handed_in"
                    task["entity_revision"] += 1
                else:
                    assert body == result
                if lost_reply:
                    lost_reply = False
                    return answer(route, {"detail": "unconfirmed response"}, 503)
                return answer(route, {"result_id": "result-one", "status": "review", "receipt_id": "result-receipt", "entity_revision": task["entity_revision"]})
            if path == base + "/results/result-one/evidence" and method == "GET":
                return answer(route, evidence)
            if path == base + "/results/result-one/comments" and method == "GET":
                return answer(route, [])
            if path == base + "/results/result-one/attest" and method == "POST":
                assert body["criterion_id"] == "C1" and body["expected_entity_revision"] == task["entity_revision"]
                evidence.append({"evidence_id": "evidence-c1", "criterion_id": "C1", "observation": "operator attestation: " + body["observation"],
                                 "verification": "operator_attested", "manifest_digest_before": None, "manifest_digest_after": None})
                task["entity_revision"] += 1
                return answer(route, {"evidence_id": "evidence-c1", "verification": "operator_attested", "receipt_id": "attest-receipt", "entity_revision": task["entity_revision"]})
            if path == base + "/results/result-one/evidence" and method == "POST":
                assert file_bound and body["criterion_id"] == "R1" and body["manifest_id"] == "manifest-one"
                assert body["expected_entity_revision"] == task["entity_revision"]
                evidence.append({"evidence_id": "evidence-r1", "criterion_id": "R1", "observation": body["observation"],
                                 "verification": "verified", "manifest_digest_before": "b" * 64, "manifest_digest_after": "b" * 64})
                task["entity_revision"] += 1
                return answer(route, {"evidence_id": "evidence-r1", "verification": "verified", "receipt_id": "file-receipt", "entity_revision": task["entity_revision"]})
            if path == base + "/results/result-one/verdicts" and method == "POST":
                assert set(body["evidence_ids"]) == ({"evidence-c1", "evidence-r1"} if file_bound else {"evidence-c1"})
                assert body["expected_entity_revision"] == task["entity_revision"] and body["accepted"] is True
                verdict = body
                task["acceptance_state"] = "accepted"
                task["entity_revision"] += 1
                return answer(route, {"verdict_id": "verdict-one", "accepted": True, "receipt_id": "verdict-receipt", "entity_revision": task["entity_revision"]})
            if path == base + "/results/result-one/accept" and method == "POST":
                assert body["verdict_id"] == "verdict-one" and body["expected_entity_revision"] == task["entity_revision"]
                task["status"] = "done"
                task["acceptance_state"] = "operator_approved"
                task["entity_revision"] += 1
                return answer(route, {"accepted": True, "receipt_id": "accept-receipt", "entity_revision": task["entity_revision"]})
            if path == base + "/results/result-one/reopen" and method == "POST":
                assert body["verdict_id"] == "verdict-one" and body["contract_revision"] == 1
                reopen_calls.append(body)
                if reopen_command is None:
                    assert body["reason"] and body["expected_entity_revision"] == task["entity_revision"]
                    reopen_command = body
                    task["status"] = "todo"
                    task["acceptance_state"] = "returned"
                    task["entity_revision"] += 1
                else:
                    assert body == reopen_command
                if lost_reopen_reply:
                    lost_reopen_reply = False
                    return answer(route, {"detail": "unconfirmed reopen response"}, 503)
                return answer(route, {"reopen_id": "reopen-one", "status": "todo", "receipt_id": "reopen-receipt", "entity_revision": task["entity_revision"]})
            handled = board.answer(method, path, url.query, body) or team.answer(method, path, url.query, body)
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
        # A desktop's card, or the row of a phone's list.
        page.locator(".pcard, .ph-row", has_text="Update catalog").first.click()
        sheet = page.locator(".sheet.pboard-sheet")
        reveal(page, sheet.get_by_role("button", name="Submit my result" if language == "en" else "Сдать мой результат")).click()
        if file_bound:
            sheet.get_by_role("button", name="Use attached prices.csv" if language == "en" else "Использовать прикреплённый prices.csv").click()
            expect(sheet.get_by_label("prices.csv")).to_be_visible()
            sheet.get_by_label("prices.csv").check()
        report = sheet.get_by_label("What was completed?" if language == "en" else "Что выполнено?")
        report.fill("Updated all catalog prices")
        page.reload()
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        reveal(page, sheet.get_by_role("button", name="Submit my result" if language == "en" else "Сдать мой результат")).click()
        expect(sheet.get_by_label("What was completed?" if language == "en" else "Что выполнено?")).to_have_value("Updated all catalog prices")
        sheet.get_by_role("button", name="Submit for review" if language == "en" else "Сдать на проверку").click()
        expect(sheet).to_contain_text("Couldn't confirm" if language == "en" else "Не удалось подтвердить")
        page.reload()
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        reveal(page, sheet.get_by_role("button", name="Submit my result" if language == "en" else "Сдать мой результат")).click()
        sheet.get_by_role("button", name="Try again" if language == "en" else "Ещё раз").last.click()
        expect(reveal(page, sheet.locator(".result-flow"))).to_be_visible()
        assert calls[0] == calls[1] and len(calls) == 2
        flow = sheet.locator(".result-flow")
        flow.get_by_text("Review my result" if language == "en" else "Проверить мой результат", exact=True).click()
        flow.get_by_label("What did you observe?" if language == "en" else "Что вы наблюдали?").fill("I checked the current prices")
        flow.get_by_role("button", name="Record my observation" if language == "en" else "Записать моё наблюдение").click()
        if file_bound:
            flow.get_by_label("Acceptance check" if language == "en" else "Критерий принятия").select_option("R1")
            flow.get_by_label("What did you observe?" if language == "en" else "Что вы наблюдали?").fill("Opened prices.csv")
            flow.get_by_role("button", name="Record file observation" if language == "en" else "Записать проверку файла").click()
        flow.get_by_label("Review conclusion" if language == "en" else "Вывод проверки").fill("All criteria observed")
        flow.get_by_role("button", name="Approve reviewed result" if language == "en" else "Одобрить проверенный результат").click()
        # The decision is the review page's footer on a phone, the result's own actions on a desktop.
        decide = page.locator(".ph-taskpage .ph-decide") if width < 1024 else flow
        expect(decide.get_by_role("button", name="Accept this result" if language == "en" else "Принять этот результат")).to_be_enabled()
        decide.get_by_role("button", name="Accept this result" if language == "en" else "Принять этот результат").click()
        expect(reveal(page, flow.get_by_text("Reopen accepted work" if language == "en" else "Вернуть принятую работу", exact=True))).to_be_visible()
        flow.get_by_text("Reopen accepted work" if language == "en" else "Вернуть принятую работу", exact=True).click()
        flow.get_by_label("Why is more work needed?" if language == "en" else "Почему нужна доработка?").fill("One price needs correction")
        flow.get_by_role("button", name="Reopen for work" if language == "en" else "Вернуть на доработку").click()
        expect(sheet.get_by_role("alert")).to_contain_text("Unconfirmed reopen response")
        page.reload()
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        reveal(page, sheet.get_by_role("button", name="Try again" if language == "en" else "Повторить")).click()
        expect(sheet).to_contain_text("Returned for another round" if language == "en" else "Возвращено на доработку")
        assert len(reopen_calls) == 2 and reopen_calls[0] == reopen_calls[1]
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        assert unhandled.report() == 0
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for viewport in (320, 390, 1440):
            for bound in (False, True):
                scenario(lang, viewport, bound)
                print(f"manual result {lang} {viewport} file={bound}: PASS")
