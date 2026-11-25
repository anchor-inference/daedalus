"""The recording and its replay in the Browser tab, and a view that goes out of sight, in a real browser.

At 1440 × 900, in English and Russian:

- "Record keyframes" in the tab's menu switches the recording on (the operator's switch, a POST);
- the agent's actions then have keyframes, and their rows in the log say so;
- a row opens its keyframe over the live picture, with the element the action named framed where
  the picture shows it, from the logged box and the picture's own width;
- the scrubber steps to another keyframe, and "Back to live" lifts the replay, the live view still
  drawing underneath;
- a page going out of sight sends the daemon VIEW ``hidden: true`` and coming back ``hidden: false``
  (the daemon stops the frames meanwhile, and watch mode stops counting the view);
- "Stop recording" switches it off again.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ROOT = ".panel .bp"
WORDS = {
    "en": {"record": "Record keyframes", "stop": "Stop recording", "live": "Back to live"},
    "ru": {"record": "Записывать ключевые кадры", "stop": "Остановить запись", "live": "К живому виду"},
}


def near(a: float, b: float, tol: float = 2.0) -> bool:
    return abs(a - b) <= tol


def menu(page, label: str) -> None:  # type: ignore[no-untyped-def]
    """Pick an item of the tab's menu; the recording's is a check box, which leaves the menu open."""
    page.locator(f"{ROOT} .bp-toolbar button[aria-haspopup='menu']").last.click()
    page.locator(".menu[role='menu'] button", has_text=label).first.click()
    if page.locator(".menu[role='menu']").count():
        page.keyboard.press("Escape")


def check(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    group = bs.add("g1", scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser")
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)

    menu(page, words["record"])
    page.wait_for_timeout(300)
    posted = bs.posted("/recording")
    print(f"[{lang}] recording posted: {posted}")
    if posted[-1:] != [{"frames": True}] or not group.recording:
        say(f"the menu did not switch the recording on: {posted}")

    first = bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")
    second = bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")
    page.wait_for_selector(f"{ROOT} .bp-log-row[data-action='{second['id']}'] .bp-log-frame", timeout=10000)
    marked = page.locator(f"{ROOT} .bp-log-row .bp-log-frame").count()
    if marked < 2:
        say(f"{marked} rows are marked as having a keyframe, not the two actions")

    # A row opens its keyframe, the element framed on it.
    page.locator(f"{ROOT} .bp-log-row[data-action='{first['id']}']").click()
    page.wait_for_selector(f"{ROOT} .bp-replay .bp-replay-img", timeout=5000)
    no = page.locator(f"{ROOT} .bp-replay").get_attribute("data-no")
    want_no = next(f["no"] for f in group.frames if f["action_id"] == first["id"])
    if str(want_no) != no:
        say(f"the row opened keyframe {no}, not its action's {want_no}")
    page.wait_for_timeout(200)
    img = page.locator(f"{ROOT} .bp-replay-img").bounding_box()
    box = page.locator(f"{ROOT} .bp-replay-box").bounding_box()
    scale = img["width"] / 1280
    want = (img["x"] + first["box"]["x"] * scale, img["y"] + first["box"]["y"] * scale, first["box"]["w"] * scale)
    print(f"[{lang}] replay img {img}; box {box}; expected {want}")
    if not (near(box["x"], want[0], 3) and near(box["y"], want[1], 3) and near(box["width"], want[2], 4)):
        say(f"the replay frames the element at {box}, not {want}")
    shown = page.evaluate(f"() => {{ const i = document.querySelector('{ROOT} .bp-replay-img'); return i.complete && i.naturalWidth; }}")
    if shown != 1280:
        say(f"the keyframe is not the picture: natural width {shown}")

    # The scrubber steps to the next keyframe.
    scrub = page.locator(f"{ROOT} .bp-replay-scrub")
    scrub.focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    if page.locator(f"{ROOT} .bp-replay").get_attribute("data-no") != str(int(no) + 1):
        say("the scrubber did not step to the next keyframe")

    # Back to live: the replay is gone, and the live picture still draws.
    drawn = int(page.locator(f"{ROOT} .bv-canvas").get_attribute("data-frames") or 0)
    page.locator(f"{ROOT} .bp-replay-live").click()
    if page.locator(f"{ROOT} .bp-replay").count():
        say("Back to live left the replay on screen")
    bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")
    page.wait_for_function(f"() => Number(document.querySelector('{ROOT} .bv-canvas').dataset.frames || 0) > {drawn}", timeout=5000)

    # Out of sight and back: the daemon is told both ways.
    live = next(c for c in bs.clients if c.tier == "live" and not c.closed)
    page.evaluate("""(hidden) => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => hidden ? 'hidden' : 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""", True)
    page.wait_for_timeout(200)
    page.evaluate("""(hidden) => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => hidden ? 'hidden' : 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""", False)
    page.wait_for_timeout(300)
    told = [v for v in live.of("view") if "hidden" in v]
    print(f"[{lang}] visibility told: {told}")
    if told[-2:] != [{"hidden": True}, {"hidden": False}]:
        say(f"the daemon was told {told} about the page going out of sight")

    menu(page, words["stop"])
    page.wait_for_timeout(300)
    if bs.posted("/recording")[-1:] != [{"frames": False}] or group.recording:
        say("Stop recording did not switch it off")
    page.close()
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
