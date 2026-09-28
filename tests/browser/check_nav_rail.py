"""The rail at the left edge of a desktop, and the phone's tab bar, in both languages.

What is checked is what the operator asked for. On a desktop a narrow rail of icons is always there, except on Settings, which takes the window,
beside the sidebar or alone when the sidebar is folded: Home, Agents, Orchestration, Terminals, Board,
Inbox with its unseen count, Services, and at its foot the menu, Settings and the account. Every icon
names itself in a tooltip of its own. The two mode items choose the mode — there is no switch at the
top of the sidebar any more — and the count of what waits in orchestration sits quietly on its icon
while the operator is in Agents. With the sidebar folded, Home is the way to unfold it: pointed at, it
shows the sidebar's icon and "Toggle sidebar" with the shortcut, and a click unfolds; with the sidebar
open it goes home. Folded, nothing of the sidebar is left — the rail is the folded form.

On a 390 px phone there is no rail; the tab bar holds Agents, Orchestration, Terminals and Board, and
More, which now holds the Inbox and carries its unseen count. Orchestration opens its list, Main
first. Nothing scrolls sideways.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

ORDER = ["home", "agents", "orchestration", "terminals", "board", "inbox", "services", "menu", "settings", "account"]
WORDS = {
    "en": {"home": "Home", "agents": "Agents", "orchestration": "Orchestration", "terminals": "Terminals", "board": "Board", "inbox": "Inbox",
           "services": "Services", "menu": "Menu", "settings": "Settings", "account": "Account", "toggle": "Toggle sidebar", "unfold": "Chats", "unfold.projects": "Projects", "more": "More"},
    "ru": {"home": "Главная", "agents": "Агенты", "orchestration": "Оркестрация", "terminals": "Терминалы", "board": "Доска", "inbox": "Входящие",
           "services": "Сервисы", "menu": "Меню", "settings": "Настройки", "account": "Аккаунт", "toggle": "Показать боковую панель", "unfold": "Чаты", "unfold.projects": "Проекты", "more": "Ещё"},
}
# The unseen notifications the bell and the Inbox count, and what waits in orchestration: Bakery's one
# request and the main chat's own confirmation (the mirrored questions are Bakery's, counted there).
UNSEEN = 3
WAITING = "2"


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, lang: str) -> None:
    focus = FocusStub.bakery(lang)
    main = MainStub(lang)
    for ask in main.asks:
        if ask["project_id"] == "p-bakery":
            ask.update(project_id=PID, project_name="Bakery 2.0")

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        if path == "/api/notifications/summary":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"unseen": UNSEEN, "needs_you": 0}))
        answered = focus.answer(request.method, path, url.query, body) or main.answer(request.method, path, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return installation(route)

    page.route("**/api/**", handle)


def go(page: Page, path: str, lang: str) -> None:
    page.goto(f"{BASE}{path}{'&' if '?' in path else '?'}token=t&lang={lang}")


def tip(page: Page, item: str) -> str:
    """Point at an item and read its tooltip, which must be shown and inside the window."""
    target = page.locator(f".rail [data-rail='{item}']")
    target.hover()
    shown = target.locator(".rail-tip")
    expect(shown).to_be_visible()
    box = shown.bounding_box()
    width = page.viewport_size["width"] if page.viewport_size else 0
    assert box and box["x"] >= 52 and box["x"] + box["width"] <= width, f"the tooltip of {item} is outside the window: {box}"
    return shown.inner_text()


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    serve(page, lang)
    go(page, "/agents", lang)
    rail = page.locator("nav.rail")
    expect(rail).to_be_visible()

    # The items, top to bottom, each named in its tooltip; the rail is narrow and icons only.
    expect(rail.locator("[data-rail]")).to_have_count(len(ORDER))
    assert rail.locator("[data-rail]").evaluate_all("els => els.map(e => e.dataset.rail)") == ORDER
    box = rail.bounding_box()
    assert box and 48 <= box["width"] <= 56, f"{lang}: the rail is {box and box['width']} px wide"
    tops = [rail.locator(f"[data-rail='{k}']").bounding_box()["y"] for k in ORDER]  # type: ignore[index]
    assert tops == sorted(tops), f"{lang}: the items are not top to bottom: {tops}"
    for key in ORDER:
        said = tip(page, key)
        want = words[key] if key != "inbox" else f"{words['inbox']} · {UNSEEN}"
        assert said.startswith(want), f"{lang}: the tooltip of {key} says {said!r}, not {want!r}"

    # The badges: the Inbox's unseen count in red, and the quiet count of what waits in orchestration.
    expect(rail.locator("[data-rail='inbox'] .rail-badge")).to_have_text(str(UNSEEN))
    expect(rail.locator("[data-rail='inbox'] .rail-badge.quiet")).to_have_count(0)
    expect(rail.locator("[data-rail='orchestration'] .rail-badge.quiet")).to_have_text(WAITING)

    # The mode items are the switch; the sidebar has none of its own any more.
    expect(page.locator(".mode-switch, .mode-tab")).to_have_count(0)
    expect(rail.locator("[data-rail='agents']")).to_have_class(re.compile(r"\bon\b.*\bmode\b"))
    rail.locator("[data-rail='orchestration']").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    expect(rail.locator("[data-rail='orchestration']")).to_have_class(re.compile(r"\bon\b.*\bmode\b"))
    expect(rail.locator("[data-rail='agents']")).not_to_have_class(re.compile(r"\bmode\b"))
    expect(rail.locator("[data-rail='orchestration'] .rail-badge")).to_have_count(0)
    # A screen both modes share keeps the mode's column and its marker, and lights its own item.
    rail.locator("[data-rail='terminals']").click()
    page.wait_for_url("**/app/terminals")
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    expect(rail.locator("[data-rail='terminals']")).to_have_class(re.compile(r"\bon\b"))
    expect(rail.locator("[data-rail='orchestration']")).to_have_class(re.compile(r"\bmode\b"))
    # Home, with the sidebar open, goes to the mode's home.
    rail.locator("[data-rail='home']").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    rail.locator("[data-rail='agents']").click()
    page.wait_for_url("**/app/agents")
    expect(page.locator("nav.sidebar:not(.orch-sidebar)")).to_be_visible()
    assert page.evaluate("localStorage.getItem('daedalus.mode')") == "agents"
    for key, path in (("board", "/app/board"), ("inbox", "/app/inbox"), ("services", "/app/services")):
        rail.locator(f"[data-rail='{key}']").click()
        page.wait_for_url(f"**{path}")
        expect(rail.locator(f"[data-rail='{key}']")).to_have_class(re.compile(r"\bon\b"))
    # Settings takes the window: its own column, the section centred beside it, the rail stepped aside.
    rail.locator("[data-rail='settings']").click()
    page.wait_for_url("**/app/settings")
    expect(page.locator(".settings-stage")).to_be_visible()
    expect(rail).to_be_hidden()
    column = page.locator(".settings-col")
    centre = column.evaluate("el => { const box = el.getBoundingClientRect(); const parent = el.parentElement.getBoundingClientRect(); return Math.abs((box.left + box.right) / 2 - (parent.left + parent.right) / 2); }")
    assert centre <= 2, f"{lang}: the settings column is {centre}px off centre"
    page.locator(".settings-nav a[href$='/settings/security']").click()
    page.wait_for_url("**/app/settings/security")
    expect(page.locator("h1.settings-title")).to_have_text("Security" if lang == "en" else "Безопасность")
    page.locator(".settings-back").click()
    page.wait_for_url("**/app/agents")
    expect(rail).to_be_visible()
    # The menu opens from the rail's foot with everything else.
    rail.locator("[data-rail='menu']").click()
    expect(page.locator(".navmenu[role='menu']")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".navmenu")).to_have_count(0)
    go(page, "/agents", lang)
    fits(page, f"{lang} desktop, sidebar open")

    # Folded: the sidebar is gone, the rail alone is left, and the conversation starts where it ends.
    page.locator("nav.sidebar .sidebar-fold").click()
    expect(page.locator("nav.sidebar")).to_have_count(0)
    expect(rail).to_be_visible()
    assert round(page.locator(".main").bounding_box()["x"]) == round(rail.bounding_box()["width"])  # type: ignore[index]
    home = rail.locator("[data-rail='home']")
    expect(home).to_have_attribute("aria-label", words["toggle"])
    expect(home).to_have_attribute("aria-expanded", "false")
    page.mouse.move(700, 400)
    # Folded, Home is the unfold button without being pointed at: a tablet has no hover, so the
    # sidebar's icon and its word show at rest, and the word fits the rail uncut.
    expect(home.locator(".rail-unfold")).to_be_visible()
    expect(home.locator(".rail-unfold-word")).to_have_text(words["unfold"])
    cut = home.locator(".rail-unfold-word").evaluate("e => e.scrollWidth > e.clientWidth")
    assert not cut, f"{lang}: the folded Home's word is cut"
    expect(home.locator("img")).to_have_count(0)
    # In orchestration the folded sidebar is the projects, and the longer Russian word must fit as well.
    go(page, "/orchestration", lang)
    expect(home.locator(".rail-unfold-word")).to_have_text(words["unfold.projects"])
    cut = home.locator(".rail-unfold-word").evaluate("e => e.scrollWidth > e.clientWidth")
    assert not cut, f"{lang}: the folded Home's word in orchestration is cut"
    go(page, "/agents", lang)
    expect(page.locator("nav.sidebar")).to_have_count(0)
    # Pointed at, it adds its words and its shortcut.
    said = tip(page, "home")
    assert said.startswith(words["toggle"]) and "\\" in said, f"{lang}: the folded Home's tooltip says {said!r}"
    expect(home.locator(".rail-tip kbd")).to_have_count(1)
    home.click()
    expect(page.locator("nav.sidebar")).to_be_visible()
    assert page.url.split("?")[0].endswith("/app/agents"), f"{lang}: unfolding moved the page to {page.url}"
    expect(rail.locator("[data-rail='home']")).not_to_have_attribute("aria-expanded", "false")
    # The shortcut folds and unfolds as well, and the fold is remembered.
    page.mouse.move(700, 400)
    page.keyboard.press("Control+\\")
    expect(page.locator("nav.sidebar")).to_have_count(0)
    page.reload()
    expect(rail.locator(".rail-home.folded")).to_be_visible()
    expect(page.locator("nav.sidebar")).to_have_count(0)
    page.keyboard.press("Control+\\")
    expect(page.locator("nav.sidebar")).to_be_visible()
    fits(page, f"{lang} desktop")


def phone(page: Page, lang: str) -> None:
    words = WORDS[lang]
    serve(page, lang)
    go(page, "/agents", lang)
    bar = page.locator("nav.tabbar")
    expect(bar).to_be_visible()
    expect(page.locator("nav.rail")).to_have_count(0)
    assert bar.locator("a[data-screen]").evaluate_all("els => els.map(e => e.dataset.screen)") == ["agents", "orchestration", "terminals", "board"]
    expect(bar.locator("a[data-screen='inbox']")).to_have_count(0)
    for label in bar.locator(".tab-label").all():
        assert label.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), f"{lang} phone: a tab's label does not fit: {label.inner_text()}"
    expect(bar.locator("a[data-screen='orchestration'] .mode-count")).to_have_text(WAITING)
    # More holds the Inbox and carries its unseen count.
    more = bar.locator("button[aria-haspopup='dialog']")
    expect(more).to_contain_text(words["more"])
    expect(more.locator(".tab-badge")).to_have_text(str(UNSEEN))
    fits(page, f"{lang} phone")
    more.click()
    inbox = page.locator(".more-sheet .more-item[href$='/inbox']")
    expect(inbox).to_contain_text(words["inbox"])
    expect(inbox.locator(".tab-badge")).to_have_text(str(UNSEEN))
    inbox.click()
    page.wait_for_url("**/app/inbox")
    # Terminals is one tap away.
    bar.locator("a[data-screen='terminals']").click()
    page.wait_for_url("**/app/terminals")
    expect(bar.locator("a[data-screen='terminals']")).to_have_class(re.compile(r"\bactive\b"))
    # Orchestration opens its list, Main first; the main chat is not opened by it.
    bar.locator("a[data-screen='orchestration']").click()
    page.wait_for_url("**/app/orchestration/projects**")
    expect(page.locator(".orch-list > :first-child .main-entry")).to_be_visible()
    expect(bar.locator("a[data-screen='orchestration']")).to_have_class(re.compile(r"\bactive\b"))
    fits(page, f"{lang} phone orchestration")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            desktop(context.new_page(), lang)
            context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
            phone(context.new_page(), lang)
            context.close()
            print(f"nav rail {lang}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
