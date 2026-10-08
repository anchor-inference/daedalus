"""A project's Terminals page manages its terminals, at 1440 px and on a 390 px phone, in both
languages.

The page listed the project's terminals and showed one, and offered nothing else: a terminal could
not be ended, renamed or cleared away from where the operator was looking at it. Each row now has
the actions the Terminals screen's own views have, asking the same questions. What is checked:

- End on a terminal with a program running asks first, and on yes the host is asked to end it once;
- End on a staff member's terminal always asks, names the member, and No leaves it running;
- Rename sends the new title and the row shows it;
- Remove clears an ended terminal from the list;
- on a phone every row has the same menu, and the row itself still leads to the terminal.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_project_terminals.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_staff_view import open_page, stand  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = {
    "en": {"end": "End", "rename": "Rename", "remove": "Remove", "save": "Save", "cancel": "Cancel", "staff": "End the member's session?", "busy": "End the terminal?"},
    "ru": {"end": "Завершить", "rename": "Переименовать", "remove": "Убрать", "save": "Сохранить", "cancel": "Отмена", "staff": "Завершить сессию сотрудника?", "busy": "Завершить терминал?"},
}


def menu(page: Page, tid: str, item: str, phone: bool = False) -> None:
    if phone:
        # A phone's card keeps its commands behind a long press (or a right click), in a sheet.
        page.locator(f".ph-tcard[data-terminal='{tid}'] .ph-tcard-open").dispatch_event("contextmenu")
        page.locator(".ph-actions .ph-mrow", has_text=item).first.click()
        return
    page.locator(f"[data-terminal-menu='{tid}'] button").first.click()
    page.get_by_role("menuitem", name=item, exact=True).click()


def kills(term, tid: str) -> int:  # type: ignore[no-untyped-def]
    return sum(1 for method, path, _ in term.requests if method == "POST" and path == f"/api/terminals/{tid}/kill")


def run(browser, lang: str, phone: bool, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    focus, term, pid = stand(lang)
    term.add("tm-build", title="npm run build", owner_kind="free", owner_id="", project_id=pid, busy=True)
    term.add("tm-done", title="psql · orders", owner_kind="free", owner_id="", project_id=pid, status="exited", exit_code=0)
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark")
    shown = "" if phone else "t=tm-build&"
    page = open_page(context, focus, term, EventFeed(), f"{BASE}/project/{pid}/terminals?{shown}token=t&lang={lang}")
    handle = (lambda tid: f".ph-tcard[data-terminal='{tid}']") if phone else (lambda tid: f"[data-terminal-menu='{tid}']")
    expect(page.locator(handle("tm-build"))).to_be_visible(timeout=20000)
    for tid in ("tm-ira", "tm-naya", "tm-build", "tm-done"):
        if page.locator(handle(tid)).count() != 1:
            say(f"{tid} has no menu")

    if os.environ.get("SHOTS") and not phone:
        page.locator("[data-terminal-menu='tm-build'] button").first.click()
        page.wait_for_timeout(300)
        page.screenshot(path=f"{os.environ['SHOTS']}/project-terminals-{lang}-{size['width']}.png")
        page.keyboard.press("Escape")
    # A program runs: End asks, and yes ends it once.
    menu(page, "tm-build", words["end"], phone)
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text(words["busy"])
    dialog.get_by_role("button", name=words["end"], exact=True).click()
    page.wait_for_timeout(600)
    if kills(term, "tm-build") != 1:
        say(f"End asked the host {kills(term, 'tm-build')} times")

    # A staff member's session: always asks, names the member, and No leaves it be.
    menu(page, "tm-ira", words["end"], phone)
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text(words["staff"])
    expect(dialog).to_contain_text("Ira")
    dialog.get_by_role("button", name=words["cancel"], exact=True).click()
    page.wait_for_timeout(400)
    if kills(term, "tm-ira"):
        say("the member's terminal was ended although the operator said no")

    # Rename.
    menu(page, "tm-naya", words["rename"], phone)
    field = page.get_by_role("textbox", name=words["rename"])
    field.fill("naya · bot")
    page.get_by_role("button", name=words["save"], exact=True).click()
    row = page.locator("[data-terminal='tm-naya']")
    expect(row.get_by_text("naya · bot", exact=True)).to_be_visible(timeout=5000)
    patched = [body for method, path, body in term.requests if method == "PATCH" and path == "/api/terminals/tm-naya"]
    if patched != [{"title": "naya · bot"}]:
        say(f"the rename sent {patched}")

    # Remove an ended one.
    menu(page, "tm-done", words["remove"], phone)
    expect(page.locator(handle("tm-done"))).to_have_count(0, timeout=5000)
    if page.evaluate("document.documentElement.scrollWidth - innerWidth") > 0:
        say("the page scrolls sideways")

    if phone:
        # The card itself leads to the terminal; its commands are only behind the long press.
        page.locator(".ph-tcard[data-terminal='tm-naya'] .ph-tcard-open").tap()
        page.wait_for_url("**/terminals/tm-naya**", timeout=5000)
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (False, True):
                run(browser, lang, phone, problems)
            print(f"project terminals {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
