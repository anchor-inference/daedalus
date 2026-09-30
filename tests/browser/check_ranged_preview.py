"""A Markdown file cited by lines opens in Source with the lines marked, and Preview is one press
away, in both languages, at 1440 px and on a 390 px phone.

A reference in an answer that carried a range (``NOTES.md :3-5``) opened the file as source with
the range marked, and its Preview button was disabled: the range decided the view outright, so a
cited Markdown file could never be read rendered. What is checked:

- pressed in the answer, the reference opens Source, the cited lines marked;
- Preview is enabled, and pressed it shows the rendered document;
- Source again marks the same lines;
- the same holds for a link that names the lines (``?lines=``).

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_ranged_preview.py
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = {"en": {"preview": "Preview", "source": "Source"}, "ru": {"preview": "Просмотр", "source": "Исходный текст"}}


def serve(page: Page) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        if urlsplit(r.request.url).path == f"/api/sessions/{S1}":
            data = copy.deepcopy(detail(S1))
            data["messages"][-1]["text"] += '\n\n<file path="NOTES.md" lines="3-5">notes</file>'
            return respond(r, data)
        return stub(r)

    page.route("**/api/**", route)


def switch(page: Page, root: str, words: dict[str, str], where: str, problems: list[str]) -> None:
    expect(page.locator(f"{root} .line.cited").first).to_be_visible(timeout=15000)
    cited = page.locator(f"{root} .line.cited").evaluate_all("els => els.map(e => e.dataset.line)")
    if cited != ["3", "4", "5"]:
        problems.append(f"{where}: the cited lines are {cited}")
    tools = page.locator(f"{root} .source-tools")
    rendered = tools.get_by_role("button", name=words["preview"], exact=True)
    if rendered.is_disabled():
        problems.append(f"{where}: Preview is disabled for a cited file")
        return
    rendered.click()
    expect(page.locator(f"{root} .preview-doc")).to_be_visible(timeout=5000)
    if page.locator(f"{root} .preview-doc table").count() != 1:
        problems.append(f"{where}: Preview does not show the rendered document")
    tools.get_by_role("button", name=words["source"], exact=True).click()
    expect(page.locator(f"{root} .source-view")).to_be_visible(timeout=5000)
    again = page.locator(f"{root} .line.cited").evaluate_all("els => els.map(e => e.dataset.line)")
    if again != ["3", "4", "5"]:
        problems.append(f"{where}: back in Source the marked lines are {again}")


def run(browser, lang: str, phone: bool, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px"
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone)
    page = context.new_page()
    serve(page)
    root = ".panel-sheet" if phone else ".panel.shown"
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}")
    evidence = page.locator('.chat-scroll .evidence[data-path="NOTES.md"]')
    expect(evidence).to_be_visible(timeout=20000)
    evidence.click()
    switch(page, root, words, f"{where} pressed", problems)
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&panel=preview&path=NOTES.md&lines=3-5")
    switch(page, root, words, f"{where} linked", problems)
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (False, True):
                run(browser, lang, phone, problems)
            print(f"ranged preview {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
