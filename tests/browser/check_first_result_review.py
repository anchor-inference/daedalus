"""A selected worker result opens its original report and review path on both screen sizes."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app
from check_project_board import PID, project, serve
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def scenario(language: str, width: int) -> None:
    task = BoardStub.task("t-hero", "Prepare the menu", status="review", project_id=PID,
                          acceptance_state="accepted", checklist=[{"text": "Menu prices match"}])
    stub = BoardStub(project(), tasks=[task])
    result = {"result_id": "res-hero", "task_id": "t-hero", "attempt_id": "attempt-hero",
              "contract_revision": 1, "outcome": "complete", "origin_kind": "worker", "author": "Menu worker",
              "original_preview": "Prepared the menu", "original_digest": "a" * 64,
              "artifacts": [{"id": "artifact-hero", "artifact_kind": "file", "artifact_key": "menu.txt",
                             "artifact_revision": 1, "digest": "b" * 64, "size_bytes": 42}],
              "checks": [], "limitations": [], "verification": "unverified", "verdict_id": None,
              "verdict_accepted": None, "verdict_head": None, "verdict_base": None,
              "current_result_id": "res-hero", "acceptance_state": "accepted", "accepted": False,
              "created_at": "2026-10-03T12:00:00Z"}
    stub.result_rows[task["id"]] = [result]
    evidence: list[dict] = []
    originals = []
    verdicts = []
    accepts = []
    original_unavailable = False
    unhandled = Unhandled()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": width, "height": 640 if width == 320 else 844 if width == 390 else 900})
        serve(page, stub, unhandled)

        def review_api(route):
            nonlocal original_unavailable
            request = route.request
            path = urlsplit(request.url).path
            body = request.post_data_json if request.post_data else None
            value = None
            if path == "/api/board/t-hero/results/res-hero/original":
                originals.append(path)
                if original_unavailable:
                    original_unavailable = False
                    return route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "original temporarily unavailable"}))
                value = {"original_text": "Prepared the menu\nPrices copied from the approved list."}
            elif path == "/api/board/t-hero/results/res-hero/evidence":
                if request.method == "POST":
                    assert body["criterion_id"] == "C1" and body["manifest_id"] == "artifact-hero"
                    evidence.append({"evidence_id": "evidence-1", "criterion_id": "C1",
                                     "observation": body["observation"], "verification": "verified",
                                     "manifest_digest_before": "b" * 64, "manifest_digest_after": "b" * 64,
                                     "observed_at": "2026-10-03T12:01:00Z"})
                    value = evidence[-1]
                else:
                    value = evidence
            elif path == "/api/board/t-hero/results/res-hero/verdicts" and request.method == "POST":
                assert body["evidence_ids"] == ["evidence-1"] and body["accepted"] is True
                verdicts.append(body)
                result.update(verification="verified", verdict_id="verdict-1", verdict_accepted=True)
                value = {"verdict_id": "verdict-1"}
            elif path == "/api/board/t-hero/results/res-hero/accept" and request.method == "POST":
                assert body["verdict_id"] == "verdict-1"
                accepts.append(body)
                task.update(status="done", acceptance_state="operator_approved")
                result.update(accepted=True, acceptance_state="operator_approved")
                value = {"receipt_id": "accept-1"}
            if value is None:
                return route.fallback()
            route.fulfill(status=200, content_type="application/json", body=json.dumps(value))

        page.route("**/api/**", review_api)
        page.goto(f"{BASE}/project/{PID}/board?task=t-hero&token=t&lang={language}")
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        action = sheet.get_by_role("button", name="Review report and evidence" if language == "en" else "Проверить отчёт и доказательства")
        expect(action).to_have_attribute("aria-expanded", "false")
        expect(sheet.locator(".result-original")).to_have_count(0)
        action.click()
        expect(action).to_have_attribute("aria-expanded", "true")
        expect(sheet.locator(".result-original")).to_contain_text("Prices copied from the approved list")
        expect(sheet.get_by_role("button", name="Approve reviewed result" if language == "en" else "Одобрить проверенный результат")).to_be_disabled()
        sheet.locator(".result-details", has_text="Evidence and original report" if language == "en" else "Доказательства и исходный отчёт").locator("summary").first.click()
        expect(action).to_have_attribute("aria-expanded", "false")
        action.click()
        expect(action).to_have_attribute("aria-expanded", "true")
        expect(sheet.locator(".result-original")).to_contain_text("Prices copied from the approved list")
        original_unavailable = True
        page.reload()
        action = sheet.get_by_role("button", name="Review report and evidence" if language == "en" else "Проверить отчёт и доказательства")
        expect(action).to_have_attribute("aria-expanded", "false")
        action.click()
        expect(sheet.get_by_text("original temporarily unavailable")).to_be_visible()
        expect(sheet.locator(".result-original")).to_have_count(0)
        sheet.get_by_role("textbox", name="What did you observe?" if language == "en" else "Что вы наблюдали?").fill("Compared every listed price")
        sheet.get_by_role("button", name="Record observation" if language == "en" else "Записать наблюдение").click()
        expect(sheet).to_contain_text("Compared every listed price")
        sheet.get_by_role("textbox", name="Review conclusion" if language == "en" else "Вывод проверки").fill("All prices match")
        approve = sheet.get_by_role("button", name="Approve reviewed result" if language == "en" else "Одобрить проверенный результат")
        page.wait_for_timeout(300)
        expect(approve).to_be_disabled()
        sheet.get_by_role("button", name="Show original report" if language == "en" else "Показать исходный отчёт").click()
        expect(sheet.locator(".result-original")).to_contain_text("Prices copied from the approved list")
        expect(approve).to_be_enabled()
        approve.click()
        accept = sheet.get_by_role("button", name="Accept this result" if language == "en" else "Принять этот результат")
        expect(accept).to_be_enabled()
        accept.click()
        expect(page.locator(".toast")).to_contain_text("Result accepted" if language == "en" else "Результат принят")
        assert len(originals) >= 2 and len(evidence) == len(verdicts) == len(accepts) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
    assert unhandled.report() == 0


if __name__ == "__main__":
    expect_app(BASE)
    for lang in ("en", "ru"):
        for width in (320, 390, 1440):
            scenario(lang, width)
            print(f"first result review {lang} {width}: PASS")
