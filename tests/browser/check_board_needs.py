"""The board of every project: "Needs you" and the grouping, in both languages, wide and on a phone.

The board drew every open card the same way, so a card waiting on the operator's answer, approval or
review was found only by opening cards one by one. Checked here: the "Needs you" filter keeps exactly
the cards the host marks as waiting, each saying why; the wide board groups by project as well as by
status, and the choice survives a reload; a phone's list offers the same filter as a chip.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app  # noqa: E402
from check_board_card import PID, project, serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"needs": "Needs you", "project": "By project", "status": "By status", "answer": "Your answer", "review": "Your review", "none": "Agents, no project"},
    "ru": {"needs": "Нужны вы", "project": "По проектам", "status": "По статусу", "answer": "Ждёт ответа", "review": "Ждёт проверки", "none": "Агенты без проекта"},
}


def board() -> BoardStub:
    tasks = [
        BoardStub.task("t-ask", "Pick the discount code", status="doing", project_id=PID),
        BoardStub.task("t-check", "Delivery limits plan", status="review", project_id=PID),
        BoardStub.task("t-quiet", "Bake the test batch", status="todo", project_id=PID),
        BoardStub.task("t-own", "Water the plants", status="todo", origin_session_id=None),
    ]
    needs = [{"id": "a1", "short_id": "a1", "kind": "question", "task_id": "t-ask", "text": "SPRING10 before or after delivery?", "created_at": "2026-09-24T09:40:00Z"}]
    return BoardStub(project(), staff=[], tasks=tasks, needs_you=needs)


def check(page: Page, lang: str, phone: bool, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    serve(page, board(), unhandled)
    page.goto(f"{BASE}/board?token=t&lang={lang}")
    cards = page.locator(".ph-task" if phone else ".task")
    expect(cards).to_have_count(4)
    if phone:
        chip = page.locator(".ph-chip", has_text=words["needs"])
        expect(chip).to_contain_text("2")
    else:
        chip = page.locator('button[data-filter="needs"]')
        expect(chip).to_contain_text("2")
    chip.click()
    expect(cards).to_have_count(2)
    expect(cards.filter(has_text="Pick the discount code")).to_contain_text(words["answer"])
    expect(cards.filter(has_text="Delivery limits plan")).to_contain_text(words["review"])
    chip.click()
    expect(cards).to_have_count(4)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{lang}: the page scrolls sideways by {overflow}px"
    if phone:
        # A phone groups by project first, as it always has, with the tasks of no project last.
        groups = page.locator(".ph-gboard-group")
        expect(groups).to_have_count(2)
        expect(groups.last).to_contain_text(words["none"])
        return
    page.locator('button[data-group="project"]').click()
    columns = page.locator('[data-grouping="project"] .kanban-col')
    expect(columns).to_have_count(2)
    expect(columns.first).to_contain_text("Bakery")
    expect(columns.first.locator(".task")).to_have_count(3)
    expect(columns.last).to_contain_text(words["none"])
    page.reload()
    expect(page.locator('[data-grouping="project"] .kanban-col')).to_have_count(2)
    page.locator('button[data-group="status"]').click()
    expect(page.locator('[data-grouping="project"]')).to_have_count(0)


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone, viewport in ((False, {"width": 1440, "height": 900}), (True, {"width": 390, "height": 844})):
                context = browser.new_context(viewport=viewport)
                check(context.new_page(), lang, phone, unhandled)
                context.close()
        browser.close()
    print("check_board_needs: ok")
    return unhandled.report()


if __name__ == "__main__":
    sys.exit(run())
