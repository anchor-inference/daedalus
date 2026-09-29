"""Session actions belong to their row, not to the surrounding project or navigation target."""
from __future__ import annotations

import os

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import S1, S2, SESSIONS, UNHANDLED, respond, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)


def run() -> None:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/usr/local/bin/chromium"))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(5000)
        page.route("**/api/**", stub)
        changed = []

        def rename(route):  # type: ignore[no-untyped-def]
            if route.request.method == "PATCH":
                title = route.request.post_data_json["title"]
                changed.append(title)
                next(s for s in SESSIONS if s["id"] == S2)["title"] = title
                return respond(route, {"title": title})
            route.fallback()

        page.route(f"**/api/sessions/{S2}", rename)
        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&scheme=dark")
        row = page.locator(f'.sidebar .erow[data-session="{S2}"]')
        expect(row).to_be_visible()
        original_url = page.url
        row.locator('.session-row-menu button').click()
        menu = page.get_by_role("menu")
        expect(menu.get_by_role("menuitem", name="Rename", exact=True)).to_be_visible()
        # Delete is a bare verb like the other items: the row it hangs off already names the session.
        expect(menu.get_by_role("menuitem", name="Delete", exact=True)).to_be_visible()
        box = menu.bounding_box()
        assert box and box["x"] >= 8 and box["x"] + box["width"] <= 1432
        menu.get_by_role("menuitem", name="Rename", exact=True).click()
        page.get_by_role("textbox", name="Rename", exact=True).fill("Renamed session")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(row.locator(".erow-title")).to_contain_text("Renamed session")
        assert changed == ["Renamed session"] and page.url == original_url
        assert page.locator(".sidebar .folder-top").first.evaluate("el => { const head = el.querySelector('.folder-head'); return !head || getComputedStyle(head).backgroundColor === 'rgba(0, 0, 0, 0)'; }")
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/sidebar-session-actions.png")
        browser.close()
    assert not UNHANDLED.report()
    print("session row: menu, rename, navigation isolation, unified background passed")


if __name__ == "__main__":
    run()
