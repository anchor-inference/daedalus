"""Drag the sidebar's edge and the panel's edge in a real browser, and refuse a drag that lags.

Resizing used to feel jerky and oddly smooth at once: every pointermove set React state, so the shell
re-rendered and wrote storage per event, and the sidebar's CSS width transition chased each new value
for 180 ms, so the edge fell behind the pointer and then glided after it. The drag now writes the
width straight onto the page once a frame (a custom property on the shell or the chat area), with
every transition of the panes off under `body.resizing`, and hands the width to React and storage
once, when the pointer lets go.

What is measured, at 1440 × 900, for both edges:

- after each step of the pointer, one frame later, the pane's width is its starting width plus the
  pointer's travel (minus it, for the panel on the right) within 1 px;
- while the pointer is down, no transition is active on the sidebar, the conversation's column, the
  chat area or the panel (their computed transition-duration is 0 s);
- React commits no more while the pointer moves than while it is still (counted through the devtools
  hook, the one place every commit passes), and storage is written once, on release;
- no task of the page's main thread takes more than 50 ms during the drags, counted in the thread's
  own CPU time on a processor calibrated in the same page (see main_thread.py), so a machine busy
  with other work does not fail the drag and a slow handler still does;
- the width survives a reload.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_pane_resize.py

`ASSERT=0` measures without judging (the way an older build is read for a before/after table).
Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from main_thread import Watch, prepare  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ASSERT = os.environ.get("ASSERT", "1") != "0"

STEPS = 12
# Twelve steps of 5 px move the edge 60 px: from the sidebar's 288 px default that ends at 348, inside
# its 360 px bound. At 7 px a step the drag ran into the bound once the default went from 272 to 288,
# and the clamp read as an edge straying from the pointer.
STRIDE = 5

# Before the app loads: React's commits are counted through a stand-in devtools hook (react-dom
# reports every commit to one when it is present), and writes of a pane's width to storage are counted.
PROBES = """
window.__commits = 0;
window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
  supportsFiber: true, isDisabled: false, renderers: new Map(),
  inject() { return 1; }, checkDCE() {}, onScheduleFiberRoot() {},
  onCommitFiberRoot() { window.__commits += 1; }, onCommitFiberUnmount() {}, onPostCommitFiberRoot() {},
};
window.__widthWrites = 0;
const setItem = Storage.prototype.setItem;
Storage.prototype.setItem = function (key, value) {
  if (String(key).startsWith('daedalus.width.')) window.__widthWrites += 1;
  return setItem.call(this, key, value);
};
try { localStorage.setItem('daedalus.session.panel', 'details'); localStorage.setItem('daedalus.sidebar', 'open'); } catch (e) {}
"""

FRAME = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))"

TRANSITIONS = """() => {
  const read = (s) => { const el = document.querySelector(s); if (!el) return null; const c = getComputedStyle(el); return { d: c.transitionDuration, p: c.transitionProperty }; };
  return { body: document.body.classList.contains('resizing'), sidebar: read('nav.sidebar'), main: read('.main'), chat: read('.chat-body'), panel: read('.panel') };
}"""


def width(page: Page, sel: str) -> float:
    return page.evaluate(f"() => document.querySelector('{sel}').getBoundingClientRect().width")


def drag(page: Page, name: str, handle: str, pane: str, sign: int) -> dict:
    """Walk the pointer STEPS × STRIDE px, one move at a time, and read the pane after each frame."""
    box = page.locator(handle).bounding_box()
    assert box, f"{name}: no handle"
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    start = width(page, pane)
    page.mouse.move(x, y)
    page.evaluate(FRAME)
    commits, writes = page.evaluate("() => [window.__commits, window.__widthWrites]")
    watch = Watch(page.context.browser, page)
    watch.begin()
    moving_from = page.evaluate("() => performance.now()")
    page.mouse.down()
    page.evaluate(FRAME)
    off: list[float] = []
    running: list[dict] = []
    for i in range(1, STEPS + 1):
        page.mouse.move(x + i * STRIDE * sign, y)
        page.evaluate(FRAME)
        want = start + i * STRIDE
        off.append(round(width(page, pane) - want, 2))
        if i in (1, STEPS // 2, STEPS):
            running.append(page.evaluate(TRANSITIONS))
    during = page.evaluate("() => [window.__commits, window.__widthWrites]")
    moved_for = page.evaluate("() => performance.now()") - moving_from
    page.mouse.up()
    page.evaluate(FRAME)
    page.wait_for_timeout(300)
    work = watch.end()
    after = page.evaluate("() => [window.__commits, window.__widthWrites]")
    # The page commits now and then with nobody touching it (the conversation's poll, a clock): the
    # baseline is a stretch with the pointer still that lasts as long as the drag did. It used to be
    # twelve frames, a fraction of a second, against a drag that on a loaded machine takes seconds of
    # round trips: the drag then caught two polls and the still stretch none, "7 commits against 0".
    idle = page.evaluate("() => window.__commits")
    page.wait_for_timeout(max(100, int(moved_for)))
    idle = page.evaluate("() => window.__commits") - idle
    return {
        "start": round(start, 1),
        "end": round(width(page, pane), 1),
        "off": off,
        "transitions": running,
        "commitsWhileMoving": during[0] - commits,
        "commitsWhileStill": idle,
        "writesWhileMoving": during[1] - writes,
        "writesOnRelease": after[1] - during[1],
        "longTasks": work.tasks[:5],
        "taskBudget": round(work.budget),
        "work": work.describe(),
    }


def judge(name: str, m: dict) -> list[str]:
    problems: list[str] = []
    worst = max(abs(o) for o in m["off"])
    if worst > 1:
        problems.append(f"{name}: the edge strays {worst}px from the pointer ({m['off']})")
    for t in m["transitions"]:
        if not t["body"]:
            problems.append(f"{name}: the body is not marked as resizing during the drag")
        for part in ("sidebar", "main", "chat", "panel"):
            got = t[part]
            if got and any(float(d.rstrip("s") or 0) > 0 for d in got["d"].split(",")):
                problems.append(f"{name}: a transition is active on {part} during the drag: {got}")
    # The page's own ticking commits a few times either way; a commit per move is the defect, and
    # would show as STEPS more than the still baseline. The margin is half of that, not two: the
    # conversation's once-a-second poll commits up to three times in a burst, and whether that burst
    # falls in the still stretch or the moving one is a matter of a few milliseconds of load order.
    if m["commitsWhileMoving"] - m["commitsWhileStill"] > STEPS // 2:
        problems.append(f"{name}: React committed {m['commitsWhileMoving']} times in {STEPS} moves, against {m['commitsWhileStill']} with the pointer still")
    if m["writesWhileMoving"]:
        problems.append(f"{name}: the width was written to storage {m['writesWhileMoving']} times during the drag")
    if m["writesOnRelease"] != 1:
        problems.append(f"{name}: the width was written {m['writesOnRelease']} times on release, not once")
    if any(d > m["taskBudget"] for d in m["longTasks"]):
        problems.append(f"{name}: a task over the budget during the drag: {m['work']}")
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(PROBES)
        page = context.new_page()
        prepare(page)
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
        page.wait_for_selector(".panel.shown", timeout=15000)
        page.wait_for_timeout(1200)

        sidebar = drag(page, "sidebar", ".desktop-column > .pane-handle", ".desktop-column", 1)
        panel = drag(page, "panel", ".panel > .pane-handle", ".panel", -1)
        print("sidebar", json.dumps(sidebar))
        print("panel", json.dumps(panel))

        # The widths are kept: a reload draws both where the drags left them.
        page.reload()
        page.wait_for_selector(".panel.shown", timeout=15000)
        page.wait_for_timeout(800)
        kept = {"sidebar": round(width(page, ".desktop-column"), 1), "panel": round(width(page, ".panel"), 1)}
        print("after reload", kept)
        browser.close()
    if ASSERT:
        problems += judge("sidebar", sidebar)
        problems += judge("panel", panel)
        if abs(kept["sidebar"] - sidebar["end"]) > 1:
            problems.append(f"the sidebar's width did not survive a reload: {sidebar['end']} → {kept['sidebar']}")
        if abs(kept["panel"] - panel["end"]) > 3:
            problems.append(f"the panel's width did not survive a reload: {panel['end']} → {kept['panel']}")
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
