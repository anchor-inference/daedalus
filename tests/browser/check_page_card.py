"""A page an agent showed the operator (PageShow), as a card in the conversation, in a real browser.

The turn showed two pages: ``orders``, which the host has at version 2, and ``gone``, which was
removed since. The card for ``orders`` carries the page's title in the reader's language and the
version that opens; Open goes to the page's screen, where the page renders in its frame. The card
for ``gone`` says the page was removed and offers nothing to open. Desktop and phone, English and
Russian; nothing scrolls sideways.

    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_page_card.py

SHOTS=<directory> also saves the pictures this looks at. Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from pages_stub import PagesStub  # noqa: E402
from screenshots import S1, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

WORDS = {
    "en": {"title": "Orders", "meta": "Page · version 2", "open": "Open", "gone": "This page has been removed"},
    "ru": {"title": "Заказы", "meta": "Страница · версия 2", "open": "Открыть", "gone": "Эта страница удалена"},
}


def turn_messages() -> list[dict]:
    calls = [{"id": "p1", "name": "PageShow", "arguments": {"slug": "orders"}}, {"id": "p2", "name": "PageShow", "arguments": {"slug": "gone"}}]
    results = [{"id": c["id"], "content": f"Shown {c['arguments']['slug']!r} to the operator: a card with Open is in this chat.", "is_error": False} for c in calls]
    return [
        {"role": "user", "seq": 701, "text": "Make me a page for the bakery's orders.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=6)},
        {"role": "assistant", "seq": 702, "text": "", "thinking": "", "tool_calls": calls, "tool_results": [], "created_at": ago(minutes=5)},
        {"role": "tool", "seq": 703, "text": "", "thinking": "", "tool_calls": [], "tool_results": results, "created_at": ago(minutes=4)},
        {"role": "assistant", "seq": 704, "text": "The orders page is published: [Orders](/app/p/orders).", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
    ]


def routes(page: Page, pages: PagesStub, app_root: str) -> None:
    def api(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        if rel == f"/api/sessions/{S1}" and r.request.method == "GET":
            return respond(r, {**detail(S1), "messages": turn_messages()})
        if pages.fulfil(r):
            return None
        return stub(r)

    def own(r) -> None:  # type: ignore[no-untyped-def]
        if "/api/" in r.request.url:
            r.fallback()
        else:
            pages.serve(r, app_root)

    page.route("**/api/**", api)
    page.route("**/pages/**", own)


def run() -> int:
    expect_app(BASE)
    app_root = BASE.rstrip("/")
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            words = WORDS[language]
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                where = f"{language} {width}px"
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile, color_scheme="dark")
                page = context.new_page()
                page.set_default_timeout(10000)
                routes(page, PagesStub(), app_root)
                page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={language}")
                page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
                # The answer's link to the page is a link, like a diagram's.
                expect(page.locator(".answer a[href='/app/p/orders']")).to_have_count(1)
                cards = page.locator(".page-card")
                expect(cards).to_have_count(2)
                orders = page.locator(".page-card[data-page='orders']")
                expect(orders.locator(".artifact-name")).to_have_text(words["title"])
                expect(orders.locator(".artifact-meta")).to_have_text(words["meta"])
                expect(orders.locator(".page-card-open")).to_have_text(words["open"])
                gone = page.locator(".page-card[data-page='gone']")
                expect(gone.locator(".artifact-meta")).to_have_text(words["gone"])
                expect(gone.locator(".page-card-open")).to_be_disabled()
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                if overflow > 0:
                    problems.append(f"{where}: the chat scrolls sideways by {overflow}px with the cards")
                box = orders.bounding_box()
                if box is None or box["x"] < 0 or box["x"] + box["width"] > width:
                    problems.append(f"{where}: the card is outside the window: {box}")
                if SHOTS:
                    Path(SHOTS).mkdir(parents=True, exist_ok=True)
                    orders.scroll_into_view_if_needed()
                    page.screenshot(path=str(Path(SHOTS) / f"page-card-{language}-{width}.png"))
                orders.locator(".page-card-open").click()
                page.wait_for_url("**/app/p/orders**")
                expect(page.frame_locator("[data-page-frame='orders']").locator(".dk-card")).to_be_visible()
                print(f"{where}: two cards, the live one opened its page, the removed one said so")
                context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    unhandled = UNHANDLED.report()
    return 1 if problems else unhandled


if __name__ == "__main__":
    sys.exit(run())
