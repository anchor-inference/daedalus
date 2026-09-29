"""The main orchestrator in the app, at 1440 px and on a 390 px phone, in both languages.

What is checked is what the operator relies on. On a desktop its chat is orchestration mode's home,
reached by the rail's Orchestration item; in that mode its entry comes first in the column, with a
pill that counts the questions waiting. On a phone the Orchestration tab opens the list, Main its
first row, and the chat is a detail of it with a back and no tab bar. The questions the projects put
to the operator wait in the panel's Questions tab, grouped by project, and the chat's flow carries
one line for them after its latest turn — there is no strip over the chat, and no card per question.
Sending from the tab posts exactly the answers to the main chat's batch route, after which the cards
leave; one that lost to an answer given elsewhere says so. The work under way is a card
per dispatch with its state and the project's last word, and it can be cancelled; a finished
dispatch is no card, only the line of the reports that closed it. A project being set up offers
"Finish setup". Settings → Models has its own model choice with the mid-tier preset preselected.
Nothing scrolls sideways.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, MainStub, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"title": "Main orchestrator", "pill": "3 questions", "pill1": "1 question", "line": "3 questions waiting", "newproject": "New project", "conflict": "Already answered elsewhere",
           "stalled": "stalled", "done": "done", "finish": "Finish setup", "confirm": "The main orchestrator asks", "placeholder": "Say which project and what to do…",
           "middle": "mid-tier", "cancel": "Cancel", "going": "under way"},
    "ru": {"title": "Главный оркестратор", "pill": "3 вопроса", "pill1": "1 вопрос", "line": "3 вопроса ждут ответа", "newproject": "Новый проект", "conflict": "Уже ответили в другом месте",
           "stalled": "застряло", "done": "готово", "finish": "Завершить настройку", "confirm": "Спрашивает главный оркестратор", "placeholder": "Какой проект и что сделать…",
           "middle": "средняя", "cancel": "Отменить", "going": "в работе"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, main: MainStub) -> None:
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = main.answer(request.method, path, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if path == "/api/settings" and request.method == "GET":
            presets = SETTINGS["presets"]  # type: ignore[index]
            middle = list(presets)[len(presets) // 2] if presets else ""
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({**SETTINGS, "dispatcher": {"preset": "", "middle": middle, "stalled_minutes": 30}}))
        return installation(route)

    page.route("**/api/**", handle)


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    main = MainStub()
    serve(page, main)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")

    # Agents mode has no entry of its own for the main chat: the rail's Orchestration item leads there.
    expect(page.locator(".sidebar .main-entry")).to_have_count(0)
    page.locator(".rail [data-rail='orchestration']").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator(".chat .chat-title")).to_have_text("Main")
    assert ("/api/main", {}) in main.posts, "the first visit opens the main chat's session"
    entry = page.locator("nav.orch-sidebar .orch-main .main-entry")
    expect(entry).to_be_visible()
    expect(entry.locator(".main-entry-name")).to_have_text(words["title"])
    expect(entry.locator(".main-pill")).to_have_text(words["pill"])
    expect(entry).to_have_class(re.compile(r"\bcurrent\b"))

    # The dispatches are in the conversation's flow, after its latest turn; nothing stands over the
    # chat, and no question is a card in it. The one line for what waits stays away while the
    # Questions tab is open beside the chat, where its "Open" would open what is already shown; the
    # phone below, with the panel shut, sees it after the flow.
    board = page.locator(".chat .timeline > .main-flow")
    expect(board).to_be_visible()
    expect(page.locator(".chat .timeline > .questions-line")).to_have_count(0)
    expect(page.locator(".main-board, .main-answered, .main-closed")).to_have_count(0)
    expect(page.locator(".timeline .q-card, .timeline .ask-card")).to_have_count(0)
    # The panel leads with every project's questions, grouped by project, oldest first.
    tab = page.locator(".panel .panel-tab[data-tab='questions']")
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(tab.locator(".count")).to_have_text("3")
    groups = page.locator(".panel .questions-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".questions-group-head")).to_contain_text("Bakery")
    expect(groups.nth(0).locator(".q-card")).to_have_count(2)
    expect(groups.nth(1).locator(".questions-group-head")).to_contain_text(words["newproject"])
    confirm = page.locator('.panel .q-card[data-ask="q3nw00"]')
    expect(confirm).to_have_class(re.compile(r"\bhost\b"))
    expect(confirm.locator(".q-meta")).to_contain_text(words["confirm"])
    expect(confirm.locator(".q-flag.host")).to_be_visible()
    expect(confirm.locator(".q-field")).to_have_count(0)
    # A request answered earlier is nowhere: not a card, not a line.
    expect(page.locator('[data-ask="q4ol00"]')).to_have_count(0)

    # The dispatches under way (the stalled setup, then the menu); the finished one is no card, only
    # the line of the reports that closed it.
    cards = board.locator(".main-dispatches .dispatch-card")
    expect(cards).to_have_count(2)
    expect(board.locator('.dispatch-card[data-dispatch="dg4h1x"]')).to_have_count(0)
    expect(cards.nth(0).locator(".dispatch-state")).to_have_text(words["stalled"])
    expect(cards.nth(1).locator(".dispatch-last")).to_contain_text("Recipes collected")
    expect(board.locator(".main-setup")).to_contain_text("Garden")

    # The reports it was woken with are a card too.
    expect(page.locator(".event-card")).to_contain_text("Garden closed dispatch dg4h1x")
    expect(page.locator(".composer textarea, .composer [contenteditable]").first).to_have_attribute("placeholder", words["placeholder"])
    fits(page, f"{lang} main chat")

    # Answer one with an option and one in words, and send them together to the main chat's route: the
    # first leaves, the second lost to the phone and says so, the answer given elsewhere standing.
    page.locator('.panel .q-card[data-ask="q1db00"] .q-chip', has_text="Postgres").click()
    late = page.locator('.panel .q-card[data-ask="q2lt00"]')
    late.locator(".q-field").fill("Only in summer")
    page.locator(".panel .questions-send-btn").click()
    expect(page.locator('.panel [data-ask="q1db00"]')).to_have_count(0, timeout=10000)
    assert ("/api/asks/answer", {"items": [{"ask_id": "ask-db", "selected": ["Postgres"]}, {"ask_id": "ask-late", "text": "Only in summer"}]}) in main.posts, main.posts
    expect(late.locator(".q-fate")).to_contain_text(words["conflict"])
    expect(entry.locator(".main-pill")).to_have_text(words["pill1"])
    late.locator(".q-clear").click()
    expect(page.locator('[data-ask="q2lt00"]')).to_have_count(0)

    # Finish the setup by hand, and cancel a dispatch after confirming.
    board.locator(".main-setup button").click()
    expect(board.locator(".main-setup")).to_have_count(0)
    assert ("/api/projects/p-garden/setup/finish", {}) in main.posts
    board.locator('.dispatch-card[data-dispatch="d7k2m9"] .dispatch-actions button').click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    page.wait_for_timeout(300)
    if not any(p[0] == "/api/dispatches/d7k2m9/cancel" for p in main.posts):
        # The danger button is the first when the safe one takes focus last.
        page.locator(".sheet-backdrop.confirm .dialog button", has_text=words["cancel"]).click()
    page.wait_for_timeout(300)
    assert any(p[0] == "/api/dispatches/d7k2m9/cancel" for p in main.posts), main.posts

    # Settings → Models: the main orchestrator's own choice, preselected with the mid-tier preset.
    page.goto(f"{BASE}/settings/models?token=t&lang={lang}")
    card = page.locator('[data-card="main-orchestrator"]')
    expect(card).to_be_visible()
    selected = card.locator("select option:checked")
    expect(selected).to_contain_text(words["middle"])


def phone(page: Page, lang: str) -> None:
    main = MainStub()
    serve(page, main)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    expect(page.locator(".start-list .main-entry")).to_have_count(0)
    fits(page, f"{lang} phone start")
    # The tab opens orchestration's list, not the main chat: Main is its first row, with the pill.
    page.locator(".tabbar a[data-screen='orchestration']").click()
    page.wait_for_url("**/app/orchestration/projects**")
    assert ("/api/main", {}) not in main.posts, "the list opened the main chat's session"
    first = page.locator(".orch-list > :first-child .main-entry")
    expect(first).to_be_visible()
    expect(first.locator(".main-pill")).to_have_text(WORDS[lang]["pill"])
    # The chat is a detail of the list: a back of its own, no tab bar, the line in its flow and the
    # questions a sheet away, behind the header's button.
    first.click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    line = page.locator(".timeline > .questions-line")
    expect(line).to_contain_text(WORDS[lang]["line"])
    assert line.evaluate("el => el === el.parentElement.lastElementChild && el.previousElementSibling.classList.contains('main-flow')"), "the line is not after the flow"
    expect(page.locator("nav.tabbar")).to_have_count(0)
    expect(page.locator(".main-board")).to_have_count(0)
    fits(page, f"{lang} phone main chat")
    page.locator(".chat-head .questions-headbtn").tap()
    expect(page.locator(".panel-sheet .questions-group")).to_have_count(2)
    for chip in page.locator(".panel-sheet .q-chip").all()[:2]:
        box = chip.bounding_box()
        assert box and box["height"] >= 39.5, f"{lang} phone: a chip is {box and box['height']}px tall"
    fits(page, f"{lang} phone questions")
    page.locator(".panel-sheet .sheet-backdrop, .panel-sheet").first.press("Escape")
    expect(page.locator(".panel-sheet")).to_have_count(0)
    page.locator(".chat-head > button.iconbtn").first.click()
    page.wait_for_url("**/app/orchestration/projects**")
    expect(page.locator(".orch-list .main-entry")).to_be_visible()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            desktop(page, lang)
            page.context.close()
            small = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True).new_page()
            phone(small, lang)
            small.context.close()
            print(f"main orchestrator {lang}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
