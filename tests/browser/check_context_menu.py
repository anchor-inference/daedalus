"""Context commands share row actions, keep drafts intact and remain inside the viewport."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP  # noqa: E402
from screenshots import P1, S1, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def run() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents?token=t&lang=en")
        page.locator(f".folder[data-project='{P1}'] .folder-head").click()
        row = page.locator(f"nav.sidebar [data-session='{S1}']")
        expect(row).to_be_visible()
        row.click(button="right")
        menu = page.locator(".context-menu")
        expect(menu.get_by_role("menuitem", name="Rename", exact=True)).to_be_visible()
        menu.get_by_role("menuitem", name="Rename", exact=True).click()
        expect(page.locator(".sheet input")).to_have_value("Bakery site")
        page.keyboard.press("Escape")
        expect(page.locator(".sheet")).to_have_count(0)
        row.focus()
        page.keyboard.press("Shift+F10")
        expect(menu).to_be_visible()
        page.keyboard.press("End")
        expect(menu.get_by_role("menuitem", name="Delete", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(row).to_be_focused()
        page.get_by_role("button", name="Model for this session", exact=True).click(button="right")
        expect(page.locator(".menu.pop")).to_be_visible()
        page.keyboard.press("Escape")
        field = page.locator(".start-composer textarea")
        field.fill("A draft with a selection")
        field.evaluate("el => { el.focus(); el.setSelectionRange(2, 7); el.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:1438, clientY:898 })); }")
        expect(menu.get_by_role("menuitem", name="Copy", exact=True)).to_be_visible()
        expect(menu.get_by_role("menuitem", name="Cut", exact=True)).to_be_visible()
        box = menu.bounding_box()
        assert box and box["x"] >= 8 and box["y"] >= 8 and box["x"] + box["width"] <= 1432 and box["y"] + box["height"] <= 892, box
        menu.get_by_role("menuitem", name="Select all", exact=True).click()
        assert field.evaluate("el => [el.selectionStart, el.selectionEnd]") == [0, 24]
        expect(field).to_have_value("A draft with a selection")
        field.evaluate("el => el.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:900, clientY:600 }))")
        menu.get_by_role("menuitem", name="Cut", exact=True).click()
        expect(field).to_have_value("")
        expect(field).to_be_focused()
        field.click(button="right")
        menu.get_by_role("menuitem", name="Paste", exact=True).click()
        expect(field).to_have_value("A draft with a selection")
        expect(field).to_be_focused()
        page.goto(f"{BASE}/agents/{S1}?token=t&lang=en")
        answer = page.locator(".answer").first
        expect(answer).to_be_visible()
        answer.click(button="right")
        expect(menu.get_by_role("menuitem", name="Copy", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        browser.close()
    print("context menus: row commands, keyboard, focus, selection and viewport passed")


if __name__ == "__main__":
    run()
