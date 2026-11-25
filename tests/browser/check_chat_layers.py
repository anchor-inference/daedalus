"""Session controls stay reachable above both side panels and inside the viewport."""
from __future__ import annotations

import os

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import S1, UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/usr/local/bin/chromium"), headless=True, args=["--no-sandbox"])
        for width in (1100, 1440, 2048):
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=en")
            expect(page.locator(".chat-title")).to_be_visible()
            # Folded, the column is gone and the button in the window's corner brings it back.
            if not page.locator("nav.sidebar").count():
                page.locator(".sidebar-unfold").click()
                expect(page.locator("nav.sidebar")).to_be_visible()
            assert page.locator(".chat-title").get_attribute("aria-haspopup") is None
            page.get_by_role("button", name="Session actions", exact=True).click()
            for label in ("Rename", "Compact history", "Export as Markdown"):
                expect(page.locator(".menu").get_by_role("menuitem", name=label, exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            for collapsed in (False, True):
                if collapsed:
                    page.locator(".sidebar button[aria-label^='Fold the sidebar']").click()
                page.locator(".model-select").click()
                menu = page.locator(".model-menu")
                expect(menu).to_be_visible()
                expect(menu.locator("button").first).to_be_visible()
                page.wait_for_timeout(250)
                box = menu.bounding_box()
                print(width, collapsed, box, flush=True)
                assert box and box["x"] >= 8 and box["x"] + box["width"] <= width - 8
                assert box["y"] >= 8 and box["y"] + box["height"] <= 892
                assert menu.evaluate("el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.left + 8, r.top + 8)); }")
                if os.environ.get("SHOTS"):
                    page.screenshot(path=f"{os.environ['SHOTS']}/models-{width}-{collapsed}.png")
                page.keyboard.press("Escape")
            page.close()
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    main()
