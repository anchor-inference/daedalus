"""The reasoning is one row of choices inside the model picker, at every size, through its existing API.

It was a child panel that floated beside the picker and was pushed against the foot of the window, away
from its row. Now: Off and the four efforts in one segmented row, the arrows walk them, a pick reaches
the host as the session's thinking and effort and leaves the box open and the same size.
"""
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
            page.wait_for_selector(".model-picker [role=option]")
            choices = page.locator(".model-picker .model-effort-seg [role='radio']")
            expect(choices).to_have_count(5)
            expect(choices).to_have_text(["Off", "Low", "Medium", "High", "Max"])
            expect(choices.nth(3)).to_have_attribute("aria-checked", "true")
            size = page.locator(".menu.model-picker").bounding_box() if width > 1024 else None
            before = len(composer.posts("/model"))
            if width < 1024:
                choices.last.click()
            else:
                # From the keyboard: Tab from the search reaches the chosen effort, the arrow moves on.
                page.keyboard.press("Tab")
                expect(choices.nth(3)).to_be_focused()
                page.keyboard.press("ArrowRight")
                expect(choices.nth(4)).to_be_focused()
                page.keyboard.press("Enter")
            assert composer.reached(page, "/model", before, f"{width} effort selection", [])
            assert composer.posts("/model")[-1][2] == {"thinking": True, "reasoning_effort": "xhigh"}, composer.posts("/model")[-1]
            # A pick leaves the box open, the same size, with the choice it made.
            expect(page.locator(".menu.model-picker")).to_have_count(1)
            expect(choices.nth(4)).to_have_attribute("aria-checked", "true")
            if size:
                after = page.locator(".menu.model-picker").bounding_box()
                assert after and (round(after["width"]), round(after["height"])) == (round(size["width"]), round(size["height"])), f"the box changed size: {size} -> {after}"
            choices.first.click()
            assert composer.posts("/model")[-1][2] == {"thinking": False}, composer.posts("/model")[-1]
            page.keyboard.press("Escape")
            expect(page.locator(".model-picker")).to_have_count(0)
            context.close()
            print(f"unified effort {width}: ok")
        browser.close()
    return composer.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
