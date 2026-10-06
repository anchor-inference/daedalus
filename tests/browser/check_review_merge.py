"""Reviewing and merging a staff branch, in project focus mode on a desktop and on a phone, in both languages.

What is checked is what the operator relies on: a branch review opens its changes and recorded
checks; a merge requires the exact result, verdict and current branch, and queues only once. A dirty
or conflicting folder blocks it; a result may be returned with a note. A task without a branch has
no merge action. A reworked task's diff can show only what the rework changed.
Nothing scrolls sideways at 390 px, and the buttons are big enough to tap.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, FocusStub, expect_app  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

WORDS = {
    "en": {
        "merge": "Merge reviewed branch", "diff": "Diff", "reject": "Return with a note", "send": "Send back", "stat": "2 files · 2 commits", "merged": "Merge requested; waiting for the result",
        "rejected": "Result returned for changes", "dirty": "The reviewed branch must be merged first.", "conflict": "conflict", "checked": "tests pass", "open": "Open review", "ci": "CI passed for branch head",
    },
    "ru": {
        "merge": "Влить проверенную ветку", "diff": "Изменения", "reject": "Вернуть с замечанием", "send": "Вернуть", "stat": "2 файла · 2 коммита", "merged": "Слияние запрошено; ждём итога",
        "rejected": "Результат возвращён на доработку", "dirty": "Сначала нужно влить проверенную ветку.", "conflict": "конфликт", "checked": "tests pass", "open": "Открыть проверку", "ci": "CI пройден для ветки head",
    },
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, focus: FocusStub) -> None:
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = focus.answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return installation(route)

    page.route("**/api/**", handle)


def endpoint(focus: FocusStub) -> dict:
    return next(t for t in focus.board.tasks if t["id"] == "t-endpoint")


def reviewed_result(focus: FocusStub) -> dict:
    """A completed worker receipt bound to the branch and an accepted verification verdict."""
    task = endpoint(focus)
    task.update(acceptance_state="accepted", contract_revision=1)
    receipt = {"result_id": "res-endpoint", "task_id": task["id"], "attempt_id": "attempt-endpoint",
               "contract_revision": 1, "outcome": "complete", "origin_kind": "worker", "author": "Max",
               "original_preview": "Endpoint implemented and tested", "original_digest": "a" * 64,
               "artifacts": [{"id": "artifact-endpoint", "artifact_kind": "file", "artifact_key": "api/notify.py", "digest": "b" * 64}],
               "checks": ["tests pass"], "limitations": [], "verification": "verified", "verdict_id": "verdict-endpoint",
               "verdict_accepted": True, "verdict_head": "head", "verdict_base": "base",
               "current_result_id": "res-endpoint", "acceptance_state": "accepted", "accepted": False,
               "created_at": "2026-09-24T09:26:00Z"}
    focus.board.result_rows[task["id"]] = [receipt]
    return receipt


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    reviewed_result(focus)
    serve(page, focus)
    # The board as a tab of the panel beside the orchestrator's chat: focus mode keeps the panel in the address.
    page.goto(f"{BASE}/project/{PID}?token=t&lang={lang}&panel=board")
    expect(page.locator(".chat.in-project.orchestrator")).to_be_visible()
    expect(page.locator(".panel .panel-tab[data-tab='board']")).to_have_attribute("aria-selected", "true")
    card = page.locator(".panel .pboard.embedded .pcard", has_text=endpoint(focus)["title"])
    expect(card).to_contain_text(words["open"])
    card.locator(".pcard-title").click()

    # The review sits in the task's sheet: branch, numbers, commits, files, receipts.
    sheet = page.locator(".sheet.pboard-sheet")
    panel = sheet.locator(".review-panel")
    expect(panel).to_be_visible()
    expect(panel.locator(".review-head")).to_contain_text("agent/max/endpoint")
    expect(panel.locator(".review-head")).to_contain_text("main")
    expect(panel.locator(".review-stat")).to_contain_text("+5")
    expect(panel.locator(".review-stat")).to_contain_text("−1")
    expect(panel.locator(".review-stat")).to_contain_text(words["stat"])
    expect(panel.locator(".review-commits li")).to_have_count(2)
    expect(panel.locator(".review-files li")).to_have_count(2)
    expect(panel.locator(".review-receipts").first.locator("li.ok")).to_contain_text(words["checked"])
    expect(panel).to_contain_text(words["ci"])
    expect(panel.locator(".review-ci-setup")).to_have_count(0)
    evidence = sheet.locator(".result-flow")
    expect(evidence).to_contain_text("Endpoint implemented and tested")
    expect(evidence.get_by_role("button", name=words["merge"])).to_be_enabled()
    # On a branch task the plain Accept is not offered beside Merge: the two would be the same button.
    expect(sheet.locator(".pboard-moves").get_by_role("button", name="Accept")).to_have_count(0)

    # The diff, file by file.
    panel.get_by_role("button", name=words["diff"]).click()
    diff = page.locator(".sheet.review-diff")
    expect(diff.locator(".diff-file")).to_have_count(2)
    expect(diff.locator(".diff-line.diff-add").first).to_contain_text("if order.paid")
    # A first result has nothing earlier to compare with, so the diff offers no second view.
    expect(diff.locator(".review-diff-scope")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(diff).to_have_count(0)

    # Send back with a note.
    evidence.get_by_role("button", name=words["reject"], exact=True).click()
    send = evidence.get_by_role("button", name=words["send"], exact=True)
    expect(send).to_be_disabled()
    evidence.locator(".result-return textarea").fill("Log unpaid orders as well")
    send.click()
    expect(page.locator(".toast")).to_contain_text(words["rejected"])
    assert focus.board.rejected == [("t-endpoint", "Log unpaid orders as well")], focus.board.rejected
    fits(page, f"{lang} desktop review")


def merging(page: Page, lang: str, *, phone: bool) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    reviewed_result(focus)
    task = endpoint(focus)
    # First the folder is dirty: Merge is disabled and says why.
    focus.board.reviews["t-endpoint"] = BoardStub.review(task, blockers=[{"code": "dirty", "text": "the folder has uncommitted changes; commit or stash them first"}])
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-endpoint")
    sheet = page.locator(".sheet.pboard-sheet")
    panel = sheet.locator(".review-panel")
    expect(panel).to_be_visible()
    merge = sheet.locator(".result-flow").get_by_role("button", name=words["merge"])
    expect(merge).to_be_disabled()
    expect(sheet.locator(".result-action .result-warning").first).to_contain_text(words["dirty"])
    if phone:
        box = merge.bounding_box()
        assert box is not None and box["height"] >= 28, box
        fits(page, f"{lang} phone review")

    # The operator commits in the folder; the review is read again and Merge goes through.
    del focus.board.reviews["t-endpoint"]
    page.reload()
    merge = page.locator(".sheet.pboard-sheet .result-flow").get_by_role("button", name=words["merge"])
    expect(merge).to_be_enabled()
    merge.click()
    expect(page.locator(".toast")).to_contain_text(words["merged"])
    assert len(focus.board.merge_requests) == 1, focus.board.merge_requests
    assert focus.board.merge_requests[0][1]["verdict_id"] == "verdict-endpoint"
    page.reload()
    expect(page.locator(".sheet.pboard-sheet .result-flow").get_by_role("button", name=words["merge"])).to_be_disabled()
    assert len(focus.board.merge_requests) == 1
    focus.board.merge_receipts["t-endpoint"]["state"] = "merged"
    task.update(status="done", merge_state="merged", acceptance_state="operator_approved")
    focus.board.result_rows["t-endpoint"][0].update(accepted=True, acceptance_state="operator_approved")
    page.reload()
    expect(page.locator(".sheet.pboard-sheet .result-flow").get_by_role("button", name=words["merge"])).to_have_count(0)
    assert len(focus.board.merge_requests) == 1
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-hero")
    expect(page.locator(".sheet.pboard-sheet .result-flow")).to_be_visible()
    expect(page.locator(".sheet.pboard-sheet .result-flow").get_by_role("button", name=words["merge"])).to_have_count(0)
    refused = page.evaluate("async () => (await fetch('/api/board/t-hero/results/res-hero/merge', {method: 'POST'})).status")
    assert refused == 409 and len(focus.board.merge_requests) == 1, (refused, focus.board.merge_requests)
    fits(page, f"{lang} {'phone' if phone else 'desktop'} merged")


def reworked(page: Page, lang: str, *, phone: bool) -> None:
    """A returned and redone task: the diff can show only what the rework changed."""
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    reviewed_result(focus)
    task = endpoint(focus)
    review = BoardStub.review(task)
    review["previous_result"] = {"id": "res-first", "head_sha": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b", "at": "2026-09-24T09:10:00Z"}
    focus.board.reviews[task["id"]] = review
    focus.board.since_previous[task["id"]] = {
        "task_id": task["id"], "previous_result": review["previous_result"],
        "since": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b", "until": "4f2a9c1d0b7e6a5f4c3b2a1d0e9f8a7b6c5d4e3f", "commits": 1,
        "files": [{"path": "api/notify.py", "added": 1, "removed": 0}], "added": 1, "removed": 0, "patch_complete": True,
        "patch": "diff --git a/api/notify.py b/api/notify.py\n--- a/api/notify.py\n+++ b/api/notify.py\n@@ -2,3 +2,4 @@ def notify(order):\n     if order.paid:\n         send(order)\n+    log(order)\n     return True\n",
    }
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-endpoint")
    panel = page.locator(".sheet.pboard-sheet .review-panel")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name=words["diff"]).click()
    diff = page.locator(".sheet.review-diff")
    scope = diff.locator(".review-diff-scope button")
    expect(scope).to_have_count(2)
    expect(scope.nth(0)).to_have_attribute("aria-pressed", "true")
    expect(diff.locator(".diff-file")).to_have_count(2)
    # The rework's own change is fetched only when asked for.
    assert focus.board.since_requests == [], focus.board.since_requests
    scope.nth(1).click()
    expect(scope.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(diff.locator(".diff-file")).to_have_count(1)
    expect(diff.locator(".review-since-range")).to_contain_text("1a2b3c4")
    expect(diff.locator(".review-since-range")).to_contain_text("4f2a9c1")
    expect(diff.locator(".diff-line.diff-add").first).to_contain_text("log(order)")
    assert focus.board.since_requests == ["t-endpoint"], focus.board.since_requests
    if phone:
        box = scope.nth(1).bounding_box()
        assert box is not None and box["height"] >= 28, box
    fits(page, f"{lang} {'phone' if phone else 'desktop'} rework diff")
    scope.nth(0).click()
    expect(diff.locator(".diff-file")).to_have_count(2)


def conflicting(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    reviewed_result(focus)
    task = endpoint(focus)
    task["merge_state"] = "conflict"
    focus.board.reviews["t-endpoint"] = BoardStub.review(task, conflicts=["api/notify.py"], blockers=[{"code": "conflicts", "text": "the merge would conflict in api/notify.py"}])
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    card = page.locator(".pcard", has_text=task["title"])
    expect(card.locator(".chip.bad")).to_contain_text(words["conflict"])
    card.locator(".pcard-title").click()
    panel = page.locator(".sheet.pboard-sheet .review-panel")
    # Said once, under the Merge it holds back, in the red of a conflict.
    expect(panel.locator(".review-why.bad")).to_contain_text("api/notify.py")
    expect(panel.locator(".review-blockers")).to_have_count(0)
    expect(page.locator(".sheet.pboard-sheet .result-flow").get_by_role("button", name=words["merge"])).to_be_disabled()


def configure_ci(page: Page, lang: str) -> None:
    focus = FocusStub.bakery(lang)
    task = endpoint(focus)
    review = BoardStub.review(task, blockers=[])
    review["ci_status"] = "not_required"
    review["ci_checks"] = []
    focus.board.reviews[task["id"]] = review
    focus.board.ci_requirement_conflicts = 1
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    page.locator(".pcard", has_text=task["title"]).locator(".pcard-title").click()
    panel = page.locator(".sheet.pboard-sheet .review-panel")
    # A folder without CI says nothing about CI; naming checks stays one fold away.
    expect(panel).not_to_contain_text("CI has not passed" if lang == "en" else "CI не пройден")
    setup = panel.locator(".review-ci-setup")
    expect(setup.locator(".review-ci-fields")).not_to_be_visible()
    setup.locator("summary").click()
    expect(setup).to_contain_text("GitHub")
    save = setup.locator("button.btn")
    expect(save).to_be_disabled()
    setup.locator("input").fill("7")
    setup.locator("textarea").fill("unit\nbuild")
    expect(save).to_be_enabled()
    save.click()
    changed = "The task changed before saving" if lang == "en" else "Задача изменилась до сохранения"
    expect(setup.get_by_role("alert")).to_contain_text(changed)
    assert len(focus.board.ci_requirement_requests) == 1
    focus.board.ci_requirement_failures = 1
    save.click()
    assert len(focus.board.ci_requirement_requests) == 2
    unknown = "Couldn't confirm the checks" if lang == "en" else "Не удалось подтвердить сохранение проверок"
    expect(setup.get_by_role("alert")).to_contain_text(unknown)
    save.click()
    assert len(focus.board.ci_requirement_requests) == 3
    first, second, retry = focus.board.ci_requirement_requests
    assert first["client_operation_id"] != second["client_operation_id"]
    assert retry == second
    assert second["expected_entity_revision"] == task["entity_revision"] - 1
    assert second["provider"] == "github" and second["repository_id"] == "7"
    assert second["check_names"] == ["unit", "build"]
    saved = "Required checks saved; the result went back to the queue" if lang == "en" else "Проверки сохранены; результат возвращён в очередь"
    expect(page.locator(".toast")).to_contain_text(saved)
    # The receipt id is bookkeeping for the server; the operator's toast says what happened, not its id.
    expect(page.locator(".toast")).not_to_contain_text("ci-receipt")
    expect(page.locator(".sheet.pboard-sheet .review-panel")).to_have_count(0)
    fits(page, f"{lang} CI requirements")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            desktop(context.new_page(), lang)
            merging(context.new_page(), lang, phone=False)
            conflicting(context.new_page(), lang)
            reworked(context.new_page(), lang, phone=False)
            configure_ci(context.new_page(), lang)
            context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            merging(context.new_page(), lang, phone=True)
            reworked(context.new_page(), lang, phone=True)
            context.close()
        browser.close()
    print("review and merge: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
