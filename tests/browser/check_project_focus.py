"""A project's focus mode on a desktop, at 1440 and 2560 px, in both languages, and that it does not
break at 390 px.

What is checked is what the operator relies on. In orchestration mode's column an orchestrated project
is one entry that says what goes on inside and how many requests wait, and it opens focus mode; the
column then holds only the project — the way back, the orchestrator, the team with each member's state and,
for a launch that waits, why; the one-off helpers, the terminals and the project's pages. The
orchestrator's chat shows the events it was woken with as a card, each of its steps once in its turn's
worked group, and its question as a card answered right there, with one of its options or words of the operator's own. The panel
beside it has the project's tabs; beside a staff member's session it has the session's tabs too, and a
header with the member's controls and the messages sent to it. The journal pages back, and takes a
note. A project without an orchestrator offers to switch one on, and says what it costs. "All
projects" goes back to orchestration's home, and Agents mode keeps its project lens as it was. Nothing scrolls sideways.
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
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID, GARDEN = "b4k3ry20f0c5", "9a4d3e2f1c0b"

WORDS = {
    "en": {
        "all": "All projects", "orchestrator": "Orchestrator", "team": "Team", "oneoff": "One-off", "terminals": "Terminals", "board": "Board", "brief": "Brief",
        "wakeups": "Wake-ups", "journal": "Journal", "folders": "Folders", "machine": "the machine's terminal limit is reached", "placeholder": "Write to the orchestrator…",
        "lev": "Write to Lev…", "folder": "Folder added", "created": "Task created", "assigned": "Assigned", "watch": "Watch set", "asked": "Asked you",
        "questions": "Questions", "events": "3 events since 09:51", "onlyyou": "edited by you only", "byorch": "changed by the orchestrator", "older": "Older entries",
        "note": "Add a note", "enable": "Switch the orchestrator on", "on": "Switch on", "cost": "fifteen times", "pause": "Pause after the turn", "accepted": "accepted",
        "staff": "6 staff", "needs": "1 needs you", "autonomy": "autonomy: normal",
        "spent": "$2.05 today", "levspend": "$1.20 today · 412k tokens", "iraspend": "subscription · window 23 %", "totals": "Today $2.05 · 7 days $10.90 · All $33.80",
        "orchspend": "Orchestrator: $0.85 today · 96k tokens",
    },
    "ru": {
        "all": "Все проекты", "orchestrator": "Оркестратор", "team": "Команда", "oneoff": "Разовые", "terminals": "Терминалы", "board": "Доска", "brief": "Бриф",
        "wakeups": "Будильники", "journal": "Журнал", "folders": "Папки", "machine": "достигнут предел терминалов машины", "placeholder": "Напишите оркестратору…",
        "lev": "Написать сотруднику Lev…", "folder": "Папка добавлена", "created": "Задача создана", "assigned": "Назначено", "watch": "Наблюдение поставлено", "asked": "Спросил вас",
        "questions": "Вопросы", "events": "3 события с 09:51", "onlyyou": "правите только вы", "byorch": "изменено оркестратором", "older": "Более ранние записи",
        "note": "Добавить заметку", "enable": "Включить оркестратор", "on": "Включить", "cost": "в пятнадцать раз", "pause": "После хода — пауза", "accepted": "принято",
        "staff": "6 сотрудников", "needs": "1 ждёт вас", "autonomy": "самостоятельность: обычная",
        "spent": "$2.05 сегодня", "levspend": "$1.20 сегодня · токенов: 412k", "iraspend": "подписка · окно 23 %", "totals": "Сегодня $2.05 · 7 дней $10.90 · Всего $33.80",
        "orchspend": "Оркестратор: $0.85 сегодня · токенов: 96k",
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
        # Everything else — the gates, a session's own sub-routes — is the invented installation's.
        return installation(route)

    page.route("**/api/**", handle)


def desktop(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")

    # Agents mode lists nothing of the project; orchestration mode lists it as one entry.
    expect(page.locator(f".sidebar [data-project='{PID}']")).to_have_count(0)
    expect(page.locator(".sidebar .erow", has_text="Orchestrator · Bakery 2.0")).to_have_count(0)
    chip_before = page.locator(".sidebar .project-chip").inner_text()
    page.locator(".rail [data-rail='orchestration']").click()
    entry = page.locator("nav.orch-sidebar .orch-row", has_text="Bakery 2.0")
    expect(entry).to_be_visible()
    expect(entry.locator(".orch-row-meta")).to_contain_text(words["staff"])
    expect(entry.locator(".needs-badge")).to_have_text("1")
    entry.click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}")

    # The column is the project's alone.
    side = page.locator("nav.project-sidebar")
    expect(side).to_be_visible()
    expect(page.locator("nav.sidebar")).to_have_count(1)
    expect(side.locator(".focus-back")).to_have_text(words["all"])
    expect(side.locator(".focus-project-name")).to_have_text("Bakery 2.0")
    expect(side.locator(".focus-project-meta")).to_contain_text(words["autonomy"])
    expect(side.locator(".focus-row.orchestrator")).to_have_class(re.compile(r"\bcurrent\b"))
    rows = side.locator(".focus-staff:not(.compact)")
    expect(rows).to_have_count(5)
    expect(side.locator(".focus-staff.compact")).to_have_count(1)
    for label in ("team", "oneoff", "terminals"):
        expect(side.locator(".focus-sec", has_text=words[label])).to_have_count(1)
    olga = side.locator(".focus-staff", has_text="Olga")
    expect(olga.locator(".focus-staff-line")).to_contain_text(words["machine"])
    assert "20 of the machine's 20" in (olga.get_attribute("title") or ""), olga.get_attribute("title")
    expect(olga.locator(".focus-dot")).to_have_class(re.compile(r"\btone-waiting\b"))
    expect(side.locator(".focus-staff", has_text="Ira").locator(".focus-staff-line")).to_have_text(invented["task.checkout"])
    expect(side.locator(".focus-staff", has_text="Naya").locator(".focus-dot")).to_have_class(re.compile(r"\btone-waiting\b"))
    expect(side.locator(".focus-row", has_text="bash · bakery-api")).to_be_visible()
    expect(side.locator(".focus-row.ended", has_text="psql · orders")).to_contain_text("0")
    # Today's spend of the whole project under its name.
    expect(side.locator(".focus-spend")).to_have_text(words["spent"])
    for label in ("board", "brief", "wakeups", "journal", "folders"):
        expect(side.locator(".focus-row", has_text=words[label])).to_have_count(1)
    # One wake-up and one watch switched on; the watch that switched itself off is not counted.
    expect(side.locator(".focus-row", has_text=words["wakeups"]).locator(".focus-row-meta")).to_have_text("2")

    # The orchestrator's chat: the events as a card, the steps in its turns, the question as a card.
    chat = page.locator(".chat.in-project.orchestrator")
    expect(chat).to_be_visible()
    card = chat.locator(".event-card")
    expect(card).to_have_count(1)
    expect(card.locator(".event-head")).to_contain_text(words["events"])
    expect(card.locator(".event-line")).to_have_count(3)
    expect(card.locator(".event-line.ok")).to_contain_text("finished a turn")
    expect(card.locator(".event-line.warn").first).to_contain_text("needs permission")
    assert "ReadStaff" not in card.inner_text()
    # Each of its tool calls is shown once: a step in the project's words inside the turn's worked
    # group, and nowhere else. The folded line names the steps the same way, not by the tool's name.
    calls = sum(len(m["tool_calls"]) for m in focus.details["orch-bakery"]["messages"])
    heads = chat.locator(".thinking-head")
    expect(heads).to_have_count(2)
    expect(heads.first.locator(".families")).to_contain_text(words["created"])
    assert "Tasks" not in heads.first.inner_text(), heads.first.inner_text()
    expect(chat.locator(".act")).to_have_count(0)
    for head in heads.all():
        head.click()
    steps = chat.locator(".activity .act.step")
    expect(steps).to_have_count(calls)
    expect(chat.locator(".act")).to_have_count(calls)
    expect(steps.filter(has_text=words["folder"])).to_contain_text("/home/operator/work/bakery-bot")
    expect(steps.filter(has_text=words["created"])).to_have_count(2)
    expect(steps.filter(has_text=words["assigned"])).to_contain_text("Max")
    expect(steps.filter(has_text=words["watch"])).to_have_count(1)
    expect(steps.filter(has_text=words["asked"])).to_have_count(1)
    assert chat.get_by_text(words["created"], exact=True).count() == 2, "a step is drawn once, inside its group"
    expect(chat.locator(".composer textarea")).to_have_attribute("placeholder", words["placeholder"])
    # The question it asked is no card in the chat: it waits in the Questions tab, which the panel
    # opens on, and is answered there (check_questions_panel.py follows it further). The chat's line
    # for what waits stays away while that tab is open beside it: its "Open" would open what is shown.
    expect(chat.locator(".ask-card, .timeline .q-card")).to_have_count(0)
    questions_tab = page.locator(".panel .panel-tab[data-tab='questions']")
    expect(questions_tab).to_have_attribute("aria-selected", "true")
    expect(questions_tab.locator(".count")).to_have_text("1")
    expect(chat.locator(".timeline > .questions-line")).to_have_count(0)
    ask = page.locator(".panel .q-card[data-ask='q4r8tz']")
    expect(ask).to_be_visible()
    ask.locator(".q-chip", has_text=invented["ask.before"]).click()
    page.locator(".panel .questions-send-btn").click()
    expect(ask).to_have_count(0, timeout=10000)
    assert focus.batches == [(f"/api/projects/{PID}/asks/answer", [{"ask_id": "ask-spring", "selected": [invented["ask.before"]]}])], focus.batches
    expect(chat.locator(".questions-line")).to_have_count(0)
    fits(page, f"{lang} {width} orchestrator")

    # The panel beside the orchestrator: the questions first, its own Details, then the project's four tabs.
    tabs = page.locator(".panel .panel-tab")
    expect(tabs).to_have_count(6)
    expect(tabs.nth(0)).to_contain_text(words["questions"])
    expect(tabs.nth(1)).to_have_attribute("data-tab", "details")
    expect(tabs.nth(2)).to_have_text(words["board"])
    page.locator(".panel .panel-tab[data-tab='board']").click()
    expect(page.locator(".panel .pboard.embedded .pcard.need")).to_contain_text("q4r8tz")
    page.locator(".panel .panel-tab[data-tab='brief']").click()
    expect(page.locator(".panel .brief-card")).to_have_count(6)
    # The badge says who edits the section: a bare "only you" beside "Allowed without you" read as
    # contradicting the heading.
    expect(page.locator(".panel .brief-card.allowed_without_operator")).to_contain_text(words["onlyyou"])
    expect(page.locator(".panel .brief-card.notes")).to_contain_text(words["byorch"])
    assert f"/app/orchestration/project/{PID}?" in page.url and "panel=brief" in page.url, page.url
    page.locator(".panel .panel-tab[data-tab='wakeups']").click()
    expect(page.locator(".panel .wakeup-row").first).to_contain_text(invented["wake.note"])
    page.locator(".panel .panel-tab[data-tab='folders']").click()
    expect(page.locator(".panel .focus-folder")).to_have_count(3)

    # A staff member's session, inside the project: its header, its messages, both sets of tabs.
    side.locator(".focus-staff", has_text="Lev").click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}/s/sess-lev**")
    head = page.locator(".staff-head")
    expect(head).to_contain_text("Lev")
    expect(head).to_contain_text("agent/lev/photos")
    expect(page.locator(".staff-message").first).to_contain_text(words["accepted"])
    expect(page.locator(".chat textarea")).to_have_attribute("placeholder", words["lev"])
    expect(page.locator(".panel .panel-tab")).to_have_count(8)
    head.get_by_role("button", name=words["pause"]).click()
    page.wait_for_timeout(300)
    assert ("st-lev", "pause") in focus.controls, focus.controls
    fits(page, f"{lang} {width} staff session")

    # The journal: a page of thirty, the older ones on request, and a note of the operator's own.
    side.locator(".focus-row", has_text=words["journal"]).click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}/journal")
    expect(page.locator(".journal-entry")).to_have_count(30)
    # What the project spends, over the journal: the totals and the orchestrator's own share today.
    expect(page.locator(".journal-usage")).to_contain_text(words["totals"])
    expect(page.locator(".journal-usage")).to_contain_text(words["orchspend"])
    expect(page.locator(".journal-entry").first.locator(".chip.author")).to_be_visible()
    page.get_by_role("button", name=words["older"]).click()
    expect(page.locator(".journal-entry")).to_have_count(35)
    page.locator(".journal-note textarea").fill("Friday: new prices")
    page.get_by_role("button", name=words["note"]).click()
    page.wait_for_timeout(300)
    assert focus.notes == ["Friday: new prices"], focus.notes
    fits(page, f"{lang} {width} journal")

    # And back to orchestration's home; Agents mode's lens is as it was.
    page.locator("nav.project-sidebar .focus-back").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator("nav.project-sidebar")).to_have_count(0)
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    page.locator(".rail [data-rail='agents']").click()
    page.wait_for_url("**/app/agents")
    assert page.locator(".sidebar .project-chip").inner_text() == chip_before

    # A project without an orchestrator: what one is, what it costs, and the switch.
    page.goto(f"{BASE}/project/{GARDEN}?token=t&lang={lang}")
    page.get_by_role("button", name=words["enable"]).click()
    sheet = page.locator(".enable-sheet")
    expect(sheet.locator(".autonomy-option")).to_have_count(3)
    expect(sheet.locator(".focus-cost")).to_contain_text(words["cost"])
    sheet.get_by_role("button", name=words["on"], exact=True).click()
    expect(page.locator(".chat.in-project.orchestrator textarea")).to_have_attribute("placeholder", words["placeholder"])
    assert focus.enabled == [(GARDEN, {"model": "", "autonomy": "normal", "concurrency_cap": 10})], focus.enabled


def phone(page: Page, lang: str) -> None:
    """Built for the desktop; the phone views are their own work. Here: nothing breaks."""
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/project/{PID}?token=t&lang={lang}")
    expect(page.locator(".chat.in-project.orchestrator .event-card")).to_be_visible()
    fits(page, f"{lang} phone orchestrator")
    for where in ("journal", "brief", "team", "board"):
        page.goto(f"{BASE}/project/{PID}/{where}?token=t&lang={lang}")
        expect(page.locator(".pagehead .iconbtn[href]").first).to_be_visible()
        if where == "team":
            # Each member's spend where the member is: dollars and tokens, or the subscription window used.
            # A phone draws the team as its own rows, not the desktop's, and the spend goes with them.
            rows = page.locator(".phone-staff-item")
            expect(rows.filter(has_text="Lev").locator(".staff-spend")).to_have_text(WORDS[lang]["levspend"])
            expect(rows.filter(has_text="Ira").locator(".staff-spend")).to_have_text(WORDS[lang]["iraspend"])
            expect(rows.filter(has_text="Max").locator(".staff-spend")).to_have_count(0)
        fits(page, f"{lang} phone {where}")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in ((1440, 900), (2560, 1300)):
                context = browser.new_context(viewport={"width": width, "height": height})
                desktop(context.new_page(), lang, width)
                context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            phone(context.new_page(), lang)
            context.close()
        browser.close()
    print("focus mode: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
