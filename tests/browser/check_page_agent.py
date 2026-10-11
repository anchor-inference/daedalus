"""A custom page with its own agent: the agent's conversation opens beside the page on a desktop and in
its place on a phone (the page staying loaded underneath), a button in the page asks the agent through
the bridge, what the agent pushes with PageEmit reaches the page and only that page, the agent's run is
forwarded, a new version waits behind a chip instead of reloading the page, and a page without an
agent gets one from the header.

    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_page_agent.py
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

from api_stub import DEFAULT_APP, expect_app
from event_feed import EventFeed
from pages_stub import PagesStub
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import S1, S2, UNHANDLED, detail, respond, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SESSIONS = {S1: "Bakery site", S2: "Bakery site: photos"}

WORDS = {
    "en": {"agent": "Agent: Bakery site", "none": "No agent", "new": "New agent for this page", "chip": "Version 3 is out"},
    "ru": {"agent": "Агент: Bakery site", "none": "Без агента", "new": "Новый агент для этой страницы", "chip": "Вышла версия 3"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def api_route(pages: PagesStub):  # type: ignore[no-untyped-def]
    """The pages' routes from their stub; the agent's session with its answers marked by run, so the
    replies a finished run hands the page can be told apart; everything else from the pictures' stub."""
    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        if r.request.method == "GET" and path.endswith(f"/api/sessions/{S1}"):
            body = detail(S1)
            body["messages"] = [{**m, "run_id": "r-page"} if m.get("role") == "assistant" else m for m in body["messages"]]
            return respond(r, body)
        if not pages.fulfil(r):
            stub(r)
    return route


def page_route(pages: PagesStub, app_root: str):  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        if "/api/" in r.request.url:
            r.fallback()
        else:
            pages.serve(r, app_root)
    return route


def open_page(browser, pages: PagesStub, feed: EventFeed, width: int, height: int, mobile: bool, language: str, app_root: str) -> Page:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    page.set_default_timeout(10000)
    page.route("**/api/**", api_route(pages))
    page.route("**/pages/**", page_route(pages, app_root))
    page.route("**/api/events**", feed.route)
    page.goto(f"{BASE}/p/orders?token=t&lang={language}")
    expect(page.frame_locator("[data-page-frame='orders']").locator("#ask")).to_be_visible()
    return page


