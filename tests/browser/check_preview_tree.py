"""The file tree beside a previewed file can be hidden, and the browser remembers it, in both
languages: at 1440 px with the panel covering the chat, and at 1024 px.

With the panel wide enough the Preview tab draws the workspace's tree to the left of the file, and
nothing could put it away: a wide diff was read through what the tree left. What is checked:

- the toolbar's tree button hides the tree, and the file takes the width it had;
- after a reload the tree stays hidden, and the button brings it back;
- a panel too narrow for the tree offers no button, and a phone's sheet neither.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_preview_tree.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = {"en": {"hide": "Hide the file tree", "show": "Show the file tree"}, "ru": {"hide": "Скрыть дерево файлов", "show": "Показать дерево файлов"}}
WIDTH = "(sel) => { const e = document.querySelector(sel); return e ? Math.round(e.getBoundingClientRect().width) : 0; }"


def opened(page: Page, lang: str) -> None:
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&panel=preview&path=NOTES.md")
    expect(page.locator(".panel.shown .preview-doc")).to_be_visible(timeout=20000)


def wide(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.add_init_script("try { if (!sessionStorage.getItem('seeded')) { localStorage.removeItem('daedalus.panel.tree'); sessionStorage.setItem('seeded', '1'); } } catch {}")
    page = context.new_page()
    page.route("**/api/**", stub)
    opened(page, lang)
    page.locator(".panel.shown .panel-actions button[aria-pressed]").first.click()
    expect(page.locator(".panel.full .panel-body.split .panel-files")).to_be_visible(timeout=5000)
    before = page.evaluate(WIDTH, ".panel-viewer")
    hide = page.get_by_role("button", name=words["hide"])
    expect(hide).to_have_attribute("aria-pressed", "true")
    hide.click()
    expect(page.locator(".panel.full .panel-files")).to_be_hidden(timeout=3000)
    after = page.evaluate(WIDTH, ".panel-viewer")
    if after < before + 150:
        problems.append(f"{lang} 1440px: with the tree hidden the file is {after} px wide, it was {before}")
    page.reload()
    expect(page.locator(".panel.shown .preview-doc")).to_be_visible(timeout=20000)
    if not page.locator(".panel.full").count():
        page.locator(".panel.shown .panel-actions button[aria-pressed]").first.click()
    page.wait_for_timeout(500)
    if page.locator(".panel .panel-body.split").count():
        problems.append(f"{lang} 1440px: the tree came back after a reload")
    show = page.get_by_role("button", name=words["show"])
    expect(show).to_have_attribute("aria-pressed", "false")
    show.click()
    expect(page.locator(".panel.full .panel-body.split .panel-files")).to_be_visible(timeout=3000)
    context.close()

    context = browser.new_context(viewport={"width": 1024, "height": 800})
    page = context.new_page()
    page.route("**/api/**", stub)
    opened(page, lang)
    page.wait_for_timeout(400)
    if page.locator(".panel .panel-body.split").count() == 0 and page.locator(".panel-tree-toggle").count():
        problems.append(f"{lang} 1024px: a panel with no room for the tree offers to hide it")
    context.close()

    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&panel=preview&path=NOTES.md")
    expect(page.locator(".panel-sheet .preview-doc")).to_be_visible(timeout=20000)
    if page.locator(".panel-sheet .panel-tree-toggle").count():
        problems.append(f"{lang} 390px: the phone's sheet offers a tree it cannot draw")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            wide(browser, lang, problems)
            print(f"preview tree {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
