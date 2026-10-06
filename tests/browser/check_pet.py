"""The signed-in companion is optional, lazy, and usable without the desktop bridge."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import UNHANDLED, stub  # noqa: E402

# A line stays twelve seconds and a touch held for 650 ms opens the menu: the companion's real timings.
# On a loaded machine the check's own round trips outlasted both. The greeting it measured after a
# reload was gone by the time the drag was over, and a touch drag whose move came a second after its
# start had become a held press. So each measurement of a bubble raises a fresh line and reads the
# boxes in the same task, a frame after it is drawn, and the touch drag is dispatched in one task.
SAY = """async ([id, selectors]) => {
    window.dispatchEvent(new CustomEvent('daedalus:pet-notice', {detail: {id, title: 'A task finished', body: 'Open the inbox', tone: 'success', category: 'run_finished', needs_you: false}}));
    const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    for (let n = 0; n < 600 && !document.querySelector('.pet-bubble')?.textContent?.includes('A task finished'); n++) await frame();
    await frame();
    const boxes = {};
    for (const selector of selectors) {
        const node = document.querySelector(selector);
        const box = node?.getBoundingClientRect();
        boxes[selector] = box ? {x: box.x, y: box.y, width: box.width, height: box.height} : null;
    }
    const bubble = document.querySelector('.pet-bubble');
    boxes.font = bubble ? parseFloat(getComputedStyle(bubble).fontSize) : null;
    boxes.background = bubble ? getComputedStyle(bubble).backgroundColor : null;
    return boxes;
}"""

TOUCH_DRAG = """([from, to]) => {
    const figure = document.querySelector('.pet-figure');
    const event = (type, [x, y]) => figure.dispatchEvent(new PointerEvent(type, {bubbles: true, pointerId: 7, pointerType: 'touch', button: 0, clientX: x, clientY: y}));
    event('pointerdown', from);
    for (let step = 1; step <= 8; step++) event('pointermove', [from[0] + (to[0] - from[0]) * step / 8, from[1] + (to[1] - from[1]) * step / 8]);
    event('pointerup', to);
}"""


def say(page: Page, notice_id: int, *selectors: str) -> dict:
    """A fresh line on the companion, and the boxes of ``selectors`` the frame after it is drawn."""
    boxes = page.evaluate(SAY, [notice_id, [".pet-bubble", *selectors]])
    assert boxes[".pet-bubble"], "the companion never showed the line"
    return boxes


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
        page.locator(".pet-bubble").get_by_text("I'm here.").wait_for()
        page.locator(".pet-figure").click(button="right")
        page.get_by_label("Emotion").select_option("surprised")
        page.get_by_label("Animation").select_option("wave")
        page.get_by_label("Object").select_option("mug")
        original = say(page, 7, ".pet-figure")
        assert original["background"].startswith("rgb("), original["background"]
        original_figure, original_bubble, original_font = original[".pet-figure"], original[".pet-bubble"], original["font"]
        assert original_figure
        page.get_by_role("slider", name="Size").press("Home")
        assert page.get_by_role("slider", name="Size").input_value() == "60"
        small = say(page, 7, ".pet-figure")
        small_figure, small_bubble, small_font = small[".pet-figure"], small[".pet-bubble"], small["font"]
        assert small_figure
        assert abs(small_figure["height"] / original_figure["height"] - 0.6) < 0.02
        assert abs(small_bubble["width"] / original_bubble["width"] - 0.6) < 0.02
        assert abs(small_font / original_font - 0.6) < 0.02
        page.reload()
        page.wait_for_selector(".pet-figure", timeout=15000)
        assert abs(page.locator(".pet-figure").bounding_box()["height"] / original_figure["height"] - 0.6) < 0.02
        page.locator(".pet-figure").click(button="right")
        assert page.get_by_role("slider", name="Size").input_value() == "60"
        page.get_by_role("slider", name="Size").press("End")
        assert page.get_by_role("slider", name="Size").input_value() == "100"
        page.keyboard.press("Escape")
        before = page.locator(".pet-figure").bounding_box()
        assert before
        page.mouse.move(before["x"] + before["width"] / 2, before["y"] + before["height"] / 2)
        page.mouse.down()
        page.mouse.move(8, 8, steps=8)
        page.mouse.up()
        after_drag = say(page, 9, ".pet-host")
        moved, bubble = after_drag[".pet-host"], after_drag[".pet-bubble"]
        assert moved and moved["x"] <= 10 and moved["y"] <= 10, moved
        assert bubble["x"] >= 8 and bubble["y"] >= 8, bubble
        assert bubble["x"] + bubble["width"] <= 1352 and bubble["y"] + bubble["height"] <= 892, bubble
        assert bubble["y"] >= moved["y"] + moved["height"] - 50 or bubble["x"] >= moved["x"] + moved["width"] - 1, bubble
        assert "custom" in page.evaluate("localStorage.getItem('daedalus.pet.position.desktop')")
        page.reload()
        page.wait_for_selector(".pet-figure", timeout=15000)
        restored = page.locator(".pet-host").bounding_box()
        assert restored and restored["x"] <= 10 and restored["y"] <= 10, restored
        page.locator(".pet-figure").click(button="right")
        page.locator(".pet-menu").wait_for()
        with_menu = say(page, 10, ".pet-menu")
        menu, bubble = with_menu[".pet-menu"], with_menu[".pet-bubble"]
        assert menu and menu["x"] >= 8 and menu["y"] >= 8, menu
        assert menu["x"] + menu["width"] <= 1352 and menu["y"] + menu["height"] <= 892, menu
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
        # Waited for, not read at once. A canvas is 300 wide before anything sizes it, so "wider than
        # nothing" held from the first moment; when the lazily loaded renderer came late, the ratio read
        # was that blank canvas's 300 over the figure's 130, which is the 2.31 a loaded run reported.
        # The renderer does not adapt its resolution to the frame rate: its density is fixed by the
        # screen and a pixel budget, so a settled canvas is the one to judge.
        sharp = "canvas => !!canvas && canvas.width / canvas.getBoundingClientRect().width >= 2.9"
        try:
            phone.wait_for_function(f"({sharp})(document.querySelector('.pet-figure canvas'))")
        except PlaywrightTimeout:
            ratio = phone.locator(".pet-figure canvas").evaluate("canvas => canvas.width / canvas.getBoundingClientRect().width")
            raise AssertionError(f"the companion's canvas never reached the screen's density: {ratio}") from None
        phone_bubble = say(phone, 8)[".pet-bubble"]
        assert phone_bubble["x"] >= 8 and phone_bubble["y"] >= 8, phone_bubble
        assert phone_bubble["x"] + phone_bubble["width"] <= 382 and phone_bubble["y"] + phone_bubble["height"] <= 836, phone_bubble
        start = phone.locator(".pet-figure").bounding_box()
        assert start
        phone.evaluate(TOUCH_DRAG, [[start["x"] + 50, start["y"] + 50], [50, 740]])
        after_touch = say(phone, 11, ".pet-host")
        moved_phone, phone_bubble = after_touch[".pet-host"], after_touch[".pet-bubble"]
        assert moved_phone and moved_phone["x"] <= 10 and moved_phone["y"] <= 619, moved_phone
        assert phone_bubble["x"] >= 8 and phone_bubble["y"] >= 8, phone_bubble
        assert phone_bubble["x"] + phone_bubble["width"] <= 382 and phone_bubble["y"] + phone_bubble["height"] <= 836, phone_bubble
        phone.locator(".pet-figure").evaluate("node => node.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'touch', clientX: 330, clientY: 120}))")
        phone.locator(".pet-menu").wait_for()
        phone.locator(".pet-figure").evaluate("node => node.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'touch'}))")
        phone.get_by_role("slider", name="Size").press("Home")
        small_phone_boxes = say(phone, 12, ".pet-figure")
        small_phone, phone_bubble = small_phone_boxes[".pet-figure"], small_phone_boxes[".pet-bubble"]
        assert small_phone and abs(small_phone["width"] - 78) < 2, small_phone
        assert phone_bubble["x"] >= 8 and phone_bubble["y"] >= 8, phone_bubble
        assert phone_bubble["x"] + phone_bubble["width"] <= 382 and phone_bubble["y"] + phone_bubble["height"] <= 836, phone_bubble
        browser.close()
    assert UNHANDLED.report() == 0


if __name__ == "__main__":
    main()
