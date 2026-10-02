"""Does the session screen scroll the document itself on a phone?

A phone must scroll only the message list: when the page itself scrolls, the header or the
composer leaves the screen and has to be dragged back. Two situations are checked:

  * plain phone viewports;
  * a visible area smaller than the layout viewport (Android WebView chrome, a soft keyboard),
    which is what `100dvh` gets wrong — the shell is then taller than the screen.

The harness is the real stylesheet with the chat shell's markup. Exit 0 when neither the
document scrolls nor the header/composer leave the visible area.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
HARNESS = (HERE / "harness.html").resolve()  # loads ../../miniapp/src/ui/styles.css, the shipped stylesheet
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
VIEWPORTS = [
    ("phone-portrait", 390, 844, 0),
    ("small-phone", 360, 640, 0),
    ("phone-landscape", 844, 390, 0),
    # The visible area is 300px shorter than the layout viewport: a soft keyboard is open.
    ("phone-keyboard", 390, 844, 300),
]

failures = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROMIUM)
    for name, w, h, shrink in VIEWPORTS:
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2, is_mobile=True, has_touch=True)
        if shrink:
            # Emulate a visible area smaller than the layout viewport, the way a keyboard does: the page
            # keeps its width and layout height, visualViewport reports less.
            page.add_init_script(
                "(() => { const real = window.visualViewport; const shrink = " + str(shrink) + ";"
                " const fake = { get width() { return real ? real.width : window.innerWidth; },"
                "   get height() { return (real ? real.height : window.innerHeight) - shrink; },"
                "   get offsetTop() { return 0; }, get offsetLeft() { return 0; },"
                "   get pageTop() { return 0; }, get pageLeft() { return 0; }, get scale() { return 1; },"
                "   addEventListener: (...a) => real && real.addEventListener(...a),"
                "   removeEventListener: (...a) => real && real.removeEventListener(...a) };"
                " Object.defineProperty(window, 'visualViewport', { get: () => fake }); })()"
            )
        page.goto(HARNESS.as_uri())
        page.wait_for_timeout(250)
        visible_h = page.evaluate("() => (window.visualViewport ? window.visualViewport.height : window.innerHeight)")
        doc = page.evaluate(
            "() => ({ scrollH: document.documentElement.scrollHeight, clientH: document.documentElement.clientHeight,"
            " appH: document.querySelector('.app').getBoundingClientRect().height,"
            " listScrollable: (() => { const el = document.querySelector('.chat-scroll');"
            " return el ? el.scrollHeight > el.clientHeight + 1 : false; })() })"
        )
        page.evaluate("() => window.scrollTo(0, 10000)")
        page.wait_for_timeout(120)
        after = page.evaluate(
            "() => ({ y: window.scrollY,"
            " head: document.querySelector('.chat-head').getBoundingClientRect().top,"
            " comp: document.querySelector('.composer').getBoundingClientRect().bottom })"
        )
        if os.environ.get("SHOTS"):
            page.screenshot(path=str(HERE / f"{name}.png"))
        page.close()
        problems = []
        if doc["appH"] > visible_h + 1:
            problems.append(f"shell {doc['appH']:.0f}px taller than the visible {visible_h:.0f}px")
        if after["y"] > 1:
            problems.append(f"the document scrolled to y={after['y']}")
        if after["head"] < -1:
            problems.append(f"header pushed off the top ({after['head']:.0f}px)")
        if after["comp"] > visible_h + 1:
            problems.append(f"composer below the visible area (bottom {after['comp']:.0f} > {visible_h:.0f})")
        if not doc["listScrollable"]:
            problems.append("the message list does not scroll")
        print(f"{name} {w}x{h}{f' (visible {visible_h:.0f})' if shrink else ''}: " + ("OK" if not problems else "FAIL: " + "; ".join(problems)))
        if problems:
            failures.append(name)
    browser.close()

print("failures:", failures or "none")
sys.exit(1 if failures else 0)
