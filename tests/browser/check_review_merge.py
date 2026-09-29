"""Reviewing and merging a staff branch, in project focus mode on a desktop and on a phone, in both languages.

What is checked is what the operator relies on: a task in review with a staff branch opens to its review —
the branch and where it merges, "+5 −1 · 2 files", the commits, the files and what the staff member
checked; the Diff opens the change file by file; Merge merges and the task is done; when the folder is
not ready, Merge is disabled and says why; Send back takes a note and returns the task to its member.
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
        "merge": "Merge", "diff": "Diff", "reject": "Send back", "send": "Send back with the note", "stat": "2 files · 2 commits", "merged": "is merged",
        "rejected": "Sent back", "dirty": "Merge waits: the folder has uncommitted changes", "conflict": "conflict", "checked": "tests pass",
    },
    "ru": {
        "merge": "Слить", "diff": "Изменения", "reject": "Вернуть", "send": "Вернуть с замечанием", "stat": "2 файла · 2 коммита", "merged": "слита",
        "rejected": "Возвращено", "dirty": "Слить пока нельзя: в папке есть незакоммиченные изменения", "conflict": "конфликт", "checked": "tests pass",
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


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    # The board as a tab of the panel beside the orchestrator's chat: focus mode keeps the panel in the address.
    page.goto(f"{BASE}/project/{PID}?token=t&lang={lang}&panel=board")
    expect(page.locator(".chat.in-project.orchestrator")).to_be_visible()
    expect(page.locator(".panel .panel-tab[data-tab='board']")).to_have_attribute("aria-selected", "true")
    card = page.locator(".panel .pboard.embedded .pcard", has_text=endpoint(focus)["title"])
    expect(card.get_by_role("button", name=words["merge"])).to_be_visible()
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
    expect(panel.locator(".review-receipts li.ok")).to_contain_text(words["checked"])
    # On a branch task the plain Accept is not offered beside Merge: the two would be the same button.
    expect(sheet.locator(".pboard-moves").get_by_role("button", name="Accept")).to_have_count(0)

    # The diff, file by file.
    panel.get_by_role("button", name=words["diff"]).click()
    diff = page.locator(".sheet.review-diff")
    expect(diff.locator(".diff-file")).to_have_count(2)
    expect(diff.locator(".diff-line.diff-add").first).to_contain_text("if order.paid")
    page.keyboard.press("Escape")
    expect(diff).to_have_count(0)

    # Send back with a note.
    panel.get_by_role("button", name=words["reject"], exact=True).click()
    send = panel.get_by_role("button", name=words["send"])
    expect(send).to_be_disabled()
    panel.locator("textarea").fill("Log unpaid orders as well")
    send.click()
    expect(page.locator(".toast")).to_contain_text(words["rejected"])
    assert focus.board.rejected == [("t-endpoint", "Log unpaid orders as well")], focus.board.rejected
    fits(page, f"{lang} desktop review")


def merging(page: Page, lang: str, *, phone: bool) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    task = endpoint(focus)
    # First the folder is dirty: Merge is disabled and says why.
    focus.board.reviews["t-endpoint"] = BoardStub.review(task, blockers=[{"code": "dirty", "text": "the folder has uncommitted changes; commit or stash them first"}])
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}&task=t-endpoint")
    sheet = page.locator(".sheet.pboard-sheet")
    panel = sheet.locator(".review-panel")
    expect(panel).to_be_visible()
    merge = panel.locator(".review-actions").get_by_role("button", name=words["merge"])
    expect(merge).to_be_disabled()
    expect(panel.locator(".review-why")).to_contain_text(words["dirty"])
    if phone:
        box = merge.bounding_box()
        assert box is not None and box["height"] >= 28, box
        fits(page, f"{lang} phone review")

    # The operator commits in the folder; the review is read again and Merge goes through.
    del focus.board.reviews["t-endpoint"]
    page.reload()
    merge = page.locator(".sheet.pboard-sheet .review-panel .review-actions").get_by_role("button", name=words["merge"])
    expect(merge).to_be_enabled()
    merge.click()
    expect(page.locator(".toast")).to_contain_text(words["merged"])
    assert focus.board.merged == ["t-endpoint"], focus.board.merged
    expect(page.locator(".sheet.pboard-sheet")).to_have_count(0)
    assert task["status"] == "done" and task["merge_state"] == "merged"
    fits(page, f"{lang} {'phone' if phone else 'desktop'} merged")


def conflicting(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    task = endpoint(focus)
    task["merge_state"] = "conflict"
    focus.board.reviews["t-endpoint"] = BoardStub.review(task, conflicts=["api/notify.py"], blockers=[{"code": "conflicts", "text": "the merge would conflict in api/notify.py"}])
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    card = page.locator(".pcard", has_text=task["title"])
    expect(card.locator(".chip")).to_contain_text(words["conflict"])
    card.locator(".pcard-title").click()
    panel = page.locator(".sheet.pboard-sheet .review-panel")
    # Said once, under the Merge it holds back, in the red of a conflict.
    expect(panel.locator(".review-why.bad")).to_contain_text("api/notify.py")
    expect(panel.locator(".review-blockers")).to_have_count(0)
    expect(panel.locator(".review-actions").get_by_role("button", name=words["merge"])).to_be_disabled()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            desktop(context.new_page(), lang)
            merging(context.new_page(), lang, phone=False)
            conflicting(context.new_page(), lang)
            context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            merging(context.new_page(), lang, phone=True)
            context.close()
        browser.close()
    print("review and merge: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
