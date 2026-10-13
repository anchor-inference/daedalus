"""Settings → Navigation, and the rail, menu and drawer it rearranges, in a real browser.

On a 1440 × 900 desktop and a 390 px phone, in English and Russian:

- with nothing configured the rail is the one the app ships with, and the published page waits in the
  More menu under its own heading, drawn with its own glyph;
- in Settings → Navigation, moving Terminals up with its arrow saves the rail's new order with the
  settings revision, and the rail redraws in that order without a reload;
- dragging Board into the Hidden list hides it from the rail and the menu;
- choosing Rail for the page puts it on the rail, and a click opens /app/p/<slug> with the placeholder
  that names it;
- an icon picked for an item is drawn on the rail; Reset to default brings the shipped rail back;
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

SHIPPED = ["inbox", "board", "calendar", "diagrams", "changes", "terminals", "companion", "schedules"]
EMPTY = {"rail": [], "more": [], "drawer": [], "hidden": [], "icons": {}}
WORDS = {
    "en": {"page": "Dosing calculator", "pages": "Pages", "customise": "Customise…", "reset": "Reset to default", "rail": "Rail"},
    "ru": {"page": "Калькулятор дозировки", "pages": "Страницы", "customise": "Настроить…", "reset": "Сбросить по умолчанию", "rail": "Панель"},
}


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


def rail(page: Page) -> list[str]:
    return page.locator(".rail .rail-places [data-rail]").evaluate_all("els => els.map(e => e.dataset.rail)")


def wait_rail(page: Page, want: list[str], say, what: str) -> None:  # type: ignore[no-untyped-def]
    try:
        page.wait_for_function("want => JSON.stringify([...document.querySelectorAll('.rail .rail-places [data-rail]')].map(e => e.dataset.rail)) === JSON.stringify(want)", arg=want, timeout=5000)
    except Exception:  # noqa: BLE001 - the message below says what was there instead
        say(f"{what}: the rail reads {rail(page)}, not {want}")


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] desktop: {text}")

    host = Host()
    page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".rail .rail-places [data-rail]", timeout=15000)
    if rail(page) != SHIPPED:
        say(f"with nothing configured the rail reads {rail(page)}")

    # The page waits in the More menu, under its own heading, with its own glyph.
    page.locator(".rail [data-rail='menu']").click()
    entry = page.locator(".navmenu [data-nav-item='page:dosing']")
    expect(entry).to_contain_text(words["page"])
    expect(page.locator(".navmenu-section", has=page.locator("[data-nav-item='page:dosing']")).locator(".navmenu-label")).to_have_text(words["pages"])
    if entry.locator("svg.ic-custom path").get_attribute("d") != SAMPLE_PAGE["icon_path"]:
        say("the page's glyph is not drawn in the menu")
    # Customise opens the section.
    page.locator(".navmenu [data-nav='customise']").click()
    page.wait_for_url("**/settings/navigation**")
    page.wait_for_selector("[data-nav-row='terminals']", timeout=10000)

    # The rail is not drawn on Settings; its order is read from the host's copy and from the rail
    # once the operator leaves.
    page.locator("[data-nav-row='terminals']").get_by_role("button", name="Move Terminals up" if lang == "en" else "Выше: Терминалы").click()
    page.wait_for_timeout(600)
    moved = ["inbox", "board", "calendar", "diagrams", "terminals", "changes", "companion", "schedules"]
    last = host.puts[-1] if host.puts else {}
    print(f"[{lang}] after the arrow: {last.get('navigation', {}).get('rail')} at {last.get('base_revision')}")
    if last.get("navigation", {}).get("rail") != moved or last.get("base_revision") != "s1":
        say(f"moving Terminals up saved {last}")
    order = page.locator("[data-nav-list='rail'] [data-nav-row]").evaluate_all("els => els.map(e => e.dataset.navRow)")
    if order != moved:
        say(f"the Rail list reads {order}")

    # Board dragged into Hidden.
    page.locator("[data-nav-row='board']").drag_to(page.locator("[data-nav-list='hidden'] .nav-cfg-head"))
    page.wait_for_timeout(600)
    if "board" not in host.settings["navigation"]["hidden"]:
        say(f"dragging Board into Hidden left {host.settings['navigation']}")
    expect(page.locator("[data-nav-row='board']")).to_have_attribute("data-place", "hidden")

    # The page sent to the rail with its selector.
    page.locator("[data-nav-row='page:dosing'] select").select_option("rail")
    page.wait_for_timeout(600)
    expect(page.locator("[data-nav-row='page:dosing']")).to_have_attribute("data-place", "rail")

    # An icon for the inbox.
    page.locator("[data-nav-row='inbox'] .nav-cfg-icon").click()
    page.locator(".nav-cfg-icons [data-icon='bolt']").click()
    page.wait_for_timeout(600)
    if host.settings["navigation"]["icons"].get("inbox") != "bolt":
        say(f"the icon was saved as {host.settings['navigation']['icons']}")

    page.locator(".settings-back").click()
    page.wait_for_selector(".rail .rail-places [data-rail]", timeout=10000)
    want = ["inbox", "calendar", "diagrams", "terminals", "changes", "companion", "schedules", "page:dosing"]
    wait_rail(page, want, say, "after the changes")
    if page.locator(".rail [data-rail='inbox'] svg").get_attribute("class") != "ic ic-bolt":
        say("the inbox's new icon is not drawn on the rail")
    page.locator(".rail [data-rail='menu']").click()
    if page.locator(".navmenu [data-nav-item='board']").count():
        say("a hidden Board is still in the menu")
    page.keyboard.press("Escape")

    # The page opens from the rail at its own address; what the page screen draws is check_pages.py's.
    page.locator(".rail [data-rail='page:dosing']").click()
    page.wait_for_url("**/app/p/dosing**")
    expect(page.locator(".rail [data-rail='page:dosing']")).to_have_attribute("aria-current", "page")

    # Reset brings the shipped rail back.
    page.goto(f"{BASE}/settings/navigation?token=t&lang={lang}")
    page.get_by_role("button", name=words["reset"]).click()
    page.wait_for_timeout(600)
    if host.settings["navigation"] != EMPTY:
        say(f"Reset left {host.settings['navigation']}")
    page.locator(".settings-back").click()
    wait_rail(page, SHIPPED, say, "after Reset")
    page.context.close()


def phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]

    def say(text: str) -> None:
        problems.append(f"[{lang}] phone: {text}")

    host = Host()
    host.settings["navigation"]["drawer"] = ["page:dosing", "board", "inbox"]
    host.settings["navigation"]["hidden"] = ["usage"]
    page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/inbox?token=t&lang={lang}")
    page.locator(".ph-menu").first.wait_for(timeout=15000)
    page.locator(".ph-menu").first.tap()
    page.locator(".ph-nrow[data-screen]").first.wait_for(timeout=5000)
    rows = page.locator(".ph-nrow[data-screen]").evaluate_all("els => els.map(e => e.dataset.screen)")
    print(f"[{lang}] drawer: {rows}")
    if rows != ["page:dosing", "board", "inbox"]:
        say(f"the drawer reads {rows}")
    expect(page.locator(".ph-nrow[data-screen='page:dosing']")).to_contain_text(words["page"])
    page.locator("[data-nav='more']").tap()
    more = page.locator(".ph-more-sheet [data-screen]").evaluate_all("els => els.map(e => e.dataset.screen)")
    if "usage" in more or "board" in more or "terminals" not in more:
        say(f"the phone's More reads {more}")
    page.keyboard.press("Escape")

    # The section itself fits the phone.
    page.goto(f"{BASE}/settings/navigation?token=t&lang={lang}")
    page.wait_for_selector("[data-drawer-row='board']", timeout=10000)
    on = page.locator("[data-drawer-list='on'] [data-drawer-row]").evaluate_all("els => els.map(e => e.dataset.drawerRow)")
    if on != ["page:dosing", "board", "inbox"]:
        say(f"the drawer list in Settings reads {on}")
    width = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    if width[0] > width[1]:
        say(f"Settings → Navigation scrolls sideways: {width}")
    # Turning Terminals on puts it at the end of the drawer.
    page.locator("[data-drawer-row='terminals'] [role='switch']").tap()
    page.wait_for_timeout(600)
    if host.settings["navigation"]["drawer"] != ["page:dosing", "board", "inbox", "terminals"]:
        say(f"turning Terminals on saved {host.settings['navigation']['drawer']}")
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
