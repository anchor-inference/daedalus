"""One press on a file card opens the file, with one read of its bytes, in both languages, at
1440 px and on a 390 px phone, in the orchestrator's chat and in the main chat.

In a project orchestrator's chat a card opened the Preview tab, wrote ``?panel=preview`` into the
address, and the address, naming a tab the orchestrator's panel did not know, closed the panel in
the same moment: the first press fetched the file and showed nothing, the second fetched it again
and stayed. What is checked:

- after one press the panel (a sheet on a phone) is open on the file, and still open a second later;
- the host was asked for the file's bytes exactly once — a second press, or two presses in a row
  before the first answer, ask for nothing more;
- beside the orchestrator the strip gains a Preview tab, selected, and the file's root is a name,
  not a way to a Files tab the orchestrator does not have.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_file_card_open.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app  # noqa: E402
from check_kept_files import ESTIMATE_ID, PID, SPEC_ID, SPEC_NAME, messages, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def shown(page: Page, phone: bool) -> None:
    root = ".panel-sheet" if phone else ".panel.shown"
    expect(page.locator(f"{root} .viewer")).to_be_visible(timeout=5000)
    expect(page.locator(f"{root}").get_by_text("Free users get three hints a day.")).to_be_visible(timeout=5000)


def press_once(page: Page, chat: str, downloads: list[str], phone: bool, where: str, problems: list[str]) -> None:
    card = page.locator(f"{chat} .kept-files .artifact", has_text=SPEC_NAME).locator(".artifact-main")
    before = downloads.count(SPEC_ID)
    card.click()
    shown(page, phone)
    page.wait_for_timeout(1000)
    root = ".panel-sheet" if phone else ".panel.shown:not(.panel-closing)"
    if page.locator(f"{root} .viewer").count() != 1:
        problems.append(f"{where}: the file closed again after one press")
    if downloads.count(SPEC_ID) - before != 1:
        problems.append(f"{where}: one press read the file {downloads.count(SPEC_ID) - before} times")
    if not phone:
        # Pressed again with the file on screen: nothing more is read.
        card.click()
        page.wait_for_timeout(600)
        if downloads.count(SPEC_ID) - before != 1:
            problems.append(f"{where}: a second press read the file again ({downloads.count(SPEC_ID) - before} reads)")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        # Two presses in a row from a closed panel: one read.
        at = downloads.count(ESTIMATE_ID)
        other = page.locator(f"{chat} .kept-files .artifact", has_text="estimate.md").first.locator(".artifact-main")
        other.dblclick()
        expect(page.locator(".panel.shown").get_by_text("Estimate: two days.")).to_be_visible(timeout=5000)
        page.wait_for_timeout(800)
        if downloads.count(ESTIMATE_ID) - at != 1:
            problems.append(f"{where}: two presses in a row read the file {downloads.count(ESTIMATE_ID) - at} times")
        if page.locator(".panel.shown:not(.panel-closing) .viewer").count() != 1:
            problems.append(f"{where}: two presses in a row left the panel closed")


def orchestrator(page: Page, lang: str, phone: bool, where: str, problems: list[str]) -> None:
    focus = FocusStub.bakery(lang)
    detail = focus.details["orch-bakery"]
    detail["messages"] = detail["messages"] + messages(lang, seq=100, events_head="[events · Bakery 2.0 · 1 since 11:50]")
    downloads = serve(page, focus.answer)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = ".chat.in-project.orchestrator"
    expect(page.locator(f"{chat} .kept-files .artifact").first).to_be_visible(timeout=15000)
    page.wait_for_timeout(800)
    press_once(page, chat, downloads, phone, where, problems)
    if not phone:
        page.locator(f"{chat} .kept-files .artifact", has_text=SPEC_NAME).locator(".artifact-main").click()
        shown(page, phone)
        tab = page.locator(".panel.shown .panel-tab[data-tab='preview']")
        if tab.count() != 1 or tab.get_attribute("aria-selected") != "true":
            problems.append(f"{where}: the orchestrator's strip has no selected Preview tab")
        if page.locator(".panel.shown .panel-crumbs button.crumb").count():
            problems.append(f"{where}: the file's root offers a Files tab the orchestrator does not have")
        if "panel=preview" not in page.url:
            problems.append(f"{where}: the address does not name the preview: {page.url}")


def main_chat(page: Page, lang: str, phone: bool, where: str, problems: list[str]) -> None:
    main = MainStub(lang)
    main.opened = True
    main.detail["messages"] = main.detail["messages"] + messages(lang, seq=10, events_head="[reports · 1 since 11:50]")
    downloads = serve(page, lambda method, path, query, body: main.answer(method, path, body))
    page.goto(f"{BASE}/orchestration?token=t&lang={lang}")
    expect(page.locator(".chat .kept-files .artifact").first).to_be_visible(timeout=15000)
    page.wait_for_timeout(800)
    press_once(page, ".chat", downloads, phone, where, problems)


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height, touch in ((1440, 900, False), (390, 844, True)):
                for name, run in (("orchestrator chat", orchestrator), ("main chat", main_chat)):
                    context = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch, is_mobile=touch)
                    run(context.new_page(), lang, touch, f"{lang} {width}px {name}", problems)
                    context.close()
            print(f"file card {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
