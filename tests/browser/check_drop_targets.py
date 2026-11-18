"""Where a file dragged in from the desktop lands, at 1440 px, in both languages.

The conversation once took a file anywhere in it under a full-window overlay that stuttered and stuck,
and a file meant for a question went to the composer. Now: over the composer, the composer's box is
marked and a drop adds the file to the message; over the agent's question above the composer, that
card is marked; over a card of the Questions tab, that card is marked and the file joins that answer
alone; over the transcript nothing is marked, the browser is told no drop is possible there, and a
drop attaches nothing. The mark goes with a dragleave that no dragover follows, and with Escape.
A sweep of a few hundred dragovers reports its long tasks.

``SHOTS`` names a directory to write the marked composer and card into.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_side_questions import UNHANDLED, WORDS, SideStub, card, open_page  # noqa: E402
from event_feed import EventFeed  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

HINTS = {
    "en": {"composer": "Drop to attach to the message", "ask": "Drop to attach to your answer", "card": "Drop to attach to this answer"},
    "ru": {"composer": "Отпустите, чтобы приложить к сообщению", "ask": "Отпустите, чтобы приложить к ответу", "card": "Отпустите, чтобы приложить к этому ответу"},
}

ASKED = {"questions": [{"question": "Which database should the cache use?", "header": "Cache", "options": [{"label": "Redis"}, {"label": "SQLite"}], "allow_custom": True}]}

# Dispatches one drag event carrying a file at the element a selector names, the way a browser does
# for a file dragged in from the desktop, and says what the page made of it.
DRAG = """([type, selector, name]) => {
  const at = document.querySelector(selector);
  if (!at) return {missing: selector};
  const data = new DataTransfer();
  data.items.add(new File(['dropped'], name, {type: 'text/plain'}));
  const event = new DragEvent(type, {dataTransfer: data, bubbles: true, cancelable: true, clientX: 10, clientY: 10});
  at.dispatchEvent(event);
  return {prevented: event.defaultPrevented, marked: [...document.querySelectorAll('[data-drop-over]')].map((el) => el.className.split(' ')[0])};
}"""


def drag(page: Page, kind: str, selector: str, name: str = "drop.txt") -> dict:
    return page.evaluate(DRAG, [kind, selector, name])


def marked(page: Page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('[data-drop-over]')].map((el) => el.className.split(' ')[0])")


def shot(page: Page, name: str) -> None:
    if SHOTS:
        page.screenshot(path=str(Path(SHOTS) / name))


def desktop(page: Page, feed: EventFeed, lang: str) -> None:
    words = WORDS[lang]
    hints = HINTS[lang]
    stub = SideStub(lang)
    stub.detail.update(status="waiting", pending=ASKED)
    open_page(page, stub, feed, lang)
    chat = page.locator(".chat")
    expect(chat.locator(".questions-line")).to_contain_text(words["line"])
    chat.locator(".questions-line").click()
    look = card(page, "ql00k2")
    expect(look).to_be_visible()
    expect(page.locator(".composer .ask")).to_be_visible()
    composer_files = page.locator(".composer .attachments .attachment")
    card_files = look.locator(".q-files .attachment")

    # Over the transcript: no mark, no drop possible, and a drop there attaches nothing.
    over = drag(page, "dragover", ".chat-scroll")
    assert over["marked"] == [] and over["prevented"], f"{lang}: transcript took a file: {over}"
    dropped = drag(page, "drop", ".chat-scroll", "nowhere.txt")
    assert dropped["prevented"], f"{lang}: a file let go of over the transcript would open in the browser"
    page.wait_for_timeout(200)
    assert composer_files.count() == 0 and card_files.count() == 0, f"{lang}: a drop on the transcript attached a file"
    assert page.locator(".dropzone").count() == 0, f"{lang}: the full-window overlay is back"

    # Over the composer: its box is marked with the words that say where the file goes.
    over = drag(page, "dragover", ".composer .composer-field")
    assert over["marked"] == ["composer"], f"{lang}: composer not marked: {over}"
    expect(page.locator(".composer .composer-box > .drop-hint")).to_be_visible()
    expect(page.locator(".composer .composer-box > .drop-hint")).to_have_text(hints["composer"])
    shot(page, f"drop-composer-{lang}.png")
    drag(page, "drop", ".composer .composer-field", "composer.txt")
    expect(composer_files).to_have_count(1)
    assert marked(page) == [], f"{lang}: the mark stayed after the drop"
    expect(page.locator(".composer .composer-box > .drop-hint")).to_be_hidden()

    # Over the agent's question: that card is marked, not the composer around it.
    over = drag(page, "dragover", ".composer .ask .ask-body")
    assert over["marked"] == ["ask"], f"{lang}: the question card not marked alone: {over}"
    expect(page.locator(".composer .ask > .drop-hint")).to_contain_text(hints["ask"])
    shot(page, f"drop-ask-{lang}.png")
    drag(page, "drop", ".composer .ask .ask-body", "answer.txt")
    expect(composer_files).to_have_count(2)

    # Over a card of the Questions tab: that card, and the file joins that answer alone.
    selector = ".questions-panel .q-card[data-ask='ql00k2'] .q-head"
    over = drag(page, "dragover", selector)
    assert over["marked"] == ["q-card"], f"{lang}: the side question's card not marked: {over}"
    expect(look.locator("> .drop-hint")).to_have_text(hints["card"])
    shot(page, f"drop-card-{lang}.png")
    # A dragleave the next dragover follows is a move inside the page: the mark stays.
    drag(page, "dragleave", selector)
    drag(page, "dragover", selector)
    page.wait_for_timeout(300)
    assert marked(page) == ["q-card"], f"{lang}: the mark flickered off on a move inside the card"
    drag(page, "drop", selector, "shot.txt")
    expect(card_files).to_have_count(1)
    expect(composer_files).to_have_count(2)
    assert card(page, "qp0rt1").locator(".q-files .attachment").count() == 0, f"{lang}: another card took the file"

    # The mark goes after a dragleave no dragover follows (the drag left the window or was cancelled).
    drag(page, "dragover", ".composer .composer-field")
    assert marked(page) == ["composer"]
    drag(page, "dragleave", ".composer .composer-field")
    page.wait_for_function("!document.querySelector('[data-drop-over]')", timeout=2000)

    # No render per dragover: sweeping a drag across the screen makes no long task.
    long_tasks = page.evaluate("""async () => {
      let long = 0;
      const observer = new PerformanceObserver((list) => { long += list.getEntries().length; });
      observer.observe({entryTypes: ['longtask']});
      const spots = ['.chat-scroll', '.composer .composer-field', '.composer .ask .ask-body', '.questions-panel .q-card .q-head'];
      const data = new DataTransfer();
      data.items.add(new File(['x'], 'x.txt', {type: 'text/plain'}));
      for (let i = 0; i < 400; i++) {
        const at = document.querySelector(spots[i % spots.length]);
        at.dispatchEvent(new DragEvent('dragenter', {dataTransfer: data, bubbles: true, cancelable: true}));
        at.dispatchEvent(new DragEvent('dragover', {dataTransfer: data, bubbles: true, cancelable: true}));
        if (i % 20 === 0) await new Promise((r) => requestAnimationFrame(r));
      }
      await new Promise((r) => setTimeout(r, 100));
      observer.disconnect();
      return long;
    }""")
    # And on Escape (last: Escape also closes the panel the card is in).
    drag(page, "dragover", selector)
    assert marked(page) == ["q-card"]
    page.keyboard.press("Escape")
    assert marked(page) == [], f"{lang}: Escape left the mark"
    # Reported rather than asserted: a loaded machine makes long tasks of its own. The guard that no
    # dragover renders anything is filedrop.test.tsx.
    print(f"drop targets {lang}: {long_tasks} long task(s) over 400 dragovers")


def main() -> int:
    expect_app(BASE)
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    feed = EventFeed()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM)
            for lang in ("en", "ru"):
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                desktop(context.new_page(), feed, lang)
                context.close()
                print(f"drop targets {lang}: ok")
            browser.close()
    finally:
        feed.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
