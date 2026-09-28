"""Exercise disclosure, steering and keyboard geometry in the built mobile app."""
from __future__ import annotations

import os

from check_composer import CHROMIUM, HOST, UNHANDLED, message, open_page
from playwright.sync_api import sync_playwright


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for width in (360, 390, 688):
            context = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True)
            HOST.status = "running"
            HOST.messages = [message(101, "user", "Build the tutorial.", run_id="r1"), message(102, "assistant", "Continuing the build…", thinking="Check the existing files first.", run_id="r1")]
            page = open_page(context, phone=True)
            assert page.locator(".thinking-head").last.get_attribute("aria-expanded") == "false"
            assert page.locator(".activity").count() == 0
            assert page.locator(".composer-steering").is_visible()
            assert page.locator(".composer .model-select").count() == 0
            assert page.locator(".composer .effort-select").count() == 0
            assert page.locator(".head-actions > button[aria-pressed]").count() == 0
            page.locator(".thinking-head").last.click()
            assert page.locator(".activity").last.is_visible()
            page.locator(".thinking-head").last.click()
            page.locator(".composer textarea").fill("Please use fewer files.")
            assert page.locator('.composer [data-action="queue"]').is_enabled()
            for height in (844, 440, 320, 844):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(150)
                rects = page.evaluate("""() => {
                  const box = (s) => document.querySelector(s).getBoundingClientRect().toJSON();
                  return {head:box('.chat-head'), scroll:box('.chat-scroll'), composer:box('.composer'),
                    y:window.scrollY, width:document.documentElement.scrollWidth};
                }""")
                assert rects["head"]["top"] >= 0, rects
                assert rects["scroll"]["top"] >= rects["head"]["bottom"], rects
                assert rects["scroll"]["bottom"] <= rects["composer"]["top"] + 1, rects
                assert rects["composer"]["bottom"] <= height + 1, rects
                assert rects["width"] <= width and rects["y"] == 0, rects
            HOST.status = "idle"
            page.reload()
            page.wait_for_selector(".composer .model-select")
            page.locator(".composer textarea").fill("")
            assert page.locator(".composer-box").bounding_box()["height"] <= 130
            page.locator(".composer .model-select").click()
            assert page.locator('.sheet input[type="radio"]').count() == 5
            for label in page.locator(".effort-option").all():
                # Rounded: a row of exactly the touch height at a fractional top measures 43.99997.
                assert round(label.bounding_box()["height"]) >= 44
            page.locator('.sheet input[value="high"]').click()
            page.wait_for_timeout(200)
            assert HOST.effort == "high"
            if os.environ.get("SHOTS"):
                page.screenshot(path=f"{os.environ['SHOTS']}/mobile-settings-{width}.png")
            page.keyboard.press("Escape")
            assert page.locator(".model-select").evaluate("el => el === document.activeElement")
            if os.environ.get("SHOTS"):
                page.screenshot(path=f"{os.environ['SHOTS']}/mobile-chat-{width}.png")
            print(f"mobile {width}: disclosure, steering, settings, keyboard geometry OK")
            context.close()
        browser.close()


if __name__ == "__main__":
    run()
    raise SystemExit(UNHANDLED.report())
