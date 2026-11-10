"""Marking up a picture in the composer, in a real browser, and refuse what does not behave.

- a pasted picture is attached as it is, with no editor in the way; it and a picked JPEG carry a
  pencil on their cards, a text file does not;
- the pencil opens the editor on the picture at its own size; the tools are picked by keys; a blur
  leaves squares of one flat colour over a fine pattern; crop and a quarter turn change the picture's
  size, and Undo and Redo walk through both;
- Done puts a PNG of the marked-up picture in the attachment's place; opening it again starts from the
  original with every step back, and undoing them all and pressing Done restores the original file;
  Cancel leaves the file as it was;
- on a phone (Russian, light, touch) the pencil sits inside its tile, the editor fits 390 px, a finger
  draws a pen stroke and an arrow, a second finger mid-stroke drops that stroke, and Done attaches;
- a 5120x2880 screenshot opens, draws and finishes in reasonable time, and an 8000x6000 photo is
  drawn no larger than a canvas may safely be. The timings are printed for the record.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_markup.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("MARKUP_SHOTS", "")
"""A directory to keep pictures of the editor in, for a person to look at; nothing is kept without it."""

# A picture drawn in the page: a gradient, a fine black-and-white checkerboard over the top-left
# quarter (what a blur must flatten), and a word. Pasted into the composer's field as a clipboard
# event carrying the file, which is the path a real paste takes; or handed back as bytes for a picker.
PICTURE = """async ([w, h, kind]) => {
  const c = new OffscreenCanvas(w, h);
  const x = c.getContext('2d');
  const g = x.createLinearGradient(0, 0, w, h);
  g.addColorStop(0, '#1d3557'); g.addColorStop(1, '#f1faee');
  x.fillStyle = g; x.fillRect(0, 0, w, h);
  x.fillStyle = '#ffffff'; x.fillRect(0, 0, w / 4, h / 4);
  x.fillStyle = '#000000';
  for (let yy = 0; yy < h / 4; yy += 4) for (let xx = (yy / 4) % 2 ? 4 : 0; xx < w / 4; xx += 8) x.fillRect(xx, yy, 4, 4);
  x.fillStyle = '#e63946'; x.font = `${Math.round(h / 12)}px sans-serif`; x.fillText('Daedalus', w * 0.55, h * 0.6);
  return await c.convertToBlob({ type: kind, quality: 0.9 });
}"""

PASTE = """async ([w, h, kind, name]) => {
  const blob = await (""" + PICTURE + """)([w, h, kind]);
  const dt = new DataTransfer();
  dt.items.add(new File([blob], name, { type: kind }));
  const field = document.querySelector('.composer-box .composer-field');
  field.focus();
  field.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  return blob.size;
}"""

BYTES = """async ([w, h, kind]) => {
  const blob = await (""" + PICTURE + """)([w, h, kind]);
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}"""

# The composer's saved draft for a session: each attachment's name, type, size and, for a rectangle
# asked about, the spread of each colour channel inside it (0 for one flat colour).
STORED = """async ([session, boxes]) => {
  const db = await new Promise((ok, no) => { const r = indexedDB.open('daedalus-composer-drafts', 1); r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });
  const row = await new Promise((ok) => { const q = db.transaction('attachments').objectStore('attachments').get(session); q.onsuccess = () => ok(q.result); q.onerror = () => ok(null); });
  db.close();
  const out = [];
  for (const file of row?.files ?? []) {
    const item = { name: file.name, type: file.type, bytes: file.size, spreads: [] };
    if (file.type.startsWith('image/')) {
      const bitmap = await createImageBitmap(file);
      item.width = bitmap.width; item.height = bitmap.height;
      if (boxes && out.length === 0) {
        const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
        const ctx = canvas.getContext('2d');
        ctx.drawImage(bitmap, 0, 0);
        for (const [bx, by, bw, bh] of boxes) {
          const data = ctx.getImageData(bx, by, bw, bh).data;
          let low = 255, high = 0;
          for (let i = 0; i < data.length; i += 4) for (let k = 0; k < 3; k++) { low = Math.min(low, data[i + k]); high = Math.max(high, data[i + k]); }
          item.spreads.push(high - low);
        }
      }
    }
    out.push(item);
  }
  return out;
}"""


def stored(page, boxes=None, count: int | None = None):  # type: ignore[no-untyped-def]
    """The saved attachments, waited for: the draft store is written a moment after the files change."""
    got = []
    for _ in range(40):
        got = page.evaluate(STORED, [S1, boxes])
        if got and (count is None or len(got) == count):
            return got
        page.wait_for_timeout(150)
    return got


def drag(page, box, start: tuple[float, float], end: tuple[float, float], steps: int = 8) -> None:  # type: ignore[no-untyped-def]
    page.mouse.move(box["x"] + box["width"] * start[0], box["y"] + box["height"] * start[1])
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * end[0], box["y"] + box["height"] * end[1], steps=steps)
    page.mouse.up()


def marks(page) -> int:  # type: ignore[no-untyped-def]
    return int(page.locator(".annotate").get_attribute("data-marks") or -1)


def size(page) -> list[int]:  # type: ignore[no-untyped-def]
    return page.locator(".annotate-canvas").evaluate("c => [c.width, c.height]")


def keep(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def open_composer(browser, lang: str, scheme: str, **context_args):  # type: ignore[no-untyped-def]
    context = browser.new_context(color_scheme=scheme, **context_args)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme={scheme}&lang={lang}")
    # Enabled once the saved draft's files are back: a paste before that is overwritten by the restore.
    page.wait_for_selector(".composer-box .composer-field", timeout=20000)
    return context, page


def card(page, i: int):  # type: ignore[no-untyped-def]
    return page.locator(".composer-box .attachments .attachment").nth(i)


def desktop(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[en] desktop: {text}")  # noqa: E731
    context, page = open_composer(browser, "en", "dark", viewport={"width": 1440, "height": 900})
    page.evaluate(PASTE, [1280, 800, "image/png", "image.png"])
    page.wait_for_selector(".composer-box .attachments .attachment", timeout=5000)
    page.wait_for_timeout(300)
    if page.locator(".annotate").count():
        say("a paste opened the editor uninvited")
    jpeg = base64.b64decode(page.evaluate(BYTES, [640, 480, "image/jpeg"]))
    page.locator(".composer-box input[type=file][multiple]").set_input_files([
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"plain words"},
        {"name": "photo.jpg", "mimeType": "image/jpeg", "buffer": jpeg},
    ])
    page.wait_for_function("() => document.querySelectorAll('.composer-box .attachments .attachment').length === 3", timeout=5000)
    pencils = [card(page, i).locator(".attachment-markup").count() for i in range(3)]
    if pencils != [1, 0, 1]:
        say(f"pencils on the pasted picture, the text and the picked JPEG: {pencils}, not [1, 0, 1]")
    original = stored(page, count=3)[0]
    print(f"[en] pasted: {original}")

    card(page, 0).locator(".attachment-markup").click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    if page.locator(".annotate-attach").inner_text().strip() != "Done":
        say(f"the editor's button says {page.locator('.annotate-attach').inner_text()!r}, not Done")
    if size(page) != [1280, 800]:
        say(f"the editor holds the picture at {size(page)}, not 1280x800")
    sheet = page.locator(".annotate-sheet").bounding_box()
    drag(page, sheet, (0.5, 0.5), (0.8, 0.51))
    page.keyboard.press("o")
    drag(page, sheet, (0.3, 0.3), (0.45, 0.45))
    page.keyboard.press("b")
    drag(page, sheet, (0.02, 0.02), (0.2, 0.2))
    page.keyboard.press("t")
    page.mouse.click(sheet["x"] + sheet["width"] * 0.5, sheet["y"] + sheet["height"] * 0.8)
    page.keyboard.type("here")
    page.keyboard.press("Enter")
    if marks(page) != 4:
        say(f"an arrow, an ellipse, a blur and a label left {marks(page)} steps, not 4")
    keep(page, "markup-desktop")
    page.keyboard.press("c")
    drag(page, sheet, (0.0, 0.0), (0.75, 0.75))
    tool = page.locator(".annotate").get_attribute("data-tool")
    if tool != "text":
        say(f"after the crop the tool is {tool}, not the one drawing before it")
    if size(page) != [960, 600]:
        say(f"the crop left {size(page)}, not 960x600")
    page.keyboard.press("q")
    if size(page) != [600, 960]:
        say(f"a quarter turn left {size(page)}, not 600x960")
    page.keyboard.press("Control+z")
    undone = size(page)
    page.keyboard.press("Control+Shift+z")
    if undone != [960, 600] or size(page) != [600, 960] or marks(page) != 6:
        say(f"Undo and Redo of the turn gave {undone} then {size(page)}, {marks(page)} steps")
    keep(page, "markup-desktop-cropped")
    page.keyboard.press("Enter")
    page.wait_for_selector(".annotate", state="detached", timeout=20000)
    # The blur covered (26..256, 16..160) of the original; the crop kept the top-left corner, and the
    # turn moved a point (x, y) to (599 - y, x). Inside one square of the blur's 27-pixel grid there is
    # one colour; the checkerboard just outside the blur still has black and white.
    flat, patterned = [550, 30, 20, 20], [404, 262, 30, 38]
    files = stored(page, [flat, patterned], count=3)
    marked = files[0]
    print(f"[en] marked: {marked}")
    if marked["type"] != "image/png" or not marked["name"].endswith(".png") or [marked.get("width"), marked.get("height")] != [600, 960]:
        say(f"the marked-up attachment is {marked}, not a 600x960 PNG")
    elif marked["spreads"][0] > 3 or marked["spreads"][1] < 150:
        say(f"the blur is not flat ({marked['spreads'][0]}) or the pattern beside it is gone ({marked['spreads'][1]})")
    if [f["name"] for f in files[1:]] != [f["name"] for f in stored(page, count=3)[1:]]:
        say("the other attachments changed")

    # Again: the original with every step back; all undone and Done gives back the very file.
    card(page, 0).locator(".attachment-markup").click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    if marks(page) != 6 or size(page) != [600, 960]:
        say(f"the second markup opened with {marks(page)} steps at {size(page)}, not 6 at 600x960")
    for _ in range(6):
        page.keyboard.press("Control+z")
    if size(page) != [1280, 800]:
        say(f"undoing every step left {size(page)}, not the original's 1280x800")
    page.locator(".annotate-attach").click()
    page.wait_for_selector(".annotate", state="detached", timeout=20000)
    back = stored(page, count=3)[0]
    if [back["name"], back["bytes"]] != [original["name"], original["bytes"]]:
        say(f"undoing everything gave {back}, not the original {original}")

    # Cancel leaves it alone.
    card(page, 0).locator(".attachment-markup").click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    drag(page, page.locator(".annotate-sheet").bounding_box(), (0.1, 0.1), (0.4, 0.4))
    page.locator(".annotate-cancel").click()
    page.wait_for_selector(".annotate", state="detached", timeout=5000)
    page.wait_for_timeout(400)
    after = stored(page, count=3)[0]
    if [after["name"], after["bytes"]] != [original["name"], original["bytes"]]:
        say(f"Cancel changed the attachment: {after}")
    context.close()


def touch(cdp, points: list[tuple[float, float]]) -> None:  # type: ignore[no-untyped-def]
    """One finger along the points, as a phone's screen reports it."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": points[0][0], "y": points[0][1]}]})
    for x, y in points[1:]:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def line(box, start: tuple[float, float], end: tuple[float, float], steps: int = 10) -> list[tuple[float, float]]:  # type: ignore[no-untyped-def]
    return [(box["x"] + box["width"] * (start[0] + (end[0] - start[0]) * i / steps), box["y"] + box["height"] * (start[1] + (end[1] - start[1]) * i / steps)) for i in range(steps + 1)]


