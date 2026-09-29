"""The Questions tab of a project's orchestrator, at 1440 px and on a 390 px phone, in both languages.

What the operator asked for, checked the way they would use it. The tab is the panel's first beside
the orchestrator, with the count of what waits; the chat carries one line for the whole list instead
of a card per question, and that line opens the tab. Staff who wait (a permission, an escalated
question) are listed above the orchestrator's own questions. A card has its title, its text folded
when long, its options as chips — several where the question allows — and one field that is the
answer itself or a note beside a chosen option, always, even for a question asked "options only"; a
folder has its options alone, and a permission has Allow, Always, Deny and "No, because…", which asks
for the because. The options are walked with the arrows and chosen with
Space or Enter, and Ctrl+Enter sends. A half-answered card is a draft, marked as one, and survives a
reload. A question the orchestrator adds appears at once; one it takes back leaves with a word, and
says so when a draft of the operator's went with it. Send carries exactly the ready drafts; the
cards answered leave with "Sent", and one that lost to an answer given elsewhere stays to say so,
with that answer, until dismissed. On a phone the header's button opens the same tab as a sheet.
Nothing scrolls sideways.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import PID, fits, serve  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

REASON = "the brief now names the day"
QUESTIONS = ("qf9n05", "q4r8tz", "qp4y01", "qo9d02")
"""The cards that are questions, a staff member's and the orchestrator's: each has its field."""
WORDS = {
    "en": {
        "tab": "Questions", "line": "6 questions waiting", "open": "Open", "requests": "Staff are waiting", "questions": "Questions",
        "perm": ["Allow", "Always", "Deny", "No, because…"], "draft": "Draft", "ready": "Ready", "more": "Show more", "less": "Show less",
        "withdrawn": "Withdrawn by the orchestrator — your draft was dropped", "sent": "Sent", "conflict": "Already answered elsewhere",
        "elsewhere": "answered in Telegram: Inter", "toast": "Sent 4; 1 already answered elsewhere", "send": "Send (5)", "info": "5 of 6 ready to send",
        "new": "Menu languages", "because": "Why not? They will read it…", "note": "Add a note to your choice…", "sent1": "1 answer sent",
    },
    "ru": {
        "tab": "Вопросы", "line": "6 вопросов ждут ответа", "open": "Открыть", "requests": "Команда ждёт", "questions": "Вопросы",
        "perm": ["Разрешить", "Всегда", "Запретить", "Нет, потому что…"], "draft": "Черновик", "ready": "Готово", "more": "Показать больше", "less": "Свернуть",
        "withdrawn": "Оркестратор отозвал вопрос — ваш черновик удалён", "sent": "Отправлено", "conflict": "Уже ответили в другом месте",
        "elsewhere": "ответ в Telegram: Inter", "toast": "Отправлено: 4; уже отвечено в другом месте: 1", "send": "Отправить (5)", "info": "5 из 6 готовы к отправке",
        "new": "Языки меню", "because": "Почему нет? Это прочитают…", "note": "Добавьте комментарий к выбору…", "sent1": "Отправлен 1 ответ",
    },
}


def card(page: Page, short: str):  # type: ignore[no-untyped-def]
    return page.locator(f".questions-panel .q-card[data-ask='{short}']")


def chip(page: Page, short: str, option: str):  # type: ignore[no-untyped-def]
    return card(page, short).locator(f".q-chip[data-option=\"{option}\"]")


def a_new_question(focus: FocusStub, lang: str) -> dict:
    ask = {
        "id": "ask-langs", "short_id": "qn3w06", "project_id": PID, "origin": "orchestrator", "kind": "question", "staff_id": None, "staff_session_id": None, "task_id": None,
        "request_ref": "orchestrator:x:ask-langs", "title": WORDS[lang]["new"], "text": "English only, or Russian as well?", "detail": {"options": ["English", "English + Russian"]},
        "routed_to": "operator", "suggestion": "", "created_at": "2026-09-24T10:05:00Z", "routed_at": "2026-09-24T10:05:00Z", "resolved_at": None, "resolved_by": None, "resolution": {},
    }
    focus.asks.append(ask)
    return ask


def desktop(page: Page, feed: EventFeed, lang: str) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    before, cash = invented["ask.before"], invented["cash"]
    focus = FocusStub.bakery(lang)
    focus.questions_of_bakery(lang)
    serve(page, focus)
    page.route("**/api/events**", feed.route)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = page.locator(".chat.in-project.orchestrator")
    expect(chat).to_be_visible()

    # The tab leads the panel beside the orchestrator, with the count of what waits.
    tab = page.locator(".panel .panel-tab[data-tab='questions']")
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(page.locator(".panel .panel-tab").first).to_have_attribute("data-tab", "questions")
    expect(tab.locator(".count")).to_have_text("6")
    # The chat has one line for the whole list, after its latest turn, and no card per question —
    # but not while the Questions tab is open beside it, where its "Open" would open what is shown.
    line = chat.locator(".timeline > .questions-line")
    expect(line).to_have_count(0)
    expect(chat.locator(".timeline .q-card, .timeline .ask-card")).to_have_count(0)
    # With the panel closed the line is there, and opens the tab.
    page.locator(".panel .panel-actions button").last.click()
    expect(page.locator(".panel")).to_have_count(0)
    expect(line).to_contain_text(words["line"])
    expect(line).to_contain_text(words["open"])
    line.click()
    expect(tab).to_have_attribute("aria-selected", "true")
    page.wait_for_url(re.compile(r"panel=questions"))

    # Staff who wait come first, then the orchestrator's questions, oldest first.
    heads = page.locator(".questions-panel .questions-section-head > span:first-child")
    expect(heads).to_have_text([words["requests"], words["questions"]])
    shorts = page.locator(".questions-panel .q-card").evaluate_all("(cards) => cards.map((c) => c.dataset.ask)")
    assert shorts == ["qf9n05", "qg1t04", "q4r8tz", "qp4y01", "qo9d02", "qf0ld3"], shorts
    expect(card(page, "q4r8tz").locator(".q-title")).to_have_text(invented["title.spring"])
    expect(card(page, "qf9n05").locator(".q-suggestion")).to_contain_text("Georgia")
    # A permission: its four answers, Always because Ira's CLI can grant one; no field until "No, because…".
    expect(card(page, "qg1t04").locator(".q-chip")).to_have_text(words["perm"])
    expect(card(page, "qg1t04").locator(".q-field")).to_have_count(0)
    # A folder is added or not: its options, and no field for words.
    expect(card(page, "qf0ld3").locator(".q-chip")).to_have_text(["Add", "Don't add"])
    expect(card(page, "qf0ld3").locator(".q-field")).to_have_count(0)
    # Every question has its field for the operator's own words or a note, even one asked "options
    # only" (the payment question is stored so), which once left the operator nothing to write in.
    for short in QUESTIONS:
        expect(card(page, short).locator(".q-field")).to_have_count(1)
    # A long text is folded, and unfolds.
    pay = card(page, "qp4y01")
    expect(pay.locator(".q-text.folded")).to_have_count(1)
    pay.locator(".q-more").click()
    expect(pay.locator(".q-text.folded")).to_have_count(0)
    expect(pay.locator(".q-more")).to_contain_text(words["less"])
    expect(pay.locator(".q-chips")).to_have_attribute("role", "group")
    expect(card(page, "q4r8tz").locator(".q-chips")).to_have_attribute("role", "radiogroup")
    fits(page, f"{lang} 1440 list")

    # The keyboard: the arrows walk the options, Space and Enter choose, the field is the next stop.
    chip(page, "q4r8tz", before).focus()
    page.keyboard.press("ArrowRight")
    expect(chip(page, "q4r8tz", invented["ask.after"])).to_be_focused()
    page.keyboard.press(" ")
    expect(chip(page, "q4r8tz", invented["ask.after"])).to_have_attribute("aria-checked", "true")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Enter")
    expect(chip(page, "q4r8tz", before)).to_have_attribute("aria-checked", "true")
    expect(chip(page, "q4r8tz", invented["ask.after"])).to_have_attribute("aria-checked", "false")
    page.keyboard.press("Tab")
    note = card(page, "q4r8tz").locator(".q-field")
    expect(note).to_be_focused()
    expect(note).to_have_attribute("placeholder", words["note"])
    page.keyboard.type("like last spring")
    expect(card(page, "q4r8tz")).to_have_attribute("data-state", "ready")
    expect(card(page, "q4r8tz").locator(".q-state")).to_contain_text(words["ready"])

    # The rest by pointer: several providers, the folder, a refusal with its reason, the typeface.
    chip(page, "qp4y01", "Stripe").click()
    chip(page, "qp4y01", cash).click()
    expect(chip(page, "qp4y01", "Stripe")).to_have_attribute("aria-checked", "true")
    chip(page, "qf0ld3", "Add").click()
    chip(page, "qg1t04", "because").click()
    expect(card(page, "qg1t04")).to_have_attribute("data-state", "draft")
    expect(card(page, "qg1t04").locator(".q-state")).to_contain_text(words["draft"])
    reason = card(page, "qg1t04").locator(".q-field")
    expect(reason).to_be_focused()
    expect(reason).to_have_attribute("placeholder", words["because"])
    reason.fill("not before the review")
    chip(page, "qf9n05", "Georgia").click()
    card(page, "qo9d02").locator(".q-field").fill("Monday")
    expect(card(page, "qo9d02")).to_have_attribute("data-state", "ready")
    send = page.locator(".questions-send-btn")
    expect(send).to_have_text(re.compile(r"\(6\)"))

    # The drafts are this device's: a reload keeps every one of them.
    page.reload()
    expect(chip(page, "qp4y01", cash)).to_have_attribute("aria-checked", "true")
    expect(card(page, "q4r8tz").locator(".q-field")).to_have_value("like last spring")
    expect(card(page, "qg1t04").locator(".q-field")).to_have_value("not before the review")
    expect(card(page, "qo9d02").locator(".q-field")).to_have_value("Monday")
    for _ in range(50):
        if feed.connected():
            break
        page.wait_for_timeout(100)
    assert feed.connected(), f"{lang}: the page never opened the event stream"

    # The orchestrator adds a question: it appears at once, marked as new, and the count follows.
    a_new_question(focus, lang)
    feed.send("ask.pending", {"request_id": "ask-langs", "request_ref": "orchestrator:x:ask-langs", "run_id": "", "title": "Bakery 2.0 · orchestrator", "questions": [], "operator_facing": True, "telegram": False}, project=PID)
    expect(card(page, "qn3w06")).to_be_visible()
    expect(card(page, "qn3w06")).to_have_class(re.compile(r"\bfresh\b"))
    expect(tab.locator(".count")).to_have_text("7")

    # It takes one back while a draft of the operator's is on it: the card says so, then goes.
    opening = next(a for a in focus.asks if a["id"] == "ask-open")
    opening.update(resolved_at="2026-09-24T10:06:00Z", resolved_by="system", resolution={"closed": REASON, "via": "withdrawn", "by": "orchestrator"})
    feed.send("ask.answered", {"request_id": "ask-open", "request_ref": "orchestrator:x:ask-open", "via": "withdrawn", "by": "orchestrator", "reason": REASON}, project=PID)
    gone = card(page, "qo9d02")
    expect(gone).to_have_attribute("data-state", "withdrawn")
    expect(gone.locator(".q-fate")).to_contain_text(words["withdrawn"])
    expect(gone.locator(".q-fate")).to_contain_text(REASON)
    expect(gone.locator(".q-field")).to_have_count(0)
    expect(gone).to_have_count(0, timeout=10000)
    drafts = page.evaluate("() => JSON.parse(localStorage.getItem('daedalus.questions.drafts') || '{}')")
    assert "ask-open" not in drafts and "ask-pay" in drafts, drafts
    expect(tab.locator(".count")).to_have_text("6")
    expect(page.locator(".questions-send-info")).to_contain_text(words["info"])
    expect(send).to_have_text(re.compile(re.escape(words["send"])))

    # Lev's question is answered on the phone a moment before Send; Ctrl+Enter sends from a field.
    focus.late["ask-font"] = {"selected": ["Inter"], "via": "telegram"}
    card(page, "q4r8tz").locator(".q-field").focus()
    page.keyboard.press("Control+Enter")
    expect(page.get_by_text(words["toast"])).to_be_visible()
    assert len(focus.batches) == 1, focus.batches
    path, items = focus.batches[0]
    assert path == f"/api/projects/{PID}/asks/answer", path
    assert items == [
        {"ask_id": "ask-font", "selected": ["Georgia"]},
        {"ask_id": "ask-push", "allow": False, "note": "not before the review"},
        {"ask_id": "ask-spring", "selected": [before], "note": "like last spring"},
        {"ask_id": "ask-pay", "selected": ["Stripe", cash]},
        {"ask_id": "ask-labs", "selected": ["Add"]},
    ], items
    # The answered cards leave with a word; the one that lost stays to say who was first, and with what.
    for short in ("qg1t04", "q4r8tz", "qp4y01", "qf0ld3"):
        expect(card(page, short)).to_have_attribute("data-state", "sent")
    expect(card(page, "q4r8tz").locator(".q-fate")).to_contain_text(words["sent"])
    lost = card(page, "qf9n05")
    expect(lost).to_have_attribute("data-state", "conflict")
    expect(lost.locator(".q-fate")).to_contain_text(words["conflict"])
    expect(lost.locator(".q-fate")).to_contain_text(words["elsewhere"])
    for short in ("qg1t04", "q4r8tz", "qp4y01", "qf0ld3"):
        expect(card(page, short)).to_have_count(0, timeout=10000)
    expect(lost).to_be_visible()
    expect(tab.locator(".count")).to_have_text("1")
    lost.locator(".q-clear").click()
    expect(lost).to_have_count(0)
    shorts = page.locator(".questions-panel .q-card").evaluate_all("(cards) => cards.map((c) => c.dataset.ask)")
    assert shorts == ["qn3w06"], shorts
    # The count left is on the tab; the chat's line stays away while the tab is open beside it.
    expect(chat.locator(".questions-line")).to_have_count(0)
    assert page.evaluate("() => Object.keys(JSON.parse(localStorage.getItem('daedalus.questions.drafts') || '{}'))") == [], "a sent draft was kept"
    fits(page, f"{lang} 1440 after the send")


def phone(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus = FocusStub.bakery(lang)
    focus.questions_of_bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    button = page.locator(".chat-head .questions-headbtn")
    expect(button.locator(".count")).to_have_text("6")
    expect(page.locator(".timeline > .questions-line")).to_contain_text(words["line"])
    fits(page, f"{lang} 390 chat")
    button.tap()
    sheet = page.locator(".panel-sheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".panel-tab[data-tab='questions']")).to_have_attribute("aria-selected", "true")
    expect(sheet.locator(".q-card")).to_have_count(6)
    for box in [c.bounding_box() for c in sheet.locator(".q-chip").all()]:
        assert box and box["height"] >= 39.5, f"{lang} 390: a chip is {box and box['height']}px tall, not a thumb's"
    for short in QUESTIONS:
        expect(sheet.locator(f".q-card[data-ask='{short}'] .q-field")).to_have_count(1)
    expect(sheet.locator(".q-card[data-ask='qf0ld3'] .q-field")).to_have_count(0)
    expect(sheet.locator(".q-card[data-ask='qg1t04'] .q-field")).to_have_count(0)
    size = sheet.locator(".q-card[data-ask='q4r8tz'] .q-field").evaluate("(e) => parseFloat(getComputedStyle(e).fontSize)")
    assert size >= 16, f"{lang} 390: the answer field is {size}px, and Safari would zoom into it"
    sheet.locator(".q-card[data-ask='qg1t04'] .q-chip[data-option='allow']").tap()
    send = sheet.locator(".questions-send-btn")
    send_box = send.bounding_box()
    assert send_box and send_box["y"] + send_box["height"] <= 844 + 0.5, f"{lang} 390: Send is below the screen ({send_box})"
    send.tap()
    expect(page.get_by_text(words["sent1"])).to_be_visible()
    assert focus.batches == [(f"/api/projects/{PID}/asks/answer", [{"ask_id": "ask-push", "allow": True}])], focus.batches
    expect(sheet.locator(".q-card[data-ask='qg1t04']")).to_have_count(0, timeout=10000)
    expect(sheet.locator(".panel-tab[data-tab='questions'] .count")).to_have_text("5")
    fits(page, f"{lang} 390 sheet")


def main() -> int:
    expect_app(BASE)
    feed = EventFeed()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM)
            for lang in ("en", "ru"):
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                desktop(context.new_page(), feed, lang)
                context.close()
                context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                phone(context.new_page(), lang)
                context.close()
                print(f"questions panel {lang}: ok")
            browser.close()
    finally:
        feed.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
