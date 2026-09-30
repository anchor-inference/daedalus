"""Search inside a chat, from its header, in the orchestrator's chat of a project's focus mode.

The chat's history is several of the host's pages long. At 1280 px and on a 390 px phone, in both
languages:

- the header's search button opens a field and a list that stay inside the window;
- typed words find the messages that hold them, newest first, with who wrote each and the words lit;
- choosing the oldest hit, four pages back, opens the chat at the turn that shows it, lit, and puts
  `#m<seq>` into the address;
- words found nowhere say so, and a host whose search is busy (429) says that, not "unavailable";
- nothing scrolls sideways.

    cd miniapp && npx vite build --outDir /tmp/app-dist
    mkdir -p /tmp/app-root && ln -s /tmp/app-dist /tmp/app-root/app
    python3 tests/browser/serve_app.py 8193 /tmp/app-root &
    APP_URL=http://127.0.0.1:8193/app python3 tests/browser/check_chat_search.py

Exit 0 when every search lands where it points.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_message_anchor import go, landed, long_chat  # noqa: E402
from check_orchestration_mode import PID, fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"open": "Search in this chat", "agent": "Agent", "empty": "Nothing found in this chat.", "busy": "Search is busy right now. Try again in a moment."},
    "ru": {"open": "Поиск в этом чате", "agent": "Агент", "empty": "В этом чате ничего не нашлось.", "busy": "Поиск сейчас занят. Попробуйте через минуту."},
}

# The words planted in three answers: one in the chat's first page, one a page back, one four back.
PLANTED = {20: "The sourdough starter came back to life.", 400: "The sourdough loaves sold out.", 690: "The sourdough oven is booked."}


def inside(page: Page, selector: str, where: str) -> list[str]:
    box = page.locator(selector).bounding_box()
    width = page.viewport_size["width"] if page.viewport_size else 0
    if not box or box["x"] < 0 or box["x"] + box["width"] > width + 1:
        return [f"{where}: {selector} is not inside the window ({box}, width {width})"]
    return []


def check(page: Page, lang: str, name: str) -> list[str]:
    problems: list[str] = []
    words = WORDS[lang]
    focus, main, questions = long_chat(lang, planted=PLANTED)
    serve(page, focus, main, lang)
    go(page, f"/orchestration/project/{PID}", lang)
    page.wait_for_selector(".chat-scroll .timeline [data-slot]", timeout=15000)

    button = page.locator(".chat-head .chat-search-btn")
    expect(button).to_have_attribute("aria-label", words["open"])
    button.click()
    field = page.locator(".chat-search-pop input[type=search]")
    expect(field).to_be_focused()
    field.fill("sourdough")
    hits = page.locator(".chat-search-pop .chat-hit")
    expect(hits).to_have_count(3, timeout=10000)
    seqs = hits.evaluate_all("(rows) => rows.map((r) => Number(r.dataset.seq))")
    if seqs != sorted(seqs, reverse=True):
        problems.append(f"{lang} {name}: the hits are not newest first ({seqs})")
    expect(hits.first.locator(".chat-hit-meta b")).to_have_text(words["agent"])
    expect(hits.first.locator("mark")).to_have_text("sourdough")
    problems += inside(page, ".chat-search-pop", f"{lang} {name} search list")
    fits(page, f"{lang} {name} search list")
    if focus.searched[-1] != ("orch-bakery", "sourdough"):
        problems.append(f"{lang} {name}: the search asked the host for {focus.searched[-1]!r}")

    # The oldest hit is an answer four pages back: the chat opens at the turn that shows it.
    answer = questions[20] + 1
    page.locator(f".chat-search-pop .chat-hit[data-seq='{answer}']").click()
    expect(page.locator(".chat-search-pop")).to_have_count(0)
    problems += landed(page, 20, f"{lang} {name} search hit")
    if not page.url.endswith(f"#m{answer}"):
        problems.append(f"{lang} {name}: the address does not say where the reader is ({page.url})")

    # Words found nowhere, and a host that is busy.
    button.click()
    expect(field).to_have_value("sourdough")
    field.fill("rye flour")
    expect(page.locator(".chat-search-pop .chat-search-note")).to_have_text(words["empty"], timeout=10000)
    focus.search_busy = True
    field.fill("sourdough starter")
    expect(page.locator(".chat-search-pop .chat-search-note")).to_have_text(words["busy"], timeout=10000)
    fits(page, f"{lang} {name} busy search")
    page.keyboard.press("Escape")
    expect(page.locator(".chat-search-pop")).to_have_count(0)
    return problems


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for name, width, height in (("desktop", 1280, 900), ("phone", 390, 844)):
                phone = name == "phone"
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone)
                problems += check(context.new_page(), lang, name)
                context.close()
                print(f"chat search {lang} {name}: done")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
