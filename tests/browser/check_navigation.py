"""Settings → Navigation, and the sidebar's pinned rows and menu it rearranges (a phone's drawer is that sidebar), in a real browser.

On a 1440 × 900 desktop and a 390 px phone, in English and Russian:

- with nothing configured the pinned rows are the ones the app ships with, and the published page is
  in the More launcher's catalogue, drawn with its own glyph and not marked as already pinned;
- in Settings → Navigation, moving Terminals up with its arrow saves the new order with the settings
  revision, and the sidebar redraws in that order without a reload;
- dragging Board into the Hidden list hides it from the sidebar and the menu;
- choosing Pinned for the page pins it, and a click opens /app/p/<slug> with the placeholder that
  names it;
- an icon picked for an item is drawn in the sidebar; Reset to default brings the shipped rows back;
- on the phone, the drawer shows the order the operator set, the page included, and nothing scrolls
  sideways; the More popover's Customise entry opens the section.

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, SAMPLE_PAGE, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")

SHIPPED = ["inbox", "board", "calendar", "changes", "terminals"]
EMPTY = {"pinned": [], "more": [], "hidden": [], "icons": {}}
WORDS = {
    "en": {"page": "Dosing calculator", "group": "Pages", "customise": "Customise…", "reset": "Reset to default", "pinned": "Pinned"},
    "ru": {"page": "Калькулятор дозировки", "group": "Страницы", "customise": "Настроить…", "reset": "Сбросить по умолчанию", "pinned": "Закреплён"},
}
# The destinations pinned in the sidebar: its rows between New chat and More.
PINNED = ".sb-pinned-nav a.sb-nav[data-nav]"


class Host:
    """The settings the page reads and writes, the navigation route that follows them, and one page."""

    def __init__(self) -> None:
        self.settings = copy.deepcopy(SETTINGS)
        self.settings["revision"] = "s1"
        self.settings["navigation"] = copy.deepcopy(EMPTY)
        self.puts: list[dict] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]

        def answer(data: object, status: int = 200) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        if rel == "/api/settings" and request.method == "GET":
            return answer(self.settings)
        if rel == "/api/navigation" and request.method == "GET":
            return answer(self.settings["navigation"])
        if rel == "/api/pages" and request.method == "GET":
            return answer({"pages": [SAMPLE_PAGE]})
        if rel == "/api/settings/validate":
            body = json.loads(request.post_data or "{}")
            return answer({"valid": True, "stale": body.get("base_revision") != self.settings["revision"], "problems": []})
        if rel == "/api/settings" and request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            self.puts.append(body)
            if body.get("base_revision") != self.settings["revision"]:
                return answer({"detail": "settings changed in another window"}, 409)
            # Each field sent replaces that field, as the host merges this section.
            self.settings["navigation"] = {**self.settings["navigation"], **body.get("navigation", {})}
            self.settings["revision"] = f"s{len(self.puts) + 1}"
            return answer(self.settings)
        return stub(route)


def pinned(page: Page) -> list[str]:
    return page.locator(PINNED).evaluate_all("els => els.map(e => e.dataset.nav)")


def wait_pinned(page: Page, want: list[str], say, what: str) -> None:  # type: ignore[no-untyped-def]
    try:
        page.wait_for_function("([sel, want]) => JSON.stringify([...document.querySelectorAll(sel)].map(e => e.dataset.nav)) === JSON.stringify(want)", arg=[PINNED, want], timeout=5000)
    except Exception:  # noqa: BLE001 - the message below says what was there instead
        say(f"{what}: the pinned rows read {pinned(page)}, not {want}")


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] desktop: {text}")

    host = Host()
    page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(PINNED, timeout=15000)
    if pinned(page) != SHIPPED:
        say(f"with nothing configured the pinned rows read {pinned(page)}")

    # The page is in the launcher under Pages with its own glyph, not marked as pinned; a pinned item
    # is listed there too, with the mark.
    page.locator(".sb-pinned-nav [data-nav='menu']").click()
    entry = page.locator(".navmenu [data-nav-item='page:dosing']")
    expect(entry).to_contain_text(words["page"])
    expect(page.locator(".launch-sec", has=page.locator("[data-nav-item='page:dosing']")).locator(".launch-label").first).to_have_text(words["group"])
    if entry.get_attribute("data-placed") is not None:
        say("the page is marked as already pinned")
    if page.locator(".navmenu [data-nav-item='inbox']").get_attribute("data-placed") is None:
        say("Inbox is not marked as already pinned")
    if entry.locator("svg.ic-custom path").get_attribute("d") != SAMPLE_PAGE["icon_path"]:
        say("the page's glyph is not drawn in the menu")
    # Customise opens the section.
    page.locator(".navmenu [data-nav='customise']").click()
    page.wait_for_url("**/settings/navigation**")
    page.wait_for_selector("[data-nav-row='terminals']", timeout=10000)

    # The order is read from the host's copy, and from the sidebar once the operator leaves Settings.
    page.locator("[data-nav-row='terminals']").get_by_role("button", name="Move Terminals up" if lang == "en" else "Выше: Терминалы").click()
    page.wait_for_timeout(600)
    moved = ["inbox", "board", "calendar", "terminals", "changes"]
    last = host.puts[-1] if host.puts else {}
    print(f"[{lang}] after the arrow: {last.get('navigation', {}).get('pinned')} at {last.get('base_revision')}")
    if last.get("navigation", {}).get("pinned") != moved or last.get("base_revision") != "s1":
        say(f"moving Terminals up saved {last}")
    order = page.locator("[data-nav-list='pinned'] [data-nav-row]").evaluate_all("els => els.map(e => e.dataset.navRow)")
    if order != moved:
        say(f"the Pinned list reads {order}")

    # Board dragged into Hidden.
    page.locator("[data-nav-row='board']").drag_to(page.locator("[data-nav-list='hidden'] .nav-cfg-head"))
    page.wait_for_timeout(600)
    if "board" not in host.settings["navigation"]["hidden"]:
        say(f"dragging Board into Hidden left {host.settings['navigation']}")
    expect(page.locator("[data-nav-row='board']")).to_have_attribute("data-place", "hidden")

    # The page pinned with its selector.
    page.locator("[data-nav-row='page:dosing'] .dropdown-btn").click()
    page.get_by_role("option", name=words["pinned"]).click()
    page.wait_for_timeout(600)
    expect(page.locator("[data-nav-row='page:dosing']")).to_have_attribute("data-place", "pinned")

    # An icon for the inbox.
    page.locator("[data-nav-row='inbox'] .nav-cfg-icon").click()
    page.locator(".nav-cfg-icons [data-icon='bolt']").click()
    page.wait_for_timeout(600)
    if host.settings["navigation"]["icons"].get("inbox") != "bolt":
        say(f"the icon was saved as {host.settings['navigation']['icons']}")

    page.locator(".settings-close").click()
    page.wait_for_selector(PINNED, timeout=10000)
    want = ["inbox", "calendar", "terminals", "changes", "page:dosing"]
    wait_pinned(page, want, say, "after the changes")
    if page.locator(".sb-pinned-nav [data-nav='inbox'] svg").get_attribute("class") != "ic ic-bolt":
        say("the inbox's new icon is not drawn in the sidebar")
    page.locator(".sb-pinned-nav [data-nav='menu']").click()
    if page.locator(".navmenu [data-nav-item='board']").count():
        say("a hidden Board is still in the menu")
    page.keyboard.press("Escape")

    # The page opens from its pinned row at its own address; what the page screen draws is check_pages.py's.
    page.locator(".sb-pinned-nav [data-nav='page:dosing']").click()
    page.wait_for_url("**/app/p/dosing**")
    expect(page.locator(".sb-pinned-nav [data-nav='page:dosing']")).to_have_attribute("aria-current", "page")

    # Reset brings the shipped rows back.
    page.goto(f"{BASE}/settings/navigation?token=t&lang={lang}")
    page.get_by_role("button", name=words["reset"]).click()
    page.wait_for_timeout(600)
    if host.settings["navigation"] != EMPTY:
        say(f"Reset left {host.settings['navigation']}")
    page.locator(".settings-close").click()
    wait_pinned(page, SHIPPED, say, "after Reset")
    page.context.close()


def phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] phone: {text}")

    # The drawer is the sidebar, so it holds what is pinned in the sidebar, in the same order.
    host = Host()
    host.settings["navigation"]["pinned"] = ["page:dosing", "board", "inbox"]
    host.settings["navigation"]["hidden"] = ["usage"]
    page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/inbox?token=t&lang={lang}")
    page.locator(".ph-menu").first.wait_for(timeout=15000)
    page.locator(".ph-menu").first.tap()
    rows_at = page.locator(".ph-drawer-root.open .sb-pinned-nav [data-nav]")
    rows_at.first.wait_for(timeout=5000)
    rows = rows_at.evaluate_all("els => els.map(e => e.dataset.nav)")
    print(f"[{lang}] drawer: {rows}")
    if rows != ["new", "page:dosing", "board", "inbox", "calendar", "changes", "terminals", "menu"]:
        say(f"the drawer reads {rows}")
    expect(page.locator(".ph-drawer-root.open [data-nav='page:dosing']")).to_contain_text(words["page"])
    page.locator(".ph-drawer-root.open [data-nav='menu']").tap()
    # The sheet is the launcher: everything shown, what the drawer pins marked, nothing hidden.
    more = page.locator(".ph-more-sheet [data-screen]").evaluate_all("els => els.map(e => [e.dataset.screen, e.dataset.placed !== undefined])")
    marks = dict(more)
    if "usage" in marks or marks.get("board") is not True or marks.get("page:dosing") is not True or marks.get("schedules") is not False:
        say(f"the phone's More reads {more}")
    page.keyboard.press("Escape")

    # The section itself fits the phone, and lists no separate drawer: the sidebar's rows are the one list.
    page.goto(f"{BASE}/settings/navigation?token=t&lang={lang}")
    page.wait_for_selector("[data-nav-row='board']", timeout=10000)
    width = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    if width[0] > width[1]:
        say(f"Settings → Navigation scrolls sideways: {width}")
    if page.locator("[data-drawer-row], [data-drawer-list]").count():
        say("Settings → Navigation still lists a phone drawer of its own")
    page.context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ([LANG] if LANG else ["en", "ru"]):
            desktop(browser, lang, problems)
            phone(browser, lang, problems)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if problems:
        return 1
    print("navigation: every step held")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