def phone(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[ru] phone: {text}")  # noqa: E731
    context, page = open_composer(browser, "ru", "light", viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
    page.evaluate(PASTE, [1170, 2532, "image/png", "image.png"])
    page.wait_for_selector(".composer-box .attachments .attachment", timeout=5000)
    page.wait_for_timeout(300)
    if page.locator(".annotate").count():
        say("a paste opened the editor uninvited")
    pasted = stored(page, count=1)
    tile = card(page, 0).locator(".attachment-open").bounding_box()
    pencil = card(page, 0).locator(".attachment-markup").bounding_box()
    remove = card(page, 0).locator(".attachment-x").bounding_box()
    if not tile or not pencil or not remove:
        say(f"the tile, its pencil or its remove button is missing: {tile}, {pencil}, {remove}")
        context.close()
        return
    inside = tile["x"] <= pencil["x"] and pencil["x"] + pencil["width"] <= tile["x"] + tile["width"] + 0.5 and pencil["y"] + pencil["height"] <= tile["y"] + tile["height"] + 0.5
    apart = pencil["y"] >= remove["y"] + remove["height"]
    if not inside or not apart:
        say(f"the pencil {pencil} is outside the tile {tile} or on the remove button {remove}")
    keep(page, "markup-phone-card")
    card(page, 0).locator(".attachment-markup").tap()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=5000)
    wide = page.evaluate("() => document.scrollingElement.scrollWidth")
    if wide > 390:
        say(f"the editor scrolls the page sideways: {wide}")
    strip = page.locator(".annotate-strip").bounding_box()
    sheet = page.locator(".annotate-sheet").bounding_box()
    bar = page.locator(".annotate-bar").bounding_box()
    if not strip or strip["y"] + strip["height"] > 844.5 or (sheet and (sheet["y"] + sheet["height"] > strip["y"] + 0.5 or sheet["y"] < bar["y"] + bar["height"] - 0.5)):
        say(f"the picture {sheet} overlaps the bar {bar} or the tools {strip}, or the tools leave the screen")
    cdp = context.new_cdp_session(page)
    page.locator(".annotate-tool[data-tool='pen']").tap()
    touch(cdp, line(sheet, (0.2, 0.2), (0.7, 0.3), 16))
    # A finger lifts and moves to the tools in a few hundred milliseconds. The stroke above arrives all
    # at once, which the browser reads as a fling, and a tap straight after a fling only stops it.
    page.wait_for_timeout(500)
    page.locator(".annotate-tool[data-tool='arrow']").tap()
    if page.locator(".annotate").get_attribute("data-tool") != "arrow":
        say("a tap on the arrow after a stroke did not pick it")
    touch(cdp, line(sheet, (0.7, 0.7), (0.4, 0.45)))
    if marks(page) != 2:
        say(f"a finger's pen stroke and arrow left {marks(page)} steps, not 2")
    # A second finger mid-stroke: a pinch, not a line.
    first = line(sheet, (0.2, 0.6), (0.5, 0.6), 4)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": first[0][0], "y": first[0][1]}]})
    for x, y in first[1:]:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
    two = [{"x": first[-1][0], "y": first[-1][1], "id": 0}, {"x": first[-1][0], "y": first[-1][1] + 80, "id": 1}]
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": two})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    if marks(page) != 2:
        say(f"a second finger mid-stroke left {marks(page)} steps; the stroke should have been dropped")
    keep(page, "markup-phone")
    page.locator(".annotate-attach").tap()
    page.wait_for_selector(".annotate", state="detached", timeout=20000)
    got = stored(page, count=1)
    print(f"[ru] phone: {got}")
    if not got or got[0]["type"] != "image/png" or [got[0].get("width"), got[0].get("height")] != [1170, 2532]:
        say(f"Done left {got}, not one 1170x2532 PNG")
    elif pasted and got[0]["bytes"] == pasted[0]["bytes"]:
        say("Done left the pasted picture as it was, without the strokes")
    context.close()


LONG_TASKS = """() => { window.__long = []; new PerformanceObserver((list) => { for (const e of list.getEntries()) window.__long.push(e.duration); }).observe({ type: 'longtask', buffered: false }); }"""


def large(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[en] large: {text}")  # noqa: E731
    context, page = open_composer(browser, "en", "light", viewport={"width": 1440, "height": 900})
    cdp = context.new_cdp_session(page)
    cdp.send("Performance.enable")
    heap = lambda: next(m["value"] for m in cdp.send("Performance.getMetrics")["metrics"] if m["name"] == "JSHeapUsedSize") / 1e6  # noqa: E731
    page.evaluate(PASTE, [5120, 2880, "image/png", "image.png"])
    page.wait_for_selector(".composer-box .attachments .attachment-markup", timeout=20000)
    before = heap()
    start = time.monotonic()
    card(page, 0).locator(".attachment-markup").click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=20000)
    opened = time.monotonic() - start
    if size(page) != [5120, 2880]:
        say(f"the editor holds the screenshot at {size(page)}")
    page.evaluate(LONG_TASKS)
    sheet = page.locator(".annotate-sheet").bounding_box()
    page.keyboard.press("p")
    start = time.monotonic()
    drag(page, sheet, (0.1, 0.5), (0.9, 0.6), steps=60)
    stroke = time.monotonic() - start
    page.keyboard.press("b")
    start = time.monotonic()
    drag(page, sheet, (0.0, 0.0), (1.0, 1.0), steps=4)
    page.wait_for_function("() => document.querySelector('.annotate').dataset.marks === '2'", timeout=20000)
    blur = time.monotonic() - start
    long_tasks = page.evaluate("() => window.__long")
    # The stroke under the pointer has a canvas of its own, the size of the picture on screen; only the
    # one the picture is drawn on may be the picture's size.
    canvases = page.evaluate("() => Array.from(document.querySelectorAll('canvas')).map((c) => c.width * c.height).filter((n) => n >= 5120 * 2880 / 2).length")
    during = heap()
    start = time.monotonic()
    page.locator(".annotate-attach").click()
    page.wait_for_selector(".annotate", state="detached", timeout=60000)
    done = time.monotonic() - start
    got = stored(page, count=1)
    print(f"[en] large 5120x2880: open {opened:.2f} s, 60-move stroke {stroke:.2f} s, full-picture blur {blur:.2f} s, Done {done:.2f} s;"
          f" long tasks during drawing {[round(d) for d in long_tasks]} ms; picture-sized canvases on the page {canvases};"
          f" JS heap {before:.0f} -> {during:.0f} MB; result {got[0] if got else None}")
    if canvases > 1:
        say(f"{canvases} picture-sized canvases on the page while drawing, not one")
    if opened > 8 or done > 30:
        say(f"too slow: open {opened:.1f} s, Done {done:.1f} s")
    if not got or [got[0].get("width"), got[0].get("height")] != [5120, 2880]:
        say(f"Done left {got}")

    page.locator(".composer-box .attachment-x").first.click()
    page.evaluate(PASTE, [8000, 6000, "image/jpeg", "image.jpg"])
    page.wait_for_selector(".composer-box .attachments .attachment-markup", timeout=30000)
    card(page, 0).locator(".attachment-markup").click()
    page.wait_for_selector(".annotate .annotate-canvas", timeout=30000)
    width, height = size(page)
    print(f"[en] large 8000x6000: drawn at {width}x{height}")
    if width * height > 16_000_000 or abs(width / height - 8000 / 6000) > 0.01:
        say(f"the 48-megapixel photo is drawn at {width}x{height}, over the cap or out of proportion")
    page.keyboard.press("Escape")
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desktop(browser, problems)
        phone(browser, problems)
        large(browser, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
