"""The operator's rules on a project's journal page, in both languages, at 1440 and 390 px.

The rules in force are pinned above the journal, whichever page of it is shown, with a word on who
sees them; a note can be written as a standing rule instead, and a rule lifted with one press. Lifted,
it leaves the pinned list, and the journal keeps its lifting. Nothing scrolls sideways.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import PID, fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"title": "Rules in force", "hint": "every member gets them with each task", "add": "Add the rule", "lift": "Lift", "kind": "rule lifted"},
    "ru": {"title": "Действующие правила", "hint": "каждый сотрудник получает их с каждой задачей", "add": "Добавить правило", "lift": "Снять", "kind": "правило снято"},
}
RULE = {
    "en": "Before any upload, ask me which account it goes to.",
    "ru": "Перед любой загрузкой спроси меня, в какой аккаунт.",
}


def journal(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}/journal?token=t&lang={lang}")

    pinned = page.locator(".journal-rules")
    expect(pinned).to_be_visible()
    expect(pinned.locator(".journal-rules-title")).to_have_text(words["title"])
    expect(pinned).to_contain_text(words["hint"])
    expect(pinned.locator(".journal-rule")).to_have_count(1)
    expect(pinned.locator(".journal-rule")).to_contain_text(invented["journal.rule"])
    # The pinned rules are not entries of the page: the journal still pages by thirty.
    expect(page.locator(".journal-entry")).to_have_count(30)
    fits(page, f"{lang} {width} rules pinned")

    page.locator(".journal-as-rule input").check()
    expect(page.locator(".journal-note textarea")).to_have_attribute("maxlength", "600")
    page.locator(".journal-note textarea").fill(RULE[lang])
    page.get_by_role("button", name=words["add"]).click()
    expect(pinned.locator(".journal-rule")).to_have_count(2)
    expect(pinned.locator(".journal-rule").nth(1)).to_contain_text(RULE[lang])
    assert focus.ruled == [RULE[lang]] and focus.notes == [], (focus.ruled, focus.notes)
    expect(page.locator(".journal-as-rule input")).not_to_be_checked()
    fits(page, f"{lang} {width} rule added")

    pinned.locator(".journal-rule").first.get_by_role("button", name=words["lift"]).click()
    expect(pinned.locator(".journal-rule")).to_have_count(1)
    assert focus.lifted == [120], focus.lifted
    expect(page.locator(".journal-entry").first.locator(".journal-kind")).to_have_text(words["kind"])
    fits(page, f"{lang} {width} rule lifted")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height, phone in ((1440, 900, False), (390, 844, True)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone)
                journal(context.new_page(), lang, width)
                context.close()
        browser.close()
    print("project rules: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
