"""The signed-in companion is optional, lazy, and usable without the desktop bridge."""

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
    chromium = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
    expect_app(base)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=["--use-gl=angle", "--use-angle=swiftshader"])
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.route("**/api/**", stub)
        page.goto(base + "/?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".rail", timeout=15000)
        assert page.locator(".pet-host").count() == 0
        assert not any("/assets/stage-" in address for address in requests)
        page.locator('[data-rail="menu"]').click()
        page.get_by_role("menuitem", name="Show companion").click()
        page.wait_for_selector(".pet-canvas", timeout=15000)
        page.wait_for_timeout(1500)
        assert page.locator(".pet-host").count() == 1
        page.goto(base + "/settings/appearance?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".settings-themes", timeout=15000)
        page.get_by_label("Reaction model").select_option("deepseek-flash")
        page.locator(".pet-figure").click(button="right")
        page.get_by_role("button", name="Generate a line").click()
        page.locator(".pet-bubble").get_by_text("I'm here.").wait_for(timeout=5000)
        page.locator(".pet-figure").click(button="right")
        page.get_by_label("Emotion").select_option("surprised")
        page.get_by_label("Animation").select_option("wave")
        page.get_by_label("Object").select_option("mug")
        page.evaluate("""() => window.dispatchEvent(new CustomEvent('daedalus:pet-notice', {detail: {id: 7, title: 'A task finished', body: 'Open the inbox', tone: 'success', category: 'run_finished', needs_you: false}}))""")
        page.locator(".pet-bubble").get_by_text("A task finished").wait_for(timeout=5000)
        page.locator(".pet-figure").click(button="right")
        page.get_by_role("button", name="Hide companion").click()
        assert page.locator(".pet-host").count() == 0
        assert not errors, errors
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.add_init_script("localStorage.setItem('daedalus.pet', 'on')")
        phone.route("**/api/**", stub)
        phone.goto(base + "/?token=t&scheme=dark&lang=en")
        phone.wait_for_selector(".pet-figure", timeout=15000)
        phone.locator(".pet-figure").evaluate("node => node.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'touch', clientX: 330, clientY: 120}))")
        phone.locator(".pet-menu").wait_for(timeout=3000)
        phone.locator(".pet-figure").evaluate("node => node.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'touch'}))")
        browser.close()
    assert UNHANDLED.report() == 0


if __name__ == "__main__":
    main()
