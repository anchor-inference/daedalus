"""The annotated snapshot, in a real browser, and refuse what does not behave.

- the Browser tab's toolbar has Snapshot; it opens a layer over the whole window with the live frame
  under it at the frame's own size;
- an arrow, a box and a pen stroke drawn with the mouse are marks; Ctrl+Z takes the last one back; a
  click with the text tool places a field, and Enter makes the words a mark;
- Attach closes the layer and leaves the composer holding one PNG, the size of the frame, with the
  box's colour where the box was drawn, and the page's address as a line of the draft;
- on a phone (Russian, light scheme) the browser sheet's bar has the same button, the layer fits 390 px
  without a sideways scroll, and Escape closes it without attaching anything;
- an HTML file in the Preview tab, which no canvas may read, opens on a blank sheet with a note saying
  why, and Attach still sends the sketch with the file's path.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_annotate.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import VIEWPORT, BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("ANNOTATE_SHOTS", "")
"""A directory to keep pictures of the layer in, for a person to look at; nothing is kept without it."""
ROOT = ".panel .bp"
PAGE_WORD = {"en": "Page", "ru": "Страница"}

# What the composer's saved draft holds for a session: the attachments, decoded, and the box's colour
# counted at the point the check drew the box's edge through. Read from the draft store because the
# composer keeps its files in React state and writes them there as they change.
STORED = """async ([session, probe]) => {
  const db = await new Promise((ok, no) => { const r = indexedDB.open('daedalus-composer-drafts', 1); r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });
  const row = await new Promise((ok) => { const q = db.transaction('attachments').objectStore('attachments').get(session); q.onsuccess = () => ok(q.result); q.onerror = () => ok(null); });
  db.close();
  const out = [];
  for (const file of row?.files ?? []) {
    const bitmap = await createImageBitmap(file);
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0);
    let hits = 0;
    if (probe) {
      const data = ctx.getImageData(probe[0] - 3, probe[1] - 3, 7, 7).data;
      for (let i = 0; i < data.length; i += 4) if (Math.abs(data[i] - probe[2]) < 40 && Math.abs(data[i + 1] - probe[3]) < 40 && Math.abs(data[i + 2] - probe[4]) < 40) hits++;
    }
    out.push({ name: file.name, type: file.type, width: bitmap.width, height: bitmap.height, hits });
  }
  return out;
}"""


def drag(page, box, start: tuple[float, float], end: tuple[float, float], steps: int = 8) -> None:  # type: ignore[no-untyped-def]
    """A mouse drag between two fractions of the picture's box."""
    page.mouse.move(box["x"] + box["width"] * start[0], box["y"] + box["height"] * start[1])
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * end[0], box["y"] + box["height"] * end[1], steps=steps)
    page.mouse.up()


def marks(page) -> int:  # type: ignore[no-untyped-def]
    return int(page.locator(".annotate").get_attribute("data-marks") or -1)


def draft_text(page) -> str:  # type: ignore[no-untyped-def]
    return page.locator(".composer-box textarea").input_value()