def desktop(browser, language: str, app_root: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    where = f"{language} 1440px"
    pages = PagesStub(agent=S1, sessions=SESSIONS)
    feed = EventFeed()
    page = open_page(browser, pages, feed, 1440, 900, False, language, app_root)
    frame = page.frame_locator("[data-page-frame='orders']")
    expect(page.locator("[data-page-agent-name]")).to_have_text(words["agent"])
    expect(page.locator(".page-agent")).to_have_count(0)

    # The conversation opens beside the page: the page narrows, nothing is covered, nothing reloads.
    born = frame.locator("html").evaluate("() => window.bornAt")
    page.locator("[data-page-agent-toggle]").click()
    aside = page.locator(f".page-agent[data-page-agent='{S1}']")
    expect(aside.locator(".chat-title")).to_have_text("Bakery site")
    stage = page.locator(".page-host-stage").bounding_box()
    column = aside.bounding_box()
    assert stage and column and stage["x"] + stage["width"] <= column["x"] + 1, f"{where}: the agent covers the page ({stage}, {column})"
    assert 320 <= column["width"] <= 900 and stage["width"] >= 360, f"{where}: the page is {stage['width']}px and the agent {column['width']}px"
    assert frame.locator("html").evaluate("() => window.bornAt") == born, f"{where}: opening the agent reloaded the page"
    fits(page, where)

    # The page asks its agent through the bridge; the request reaches the page's own ask route with its file.
    frame.locator("#ask").click()
    expect(frame.locator("#asked")).to_have_text("run-page-1")
    raw = pages.asks[0].get("raw", "")
    assert "Optimise this mesh" in raw and 'filename="part.stl"' in raw, f"{where}: the ask carried {raw[:300]!r}"

    # What the agent pushes reaches this page; a push for another page does not.
    feed.send("pages.emit", {"slug": "elsewhere", "event": "scene.updated", "data": {"file": "wrong.glb"}})
    feed.send("pages.emit", {"slug": "orders", "event": "scene.updated", "data": {"file": "model.glb"}})
    expect(frame.locator("#emitted")).to_have_text(json.dumps({"file": "model.glb"}, separators=(",", ":")))
    # The agent's run is forwarded, and another session's is not.
    feed.send("run.started", {"run_id": "r-other", "origin": "operator", "title": "x"}, session=S2)
    feed.send("run.finished", {"run_id": "r-page", "status": "completed", "duration_s": 3.0, "watched": True, "origin": "page:orders", "operator_facing": True, "telegram": False, "title": "Bakery site", "summary": "Optimised."}, session=S1)
    expect(frame.locator("#run")).to_have_text("completed")

    # A new version waits behind a chip: the page is not reloaded under the operator, the agent stays.
    pages.publish("orders")
    feed.send("pages.changed", {"slug": "orders", "change": "published", "version": 3})
    chip = page.locator("[data-page-newversion]")
    expect(chip).to_contain_text(words["chip"])
    assert frame.locator("html").evaluate("() => window.bornAt") == born, f"{where}: the new version reloaded the page by itself"
    expect(aside).to_be_visible()
    chip.click()
    expect(chip).to_have_count(0)
    expect(frame.locator("#ask")).to_be_visible()
    assert frame.locator("html").evaluate("() => window.bornAt") != born, f"{where}: the chip did not reload the page"
    expect(aside).to_be_visible()

    # The page opens its agent itself; closing it is remembered for the page.
    page.locator("[data-page-agent-toggle]").click()
    expect(page.locator(".page-agent")).to_have_count(0)
    frame.locator("#talk").click()
    expect(page.locator(".page-agent")).to_be_visible()
    page.locator("[data-page-agent-toggle]").click()
    page.reload()
    expect(page.frame_locator("[data-page-frame='orders']").locator("#ask")).to_be_visible()
    expect(page.locator(".page-agent")).to_have_count(0)
    page.context.close()
    feed.close()
    print(f"{where}: the agent opened beside the page, the page asked it, a push and a run reached the page only, a new version waited behind the chip")


def unbound(browser, language: str, app_root: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    where = f"{language} 1440px, no agent"
    pages = PagesStub(sessions=SESSIONS, new_agent=S2)
    feed = EventFeed()
    page = open_page(browser, pages, feed, 1440, 900, False, language, app_root)
    frame = page.frame_locator("[data-page-frame='orders']")
    expect(page.locator("[data-page-agent-name]")).to_have_text(words["none"])
    expect(page.locator("[data-page-agent-toggle]")).to_have_count(0)
    frame.locator("#ask").click()
    expect(frame.locator("#asked")).to_contain_text("refused")
    page.locator(".page-agent-chip").click()
    page.locator(".menu [role='menuitem']", has_text=words["new"]).click()
    expect(page.locator(f".page-agent[data-page-agent='{S2}'] .chat-title")).to_have_text("Bakery site: photos")
    assert "POST /api/pages/orders/agent" in pages.requests, pages.requests
    page.context.close()
    feed.close()
    print(f"{where}: the page said it had no agent, and got a new one from the header")


def phone(browser, language: str, app_root: str) -> None:  # type: ignore[no-untyped-def]
    where = f"{language} 390px"
    pages = PagesStub(agent=S1, sessions=SESSIONS)
    feed = EventFeed()
    page = open_page(browser, pages, feed, 390, 844, True, language, app_root)
    frame = page.frame_locator("[data-page-frame='orders']")
    born = frame.locator("html").evaluate("() => window.bornAt")
    fits(page, where)
    page.locator("[data-page-agent-toggle]").click()
    aside = page.locator(".page-agent")
    expect(aside.locator(".chat-title")).to_have_text("Bakery site")
    box = aside.bounding_box()
    assert box and abs(box["width"] - 390) <= 2 and box["height"] > 600, f"{where}: the conversation is {box}"
    expect(page.locator(".page-host-stage")).to_be_hidden()
    fits(page, f"{where} with the agent")
    # A push while the conversation is shown still reaches the page underneath.
    feed.send("pages.emit", {"slug": "orders", "event": "scene.updated", "data": {"n": 1}})
    page.locator("[data-page-agent-toggle]").click()
    expect(aside).to_have_count(0)
    expect(frame.locator("#emitted")).to_have_text('{"n":1}')
    assert frame.locator("html").evaluate("() => window.bornAt") == born, f"{where}: switching to the agent and back reloaded the page"
    page.context.close()
    feed.close()
    print(f"{where}: the conversation took the page's place and gave it back as it was")


def run() -> int:
    expect_app(BASE)
    app_root = BASE.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            desktop(browser, language, app_root)
            unbound(browser, language, app_root)
            phone(browser, language, app_root)
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
