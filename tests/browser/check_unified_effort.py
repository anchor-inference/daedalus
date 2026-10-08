"""Nested effort keyboard navigation stays within the model menu and uses its existing API."""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_composer as composer  # noqa: E402
from api_stub import expect_app, reveal_composer  # noqa: E402


def main() -> int:
    expect_app(composer.BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=composer.CHROMIUM)
        for width in (1440, 390):
            composer.HOST = composer.Host()
            context = browser.new_context(viewport={"width": width, "height": 900 if width > 1024 else 600})
            page = composer.open_page(context)
            trigger = page.locator(".composer .model-select")
            reveal_composer(page)
            trigger.click()
            entry = page.locator(".effort-entry")
            entry.wait_for()
            entry.focus()
            page.keyboard.press("ArrowRight")
            expect(page.locator(".effort-submenu input:checked")).to_be_focused()
            expect(page.locator(".effort-submenu")).to_be_visible()
            page.wait_for_timeout(100)
            bounds = page.locator(".effort-submenu").bounding_box()
            assert bounds and bounds["y"] >= 8 and bounds["y"] + bounds["height"] <= page.viewport_size["height"] - 7, "Opening effort must reveal the options in a short window"
            before = len(composer.posts("/model"))
            page.keyboard.press("Escape")
            expect(page.locator(".effort-submenu")).to_have_count(0)
            expect(page.locator(".model-list")).to_have_count(1)
            expect(entry).to_be_focused()
            assert len(composer.posts("/model")) == before, "Opening and closing effort must not change it"
            page.keyboard.press("ArrowRight")
            page.keyboard.press("ArrowDown")
            expect(page.locator(".model-list")).to_have_count(0)
            assert composer.reached(page, "/model", before, "keyboard effort selection", [])
            assert composer.posts("/model")[-1][2] == {"thinking": True, "reasoning_effort": "xhigh"}
            context.close()
            print(f"unified effort keyboard {width}: ok")
        browser.close()
    return composer.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
