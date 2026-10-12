"""Every text the agent writes is a reply on the page, and it splits the steps around it.

The case that asked for it: the agent wrote its whole answer, then wrote the sources file, then
closed with one line. The answer had become an "intermediate" text and was folded into the steps
with the tool calls, so the reader saw a one-line answer under a closed log. Now a text between
steps is drawn as a reply at the answer's size, the steps before and after it are blocks of their
own, the copy and retry row sits under the turn's last text only, and the appearance setting
"In the steps" brings back the old single block. The same holds while the run is still working.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_interim_replies.py

With SHOTS=<dir> it also photographs the turn on a desktop and a phone, in Russian.
"""
from __future__ import annotations

import json
import os

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub
from playwright.sync_api import Page, expect, sync_playwright

LONG = (
    "## What follows for the project\n\n"
    "The daemon already talks to Chromium over CDP directly, without the Playwright layer, and that layer "
    "is what the measurements flag. Three changes move the needle without breaking a feature:\n\n"
    "1. **Patch `screenX`/`screenY` per session**, before the document loads.\n"
    "2. **Drop the marker flags** that every detection list knows.\n"
    "3. **A headed mode** on the server, under Xvfb.\n\n"
    "The full write-up with quotes and a verdict table for every target is in the file."
)
CLOSE = "The source log with quotes, and notes on what could not be checked, is in `research/sources.md`."


def _conversation(running: bool) -> list[dict]:
    rows = [
        message(301, "user", "Compare the agent browsers and tell me what to change."),
        message(302, "assistant", "", run_id="r1", tool_calls=[{"id": "s1", "name": "WebSearch", "arguments": {"query": "agent browser benchmark"}}, {"id": "s2", "name": "WebFetch", "arguments": {"url": "https://example.org/bench"}}]),
        message(303, "tool", "", run_id="r1", tool_results=[{"id": "s1", "content": "results", "is_error": False}, {"id": "s2", "content": "page", "is_error": False}]),
        message(304, "assistant", LONG, run_id="r1", tool_calls=[{"id": "w1", "name": "Write", "arguments": {"path": "research/sources.md", "content": "…"}}]),
    ]
    if running:
        # The run is still writing the file: the call is out, its result is not, nothing is settled.
        return [{**row, "live": True} if row["seq"] >= 302 else row for row in rows]
    return rows + [
        message(305, "tool", "", run_id="r1", tool_results=[{"id": "w1", "content": "Wrote research/sources.md (4.1 KB)", "is_error": False}]),
        message(306, "assistant", CLOSE, run_id="r1"),
    ]


def _open(page: Page, lang: str, replies: str | None) -> None:
    if replies:
        page.add_init_script(f"localStorage.setItem('daedalus.appearance', {json.dumps(json.dumps({'replies': replies}))})")
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}&scheme=dark")
    expect(page.locator(".turn .answer").first).to_be_visible(timeout=10000)


def _check_split(page: Page) -> None:
    turn = page.locator(".turn").last
    answers = turn.locator(".answer")
    expect(answers).to_have_count(2)
    expect(answers.nth(0)).to_contain_text("Drop the marker flags")
    expect(answers.nth(1)).to_contain_text("research/sources.md")
    # The reply is drawn at the answer's size, not as a step's grey note.
    sizes = page.evaluate("() => [...document.querySelectorAll('.turn:last-child .answer')].map((e) => getComputedStyle(e).fontSize)")
    assert len(set(sizes)) == 1, f"the reply and the answer differ in size: {sizes}"
    heads = turn.locator(".thinking-head")
    expect(heads).to_have_count(2)
    # Each block of steps opens on its own and holds only its own calls.
    heads.nth(1).click()
    expect(turn.locator(".activity")).to_have_count(1)
    expect(turn.locator(".activity .note")).to_have_count(0)
    heads.nth(1).click()
    # One action row under the agent's words, after the last of them.
    rows = turn.locator(".msg-actions")
    expect(rows).to_have_count(2)
    after = page.evaluate(
        """() => {
          const turn = [...document.querySelectorAll('.turn')].at(-1);
          const answers = turn.querySelectorAll('.answer');
          const row = [...turn.querySelectorAll('.msg-actions')].at(-1);
          return !!(answers[answers.length - 1].compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING)
            && !!(answers[0].compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING);
        }"""
    )
    assert after, "the copy row must come after the turn's last text"


def main() -> None:
    shots = os.environ.get("SHOTS")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)

        HOST.status = "idle"
        HOST.messages = _conversation(running=False)
        for lang in ("en", "ru"):
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            _open(page, lang, None)
            _check_split(page)
            if shots and lang == "ru":
                page.locator(".turn").last.locator(".answer").first.scroll_into_view_if_needed()
                page.screenshot(path=f"{shots}/interim-replies-desktop-ru.png")
            page.close()

        phone = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        _open(phone, "ru", None)
        expect(phone.locator(".turn").last.locator(".answer")).to_have_count(2)
        expect(phone.locator(".turn").last.locator(".thinking-head")).to_have_count(2)
        width = phone.evaluate("() => document.documentElement.scrollWidth")
        assert width <= 390, f"the phone page scrolls sideways: {width}"
        if shots:
            phone.locator(".turn").last.locator(".thinking-head").nth(1).scroll_into_view_if_needed()
            phone.screenshot(path=f"{shots}/interim-replies-phone-ru.png")
        phone.close()

        # The old way, by choice: one block of steps with the reply folded inside it.
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        _open(page, "en", "merged")
        turn = page.locator(".turn").last
        expect(turn.locator(".answer")).to_have_count(1)
        expect(turn.locator(".thinking-head")).to_have_count(1)
        turn.locator(".thinking-head").click()
        expect(turn.locator(".activity .note")).to_have_count(1)
        if shots:
            page.screenshot(path=f"{shots}/interim-replies-merged-en.png")
        page.close()

        # While the run works: the reply already stands on its own, and the live block is after it.
        HOST.status = "running"
        HOST.messages = _conversation(running=True)
        for lang, size in (("ru", {"width": 1280, "height": 900}), ("ru", {"width": 390, "height": 844})):
            page = browser.new_page(viewport=size)
            _open(page, lang, None)
            turn = page.locator(".turn").last
            expect(turn.locator(".answer")).to_have_count(1)
            expect(turn.locator(".answer")).to_contain_text("Drop the marker flags")
            heads = turn.locator(".thinking-head")
            expect(heads).to_have_count(2)
            expect(heads.nth(1).locator(".dots.on")).to_have_count(1)
            expect(heads.nth(0).locator(".dots.on")).to_have_count(0)
            if shots:
                heads.nth(1).scroll_into_view_if_needed()
                page.screenshot(path=f"{shots}/interim-replies-live-{'phone' if size['width'] < 500 else 'desktop'}-ru.png")
            page.close()
        HOST.status = "idle"

        # The setting is where the other display choices are.
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/settings/appearance?token=t&lang=ru")
        expect(page.get_by_text("Ответы агента между шагами")).to_be_visible(timeout=10000)
        page.get_by_role("radio", name="В шагах").click()
        stored = page.evaluate("() => JSON.parse(localStorage.getItem('daedalus.appearance') || '{}').replies")
        assert stored == "merged", f"the choice was not kept: {stored!r}"
        page.close()
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    main()
