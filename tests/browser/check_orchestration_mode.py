"""Orchestration as a mode of its own, at 1440 px and on a 390 px phone, in both languages.

What is checked is what the operator asked for. The two modes are the rail's Agents and
Orchestration items (on a phone, the first two tabs); the mode is remembered by the device and has
addresses of its own. On a desktop, switching into orchestration opens the main orchestrator's chat
at once, and the column then holds Main and only the projects with an orchestrator, each with what it
is doing, its team and how many requests wait; on a phone it opens that list, Main first. Agents mode
holds none of them — not the project, not its orchestrator or staff, not the main chat — and says
only, as one quiet count on the rail's Orchestration icon, how many requests wait over there. A project opens in its focus mode and its way back returns to orchestration's home. Old links
(the host's notifications, Telegram, a plain session address) land in orchestration mode; Terminals
stays reachable from both; the palette and the `g` keys work in both. Nothing scrolls sideways.
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
from api_stub import DEFAULT_APP, MAIN_SID, FocusStub, MainStub, expect_app, open_drawer  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID, GARDEN = "b4k3ry20f0c5", "9a4d3e2f1c0b"

WORDS = {
    "en": {"agents": "Agents", "orchestration": "Orchestration", "staff": "6 staff", "working": "4 working", "open": "Open project Bakery 2.0", "notice": "Bakery 2.0: the board changed", "board": "Board"},
    "ru": {"agents": "Агенты", "orchestration": "Оркестрация", "staff": "6 сотрудников", "working": "4 работают", "open": "Открыть проект Bakery 2.0", "notice": "Bakery 2.0: доска изменилась", "board": "Доска"},
}

# What waits for the operator: the one request of Bakery (its `needs_you`), and the main chat's own
# confirmation of a project that does not exist yet. The two questions the main chat mirrors from
# Bakery are Bakery's, and are counted once, there.
WAITING = "2"


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def notice(lang: str) -> dict:
    """A notification the host wrote before orchestration mode existed: its link is the old address."""
    return {
        "id": 77, "at": "2026-09-25T10:00:00Z", "updated_at": "2026-09-25T10:00:00Z", "category": "project_report", "kind": "project_report", "level": "normal", "tone": "info",
        "title": WORDS[lang]["notice"], "body": "", "link": f"/app/project/{PID}/board", "session_id": None, "run_id": None, "project_id": PID, "staff_id": None, "terminal_id": None,
        "source": "check", "dedupe_key": None, "request_ref": None, "count": 1, "actions": [], "seen": False, "resolved": None, "needs_you": False, "delivered": {},
    }


def stubs(lang: str) -> tuple[FocusStub, MainStub]:
    # An ordinary chat in the project without an orchestrator: the Agents list's to show.
    notes = {"id": "sess-notes", "title": "Seed catalogue", "status": "idle", "created_at": "2026-09-25T08:00:00Z", "last_message_at": "2026-09-25T09:58:00Z", "run_id": None, "model": "Claude Opus 5", "metadata": {}, "project_id": GARDEN, "project": "Garden"}
    focus = FocusStub.bakery(lang, other_sessions=[notes])
    garden = next(p for p in focus.listing["projects"] if p["id"] == GARDEN)
    garden.update(total=1, last_message_at=notes["last_message_at"])
    focus.details["sess-notes"] = FocusStub.session_detail("sess-notes", notes["title"], focus.project(GARDEN), [])
    main = MainStub(lang)
    for ask in main.asks:
        if ask["project_id"] == "p-bakery":
            ask.update(project_id=PID, project_name="Bakery 2.0")
    return focus, main


def serve(page: Page, focus: FocusStub, main: MainStub, lang: str) -> None:
    entry = notice(lang)

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        if path == "/api/notifications/summary":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"unseen": 0 if entry["seen"] else 1, "needs_you": 0}))
        if path == "/api/notifications" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"entries": [entry], "next_before": None, "summary": {"unseen": 1, "needs_you": 0}}))
        if path == "/api/notifications/seen":
            entry["seen"] = True
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"marked": 1, "summary": {"unseen": 0, "needs_you": 0}}))
        answered = focus.answer(request.method, path, url.query, body) or main.answer(request.method, path, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return installation(route)

    page.route("**/api/**", handle)


def go(page: Page, path: str, lang: str) -> None:
    joiner = "&" if "?" in path else "?"
    page.goto(f"{BASE}{path}{joiner}token=t&lang={lang}")


def keys(page: Page, *letters: str) -> None:
    """Typed with nothing focused that takes text: the `g` keys ignore a field that has the focus."""
    page.evaluate("document.activeElement && document.activeElement.blur()")
    for letter in letters:
        page.keyboard.press(letter)


def desktop(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus, main = stubs(lang)
    serve(page, focus, main, lang)
    go(page, "/agents", lang)

    # Agents mode: the rail's mode items, nothing of orchestration in the column, one quiet count.
    side = page.locator("nav.sidebar")
    rail = page.locator("nav.rail")
    expect(page.locator(".mode-switch")).to_have_count(0)
    expect(rail.locator("[data-rail='agents']")).to_have_class(re.compile(r"\bon\b"))
    expect(rail.locator("[data-rail='agents']")).to_have_attribute("aria-label", words["agents"])
    expect(rail.locator("[data-rail='orchestration'] .rail-badge.quiet")).to_have_text(WAITING)
    expect(side.locator(f".folder[data-project='{GARDEN}']")).to_be_visible()
    expect(side.locator(f"[data-project='{PID}']")).to_have_count(0)
    expect(side.locator(".main-entry, .main-entry-strip")).to_have_count(0)
    expect(side.locator(".erow", has_text="Orchestrator")).to_have_count(0)
    expect(side.locator(".erow[data-session='sess-lev']")).to_have_count(0)
    fits(page, f"{lang} agents mode")

    # Into orchestration: the main chat at once, and a column of Main and the orchestrated projects.
    rail.locator("[data-rail='orchestration']").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator(".main-flow")).to_be_visible()
    expect(page.locator(".chat .chat-title")).to_have_text("Main")
    assert ("/api/main", {}) in main.posts, "switching in opens the main chat's session"
    orch = page.locator("nav.orch-sidebar")
    expect(orch).to_be_visible()
    expect(rail.locator("[data-rail='orchestration']")).to_have_class(re.compile(r"\bon\b"))
    expect(rail.locator(".rail-badge.quiet")).to_have_count(0)
    expect(orch.locator(".orch-main .main-entry")).to_have_class(re.compile(r"\bcurrent\b"))
    rows = orch.locator(".orch-row")
    expect(rows).to_have_count(1)
    row = rows.first
    expect(row).to_have_attribute("data-project", PID)
    expect(row.locator(".orch-row-meta")).to_contain_text(words["staff"])
    expect(row.locator(".orch-row-meta")).to_contain_text(words["working"])
    expect(row.locator(".needs-badge")).to_have_text("1")
    expect(orch.locator(f"[data-project='{GARDEN}']")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('daedalus.mode')") == "orchestration"
    fits(page, f"{lang} orchestration home")

    # Remembered by the device: a bare /app opens orchestration again, at the main chat on a desktop.
    go(page, "/", lang)
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator("nav.orch-sidebar")).to_be_visible()

    # A project opens in its focus mode, and its way back is orchestration's home.
    page.locator("nav.orch-sidebar .orch-row").first.click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}")
    expect(page.locator("nav.project-sidebar")).to_be_visible()
    page.locator("nav.project-sidebar .focus-back").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator("nav.orch-sidebar")).to_be_visible()

    # Terminals is a screen of both modes: reached with `g t`, it keeps the column of the mode it was
    # opened from. `g a` and `g o` move between the modes.
    keys(page, "g", "t")
    page.wait_for_url("**/app/terminals")
    expect(page.locator("nav.orch-sidebar")).to_be_visible()
    expect(rail.locator("[data-rail='orchestration']")).to_have_class(re.compile(r"\bmode\b"))
    expect(rail.locator("[data-rail='terminals']")).to_have_class(re.compile(r"\bon\b"))
    keys(page, "g", "a")
    page.wait_for_url("**/app/agents")
    expect(rail.locator("[data-rail='agents']")).to_have_class(re.compile(r"\bon\b"))
    # The address can change before the destination finishes rendering; wait for the Agents column
    # before sending another shortcut from that screen.
    expect(side.locator(f".folder[data-project='{GARDEN}']")).to_be_visible()
    keys(page, "g", "t")
    page.wait_for_url("**/app/terminals")
    expect(page.locator("nav.sidebar")).to_be_visible()
    expect(rail.locator("[data-rail='agents']")).to_have_class(re.compile(r"\bmode\b"))
    keys(page, "g", "o")
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))

    # The palette from Agents mode opens an orchestrated project in orchestration mode.
    go(page, "/agents", lang)
    expect(page.locator("nav.sidebar")).to_be_visible()
    page.keyboard.press("Control+k")
    page.locator(".palette-sheet input").fill("Bakery")
    page.locator(".palette-row", has_text=words["open"]).first.click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}")
    expect(page.locator("nav.project-sidebar")).to_be_visible()

    # Old addresses: a project's page, the main chat, and sessions named by their plain address.
    go(page, f"/project/{PID}/board", lang)
    page.wait_for_url(f"**/app/orchestration/project/{PID}/board**")
    expect(page.locator("nav.project-sidebar")).to_be_visible()
    go(page, "/main", lang)
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator(".main-flow")).to_be_visible()
    go(page, "/agents/orch-bakery", lang)
    page.wait_for_url(re.compile(rf"/app/orchestration/project/{PID}(\?|$)"))
    expect(page.locator(".chat.in-project.orchestrator")).to_be_visible()
    go(page, "/agents/sess-lev", lang)
    page.wait_for_url(f"**/app/orchestration/project/{PID}/s/sess-lev**")
    go(page, f"/agents/{MAIN_SID}", lang)
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    # An ordinary chat stays where it is.
    go(page, "/agents/sess-notes", lang)
    page.wait_for_timeout(600)
    assert "/app/agents/sess-notes" in page.url, page.url

    # A notification the host linked the old way, opened from the bell in Agents mode.
    go(page, "/agents", lang)
    page.locator("nav.sidebar .bell").click()
    page.locator(".bell-pop .notice-row[data-notice='77']").first.click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}/board**")
    expect(page.locator("nav.project-sidebar")).to_be_visible()


def phone(page: Page, lang: str) -> None:
    words = WORDS[lang]
    focus, main = stubs(lang)
    serve(page, focus, main, lang)
    go(page, "/agents", lang)

    # No bottom bar: the drawer carries both modes as a switch, the count waiting quietly on
    # Orchestration's side; Agents mode lists its own projects and none with an orchestrator.
    expect(page.locator("nav.tabbar")).to_have_count(0)
    expect(page.locator(".ph-home .main-entry")).to_have_count(0)
    open_drawer(page)
    drawer = page.locator(".ph-drawer-root.open")
    seg = drawer.locator(".ph-seg button")
    expect(seg.nth(0)).to_have_attribute("aria-checked", "true")
    expect(seg.nth(1)).to_contain_text(words["orchestration"])
    expect(seg.nth(1).locator("[data-waiting]")).to_have_text(WAITING)
    expect(drawer.locator(f"[data-project='{PID}']")).to_have_count(0)
    expect(drawer.locator(f"[data-project='{GARDEN}']")).to_be_visible()
    fits(page, f"{lang} phone agents")

    # One tap into orchestration's lists: Main first, then the projects; "See all" is the list the
    # desktop's column holds, as a page.
    seg.nth(1).click()
    expect(seg.nth(1)).to_have_attribute("aria-checked", "true")
    expect(drawer.locator("[data-nav='main']")).to_be_visible()
    expect(drawer.locator(f"[data-project='{PID}'] .ph-badge")).to_have_text("1")
    drawer.locator("[data-nav='orchestration-list']").click()
    page.wait_for_url("**/app/orchestration/projects**")
    listing = page.locator(".orch-list")
    expect(listing.locator("> :first-child .main-entry")).to_be_visible()
    # Main opens the chat as a detail of the list: no bar, and its back returns to the list.
    listing.locator(".main-entry").click()
    page.wait_for_url(re.compile(r"/app/orchestration(\?|$)"))
    expect(page.locator(".timeline > .questions-line")).to_be_visible()
    expect(page.locator("nav.tabbar")).to_have_count(0)
    fits(page, f"{lang} phone main chat")
    page.locator(".chat-head > button.iconbtn, .chat-head > .ph-ib").first.click()
    page.wait_for_url("**/app/orchestration/projects**")
    expect(listing.locator(".main-entry")).to_be_visible()
    # Remembered by the phone too: a bare /app opens orchestration at its list.
    go(page, "/", lang)
    page.wait_for_url("**/app/orchestration/projects**")
    expect(listing.locator(".main-entry")).to_be_visible()
    expect(listing.locator(".orch-row")).to_have_count(1)
    expect(listing.locator(".orch-row .needs-badge")).to_have_text("1")
    fits(page, f"{lang} phone orchestration list")
    listing.locator(".orch-row").first.click()
    page.wait_for_url(re.compile(rf"/app/orchestration/project/{PID}(\?|$)"))
    project_tabs = page.locator("nav.tabbar.project-tabs > a, nav.tabbar.project-tabs > button")
    expect(project_tabs).to_have_count(5)
    assert project_tabs.evaluate_all("els => els.map(e => e.dataset.tab)") == ["orchestrator", "board", "team", "terminals", "more"]
    expect(project_tabs.nth(0)).to_have_class(re.compile(r"\bactive\b"))
    go(page, f"/orchestration/project/{PID}/team", lang)
    # The board is a tab of its own, and the project's other pages are in More's sheet.
    expect(page.locator("nav.tabbar.project-tabs a[data-tab='board']")).to_have_text(words["board"])
    page.locator("nav.tabbar.project-tabs button[data-tab='more']").click()
    expect(page.locator(".sheet.more-sheet .more-item[data-more='journal']")).to_be_visible()
    page.keyboard.press("Escape")
    # The project's hamburger opens the same drawer, in orchestration mode, with the project marked.
    page.locator(".pagehead .ph-menu").first.click()
    expect(drawer.locator(".ph-seg button").nth(1)).to_have_attribute("aria-checked", "true")
    expect(drawer.locator(f"[data-project='{PID}']")).to_have_class(re.compile(r"\bon\b"))

    # And one tap back to Agents: a new chat is Agents' home.
    drawer.locator(".ph-seg button").nth(0).click()
    drawer.locator(".ph-newchat").click()
    page.wait_for_url("**/app/agents**")
    expect(page.locator(".ph-home")).to_be_visible()

    # A notification with the old link, opened from the Inbox.
    go(page, "/inbox", lang)
    # The Inbox's card of an unread entry opens its link at once.
    page.locator(".notice-row[data-notice='77']").first.click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}/board**")
    expect(page.locator(".phone-board .pboard")).to_be_visible()
    expect(page.locator("nav.tabbar.project-tabs .active")).to_have_attribute("data-tab", "board")
    fits(page, f"{lang} phone deep link")


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
            print(f"orchestration mode {lang}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