def keep(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def desktop(browser, scenes, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[en] desktop: {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    url = scenes["shop"].url
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=en&panel=browser")
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)

    button = page.locator(f"{ROOT} .bp-toolbar .annotate-open")
    if not button.count():
        say("the Browser tab's toolbar has no Snapshot button")
        context.close()
        return
    button.click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    size = page.locator(".annotate-canvas").evaluate("c => [c.width, c.height]")
    if size != list(VIEWPORT):
        say(f"the picture is {size}, not the frame's {list(VIEWPORT)}")
    cover = page.locator(".annotate").bounding_box()
    if not cover or cover["width"] < 1440 or cover["height"] < 900:
        say(f"the layer does not cover the window: {cover}")
    box = page.locator(".annotate-canvas").bounding_box()

    # The arrow is the tool it opens on.
    drag(page, box, (0.6, 0.6), (0.4, 0.4))
    page.locator(".annotate-tool[data-tool='box']").click()
    page.locator(".annotate-swatch").nth(3).click()
    drag(page, box, (0.1, 0.1), (0.3, 0.3))
    page.locator(".annotate-tool[data-tool='pen']").click()
    drag(page, box, (0.7, 0.2), (0.9, 0.3), steps=12)
    if marks(page) != 3:
        say(f"three strokes left {marks(page)} marks")
    page.keyboard.press("Control+z")
    if marks(page) != 2:
        say(f"Ctrl+Z left {marks(page)} marks, not 2")
    page.locator(".annotate-tool[data-tool='text']").click()
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.8)
    field = page.locator(".annotate-text")
    if not field.count():
        say("a click with the text tool placed no field")
    else:
        page.keyboard.type("this button")
        page.keyboard.press("Enter")
    if marks(page) != 3:
        say(f"the text did not become a mark: {marks(page)} marks")
    keep(page, "annotate-desktop")

    page.locator(".annotate-attach").click()
    page.wait_for_selector(".annotate", state="detached", timeout=5000)
    page.wait_for_function("() => document.querySelectorAll('.composer-box .attachments > *').length >= 1", timeout=5000)
    text = draft_text(page)
    print(f"[en] desktop draft: {text!r}")
    if f"{PAGE_WORD['en']}: {url}" not in text:
        say(f"the draft holds {text!r}, not the page's address {url}")
    # The box went from 10 % to 30 %: its top edge passes through (20 %, 10 %), in the blue swatch.
    probe = [round(VIEWPORT[0] * 0.2), round(VIEWPORT[1] * 0.1), 0x0A, 0x84, 0xFF]
    stored = []
    for _ in range(20):
        stored = page.evaluate(STORED, [S1, probe])
        if stored:
            break
        page.wait_for_timeout(150)
    print(f"[en] desktop attachments: {stored}")
    if len(stored) != 1:
        say(f"the composer holds {len(stored)} attachments, not one")
    else:
        shot = stored[0]
        if shot["type"] != "image/png" or not shot["name"].startswith("snapshot-shop.example.com-"):
            say(f"the attachment is {shot['name']} ({shot['type']})")
        if [shot["width"], shot["height"]] != list(VIEWPORT):
            say(f"the attached picture is {shot['width']}×{shot['height']}, not the frame's size")
        if shot["hits"] < 3:
            say("the attached picture has no box where the box was drawn")
    context.close()


def phone(browser, scenes, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[ru] phone: {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    gid = "g-ph"
    bs.add(gid, scene="cart", owner_id=S1)
    url = scenes["cart"].url
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="light")
    context.add_init_script("try { localStorage.setItem('daedalus.browser.announced', JSON.stringify(['" + gid + "'])); } catch (e) {}")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=light&lang=ru", wait=".browser-headbtn")
    page.locator(".browser-headbtn").tap()
    page.wait_for_selector(".panel-sheet .bp .bv[data-state='live']", timeout=10000)
    wait_frames(page, ".panel-sheet .bp", 1)
    button = page.locator(".panel-sheet .bp-phonebar .annotate-open")
    if not button.count():
        say("the browser sheet's bar has no Snapshot button")
        context.close()
        return
    button.tap()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    wide = page.evaluate("() => document.scrollingElement.scrollWidth")
    if wide > 390:
        say(f"the layer scrolls the page sideways: {wide}")
    bar = page.locator(".annotate-bar").bounding_box()
    canvas = page.locator(".annotate-canvas").bounding_box()
    if not canvas or canvas["width"] < 300 or (bar and canvas["y"] < bar["y"] + bar["height"] - 1):
        say(f"the picture is squeezed or under the bar: {canvas}, bar {bar}")
    drag(page, canvas, (0.2, 0.2), (0.6, 0.5))
    keep(page, "annotate-phone")
    page.keyboard.press("Escape")
    page.wait_for_selector(".annotate", state="detached", timeout=5000)
    if page.evaluate(STORED, [S1, None]):
        say("Escape attached the picture anyway")

    # Again, and this time Attach: the line lands in the draft under the sheet.
    button.tap()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    canvas = page.locator(".annotate-canvas").bounding_box()
    drag(page, canvas, (0.2, 0.2), (0.6, 0.5))
    page.locator(".annotate-attach").tap()
    page.wait_for_selector(".annotate", state="detached", timeout=5000)
    page.wait_for_timeout(300)
    text = draft_text(page)
    print(f"[ru] phone draft: {text!r}")
    if f"{PAGE_WORD['ru']}: {url}" not in text:
        say(f"the draft holds {text!r}, not the page's address {url}")
    focused = page.evaluate("() => document.activeElement?.tagName")
    if focused == "TEXTAREA":
        say("the composer under the sheet took the focus and would raise the keyboard")
    context.close()


def preview(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[en] preview: {text}")  # noqa: E731
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=en&panel=preview&path=site/index.html")
    page.wait_for_selector(".html-frame", timeout=20000)
    button = page.locator(".panel-toolbar .annotate-open")
    try:
        button.wait_for(timeout=5000)
    except Exception:
        say("the Preview tab's toolbar has no Snapshot button for an HTML file")
        context.close()
        return
    button.click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    if not page.locator(".annotate-note").count():
        say("the blank sheet does not say why there is no picture")
    canvas = page.locator(".annotate-canvas").bounding_box()
    page.locator(".annotate-tool[data-tool='box']").click()
    drag(page, canvas, (0.2, 0.2), (0.5, 0.5))
    keep(page, "annotate-preview")
    page.locator(".annotate-attach").click()
    page.wait_for_selector(".annotate", state="detached", timeout=5000)
    page.wait_for_function("() => document.querySelectorAll('.composer-box .attachments > *').length >= 1", timeout=5000)
    text = draft_text(page)
    print(f"[en] preview draft: {text!r}")
    if "Page: site/index.html" not in text:
        say(f"the draft holds {text!r}, not the file's path")
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        desktop(browser, scenes, problems)
        phone(browser, scenes, problems)
        preview(browser, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
