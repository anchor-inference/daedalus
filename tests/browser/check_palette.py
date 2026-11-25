"""The command palette, Ctrl K, in a real browser, in English and Russian.

On a 1440 × 900 desktop:

- it opens centred, 640 wide, high in the window, with the field focused: a 48 px field row, 28 px group
  labels over 36 px rows, a 36 px foot with the keys and the count; Esc closes it and gives the focus back;
- empty, it offers a few chats, actions and places; typed, it narrows each group and marks the words in
  the names, the first row ready for Enter;
- a chat is found by name and, through the host's search, by meaning (a word that is not in its name);
- Enter opens a row, Ctrl Enter opens a chat beside the open one (`?with=`), Tab goes from group to group,
  a click opens a row;
- a setting is found as its section and as the rows inside it, and Enter lands on that section;
- words that match nothing say so, and the foot counts zero.

On a 390 px phone the same body is the bottom sheet, opened from the More sheet's "search everywhere",
with the typed words kept, and nothing scrolls sideways.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, S4, S6, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")

WORDS = {
    "en": {"chats": "Chats", "actions": "Actions", "go": "Go to", "settings": "Settings", "section": "Section", "nothing": "Nothing matches.", "zero": "0 results", "navigation": "Navigation", "field": "Desktop navigation"},
    "ru": {"chats": "Чаты", "actions": "Действия", "go": "Переход", "settings": "Настройки", "section": "Раздел", "nothing": "Ничего не нашлось.", "zero": "0 результатов", "navigation": "Навигация", "field": "Навигация на компьютере"},
}


def open_palette(page: Page) -> None:
    page.keyboard.press("Control+k")
    page.wait_for_selector(".palette-sheet .combo-input", timeout=5000)
    page.wait_for_timeout(250)


def rows(page: Page) -> list[str]:
    return page.locator(".palette-row").all_inner_texts()


def highlighted(page: Page) -> str | None:
    return page.evaluate("() => document.querySelector('.palette-row[data-highlighted] .palette-title')?.textContent ?? null")


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] desktop: {text}")

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}")
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)
    page.wait_for_selector(".sb-chat", timeout=15000)
    page.wait_for_timeout(500)

    open_palette(page)
    box = page.locator(".palette-sheet").bounding_box()
    assert box
    if round(box["width"]) != 640 or abs(box["x"] + box["width"] / 2 - 720) > 1:
        say(f"the palette is not 640 wide and centred: {box}")
    if not 100 <= box["y"] <= 120:
        say(f"the palette starts {box['y']} down, not at 12% of the window")
    sizes = page.evaluate("""() => ({
        field: Math.round(document.querySelector('.palette-box .combo-bar').getBoundingClientRect().height),
        row: [...new Set([...document.querySelectorAll('.palette-row')].map(r => Math.round(r.getBoundingClientRect().height)))],
        label: [...new Set([...document.querySelectorAll('.palette-box .combo-group-label')].map(r => Math.round(r.getBoundingClientRect().height)))],
        foot: Math.round(document.querySelector('.palette-foot').getBoundingClientRect().height),
        focus: document.activeElement === document.querySelector('.palette-sheet .combo-input'),
    })""")
    if sizes["field"] != 48 or sizes["row"] != [36] or sizes["label"] != [28] or sizes["foot"] != 36:
        say(f"sizes: field {sizes['field']}, rows {sizes['row']}, labels {sizes['label']}, foot {sizes['foot']}")
    if not sizes["focus"]:
        say("the field does not have the focus")
    labels = page.locator(".palette-box .combo-group-label").all_inner_texts()
    if labels[:3] != [words["chats"], words["actions"], words["go"]]:
        say(f"the empty palette's groups read {labels}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if page.locator(".palette-sheet").count():
        say("Esc did not close the palette")

    # Typed: narrowed, the words marked, the first row ready.
    open_palette(page)
    page.keyboard.type("weekly", delay=20)
    page.wait_for_timeout(700)
    if highlighted(page) != "Weekly digest":
        say(f"after typing a chat's name the highlighted row is {highlighted(page)}")
    if not page.locator(".palette-row .palette-hit").count():
        say("the typed words are not marked in the names")
    # Ctrl Enter opens it beside the open chat.
    page.keyboard.press("Control+Enter")
    page.wait_for_timeout(700)
    if f"with={S4}" not in page.url or page.locator(".palette-sheet").count():
        say(f"Ctrl Enter did not open the chat beside: {page.url}")

    # A chat by meaning: the word is not in its name.
    open_palette(page)
    page.keyboard.type("budget", delay=20)
    page.wait_for_function("() => [...document.querySelectorAll('.palette-row')].some(r => r.textContent.includes('Expense tracker'))", timeout=5000)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    if f"/agents/{S6}" not in page.url:
        say(f"Enter on a chat found by meaning went to {page.url}")

    # Tab walks the groups.
    open_palette(page)
    page.keyboard.press("Tab")
    page.wait_for_timeout(150)
    first = page.evaluate("() => document.querySelector('.palette-row[data-highlighted]')?.querySelector('[data-group]')?.dataset.group")
    page.keyboard.press("Tab")
    page.wait_for_timeout(250)
    second = page.evaluate("() => document.querySelector('.palette-row[data-highlighted]')?.querySelector('[data-group]')?.dataset.group")
    if not first or not second or first == second:
        say(f"Tab went from {first} to {second}, not to another group")
    page.keyboard.press("Shift+Tab")
    page.wait_for_timeout(250)
    back = page.evaluate("() => document.querySelector('.palette-row[data-highlighted]')?.querySelector('[data-group]')?.dataset.group")
    if back != first:
        say(f"Shift+Tab went back to {back}, not {first}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)

    # Settings: the section, and a row inside it, each landing on its section.
    open_palette(page)
    page.keyboard.type(words["navigation"], delay=20)
    page.wait_for_function("(word) => [...document.querySelectorAll('.palette-row')].some(r => r.textContent.includes(word))", arg=words["section"], timeout=5000)
    labels = page.locator(".palette-box .combo-group-label").all_inner_texts()
    if words["settings"] not in labels:
        say(f"no Settings group while typing {words['navigation']!r}: {labels}")
    page.keyboard.press("Enter")
    page.wait_for_url("**/settings/navigation**", timeout=5000)
    page.keyboard.press("Escape")
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}")
    page.wait_for_selector(".sb-chat", timeout=15000)
    open_palette(page)
    page.keyboard.type(words["field"], delay=20)
    page.wait_for_timeout(600)
    texts = rows(page)
    field_row = next((i for i, text in enumerate(texts) if words["field"] in text and "Section" not in text and words["section"] not in text), None)
    if field_row is None:
        say(f"no settings row for {words['field']!r}: {texts}")
    else:
        for _ in range(field_row):
            page.keyboard.press("ArrowDown")
        if highlighted(page) is None:
            page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        page.wait_for_url("**/settings/navigation#*", timeout=5000)

    # A click opens a row; nothing that matches says so.
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}")
    page.wait_for_selector(".sb-chat", timeout=15000)
    open_palette(page)
    page.keyboard.type("zzqqxx", delay=10)
    page.wait_for_timeout(700)
    if words["nothing"] not in page.locator(".palette-box").inner_text() or words["zero"] not in page.locator(".palette-foot").inner_text():
        say("no results are not said, or not counted")
    page.keyboard.press("Control+a")
    page.keyboard.type("Expense", delay=10)
    page.wait_for_timeout(500)
    page.locator(".palette-row", has_text="Expense tracker").first.click()
    page.wait_for_timeout(600)
    if f"/agents/{S6}" not in page.url or page.locator(".palette-sheet").count():
        say(f"a click on a row went to {page.url}")
    width = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if width > 0:
        say(f"the page scrolls sideways by {width}")
    if errors:
        say(f"errors: {errors}")
    context.close()


def phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    def say(text: str) -> None:
        problems.append(f"[{lang}] phone: {text}")

    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="light")
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.locator(".ph-menu").first.wait_for(timeout=15000)
    page.locator(".ph-menu").first.tap()
    page.locator(".ph-drawer-root.open [data-nav='menu']").tap()
    sheet = page.locator(".ph-more-sheet")
    sheet.locator(".launch-field").wait_for(timeout=5000)
    sheet.locator(".launch-field").fill("Expense")
    page.wait_for_timeout(300)
    sheet.locator(".launch-everywhere").tap()
    page.wait_for_selector(".palette-sheet .combo-input", timeout=5000)
    if page.locator(".palette-sheet .combo-input").input_value() != "Expense":
        say("the sheet did not open the palette on the words typed")
    page.wait_for_timeout(700)
    if not any("Expense tracker" in text for text in rows(page)):
        say(f"the phone's palette does not find the chat: {rows(page)}")
    box = page.locator(".palette-sheet").bounding_box()
    assert box
    if box["x"] < 0 or box["x"] + box["width"] > 390.5 or box["y"] + box["height"] > 844.5:
        say(f"the palette sheet leaves the screen: {box}")
    width = page.evaluate("() => [document.documentElement.scrollWidth, document.querySelector('.palette-sheet .sheet-body').scrollWidth - document.querySelector('.palette-sheet .sheet-body').clientWidth]")
    if width[0] > 390 or width[1] > 0:
        say(f"the palette scrolls sideways: {width}")
    page.locator(".palette-row", has_text="Expense tracker").first.tap()
    page.wait_for_timeout(600)
    if f"/agents/{S6}" not in page.url:
        say(f"a tap on a row went to {page.url}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        for lang in [LANG] if LANG else ["en", "ru"]:
            desktop(browser, lang, problems)
            phone(browser, lang, problems)
        browser.close()
    for problem in problems:
        print(problem)
    print("problems:", len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
