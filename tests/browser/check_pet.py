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
        assert page.get_by_role("menuitem", name="Show companion").evaluate("node => getComputedStyle(node).backgroundColor") == "rgba(0, 0, 0, 0)"
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
        page.keyboard.press("Escape")
        figure = page.locator(".pet-figure")
        before = figure.bounding_box()
        assert before
        page.mouse.move(before["x"] + before["width"] / 2, before["y"] + before["height"] / 2)
        page.mouse.down()
        page.mouse.move(8, 8, steps=8)
        page.mouse.up()
        moved = page.locator(".pet-host").bounding_box()
        assert moved and moved["x"] <= 10 and moved["y"] <= 10, moved
        bubble = page.locator(".pet-bubble").bounding_box()
        assert bubble and bubble["x"] >= 8 and bubble["y"] >= 8, bubble
        assert bubble["x"] + bubble["width"] <= 1352 and bubble["y"] + bubble["height"] <= 892, bubble
        assert bubble["y"] >= moved["y"] + moved["height"] - 50 or bubble["x"] >= moved["x"] + moved["width"] - 1, bubble
        assert "custom" in page.evaluate("localStorage.getItem('daedalus.pet.position.desktop')")
        page.reload()
        page.wait_for_selector(".pet-figure", timeout=15000)
        restored = page.locator(".pet-host").bounding_box()
        assert restored and restored["x"] <= 10 and restored["y"] <= 10, restored
        page.locator(".pet-figure").click(button="right")
        menu = page.locator(".pet-menu").bounding_box()
        assert menu and menu["x"] >= 8 and menu["y"] >= 8, menu
        assert menu["x"] + menu["width"] <= 1352 and menu["y"] + menu["height"] <= 892, menu
        bubble = page.locator(".pet-bubble").bounding_box()
        assert bubble
        overlap = max(0, min(menu["x"] + menu["width"], bubble["x"] + bubble["width"]) - max(menu["x"], bubble["x"])) * max(0, min(menu["y"] + menu["height"], bubble["y"] + bubble["height"]) - max(menu["y"], bubble["y"]))
        assert overlap == 0, (menu, bubble)
        page.get_by_role("button", name="Hide companion").click()
        assert page.locator(".pet-host").count() == 0
        assert not errors, errors
        phone = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        phone.add_init_script("localStorage.setItem('daedalus.pet', 'on')")
        phone.route("**/api/**", stub)
        phone.goto(base + "/?token=t&scheme=dark&lang=en")
        phone.wait_for_selector(".pet-figure", timeout=15000)
        phone.wait_for_function("document.querySelector('.pet-figure canvas')?.width > 0")
        ratio = phone.locator(".pet-figure canvas").evaluate("canvas => canvas.width / canvas.getBoundingClientRect().width")
        assert ratio >= 2.9, ratio
        phone.evaluate("""() => window.dispatchEvent(new CustomEvent('daedalus:pet-notice', {detail: {id: 8, title: 'A task finished', body: 'Open the inbox', tone: 'success', category: 'run_finished', needs_you: false}}))""")
        phone.locator(".pet-bubble").wait_for(timeout=3000)
        phone_bubble = phone.locator(".pet-bubble").bounding_box()
        assert phone_bubble and phone_bubble["x"] >= 8 and phone_bubble["y"] >= 8, phone_bubble
        assert phone_bubble["x"] + phone_bubble["width"] <= 382 and phone_bubble["y"] + phone_bubble["height"] <= 836, phone_bubble
        start = phone.locator(".pet-figure").bounding_box()
        assert start
        touch = phone.context.new_cdp_session(phone)
        touch.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": start["x"] + 50, "y": start["y"] + 50}]})
        touch.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": 50, "y": 740}]})
        touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        moved_phone = phone.locator(".pet-host").bounding_box()
        assert moved_phone and moved_phone["x"] <= 10 and moved_phone["y"] <= 619, moved_phone
        phone_bubble = phone.locator(".pet-bubble").bounding_box()
        assert phone_bubble and phone_bubble["x"] >= 8 and phone_bubble["y"] >= 8, phone_bubble
        assert phone_bubble["x"] + phone_bubble["width"] <= 382 and phone_bubble["y"] + phone_bubble["height"] <= 836, phone_bubble
        phone.locator(".pet-figure").evaluate("node => node.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'touch', clientX: 330, clientY: 120}))")
        phone.locator(".pet-menu").wait_for(timeout=3000)
        phone.locator(".pet-figure").evaluate("node => node.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'touch'}))")
        browser.close()
    assert UNHANDLED.report() == 0


if __name__ == "__main__":
    main()
