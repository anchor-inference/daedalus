"""A custom page inside the app: it renders in its frame at a desktop and a phone size, follows the
app's theme as it changes, reaches the app through the bridge (a toast) and the API (its storage), and
a page that does not exist says so.

    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_pages.py
"""
from __future__ import annotations

import os
import sys

from api_stub import DEFAULT_APP, expect_app
from pages_stub import PagesStub
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"title": "Orders", "missing": "No such page", "toast": "Hello from the orders page", "versions": "Versions"},
    "ru": {"title": "Заказы", "missing": "Такой страницы нет", "toast": "Hello from the orders page", "versions": "Версии"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def tokens_match(page: Page, where: str) -> None:
    """The page's root carries the app's computed tokens: the colours and the type scale alike."""
    frame = page.frame_locator("[data-page-frame='orders']")
    expect(frame.locator("#theme")).not_to_have_text("none", timeout=8000)
    for name in ("--bg", "--fg", "--accent", "--fs-14", "--row-h"):
        outer = page.evaluate(f"getComputedStyle(document.documentElement).getPropertyValue('{name}').trim()")
        inner = page.frame_locator("[data-page-frame='orders']").locator("html").evaluate(f"el => getComputedStyle(el).getPropertyValue('{name}').trim()")
        assert outer and outer == inner, f"{where}: {name} is {inner!r} in the page and {outer!r} in the app"


def api_route(pages: PagesStub):  # type: ignore[no-untyped-def]
    """The pages' routes from the stub that keeps them, everything else from the pictures' stub."""
    def route(r) -> None:  # type: ignore[no-untyped-def]
        if not pages.fulfil(r):
            stub(r)
    return route


def page_route(pages: PagesStub, app_root: str):  # type: ignore[no-untyped-def]
    """A page's own address. ``**/pages/**`` also matches ``/api/pages/…``, which goes on to the API's route."""
    def route(r) -> None:  # type: ignore[no-untyped-def]
        if "/api/" in r.request.url:
            r.fallback()
        else:
            pages.serve(r, app_root)
    return route


def run() -> int:
    expect_app(BASE)
    app_root = BASE.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            words = WORDS[language]
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                pages = PagesStub()
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(8000)
                page.route("**/api/**", api_route(pages))
                page.route("**/pages/**", page_route(pages, app_root))
                where = f"{language} {width}px"

                page.goto(f"{BASE}/p/orders?token=t&lang={language}")
                expect(page.locator(".page-host .pagehead h1")).to_have_text(words["title"])
                frame = page.frame_locator("[data-page-frame='orders']")
                expect(frame.locator(".dk-card")).to_be_visible()
                assert pages.signins == 1, f"{where}: the frame was drawn without asking for the cookie ({pages.signins})"
                # The frame fills what the header leaves, to the bottom of the window.
                box = page.locator("[data-page-frame='orders']").bounding_box()
                assert box is not None and abs(box["y"] + box["height"] - height) <= 2 and abs(box["width"] - page.locator(".page-host").bounding_box()["width"]) <= 2, f"{where}: the frame is {box}"
                if mobile:
                    expect(page.locator(".page-host .ph-menu")).to_be_visible()
                fits(page, where)
                tokens_match(page, where)

                # The operator picks another look: appearance.ts repaints the app's root, and the page follows.
                page.evaluate("""() => {
                    const root = document.documentElement;
                    root.style.setProperty('--bg', '#f7f1e8');
                    root.style.setProperty('--accent', '#9a4e22');
                    root.dataset.scheme = 'light';
                    root.dataset.theme = 'zakat';
                }""")
                expect(frame.locator("#theme")).to_have_text("zakat/light")
                expect(frame.locator("html")).to_have_attribute("data-scheme", "light")
                tokens_match(page, f"{where} after the theme changed")

                # The bridge: a toast in the app, asked for by the page.
                frame.locator("#toast").click()
                expect(page.locator(".toast")).to_contain_text(words["toast"])
                # The API: the page's storage, written and read back through the same-origin fetch.
                frame.locator("#save").click()
                expect(frame.locator("#kept")).to_have_text("Rye, two loaves")
                assert ("orders", "note") in pages.storage and "PUT /api/pages/orders/storage/note" in pages.requests, pages.requests

                # The versions: the live one checked, another one a click away.
                page.locator(".page-host-versions").click()
                expect(page.locator(".menu [role='menuitemcheckbox'][aria-checked='true']")).to_have_count(1)
                page.keyboard.press("Escape")

                page.goto(f"{BASE}/p/nowhere?token=t&lang={language}")
                expect(page.locator("[data-page-state='missing']")).to_contain_text(words["missing"])
                fits(page, f"{where} missing")
                context.close()
                print(f"{where}: the page rendered in its frame, followed the theme, toasted, kept a value; a missing page said so")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
