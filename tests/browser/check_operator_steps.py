"""A member's steps for the operator, as a card in the orchestrator's chat, at 1280 and 390 px, in both languages.

The steps used to reach the operator only through the orchestrator's retelling, which lost the table
of accounts and then a step. The host now puts them into the chat word for word, and the app draws
them from their fields: the goal, a badge that says the member did not walk them on the running
version (and why), the accounts as a table, the steps numbered, what to expect, how to check and what
they do not cover, the member's name, and the file they are kept in — which downloads, and on a
desk opens in the panel. The card can be answered, like a report. Nothing on it reaches past the
window, and the markdown the model reads is not drawn a second time as an events card.

    cd miniapp && npx vite build --outDir /tmp/app-dist
    mkdir -p /tmp/app-root && ln -s /tmp/app-dist /tmp/app-root/app
    python3 tests/browser/serve_app.py 8195 /tmp/app-root &
    APP_URL=http://127.0.0.1:8195/app python3 tests/browser/check_operator_steps.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
PID = "b4k3ry20f0c5"
FILE_ID = "5e0b7a11c0de"
FILE_NAME = "operator-steps-t-bot-1.md"

WORDS = {
    "en": {"label": "Steps for you", "unverified": "Not checked on the running version", "from": "from Naya · t-bot", "account": "Account",
           "expected": "What you will see", "check": "How to check", "limits": "What it does not cover", "reply": "Reply to this", "chip": "Replying to"},
    "ru": {"label": "Шаги для вас", "unverified": "Не проверено на работающей версии", "from": "от Naya · t-bot", "account": "Учётная запись",
           "expected": "Что вы увидите", "check": "Как проверить", "limits": "Чего не покрывает", "reply": "Ответить на это", "chip": "Ответ на"},
}


def check(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    where = f"{lang} {width}"
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = page.locator(".chat.in-project.orchestrator")
    card = chat.locator(".steps-card")
    expect(card).to_have_count(1, timeout=15000)
    card.scroll_into_view_if_needed()
    expect(card).to_be_visible()
    expect(card).to_have_attribute("aria-label", words["label"])
    expect(card.locator(".steps-kind")).to_have_text(words["label"])
    expect(card.locator(".steps-from")).to_have_text(words["from"])
    expect(card.locator(".steps-goal")).to_have_text(invented["steps.goal"])

    # Not walked on the running version: said on the card, in the warning tone, with the member's why.
    assert "unverified" in (card.get_attribute("class") or ""), f"{where}: the card is not marked as unverified"
    badge = card.locator(".steps-badge")
    assert "warn" in (badge.get_attribute("class") or ""), f"{where}: the badge is not in the warning tone"
    expect(badge).to_contain_text(words["unverified"])
    expect(badge).to_contain_text(invented["steps.how"])

    # The accounts stay a table, the steps a numbered list, the rest labelled.
    expect(card.locator(".steps-roles th").first).to_have_text(words["account"])
    rows = card.locator(".steps-roles tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text(invented["steps.bakery"])
    expect(rows.nth(1)).to_contain_text(invented["steps.baker.for"])
    steps = card.locator(".steps-list > li")
    expect(steps).to_have_count(3)
    expect(steps.nth(1)).to_have_text(invented["steps.2"])
    facts = card.locator(".steps-facts dt")
    assert facts.all_inner_texts() == [words["expected"], words["check"], words["limits"]], facts.all_inner_texts()
    expect(card.locator(".steps-facts dd").nth(1)).to_have_text(invented["steps.check"])

    # The file the steps are kept in: a card that downloads it, from the files the host keeps by handle.
    file = card.locator(".artifact")
    expect(file).to_have_count(1)
    expect(file.locator(".artifact-name")).to_have_text(FILE_NAME)
    href = file.locator("a[aria-label]").first.get_attribute("href") or ""
    assert f"/api/files/{FILE_ID}" in href and FILE_NAME in href, f"{where}: the file's download goes to {href}"

    # The markdown the model and the file carry is not drawn a second time: the chat's one events card
    # is still the batch it was woken with.
    expect(chat.locator(".event-card")).to_have_count(1)
    assert "| account |" not in chat.inner_text(), f"{where}: the steps' markdown table is printed in the chat"

    # Nothing on the card reaches past the window.
    box = card.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, f"{where}: the card is outside the window: {box}"
    wide = card.evaluate("el => [...el.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > window.innerWidth + 1).length")
    assert wide == 0, f"{where}: {wide} element(s) of the card reach past the window"
    fits(page, where)
    if SHOTS:
        card.screenshot(path=f"{SHOTS}/steps-{lang}-{width}.png")

    # The card can be answered: the quote waits over the composer, naming the member and the goal.
    card.locator(".steps-reply").click()
    chip = chat.locator(".reply-chip")
    expect(chip).to_be_visible()
    expect(chip).to_contain_text(words["chip"])
    expect(chip.locator(".reply-chip-text")).to_contain_text(f"Naya: {invented['steps.goal']}")


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in ((1280, 900), (390, 844)):
                phone = width < 1024
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone)
                page = context.new_page()
                check(page, lang, width)
                print(f"{lang} {width}: ok")
                context.close()
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
