"""The More launcher, in a real browser, in English and Russian.

On a 1440 × 900 desktop:

- it opens 8 px beside the sidebar's edge, level with the More row, 560 wide, whole inside the
  window, without moving the composer;
- every destination is in its group (Work, Autonomy, Knowledge, Observe, Pages), in a 44 px row with a
  line saying what it is and a pin; four chips under Frequent, learned from use;
- focus starts in the search field; Down enters the chips, the arrows walk the rows and their pins as
  one grid, Up from the top returns to the field; Tab goes from one group to the next and never leaves
  the box; Esc empties a typed search first and closes on the second press, giving focus back to the
  More row;
- typing narrows the list to the destinations that match, Enter opens the first; words that match no
  destination offer the command palette, opened on those words;
- the pin on a row writes `[navigation] pinned` through PUT /api/settings with the revision it read, and
  the sidebar gains (or loses) the row without a reload; two pins pressed together both land;
  a right click offers the same toggle.

On a 390 px phone the drawer's More is the same launcher as a sheet: the field, the chips (wrapping to a
second line where four do not fit), one column of rows, Settings, Customise and About, nothing sideways;
its pin edits the drawer's list.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_navigation import PINNED, Host  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")

WORDS = {
    "en": {"groups": ["Work", "Autonomy", "Knowledge", "Observe", "Pages"], "unpin": "Unpin", "usage": "Usage", "nothing": "zzqqxx", "everywhere": "Search everywhere", "palette": "Search and go"},
    "ru": {"groups": ["Работа", "Автономия", "Знания", "Наблюдение", "Страницы"], "unpin": "Открепить", "usage": "Расход", "nothing": "zzqqxx", "everywhere": "Искать", "palette": "Поиск и переход"},
}


def open_launcher(page: Page) -> None:
    page.locator(".sb-pinned-nav [data-nav='menu']").click()
    page.wait_for_selector(".navmenu[role='dialog'] .launch-field", timeout=5000)


def pinned_rows(page: Page) -> list[str]:
    return page.locator(PINNED).evaluate_all("els => els.map(e => e.dataset.nav)")


def focused(page: Page) -> str:
    return page.evaluate("() => { const a = document.activeElement; return a ? (a.dataset.chip || a.dataset.navItem || a.dataset.nav || a.className) : ''; }")


def group_of_focus(page: Page) -> str:
    return page.evaluate("() => document.activeElement.closest('[data-launch-group]')?.dataset.group || document.activeElement.closest('[data-launch-group]')?.className || ''")


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] desktop: {text}")

    host = Host()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)
    page.wait_for_selector(".start-composer", timeout=15000)
    page.wait_for_timeout(500)
    composer_before = page.locator(".start-composer").bounding_box()
    open_launcher(page)

    box = page.locator(".navmenu").bounding_box()
    trigger = page.locator(".sb-pinned-nav [data-nav='menu']").bounding_box()
    sidebar = page.locator("nav.sidebar").bounding_box()
    assert box and trigger and sidebar
    if abs(box["x"] - sidebar["x"] - sidebar["width"] - 8) > 1 or round(box["width"]) != 560:
        say(f"the launcher is not 560 wide, 8 px beside the sidebar: {box}")
    if box["y"] > trigger["y"] + 1:
        say(f"the launcher starts below its More row: {box} against {trigger}")
    if box["y"] < 8 or box["y"] + box["height"] > 900 - 7:
        say(f"the launcher leaves the window: {box}")
    composer_after = page.locator(".start-composer").bounding_box()
    if not composer_before or not composer_after or (round(composer_after["x"]), round(composer_after["width"])) != (round(composer_before["x"]), round(composer_before["width"])):
        say("opening the launcher moved the composer")
    if page.locator(".navmenu .lang, .navmenu .segmented").count():
        say("the launcher still has a language switch")

    # The groups in their order, each row 44 high with its line and its pin.
    labels = page.locator(".navmenu .launch-sec[data-group] .launch-label").all_inner_texts()
    if labels != words["groups"]:
        say(f"the groups read {labels}")
    heights = page.locator(".navmenu .launch-cell").evaluate_all("els => [...new Set(els.map(e => Math.round(e.getBoundingClientRect().height)))]")
    if heights != [44]:
        say(f"the rows are {heights} high, not 44")
    if page.locator(".navmenu .launch-cell .launch-desc").count() != page.locator(".navmenu .launch-cell").count():
        say("a row has no line saying what it is")
    columns = page.locator(".navmenu .launch-grid").first.evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
    if columns != 2:
        say(f"the grid has {columns} columns")
    if page.locator(".navmenu .launch-chip").count() != 4:
        say("Frequent does not hold four chips")

    # The keyboard: one grid from the field down, the pin one step right of its row.
    if focused(page) != "launch-field":
        say(f"focus starts on {focused(page)}, not the field")
    chips = page.locator(".navmenu .launch-chip").evaluate_all("els => els.map(e => e.dataset.chip)")
    page.keyboard.press("ArrowDown")
    if focused(page) != chips[0]:
        say(f"Down from the field went to {focused(page)}, not the first chip {chips[0]}")
    page.keyboard.press("ArrowRight")
    if focused(page) != chips[1]:
        say(f"Right from a chip went to {focused(page)}, not the next chip {chips[1]}")
    page.keyboard.press("ArrowDown")
    if not page.evaluate("() => document.activeElement.closest('.launch-grid') !== null"):
        say(f"Down from a chip went to {focused(page)}, not into the groups")
    here = focused(page)
    page.keyboard.press("ArrowRight")
    if not page.evaluate("() => document.activeElement.classList.contains('launch-pin')") and not page.evaluate("() => document.activeElement.classList.contains('launch-row')"):
        say(f"Right from {here} went to {focused(page)}, neither its pin nor the next row")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    if focused(page) != "launch-field":
        say(f"Up from the top row went to {focused(page)}, not the field")
    # Tab walks the groups: each press lands in another one, and a long walk stays inside the box.
    seen: list[str] = []
    for _ in range(9):
        page.keyboard.press("Tab")
        seen.append(group_of_focus(page))
    if not page.evaluate("() => !!document.activeElement.closest('.navmenu')"):
        say("Tab left the launcher")
    if any(a == b for a, b in zip(seen, seen[1:], strict=False)):
        say(f"Tab stayed in a group: {seen}")
    if "work" not in seen or "pages" not in seen:
        say(f"Tab did not reach every group: {seen}")
    page.keyboard.press("Shift+Tab")
    if group_of_focus(page) != seen[-2]:
        say("Shift+Tab did not go back one group")
    page.locator(".navmenu .launch-field").focus()

    # The search narrows the destinations; Enter opens the first.
    page.keyboard.type(words["usage"][:5])
    page.wait_for_timeout(300)
    shown = page.locator(".navmenu .launch-grid .launch-row").evaluate_all("els => els.map(e => e.dataset.navItem)")
    if shown != ["usage"]:
        say(f"typing {words['usage'][:5]!r} shows {shown}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    if page.locator(".navmenu").count() != 1 or page.locator(".navmenu .launch-field").input_value():
        say("Esc with words typed did not just empty the field")
    page.keyboard.type(words["usage"])
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    if page.locator(".navmenu").count() or not page.url.split("?")[0].endswith("/usage"):
        say(f"Enter on the first result left the launcher at {page.url}")

    # Words that are no destination go on to the palette, which keeps them.
    open_launcher(page)
    page.keyboard.type(words["nothing"])
    page.wait_for_timeout(300)
    if page.locator(".navmenu .launch-grid .launch-row").count():
        say("a nonsense word still shows destinations")
    everywhere = page.locator(".navmenu .launch-everywhere")
    if not everywhere.count() or words["everywhere"] not in everywhere.inner_text():
        say("words that match nothing do not offer the palette")
    page.keyboard.press("Enter")
    page.wait_for_selector(".palette-sheet", timeout=5000)
    if page.locator(".palette-sheet .combo-input").input_value() != words["nothing"]:
        say("the palette did not open on the words typed in the launcher")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # A pin writes the navigation, with the revision it read, and the sidebar follows at once.
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)
    before = pinned_rows(page)
    open_launcher(page)
    row = page.locator(".navmenu .launch-cell", has=page.locator("[data-nav-item='health']"))
    if row.locator(".launch-pin").get_attribute("aria-pressed") != "false":
        say("Health starts out pinned")
    row.hover()
    row.locator(".launch-pin").click()
    page.wait_for_function("([sel]) => [...document.querySelectorAll(sel)].some(e => e.dataset.nav === 'health')", arg=[PINNED], timeout=5000)
    if pinned_rows(page) != before + ["health"]:
        say(f"after pinning Health the sidebar reads {pinned_rows(page)}")
    if not host.puts or host.puts[-1]["navigation"]["pinned"][-1] != "health" or not host.puts[-1].get("base_revision"):
        say(f"the write was {host.puts[-1:]}")
    if row.locator(".launch-pin").get_attribute("aria-pressed") != "true":
        say("the pin does not show Health as pinned")
    # It is still open, and the pin is reached from the keyboard: Space presses it.
    row.locator(".launch-pin").focus()
    page.keyboard.press("Space")
    page.wait_for_function("([sel]) => ![...document.querySelectorAll(sel)].some(e => e.dataset.nav === 'health')", arg=[PINNED], timeout=5000)
    if pinned_rows(page) != before:
        say(f"after unpinning Health with the keyboard the sidebar reads {pinned_rows(page)}")
    # Two pins together: neither is refused as a stale write.
    puts = len(host.puts)
    for item in ("usage", "memory"):
        page.locator(f".navmenu [data-nav-item='{item}']").locator("xpath=following-sibling::button").click()
    page.wait_for_function("([sel]) => ['usage', 'memory'].every((id) => [...document.querySelectorAll(sel)].some(e => e.dataset.nav === id))", arg=[PINNED], timeout=5000)
    if len(host.puts) - puts != 2:
        say(f"two pins sent {len(host.puts) - puts} writes")
    # The right click offers the same toggle.
    page.locator(".navmenu [data-nav-item='usage']").click(button="right")
    page.get_by_role("menuitem", name=words["unpin"]).click()
    page.wait_for_function("([sel]) => ![...document.querySelectorAll(sel)].some(e => e.dataset.nav === 'usage')", arg=[PINNED], timeout=5000)
    page.keyboard.press("Escape")

    # Learned from use: Board and Inbox opened three times each are the first chips, though both are
    # pinned in the sidebar.
    for _ in range(3):
        page.goto(f"{BASE}/inbox?token=t&lang={lang}")
        page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)
        page.goto(f"{BASE}/board?token=t&lang={lang}")
        page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)
    open_launcher(page)
    first = page.locator(".navmenu .launch-chip").evaluate_all("els => els.slice(0, 2).map(e => e.dataset.chip)")
    if sorted(first) != ["board", "inbox"]:
        say(f"after opening Board and Inbox three times each the chips are {first}")

    # The foot.
    page.locator(".navmenu [data-nav='customise']").click()
    page.wait_for_url("**/settings/navigation**", timeout=5000)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)

    # A small window: the launcher shrinks, scrolls inside itself and stays whole.
    page.set_viewport_size({"width": 1024, "height": 600})
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    # Below 1100 px the sidebar starts folded; the corner's button brings it back with More in it.
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu'], .sidebar-unfold", timeout=15000)
    if page.locator(".sidebar-unfold").count():
        page.locator(".sidebar-unfold").click()
    page.wait_for_selector(".sb-pinned-nav [data-nav='menu']", timeout=15000)
    open_launcher(page)
    box = page.locator(".navmenu").bounding_box()
    assert box
    if box["x"] + box["width"] > 1024 - 7 or box["y"] < 8 or box["y"] + box["height"] > 600 - 7:
        say(f"at 1024 x 600 the launcher leaves the window: {box}")
    if not page.evaluate("() => { const b = document.querySelector('.navmenu .launch-body'); return b.scrollHeight > b.clientHeight; }"):
        say("at 1024 x 600 the body does not scroll inside the launcher")
    if not page.evaluate("() => document.querySelector('.navmenu .launch-foot').getBoundingClientRect().bottom <= innerHeight - 7"):
        say("at 1024 x 600 the foot is pushed out of the window")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    if not page.evaluate("() => document.activeElement === document.querySelector('.sb-pinned-nav [data-nav=\"menu\"]')"):
        say("focus did not go back to the More button")
    if errors:
        say(f"errors: {errors}")
    context.close()


def phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    def say(text: str) -> None:
        problems.append(f"[{lang}] phone: {text}")

    host = Host()
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="light")
    page = context.new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.locator(".ph-menu").first.wait_for(timeout=15000)
    page.locator(".ph-menu").first.tap()
    page.locator(".ph-drawer-root.open [data-nav='more']").tap()
    sheet = page.locator(".ph-more-sheet")
    sheet.locator(".launch").wait_for(timeout=5000)
    page.wait_for_timeout(300)
    if not sheet.locator(".launch-field").count():
        say("the sheet has no search field")
    if sheet.locator(".launch-chip").count() != 4:
        say("the sheet's Frequent does not hold four chips")
    drawer = page.locator(".ph-drawer-root [data-screen]").evaluate_all("els => els.map(e => e.dataset.screen)")
    marked = sheet.locator(".launch-grid [data-placed]").evaluate_all("els => els.map(e => e.dataset.screen)")
    if not marked or any(m not in drawer for m in marked):
        say(f"the marked items {marked} are not the drawer's {drawer}")
    columns = sheet.locator(".launch-grid").first.evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
    if columns != 1:
        say(f"the sheet's grid has {columns} columns")
    for key in ("settings", "customise", "about"):
        if not sheet.locator(f"[data-nav='{key}']").count():
            say(f"the sheet has no {key}")
    if page.locator(".ph-more-sheet .lang, .ph-more-sheet .segmented").count():
        say("the sheet still has a language switch")
    width = page.evaluate("() => [document.documentElement.scrollWidth, document.querySelector('.ph-more-sheet .sheet-body').scrollWidth - document.querySelector('.ph-more-sheet .sheet-body').clientWidth]")
    if width[0] > 390 or width[1] > 0:
        say(f"the sheet scrolls sideways: {width}")
    # The pin edits the drawer's list: Memory goes in at its end.
    sheet.locator(".launch-cell", has=page.locator("[data-nav-item='memory']")).locator(".launch-pin").tap()
    page.wait_for_timeout(600)
    if not host.puts or host.puts[-1]["navigation"]["drawer"][-1] != "memory":
        say(f"the pin wrote {host.puts[-1:]}")
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
