"""The voice control keeps its microphone while its visual switches between mascot forms."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import UNHANDLED, stub  # noqa: E402


def main() -> None:
    base = os.environ.get("APP_URL", DEFAULT_APP)
    expect_app(base)
    chromium = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
        for width, height, mobile in [(1440, 900, False), (390, 844, True)]:
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=3 if mobile else 1)
            errors: list[str] = []
            page.on("pageerror", lambda error, sink=errors: sink.append(str(error)))
            page.route("**/api/**", stub)
            page.goto(base + "/voice?token=t&scheme=dark&lang=en")
            page.locator(".voice-mascot .pet-canvas").wait_for(timeout=20000)
            page.wait_for_function("document.querySelector('.voice-mascot canvas')?.width > 0")
            assert page.locator(".pet-host").count() == 0
            if not mobile:
                page.evaluate("localStorage.setItem('daedalus.pet', 'on'); window.dispatchEvent(new Event('daedalus:pet-preference'))")
                page.locator(".pet-host").wait_for(state="attached")
                assert page.locator(".pet-figure").count() == 0
                page.evaluate("window.dispatchEvent(new CustomEvent('daedalus:pet-notice', {detail: {id: 7, title: 'A task finished', tone: 'success', needs_you: false}}))")
                page.locator(".pet-bubble").get_by_text("A task finished").wait_for()
                page.evaluate("localStorage.setItem('daedalus.pet', 'off'); window.dispatchEvent(new Event('daedalus:pet-preference'))")
            if mobile:
                ratio = page.locator(".voice-mascot canvas").evaluate("canvas => canvas.width / canvas.getBoundingClientRect().width")
                assert ratio >= 2, ratio
            page.get_by_role("button", name="Head", exact=True).click()
            page.locator(".voice-mascot canvas").wait_for()
            page.wait_for_function("document.querySelector('.voice-mascot canvas')?.width >= document.querySelector('.voice-mascot canvas')?.getBoundingClientRect().width * .95")
            assert page.get_by_role("button", name="Head", exact=True).get_attribute("aria-pressed") == "true"
            assert page.locator(".voice-mascot canvas").evaluate("canvas => !canvas.getContext('webgl2')?.isContextLost()")
            page.get_by_role("button", name="No mascot").click()
            assert page.locator(".voice-orb").count() == 1
            assert page.locator(".voice-mascot").count() == 0
            page.reload()
            page.locator(".voice-orb").wait_for()
            page.get_by_role("button", name="Mascot", exact=True).click()
            page.locator(".voice-mascot canvas").wait_for()
            assert not errors, errors
            page.close()
        browser.close()
    assert UNHANDLED.report() == 0


if __name__ == "__main__":
    main()
