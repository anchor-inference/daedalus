"""The More launcher, in a real browser, in English and Russian.

On a 1440 × 900 desktop:

- it opens beside the rail's More button, 600 wide, whole inside the window, without moving the
  composer; at 1024 × 600 it shrinks and slides to stay whole;
- focus starts in the search field; Down enters the tiles, Down again lands in the catalogue under the
  tile, Up from the top row returns to the field; Tab never leaves it; Esc empties a typed search
  first and closes on the second press, giving focus back to the button;
- typing finds a screen, a chat and a setting; Enter opens the first result;
- the pin on a catalogue item moves it to the first tile and keeps it there after a reload; the right
  click offers Unpin, which takes it back;
- screens opened three times become tiles, learned from use;
- Settings, Customise and About lead to their sections; no language switch and no companion toggle.

On a 390 px phone the drawer's More is the same launcher as a sheet: the field, four tiles in a row,
the catalogue with the drawer's items marked, Settings, Customise and About, nothing sideways.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")

WORDS = {
    "en": {"memory": "Memory", "unpin": "Unpin", "chat": "Weekly digest", "usage": "Usage"},
    "ru": {"memory": "Память", "unpin": "Открепить", "chat": "Weekly digest", "usage": "Расход"},
}


def open_launcher(page: Page) -> None:
    page.locator(".rail [data-rail='menu']").click()
    page.wait_for_selector(".navmenu[role='dialog'] .launch-field", timeout=5000)


def tiles(page: Page) -> list[str]:
    return page.locator(".navmenu .launch-tiles [data-nav-item]").evaluate_all("els => els.map(e => e.dataset.navItem)")


def focused(page: Page) -> str:
    return page.evaluate("() => { const a = document.activeElement; return a ? (a.dataset.navItem || a.dataset.nav || a.className) : ''; }")


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] desktop: {text}")

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
    page.wait_for_selector(".start-composer", timeout=15000)
    page.wait_for_timeout(500)
    composer_before = page.locator(".start-composer").bounding_box()
    open_launcher(page)

    box = page.locator(".navmenu").bounding_box()
    trigger = page.locator(".rail [data-rail='menu']").bounding_box()
    assert box and trigger
    if abs(box["x"] - trigger["x"] - trigger["width"] - 6) > 1 or round(box["width"]) != 600:
        say(f"the launcher is not 600 wide beside its button: {box}")
    if box["y"] < 8 or box["y"] + box["height"] > 900 - 7:
        say(f"the launcher leaves the window: {box}")
    composer_after = page.locator(".start-composer").bounding_box()
    if not composer_before or not composer_after or (round(composer_after["x"]), round(composer_after["width"])) != (round(composer_before["x"]), round(composer_before["width"])):
        say("opening the launcher moved the composer")
    if page.locator(".navmenu .lang, .navmenu .segmented").count():
        say("the launcher still has a language switch")

    # The keyboard: one grid from the field down.
    if focused(page) != "launch-field":
        say(f"focus starts on {focused(page)}, not the field")
    first_tile = tiles(page)[0]
    page.keyboard.press("ArrowDown")
    if focused(page) != first_tile:
        say(f"Down from the field went to {focused(page)}, not the first tile {first_tile}")
    page.keyboard.press("ArrowDown")
    under = page.evaluate("() => document.activeElement.closest('.launch-catalogue') !== null")
    if not under:
        say(f"Down from a tile went to {focused(page)}, not into the catalogue")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    if focused(page) != "launch-field":
        say(f"Up from the top row went to {focused(page)}, not the field")
    for _ in range(40):
        page.keyboard.press("Tab")
    if not page.evaluate("() => !!document.activeElement.closest('.navmenu')"):
        say("Tab left the launcher")
    page.locator(".navmenu .launch-field").focus()

    # The search: a screen, a chat, a setting.
    page.keyboard.type(words["memory"][:3])
    page.wait_for_timeout(800)
    labels = page.locator(".navmenu [data-result]").evaluate_all("els => els.map(e => e.dataset.result)")
    if not any(r.startswith("go-memory") for r in labels) or not any(r.startswith("set-") for r in labels):
        say(f"searching for memory finds {labels}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    if page.locator(".navmenu").count() != 1 or page.locator(".navmenu .launch-field").input_value():
        say("Esc with words typed did not just empty the field")
    page.keyboard.type(words["chat"])
    page.wait_for_timeout(300)
    if not page.locator(".navmenu [data-result^='s-']").count():
        say("searching for a chat's title does not find it")
    page.keyboard.press("Escape")
    page.keyboard.type(words["usage"])
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    if page.locator(".navmenu").count() or not page.url.split("?")[0].endswith("/usage"):
        say(f"Enter on the first result left the launcher at {page.url}")

    # A pin from the catalogue goes to the first tile and stays after a reload.
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
    open_launcher(page)
    row = page.locator(".navmenu .launch-catalogue .launch-cell", has=page.locator("[data-nav-item='health']"))
    row.hover()
    row.locator(".launch-pin").click()
    page.wait_for_timeout(200)
    if tiles(page)[:1] != ["health"]:
        say(f"pinned Health is not the first tile: {tiles(page)}")
    page.reload()
    page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
    open_launcher(page)
    if tiles(page)[:1] != ["health"]:
        say(f"after a reload the tiles are {tiles(page)}")
    page.locator(".navmenu .launch-tiles [data-nav-item='health']").click(button="right")
    page.get_by_role("menuitem", name=words["unpin"]).click()
    page.wait_for_timeout(200)
    if "health" in tiles(page):
        say(f"Unpin left Health on the tiles: {tiles(page)}")
    page.keyboard.press("Escape")

    # Learned from use: Board and Inbox opened three times each become the first tiles, though both
    # are on the rail.
    for _ in range(3):
        page.goto(f"{BASE}/inbox?token=t&lang={lang}")
        page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
        page.goto(f"{BASE}/board?token=t&lang={lang}")
        page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
    open_launcher(page)
    if sorted(tiles(page)[:2]) != ["board", "inbox"]:
        say(f"after opening Board and Inbox three times each the tiles are {tiles(page)}")

    # The foot.
    page.locator(".navmenu [data-nav='customise']").click()
    page.wait_for_url("**/settings/navigation**", timeout=5000)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
    open_launcher(page)
    page.locator(".navmenu [data-nav='about']").click()
    page.wait_for_url("**/settings/about**", timeout=5000)

    # A small window: the launcher shrinks and stays whole.
    page.set_viewport_size({"width": 1024, "height": 600})
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".rail [data-rail='menu']", timeout=15000)
    open_launcher(page)
    box = page.locator(".navmenu").bounding_box()
    assert box
    if box["x"] + box["width"] > 1024 - 7 or box["y"] < 8 or box["y"] + box["height"] > 600 - 7:
        say(f"at 1024 x 600 the launcher leaves the window: {box}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    if not page.evaluate("() => document.activeElement === document.querySelector('.rail [data-rail=\"menu\"]')"):
        say("focus did not go back to the More button")
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
    page.locator(".ph-drawer-root.open [data-nav='more']").tap()
    sheet = page.locator(".ph-more-sheet")
    sheet.locator(".launch").wait_for(timeout=5000)
    page.wait_for_timeout(300)
    if not sheet.locator(".launch-field").count():
        say("the sheet has no search field")
    row = sheet.locator(".launch-tiles [data-screen]").evaluate_all("els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    if len(row) != 4 or len(set(row)) != 1:
        say(f"the tiles are not one row of four: {row}")
    drawer = page.locator(".ph-drawer-root [data-screen]").evaluate_all("els => els.map(e => e.dataset.screen)")
    marked = sheet.locator(".launch-catalogue [data-placed]").evaluate_all("els => els.map(e => e.dataset.screen)")
    if not marked or any(m not in drawer for m in marked):
        say(f"the marked items {marked} are not the drawer's {drawer}")
    for key in ("settings", "customise", "about"):
        if not sheet.locator(f"[data-nav='{key}']").count():
            say(f"the sheet has no {key}")
    if page.locator(".ph-more-sheet .lang, .ph-more-sheet .segmented").count():
        say("the sheet still has a language switch")
    width = page.evaluate("() => [document.documentElement.scrollWidth, document.querySelector('.ph-more-sheet .sheet-body').scrollWidth - document.querySelector('.ph-more-sheet .sheet-body').clientWidth]")
    if width[0] > 390 or width[1] > 0:
        say(f"the sheet scrolls sideways: {width}")
    sheet.locator("[data-nav='settings']").tap()
    page.wait_for_url("**/settings**", timeout=5000)
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
