"""Quoting a paragraph picked by a triple click, and translating one of the agent's notes, in a real browser.

A triple click selects a paragraph by running the selection to the first point of the next block; for
the last paragraph of an answer that point lies in the row of actions under it, and the Reply / Ask on
the side pill never came, while a drag over the same words raised it. Here: a triple click on the last
paragraph and on one in the middle (with inline code) raises the pill, and Reply quotes exactly that
paragraph; a double click and a selection grown with Shift+End raise it too; a selection that really
takes in the next message raises nothing.

Then "Translate": under a note the agent wrote between steps, in a session that translates nothing, it
shows on hover, asks the host once however often it is pressed, shows the translation with "Show
original", and a failure says why, keeps the original and offers another try. The final answer has it
in its row of actions. In English and Russian; screenshots into ``SHOTS`` when set.

    APP_URL=http://127.0.0.1:8371/app python3 tests/browser/check_select_translate.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from check_composer import BASE, CHROMIUM, HOST, SESSION, message
from check_composer import stub as composer_stub
from playwright.sync_api import expect, sync_playwright

SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None
NOTE = "No MR pipelines on `main`. Merging both."
NOTE_RU = "Пайплайнов MR на `main` нет. Сливаю обе."
FAILING = "Rebasing `feature/menu` onto the new base."
MIDDLE = "Second paragraph with retry() inline code and more words here."
ANSWER = "First paragraph says `parse` is fixed in the loader.\n\nSecond paragraph with `retry()` inline code and more words here.\n\nThird one ends the answer."
ANSWERS_RU = "Answers to your side questions (the operator answered them in the side panel):\n\n[qacc01] Оставлять доступ?\nAnswer: Opened ones stay — оставь навсегда"
ANSWERS_EN = "Answers to your side questions (the operator answered them in the side panel):\n\n[qacc01] Keep access?\nAnswer: Opened ones stay — keep it forever"
ASKED: list[int] = []
WORDS = {
    "en": {"reply": "Reply", "translate": "Translate", "original": "Show original", "translation": "Show translation", "failed": "Translation failed — showing the original", "retry": "Try again", "action": "Translate into RU"},
    "ru": {"reply": "Ответить", "translate": "Перевести", "original": "Показать оригинал", "translation": "Показать перевод", "failed": "Перевести не удалось — показан оригинал", "retry": "Попробовать ещё раз", "action": "Перевести на RU"},
}


def stub(route) -> None:  # type: ignore[no-untyped-def]
    path = route.request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == f"/api/sessions/{SESSION}" and route.request.method == "GET":
        detail = {**HOST.detail(), "translation": {"agent": "", "operator": "ru", "active": False, "explicit": True, "default": ""}}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
    if rel.startswith(f"/api/sessions/{SESSION}/messages/") and rel.endswith("/translate") and route.request.method == "POST":
        seq = int(rel.split("/")[5])
        ASKED.append(seq)
        if seq == 4:
            return route.fulfill(status=502, content_type="application/json", body=json.dumps({"detail": "the translator did not answer within 120 s"}))
        done = {"direction": "in", "state": "done", "from": "", "to": "ru", "alternate": NOTE, "asked": True}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"seq": seq, "text": NOTE_RU, "translation": done}))
    return composer_stub(route)


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / name))


def quoted(page, words: dict[str, str]) -> str:  # type: ignore[no-untyped-def]
    """Press Reply on the pill and read the quote its popover shows, then give the reply up."""
    pill = page.locator(".reply-pill")
    expect(pill).to_be_visible()
    pill.get_by_role("button", name=words["reply"]).click()
    quote = page.locator(".reply-popover .reply-quote")
    expect(quote).to_be_visible()
    text = quote.inner_text()
    page.keyboard.press("Escape")
    expect(page.locator(".reply-popover")).to_have_count(0)
    return text


def clear(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate("getSelection().removeAllRanges()")
    expect(page.locator(".reply-pill")).to_have_count(0)


def walk(browser, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    HOST.status = "idle"
    HOST.pending = None
    HOST.messages = [
        message(1, "user", "Merge them"),
        message(2, "assistant", NOTE, tool_calls=[{"id": "c1", "name": "Exec", "arguments": {"command": "git merge"}}]),
        message(3, "tool", "", tool_results=[{"id": "c1", "content": "ok", "is_error": False}]),
        message(4, "assistant", FAILING, tool_calls=[{"id": "c2", "name": "Exec", "arguments": {"command": "git rebase"}}]),
        message(5, "tool", "", tool_results=[{"id": "c2", "content": "ok", "is_error": False}]),
        message(6, "assistant", ANSWER),
        message(7, "user", "Next"),
        message(8, "assistant", "Another answer after it."),
        # Answers to side questions in a translated chat: the operator's words shown, what the agent got behind a switch.
        message(9, "user", ANSWERS_RU, origin="side_questions",
                translation={"direction": "out", "state": "done", "from": "ru", "to": "en", "alternate": ANSWERS_EN}),
        message(10, "assistant", "Keeping the snapshot."),
    ]
    ASKED.clear()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}&scheme=dark")
    answer = page.locator(".answer", has_text="First paragraph")
    expect(answer).to_be_visible()

    # The last paragraph by a triple click: the selection runs into the row of actions, the pill still comes.
    answer.locator("p", has_text="Third one").click(click_count=3)
    expect(page.locator(".reply-pill")).to_be_visible()
    shot(page, f"triple-click-pill-{lang}.png")
    assert quoted(page, words) == "Third one ends the answer.", "the quote is the paragraph alone"
    clear(page)

    # A paragraph in the middle, inline code and all; nothing of the next paragraph, no trailing line break.
    answer.locator("p", has_text="Second paragraph").click(click_count=3)
    assert quoted(page, words) == MIDDLE
    clear(page)

    # A double click (a word) and a selection grown from it by the keyboard.
    answer.locator("p", has_text="First paragraph").locator("code").dblclick()
    expect(page.locator(".reply-pill")).to_be_visible()
    page.keyboard.press("Shift+End")
    page.wait_for_timeout(300)
    assert quoted(page, words).startswith("parse is fixed"), "the keyboard's selection is quoted"
    clear(page)

    # A selection that really takes in the next message is no reply to either.
    page.evaluate("""() => {
      const from = [...document.querySelectorAll('.answer p')].find((p) => p.textContent.startsWith('Third one'));
      const to = [...document.querySelectorAll('.answer')].find((a) => a.textContent.startsWith('Another answer'));
      getSelection().setBaseAndExtent(from.firstChild, 0, to.querySelector('p').firstChild, 7);
    }""")
    page.wait_for_timeout(400)
    expect(page.locator(".reply-pill")).to_have_count(0)
    clear(page)

    # "Translate" under a note between steps: out of sight until the pointer is on the note.
    note = page.locator(".answer.interim", has_text="No MR pipelines")
    offer = page.locator(".answer.interim + .answer-translation.offer [data-translate='offer']").first
    expect(offer).to_have_css("opacity", "0")
    note.hover()
    expect(offer).to_have_css("opacity", "1")
    expect(offer).to_have_text(words["translate"])
    shot(page, f"note-translate-offer-{lang}.png")
    offer.hover()
    offer.click()
    expect(page.locator(".answer.interim", has_text="Пайплайнов MR")).to_be_visible()
    expect(page.locator(".answer.interim code", has_text="main").first).to_be_visible()
    line = page.locator(".answer-translation[data-translation='done']")
    expect(line).to_be_visible()
    assert ASKED == [2], ASKED
    page.mouse.move(5, 5)
    shot(page, f"note-translated-{lang}.png")
    line.get_by_role("button", name=words["original"]).click()
    expect(page.locator(".answer.interim", has_text="No MR pipelines")).to_be_visible()
    line.get_by_role("button", name=words["translation"]).click()
    expect(page.locator(".answer.interim", has_text="Пайплайнов MR")).to_be_visible()
    assert ASKED == [2], "showing the original and back asks the host nothing"

    # A failure says why and keeps the original; another try asks again.
    failing = page.locator(".answer.interim", has_text="Rebasing")
    failing.hover()
    page.locator(".answer.interim + .answer-translation.offer [data-translate='offer']").first.click()
    failed = page.locator(".answer-translation[data-translation='failed']")
    expect(failed).to_contain_text(words["failed"])
    expect(failed).to_contain_text("did not answer")
    expect(failing).to_contain_text("Rebasing")
    page.mouse.move(5, 5)
    shot(page, f"note-translate-failed-{lang}.png")
    failed.get_by_role("button", name=words["retry"]).click()
    page.wait_for_timeout(300)
    assert ASKED == [2, 4, 4], ASKED

    # The final answer: "Translate" in its row of actions, which is already in the operator's script for none.
    actions = answer.locator("xpath=following-sibling::div[contains(@class,'msg-actions')][1]")
    expect(actions.get_by_role("button", name=words["action"])).to_be_visible()

    # The answers to side questions: the operator's words, and what the agent got on request.
    note = page.locator(".sysnote.answers")
    note.locator(".sysnote-head").click()
    expect(note.locator(".sysnote-body")).to_contain_text("оставь навсегда")
    flip = note.locator("[data-translation='out']")
    flip.click()
    expect(note.locator(".sysnote-body")).to_contain_text("keep it forever")
    flip.click()
    expect(note.locator(".sysnote-body")).to_contain_text("оставь навсегда")
    page.close()


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        try:
            for lang in ("en", "ru"):
                walk(browser, lang)
                print(f"ok {lang}")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
