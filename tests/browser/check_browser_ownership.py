"""A closed browser remains distinguishable from a reconnectable one in the contextual tab."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def check(browser, scenes, lang: str, viewport: dict[str, int]) -> None:  # type: ignore[no-untyped-def]
    host = BrowserStub(scenes)
    host.add("closed-shop", scene="shop", owner_id=S1, owner_label="Shop agent", status="lost")
    context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=viewport["width"] < 500, has_touch=viewport["width"] < 500)
    page = open_page(context, host, stub, f"{BASE}/agents/{S1}?token=t&lang={lang}&panel=browser", wait="body")
    state = {"alive": "no"}

    def ownership(route) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "group_id": "closed-shop", "alive": state["alive"], "reason": "not_in_daemon" if state["alive"] == "no" else "daemon_unavailable",
            "observed_at": "2026-10-03T10:00:00Z", "last_safe_url": "https://example.test/shop",
            "reconnectable": False, "instance_generation": "generation", "authorization_state": "authenticated_operator",
        }))

    page.route("**/api/runtime/browsers/closed-shop/ownership", ownership)
    empty = page.locator(".bp-empty")
    expect(empty).to_be_visible(timeout=10000)
    empty.locator(".bp-ownership > summary").first.click()
    row = empty.locator(".bp-ownership .bp-ownership", has_text="Shop agent")
    row.locator("summary").click()
    expect(row.locator(".bp-ownership-body")).to_contain_text("https://example.test/shop", timeout=5000)
    want = "different browser" if lang == "en" else "другом браузере"
    expect(row.locator(".bp-ownership-body")).to_contain_text(want)
    assert not row.locator("a[href^='https://example.test']").count(), "saved URL became an implicit new-browser action"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{lang} {viewport}: ownership overflow"
    state["alive"] = "unknown"
    row.locator("button").click()
    want = "cannot confirm" if lang == "en" else "не может подтвердить"
    expect(row.locator(".bp-ownership-body")).to_contain_text(want, timeout=5000)
    context.close()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang, {"width": 1440, "height": 900})
            check(browser, scenes, lang, {"width": 390, "height": 560})
        browser.close()
    print("browser ownership holds")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
