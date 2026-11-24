"""The take-over view zooms the page's picture, and a zoomed picture is sharp, in English and Russian, at 1440 × 900:

- while the operator drives, the corner's buttons, Ctrl with plus, minus and zero, and Ctrl with the
  wheel about the pointer zoom the picture, and none of it reaches the page;
- zoomed, the view asks for its frames at JPEG 92, never degraded, and lays a sharp frame over the
  zoomed part: a ``/still`` of exactly what the box shows, at the screen's pixels;
- the sign-in code of the ``qr`` scene, one CSS pixel a module, is under one screen pixel a module
  before the zoom; enlarged from the live frame alone it is a grey blur, and under the sharp frame it is
  black and white at several screen pixels a module — both counted on a screenshot of the screen;
- the middle button drags the zoomed picture, and is no click on the page;
- handing the page back puts the zoom and the sharp frame away.

``ZOOM_SHOTS=<dir>`` keeps the three pictures of the code (before, enlarged, sharp) for a QR decoder.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_zoom.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import io
import math
import os
import re
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_pages import QR_CODE  # noqa: E402
from browser_stub import VIEWPORT, BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("ZOOM_SHOTS", "")
ROOT = ".panel .bp"
VIEWER = f"{ROOT} .bv"
MODULES = 45
WORDS = {"en": {"zoom": "Zoom"}, "ru": {"zoom": "Масштаб"}}


def wait_for(page, predicate, seconds: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    """Wait on the page's own clock: the stub's socket is served by the same loop, which a sleep stops."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(100)
    return predicate()


def rect_of(page) -> tuple[float, float, float, float]:  # type: ignore[no-untyped-def]
    x, y, w, h = (float(v) for v in page.locator(f"{VIEWER} .bv-canvas").get_attribute("data-rect").split(","))
    return x, y, w, h


def code_on_screen(page, box: dict[str, float]) -> tuple[dict[str, float], float]:  # type: ignore[no-untyped-def]
    """Where the code is on the screen now, and its screen pixels a module."""
    origin = page.locator(VIEWER).bounding_box()
    x, y, w, _ = rect_of(page)
    per = w / VIEWPORT[0]
    clip = {"x": origin["x"] + x + box["x"] * per, "y": origin["y"] + y + box["y"] * per, "width": box["w"] * per, "height": box["h"] * per}
    return clip, box["w"] * per / MODULES


def modules() -> list[list[bool]]:
    """The code's dark modules, read from its own drawing: segno draws each row as runs (``h``) between
    moves (``m``), along the middle of the row."""
    path = re.search(r' d="([^"]+)"', QR_CODE).group(1)  # type: ignore[union-attr]
    dark = [[False] * MODULES for _ in range(MODULES)]
    x = y = 0.0
    for op, a, b in re.findall(r"([MmHh])(-?[\d.]+)(?:[ ,](-?[\d.]+))?", path):
        if op in "Mm":
            x, y = (float(a), float(b)) if op == "M" else (x + float(a), y + float(b))
            continue
        run = float(a) if op == "h" else float(a) - x
        for i in range(int(round(x)), int(round(x + run))):
            dark[int(y - 0.5)][i] = True
        x += run
    return dark


def wrong_modules(png: bytes) -> float:
    """The share of the code's modules a reader sampling each module's centre gets wrong: what a
    scanner needs, measured without one. Level M recovers about a seventh of the code; a picture that
    gets a fifth or more of its modules wrong is not read."""
    image = Image.open(io.BytesIO(png)).convert("L")
    truth = modules()
    wrong = 0
    for row in range(MODULES):
        for col in range(MODULES):
            px = image.getpixel((min(image.width - 1, int((col + 0.5) * image.width / MODULES)), min(image.height - 1, int((row + 0.5) * image.height / MODULES))))
            wrong += (px < 128) != truth[row][col]
    return wrong / MODULES**2


def picture(page, box: dict[str, float], name: str, lang: str) -> tuple[float, float]:  # type: ignore[no-untyped-def]
    clip, per_module = code_on_screen(page, box)
    shown = page.locator(VIEWER).bounding_box()
    # Only the part of the code the box shows is pictured; zoomed in, that is all of it.
    left, top = max(clip["x"], shown["x"]), max(clip["y"], shown["y"])
    right, bottom = min(clip["x"] + clip["width"], shown["x"] + shown["width"]), min(clip["y"] + clip["height"], shown["y"] + shown["height"])
    png = page.screenshot(clip={"x": left, "y": top, "width": right - left, "height": bottom - top})
    if SHOTS:
        Path(SHOTS, f"{lang}-{name}.png").write_bytes(png)
    return per_module, wrong_modules(png)


