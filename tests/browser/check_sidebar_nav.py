"""The sidebar on a desktop, and the phone's drawer, in both languages.

What is checked is what the operator asked for. On a desktop the sidebar is the whole of the
navigation — there is no rail of icons beside it — 300 px wide, with a 52 px head (the mark, the mode
menu, search, the fold), the pinned rows (New chat, Inbox with its unseen count, Board, Calendar,
Changes, Terminals, More), the mode's list, and a 52 px foot (the account and Settings). Every row is
one 32 px line with its text 12 px in. The mode menu at the top chooses the mode and says how much
waits in orchestration while the operator is in Agents. Folded, nothing of the sidebar is left but a
button in the window's corner that brings it back; the shortcut folds and unfolds it, and the fold is
remembered.

On a 390 px phone there is no sidebar; every top bar's hamburger opens the drawer, which holds the
switch between the modes, the destinations and More. Orchestration opens its list, Main first.
Nothing scrolls sideways.
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
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app, switch_mode  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

ORDER = ["new", "inbox", "board", "calendar", "changes", "terminals", "menu"]
WORDS = {
    "en": {"new": "New chat", "agents": "Agents", "orchestration": "Orchestration", "terminals": "Terminals", "board": "Board", "inbox": "Inbox",
           "calendar": "Calendar", "changes": "Changes", "menu": "More", "settings": "Settings", "account": "Account", "unfold": "Show the sidebar", "mode": "Mode"},
    "ru": {"new": "Новый чат", "agents": "Агенты", "orchestration": "Оркестрация", "terminals": "Терминалы", "board": "Доска", "inbox": "Входящие",
           "calendar": "Календарь", "changes": "Изменения", "menu": "Ещё", "settings": "Настройки", "account": "Аккаунт", "unfold": "Показать боковую панель", "mode": "Режим"},
}
# The unseen notifications the Inbox counts, and what waits in orchestration: Bakery's one
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


def box(page: Page, selector: str) -> dict:
    found = page.locator(selector).first.bounding_box()
    assert found, f"{selector} is not drawn"
    return found


def mode_to(page: Page, lang: str, mode: str) -> None:
    """Choose a mode from the menu at the top of the sidebar."""
    page.locator(".sidebar-head .sb-mode").click()
    page.get_by_role("menuitem", name=re.compile(rf"^{WORDS[lang][mode]}")).click()


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    serve(page, lang)
    go(page, "/agents", lang)
    side = page.locator("nav.sidebar")
    expect(side).to_be_visible()
    expect(page.locator(".rail, [data-rail]")).to_have_count(0)

    # The geometry the design draws: 300 px, a 52 px head and foot, 32 px rows with their text 12 px in.
    column = box(page, ".desktop-column")
    assert column["x"] == 0 and column["width"] == 300, f"{lang}: the sidebar is {column}, not 300 px at the left edge"
    assert round(box(page, ".main")["x"]) == 300, f"{lang}: the screen does not start where the sidebar ends"
    assert round(box(page, ".sidebar-head")["height"]) == 52 and round(box(page, ".sidebar-foot")["height"]) == 52, f"{lang}: the head or the foot is not 52 px"
    pinned = side.locator(".sb-pinned-nav [data-nav]")
    order = pinned.evaluate_all("els => els.map(e => e.dataset.nav)")
    assert order == ORDER, f"{lang}: the pinned rows are {order}"
    for row in side.locator(".sb-pinned-nav .sb-nav").all():
        b = row.bounding_box()
        # 8 px in from either side of the 299 px inside the column's right-hand line, as the design draws it.
        assert b and round(b["height"]) == 32 and round(b["x"]) == 8 and round(b["width"]) == 283, f"{lang}: a pinned row is {b}"
    for key in ORDER:
        row = side.locator(f".sb-pinned-nav [data-nav='{key}']")
        said = (row.get_attribute("aria-label") or row.inner_text()).strip()
        assert said.startswith(words[key]), f"{lang}: {key} is not named: {said!r}"
        text = row.locator(".sb-nav-t")
        tb, rb = text.bounding_box(), side.locator(f".sb-nav:has([data-nav='{key}']), .sb-nav[data-nav='{key}']").first.bounding_box()
        assert tb and rb and round(tb["x"] - rb["x"]) == 38, f"{lang}: {key}'s name starts {tb and rb and tb['x'] - rb['x']} px in, not 12 + 16 + 10"

    # The Inbox's unseen count in the attention colour; what waits in orchestration is a dot on the
    # mode's name and a count in its menu.
    expect(side.locator("[data-nav='inbox'] .sb-nav-n.urgent")).to_have_text(str(UNSEEN))
    expect(side.locator(".sb-mode")).to_have_attribute("data-mode", "agents")
    expect(side.locator(".sb-mode .sb-mode-pip")).to_have_count(1)
    side.locator(".sb-mode").click()
    expect(page.get_by_role("menuitem", name=re.compile(rf"^{words['orchestration']}")).locator(".sb-mode-wait")).to_have_text(WAITING)
    page.keyboard.press("Escape")
    mode_to(page, lang, "orchestration")
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    expect(page.locator("nav.orch-sidebar .sb-mode")).to_have_attribute("data-mode", "orchestration")
    expect(page.locator("nav.orch-sidebar .sb-mode-pip")).to_have_count(0)
    # A screen both modes share keeps the mode's column, and lights its own row.
    page.locator(".sb-pinned-nav [data-nav='terminals']").click()
    page.wait_for_url("**/app/terminals")
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    expect(page.locator(".sb-pinned-nav [data-nav='terminals']")).to_have_class(re.compile(r"\bon\b"))
    # The mark goes to the mode's home.
    page.locator(".sidebar-head .sb-mark").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    mode_to(page, lang, "agents")
    page.wait_for_url("**/app/agents")
    expect(page.locator("nav.sidebar:not(.orch-sidebar)")).to_be_visible()
    assert page.evaluate("localStorage.getItem('daedalus.mode')") == "agents"
    for key, path in (("board", "/app/board"), ("inbox", "/app/inbox"), ("calendar", "/app/calendar")):
        page.locator(f".sb-pinned-nav [data-nav='{key}']").click()
        page.wait_for_url(f"**{path}")
        expect(page.locator(f".sb-pinned-nav [data-nav='{key}']")).to_have_class(re.compile(r"\bon\b"))
    # Settings is a dialog over the screen it was opened from: the sidebar and the screen stay under
    # it, and closing it goes back there however many sections were opened inside it.
    page.locator(".sidebar-foot [data-nav='settings']").click()
    page.wait_for_url("**/app/settings")
    dialog = page.locator(".settings-dialog[role='dialog'][aria-modal='true']")
    expect(dialog).to_be_visible()
    expect(side).to_be_visible()
    assert round(box(page, ".desktop-column")["width"]) == 300, f"{lang}: the sidebar changed width under Settings"
    page.locator(".settings-nav a[href$='/settings/security']").click()
    page.wait_for_url("**/app/settings/security")
    expect(page.locator("h1.settings-title")).to_have_text("Security" if lang == "en" else "Безопасность")
    page.locator(".settings-close").click()
    page.wait_for_url(re.compile(r"/app/calendar(/|\?|$)"))
    expect(dialog).to_be_hidden()
    # The account is Settings' security section.
    page.locator(".sidebar-foot [data-nav='account']").click()
    page.wait_for_url("**/app/settings/security")
    page.locator(".settings-close").click()
    expect(dialog).to_be_hidden()
    page.locator(".sb-pinned-nav [data-nav='new']").click()
    page.wait_for_url("**/app/agents")
    expect(page.locator(".sb-pinned-nav .sb-newchat")).to_have_class(re.compile(r"\bon\b"))
    # More opens beside its row with everything else.
    page.locator(".sb-pinned-nav [data-nav='menu']").click()
    expect(page.locator(".navmenu[role='dialog']")).to_be_visible()
    menu_box = box(page, ".navmenu")
    trigger_box = box(page, ".sb-pinned-nav [data-nav='menu']")
    assert abs(menu_box["x"] - trigger_box["x"] - trigger_box["width"] - 6) <= 1, "More must open beside its own row"
    assert menu_box["y"] >= 8 and menu_box["y"] + menu_box["height"] <= page.viewport_size["height"] - 7  # type: ignore[index]
    field_box = box(page, ".start-composer")
    main_box = box(page, ".main")
    assert field_box["x"] >= main_box["x"] and field_box["x"] + field_box["width"] <= main_box["x"] + main_box["width"] + 1, "Opening More must not displace the composer"
    page.keyboard.press("Escape")
    expect(page.locator(".navmenu")).to_have_count(0)
    go(page, "/agents", lang)
    fits(page, f"{lang} desktop, sidebar open")

    # Folded: the sidebar is gone, the screen has the window, and one button in the corner brings it back.
    page.locator("nav.sidebar .sidebar-fold").click()
    expect(page.locator("nav.sidebar")).to_have_count(0)
    unfold = page.locator(".sidebar-unfold")
    expect(unfold).to_be_visible()
    assert round(box(page, ".main")["x"]) == 0, f"{lang}: folded, the screen does not start at the window's edge"
    corner = box(page, ".sidebar-unfold")
    assert corner["x"] <= 12 and corner["y"] <= 12 and round(corner["width"]) == 32, f"{lang}: the unfold button is {corner}"
    expect(unfold).to_have_attribute("aria-label", words["unfold"])
    expect(unfold).to_have_attribute("aria-expanded", "false")
    # A header under the button moves its title over by the button's width.
    go(page, "/inbox", lang)
    expect(page.locator("nav.sidebar")).to_have_count(0)
    title = box(page, ".pagehead-title")
    assert title["x"] >= corner["x"] + corner["width"] + 4, f"{lang}: the folded header's title {title} sits under the unfold button {corner}"
    unfold.hover()
    expect(page.locator(".tooltip").filter(has_text=words["unfold"])).to_be_visible()
    unfold.click()
    expect(page.locator("nav.sidebar")).to_be_visible()
    assert page.url.split("?")[0].endswith("/app/inbox"), f"{lang}: unfolding moved the page to {page.url}"
    # The shortcut folds and unfolds as well, and the fold is remembered.
    page.mouse.move(700, 400)
    page.keyboard.press("Control+\\")
    expect(page.locator("nav.sidebar")).to_have_count(0)
    page.reload()
    expect(page.locator(".sidebar-unfold")).to_be_visible()
    expect(page.locator("nav.sidebar")).to_have_count(0)
    page.keyboard.press("Control+\\")
    expect(page.locator("nav.sidebar")).to_be_visible()
    fits(page, f"{lang} desktop")


def phone(page: Page, lang: str) -> None:
    """A phone has no bottom bar and no sidebar beside the screen: every top bar's hamburger opens the
    drawer, which is the desktop's sidebar — the same head with the mode menu, the same pinned rows in
    the same order, the same foot. Escape, Back and the scrim close it, focus goes back to the
    hamburger, and a destination takes the drawer's history entry."""
    words = WORDS[lang]
    serve(page, lang)
    go(page, "/agents", lang)
    expect(page.locator("nav.tabbar")).to_have_count(0)
    expect(page.locator("nav.sidebar:visible")).to_have_count(0)
    menu = page.locator(".ph-top .ph-menu")
    expect(menu).to_be_visible()
    fits(page, f"{lang} phone")
    menu.click()
    drawer = page.locator(".ph-drawer-root.open .ph-drawer")
    expect(drawer).to_be_visible()
    side = drawer.locator("nav.sidebar")
    expect(side.locator(".sidebar-head .sb-mode")).to_contain_text(words["agents"])
    order = side.locator(".sb-pinned-nav [data-nav]").evaluate_all("els => els.map(e => e.dataset.nav)")
    assert order == ORDER, f"{lang} phone: the drawer's pinned rows are {order}, not the desktop's {ORDER}"
    heights = side.locator(".sb-pinned-nav .sb-nav").evaluate_all("els => els.map(e => Math.round(e.getBoundingClientRect().height))")
    assert set(heights) == {44}, f"{lang} phone: the drawer's rows are {heights}, not 44"
    for label in side.locator(".sb-nav-t").all():
        assert label.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), f"{lang} phone: a destination's name does not fit: {label.inner_text()}"
    expect(side.locator("[data-nav='inbox']")).to_contain_text(words["inbox"])
    expect(side.locator("[data-nav='inbox'] .sb-nav-n.urgent")).to_have_text(str(UNSEEN))
    expect(side.locator(".sidebar-foot [data-nav='settings']")).to_be_visible()
    fits(page, f"{lang} phone drawer")
    # Escape closes it and gives focus back to the hamburger.
    page.keyboard.press("Escape")
    expect(page.locator(".ph-drawer-root.open")).to_have_count(0)
    assert page.evaluate("() => document.activeElement && document.activeElement.classList.contains('ph-menu')"), f"{lang} phone: focus did not return to the hamburger"
    # Back closes it without leaving the page, and so does its own close button.
    menu.click()
    expect(page.locator(".ph-drawer-root.open")).to_have_count(1)
    page.go_back()
    expect(page.locator(".ph-drawer-root.open")).to_have_count(0)
    assert page.url.split("?")[0].endswith("/app/agents"), page.url
    menu.click()
    page.locator(".ph-drawer-root.open .sidebar-close").click()
    expect(page.locator(".ph-drawer-root.open")).to_have_count(0)
    assert page.url.split("?")[0].endswith("/app/agents"), page.url
    # A destination replaces the drawer's history entry: Back from it returns to the page under it.
    menu.click()
    page.locator(".ph-drawer-root.open [data-nav='inbox']").click()
    page.wait_for_url("**/app/inbox")
    expect(page.locator(".ph-drawer-root.open")).to_have_count(0)
    page.go_back()
    page.wait_for_url("**/app/agents**")
    expect(page.locator(".ph-drawer-root.open")).to_have_count(0)
    # Terminals is two taps away from anywhere, and the drawer marks where the reader is.
    page.locator(".ph-menu:visible").first.click()
    page.locator(".ph-drawer-root.open [data-nav='terminals']").click()
    page.wait_for_url("**/app/terminals")
    page.locator(".ph-menu:visible").first.click()
    expect(page.locator(".ph-drawer-root.open [data-nav='terminals']")).to_have_class(re.compile(r"\bon\b"))
    # The mode menu goes to orchestration's home, and the drawer there lists Main and the projects.
    switch_mode(page, "orchestration", scope=".ph-drawer-root.open")
    page.wait_for_url("**/app/orchestration**")
    expect(page.locator(".ph-drawer-root.open")).to_have_count(0)
    page.locator(".ph-menu:visible").first.click()
    expect(page.locator(".ph-drawer-root.open .orch-sidebar .orch-main")).to_be_visible()
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
            print(f"sidebar nav {lang}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