def check(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    bs.add("g1", scene="qr", owner_id=S1)
    box = scenes["qr"].boxes["qr"]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    url = f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser"
    page = open_page(context, bs, stub, url)
    page.wait_for_selector(f"{VIEWER}[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)
    if page.locator(f"{VIEWER} .bv-zoom").count():
        say("the zoom is offered while the agent drives")
    page.locator(f"{ROOT} .bp-control.take").click()
    page.wait_for_selector(f"{ROOT}[data-drive='you'] .bv .bv-zoom", timeout=5000)
    bar = page.locator(f"{VIEWER} .bv-zoom")
    if bar.get_attribute("aria-label") != WORDS[lang]["zoom"]:
        say(f"the zoom's toolbar is named {bar.get_attribute('aria-label')!r}")
    live = next(c for c in bs.clients if c.tier == "live" and not c.closed)

    if modules()[4][4:11] != [True] * 7 or modules()[5][5:10] != [False] * 5:
        say("the code's drawing was not read as a code: its finder pattern is not where it must be")
    before, wrong_before = picture(page, box, "before", lang)
    print(f"[{lang}] before the zoom: {before:.2f} screen px a module, {wrong_before:.0%} of the modules wrong")
    if before >= 1:
        say(f"the code already has {before:.2f} screen pixels a module: the check proves nothing")

    # Ctrl with the wheel over the code: the picture zooms about it, the page hears nothing.
    bs.stills_on = False
    inputs = len(bs.inputs("g1"))
    views = len(live.of("view"))
    clip, _ = code_on_screen(page, box)
    page.mouse.move(clip["x"] + clip["width"] / 2, clip["y"] + clip["height"] / 2)
    page.keyboard.down("Control")
    for _ in range(8):
        page.mouse.wheel(0, -100)
        page.wait_for_timeout(30)
    page.keyboard.up("Control")
    page.wait_for_timeout(300)
    scale = float(page.locator(VIEWER).get_attribute("data-zoom") or 1)
    print(f"[{lang}] Ctrl+wheel zoomed to {scale:.2f}")
    if scale < 5:
        say(f"Ctrl with the wheel zoomed only to {scale:.2f}")
    if [i for i in bs.inputs("g1")[inputs:] if i.get("type") != "move"]:
        say(f"the zoom's wheel reached the page: {bs.inputs('g1')[inputs:]}")
    if not wait_for(page, lambda: any(v.get("quality") == 92 and v.get("adaptive") is False for v in live.of("view")[views:])):
        say(f"zoomed, the view asked for no sharper frames: {live.of('view')[views:]}")
    page.wait_for_timeout(800)
    enlarged, wrong_enlarged = picture(page, box, "enlarged", lang)
    print(f"[{lang}] the live frame enlarged: {enlarged:.2f} screen px a module, {wrong_enlarged:.1%} of the modules wrong")

    # The sharp frame: the zoomed part drawn again at the screen's pixels.
    bs.stills_on = True
    asked = len(bs.posted("/still"))
    page.locator(f"{VIEWER} .bv-zoom-sharp").click()
    if not wait_for(page, lambda: page.locator(VIEWER).get_attribute("data-still") == "shown"):
        say("the sharp frame never came")
    else:
        body = bs.posted("/still")[-1]
        _, _, w, _ = rect_of(page)
        per_css = w / VIEWPORT[0]
        print(f"[{lang}] sharp frame asked for {body}")
        if not (body["x"] <= box["x"] and body["y"] <= box["y"] and body["x"] + body["w"] >= box["x"] + box["w"] and body["y"] + body["h"] >= box["y"] + box["h"]):
            say(f"the sharp frame {body} does not hold the code at {box}")
        if body["scale"] != min(4, math.ceil(per_css * 4) / 4):
            say(f"the sharp frame's scale {body['scale']} is not the screen's {per_css:.2f} pixels a CSS pixel")
        sharp, wrong_sharp = picture(page, box, "sharp", lang)
        print(f"[{lang}] the sharp frame: {sharp:.2f} screen px a module, {wrong_sharp:.1%} of the modules wrong")
        if sharp < 2.5:
            say(f"zoomed, the code has only {sharp:.2f} screen pixels a module")
        if wrong_sharp > 0.01:
            say(f"the sharp frame gets {wrong_sharp:.1%} of the modules wrong")
        if wrong_before < 0.2:
            say(f"before the zoom only {wrong_before:.0%} of the modules were wrong: the check proves nothing")
    if len(bs.posted("/still")) <= asked:
        say("the sharp frame's button asked for nothing")

    # The keyboard: Ctrl+0 back to fit, Ctrl+= in, Ctrl+- out, none of them sent on.
    page.locator(f"{VIEWER} .bv-ime").focus()
    inputs = len(bs.inputs("g1"))
    page.keyboard.press("Control+0")
    if not wait_for(page, lambda: page.locator(VIEWER).get_attribute("data-zoom") == "1.00" and page.locator(VIEWER).get_attribute("data-still") == ""):
        say(f"Ctrl+0 left the zoom at {page.locator(VIEWER).get_attribute('data-zoom')}")
    page.keyboard.press("Control+Equal")
    page.keyboard.press("Control+Equal")
    page.keyboard.press("Control+Minus")
    if page.locator(VIEWER).get_attribute("data-zoom") != "1.25":
        say(f"two Ctrl+= and a Ctrl+- zoomed to {page.locator(VIEWER).get_attribute('data-zoom')}, not 1.25")
    if len(bs.inputs("g1")) != inputs:
        say(f"a zoom chord reached the page: {bs.inputs('g1')[inputs:]}")

    # The buttons, and the middle button's drag.
    for _ in range(4):
        page.locator(f"{VIEWER} .bv-zoom button").nth(2).click()
    if float(page.locator(VIEWER).get_attribute("data-zoom") or 1) < 2.9:
        say(f"the zoom-in button zoomed to {page.locator(VIEWER).get_attribute('data-zoom')}")
    x0, y0, _, _ = rect_of(page)
    inputs = len(bs.inputs("g1"))
    shown = page.locator(VIEWER).bounding_box()
    page.mouse.move(shown["x"] + shown["width"] / 2, shown["y"] + shown["height"] / 2)
    page.mouse.down(button="middle")
    page.mouse.move(shown["x"] + shown["width"] / 2 + 120, shown["y"] + shown["height"] / 2 + 60, steps=6)
    page.mouse.up(button="middle")
    # A move's render is not a press's: it may land a frame after the button is up.
    wait_for(page, lambda: abs(rect_of(page)[0] - x0 - 120) < 2, 2.0)
    x1, y1, _, _ = rect_of(page)
    print(f"[{lang}] the middle button moved the picture by {x1 - x0:.0f}, {y1 - y0:.0f}")
    if abs(x1 - x0 - 120) > 2 or abs(y1 - y0 - 60) > 2:
        say(f"the middle button's drag moved the picture by {x1 - x0:.1f}, {y1 - y0:.1f}, not 120, 60")
    if [i for i in bs.inputs("g1")[inputs:] if i.get("type") in ("down", "up")]:
        say(f"the middle button's drag clicked the page: {bs.inputs('g1')[inputs:]}")
    page.locator(f"{VIEWER} .bv-zoom-level").click()
    if page.locator(VIEWER).get_attribute("data-zoom") != "1.00":
        say("the scale's button did not put the zoom back")

    # Handing the page back puts the zoom and the sharp frame away.
    page.locator(f"{VIEWER} .bv-zoom button").nth(2).click()
    page.locator(f"{VIEWER} .bv-zoom-sharp").click()
    wait_for(page, lambda: page.locator(VIEWER).get_attribute("data-still") == "shown")
    page.keyboard.press("Control+Shift+K")
    if not wait_for(page, lambda: page.locator(f"{ROOT}[data-drive='you']").count() == 0):
        say("Ctrl+Shift+K did not hand the page back")
    put_away = lambda: page.locator(VIEWER).get_attribute("data-zoom") == "1.00" and page.locator(VIEWER).get_attribute("data-still") == "" and not page.locator(f"{VIEWER} .bv-zoom").count()  # noqa: E731
    if not wait_for(page, put_away, 2.0):
        say(f"handed back, the zoom is {page.locator(VIEWER).get_attribute('data-zoom')}, the sharp frame {page.locator(VIEWER).get_attribute('data-still')!r}")
    page.close()
    context.close()


def main() -> int:
    problems: list[str] = []
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser, sharp=("qr",))
        for lang in ("en", "ru"):
            check(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
