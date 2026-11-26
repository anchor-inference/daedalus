"""The Browser tool is bounded by the panel it is drawn in, at every width the panel takes.

The tool's stage once sized itself from what it held: in a panel wide enough to put the action log
beside the picture, a request of the agent's or a sign-in reason on one line made the stage as wide
as that line, the hand-over card and the form ran past the panel's edge, and the picture, centred in
the too-wide stage, showed a dark band and the left half of the page. This drives the tool through
its states at the window sizes and panel widths people use and refuses:

- any element of the tool that extends past the panel's content box (after the clipping of the
  tool's own scrolling areas, so a list scrolled out of view is not counted, but the area is);
- a picture that is not the whole page: its box outside the box it is drawn in, a proportion other
  than the page's, or a size other than the page fitted (letterboxed) into that box;
- a frame whose foot (who holds the page, the one action) is outside the panel;
- a hand-over card or sign-in form wider than a readable column (560 px) in a wide panel;
- the log beside the picture narrower than its 280 px column;
- the agent's name on its pointer cut by the picture's edge;
- the corner preview in the conversation reaching over the panel.

States: the agent acting on a page with the log open and folded, waiting for the operator with the
sign-in form, the operator recording, a recorded step replayed, the switch to the operator's own
Chrome and its empty state, and the corner preview. Sizes: windows of 1280, 1366, 1600, 1920 and
2560 with the panel at its default width, then at 1920 the panel dragged to 400, 520, 700 and 1000
px, and the panel expanded over the conversation.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py <port> <root with app -> miniapp/dist> &
    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_browser_panel_fit.py

Exit 0 when every state holds at every size.
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
# A request long enough that, on one line, it is wider than any panel: what the operator's agent
# wrote when it stopped at a payment provider's sign-in.
REASON = ("Sign in to robokassa.example.com to check LANGPT-1545 in Robokassa: the invoice of the "
          "26th was paid twice and the second payment has to be refunded before the end of the month")
WINDOWS = [(1280, 800), (1366, 768), (1600, 900), (1920, 1080), (2560, 1440)]
PANEL_WIDTHS = [400, 520, 700, 1000]
COLUMN = 560
LOG_COLUMN = 280

MEASURE = """(root) => {
  const body = document.querySelector('.panel .panel-body');
  if (!body) return { problems: ['no panel body'] };
  const B = body.getBoundingClientRect();
  const problems = [];
  const name = (el) => (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : el.tagName.toLowerCase());
  const tool = body.querySelector('.bp, .bp-empty');
  if (!tool) return { problems: ['no Browser tool in the panel'] };
  for (const el of [tool, ...tool.querySelectorAll('*')]) {
    const r = el.getBoundingClientRect();
    // A visually hidden control (a switch's input) is one pixel and placed anywhere.
    if (r.width <= 1 || r.height <= 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.position === 'fixed') continue;
    let L = r.left, R = r.right, T = r.top, D = r.bottom;
    for (let a = el.parentElement; a && a !== body; a = a.parentElement) {
      const s = getComputedStyle(a);
      const ar = a.getBoundingClientRect();
      if (s.overflowX !== 'visible') { L = Math.max(L, ar.left); R = Math.min(R, ar.right); }
      if (s.overflowY !== 'visible') { T = Math.max(T, ar.top); D = Math.min(D, ar.bottom); }
    }
    if (R - L <= 0 || D - T <= 0) continue;
    if (L < B.left - 1 || R > B.right + 1 || T < B.top - 1 || D > B.bottom + 1) {
      problems.push(`${name(el)} reaches ${Math.round(L - B.left)}..${Math.round(R - B.left)} x ${Math.round(T - B.top)}..${Math.round(D - B.top)} of a ${Math.round(B.width)} x ${Math.round(B.height)} panel`);
    }
  }
  const out = { panel: Math.round(B.width), problems: problems.slice(0, 6) };
  const bv = tool.querySelector('.bp-view .bv, .bp-stage-wrap > .bv');
  const canvas = bv && bv.querySelector('.bv-canvas.painted');
  if (bv && canvas && !tool.querySelector('.bp-replay')) {
    const v = bv.getBoundingClientRect();
    const c = canvas.getBoundingClientRect();
    const ratio = canvas.width / canvas.height;
    out.picture = [Math.round(c.width), Math.round(c.height), Math.round(v.width), Math.round(v.height)];
    if (c.left < v.left - 1 || c.right > v.right + 1 || c.top < v.top - 1 || c.bottom > v.bottom + 1) out.problems.push(`the picture ${out.picture} is outside the box it is drawn in`);
    if (Math.abs(c.width / c.height - ratio) > ratio * 0.01) out.problems.push(`the picture is ${Math.round(c.width)} x ${Math.round(c.height)}, not the page's ${canvas.width} x ${canvas.height} proportion`);
    const fit = Math.min(v.width / canvas.width, v.height / canvas.height);
    if (Math.abs(c.width - canvas.width * fit) > 2) out.problems.push(`the picture is ${Math.round(c.width)} wide where the page fitted into its box is ${Math.round(canvas.width * fit)}`);
  }
  const replay = tool.querySelector('.bp-replay .bp-replay-img');
  if (replay) {
    const stage = tool.querySelector('.bp-replay-stage').getBoundingClientRect();
    const r = replay.getBoundingClientRect();
    if (r.left < stage.left - 1 || r.right > stage.right + 1 || r.top < stage.top - 1 || r.bottom > stage.bottom + 1) out.problems.push('the replayed picture is outside its stage');
  }
  const frame = tool.querySelector('.bp-frame');
  const foot = frame && frame.querySelector('.bp-banner');
  if (foot) {
    const control = foot.querySelector('.bp-control');
    const fr = frame.getBoundingClientRect();
    if (control) {
      const k = control.getBoundingClientRect();
      if (k.right > fr.right + 1 || k.left < fr.left - 1 || k.bottom > fr.bottom + 1) out.problems.push('the foot\\'s action is outside the frame');
    }
  }
  for (const sel of ['.bp-stage-wrap > .bp-handover', '.bp-stage-wrap > .login-card']) {
    const card = tool.querySelector(sel);
    if (card && card.getBoundingClientRect().width > root.column + 1) out.problems.push(`${sel} is ${Math.round(card.getBoundingClientRect().width)} wide, wider than a readable column`);
  }
  const main = tool.querySelector('.bp-main');
  const log = tool.querySelector('.bp-log');
  if (main && log && getComputedStyle(main).flexDirection === 'row') {
    const w = log.getBoundingClientRect().width;
    if (w < root.log - 1) out.problems.push(`the log beside the picture is ${Math.round(w)} wide, not its ${root.log} px column`);
  }
  const tag = tool.querySelector('.bv-cursor-tag');
  if (tag && bv) {
    const t = tag.getBoundingClientRect();
    const v = (bv.querySelector('.bv-canvas') || bv).getBoundingClientRect();
    if (t.right > v.right + 1 || t.bottom > v.bottom + 1 || t.left < v.left - 1) out.problems.push('the agent\\'s name on its pointer is cut by the picture\\'s edge');
  }
  return out;
}"""


def set_panel_width(page, target: int) -> None:  # type: ignore[no-untyped-def]
    """Drag the panel's edge until it is ``target`` wide; the panel's right edge is the window's."""
    handle = page.locator(".panel > .pane-handle").bounding_box()
    panel = page.locator(".panel").bounding_box()
    assert handle and panel
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    to = panel["x"] + panel["width"] - target + (x - panel["x"])
    page.mouse.move(x, y)
    page.mouse.down()
    for i in range(1, 11):
        page.mouse.move(x + (to - x) * i / 10, y)
    page.mouse.up()
    page.mouse.move(5, 5)
    page.wait_for_timeout(350)


def sizes(page, measure):  # type: ignore[no-untyped-def]
    """Walk the sizes, calling ``measure(label)`` at each."""
    for w, h in WINDOWS:
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(350)
        measure(f"{w}x{h}")
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.wait_for_timeout(300)
    for target in PANEL_WIDTHS:
        set_panel_width(page, target)
        measure(f"1920 panel {target}")
    page.keyboard.press("Control+Shift+Period")
    page.wait_for_selector(".panel.full", timeout=5000)
    page.wait_for_timeout(400)
    measure("1920 expanded")
    page.keyboard.press("Control+Shift+Period")


def open_tool(browser, scenes, setup=None, user=None):  # type: ignore[no-untyped-def]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1, owner_label="Bakery site", acting=True)
    if user is not None:
        bs.user = user
    if setup:
        setup(bs)
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.browser.announced', JSON.stringify(['g1'])); } catch (e) {}")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=ru&panel=browser")
    return bs, page


def live(page) -> None:  # type: ignore[no-untyped-def]
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=15000)
    wait_frames(page, ROOT, 1)
    page.wait_for_timeout(400)


def needs(bs) -> None:  # type: ignore[no-untyped-def]
    bs.navigate("g1", "signin")
    bs.needs_you("g1", "login", REASON)
    bs.login_ask("g1", "robokassa.example.com", reason=REASON)


def recording(bs) -> None:  # type: ignore[no-untyped-def]
    g = bs.groups["g1"]
    g.recording = True
    bs.keyframe(g, "start")
    bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")


def run_state(browser, scenes, state: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{state}] {text}")  # noqa: E731
    user = None
    setup = None
    if state in ("handover",):
        setup = needs
    if state == "replay":
        setup = recording
    if state == "chrome-switch":
        user = {"env": "agent", "consented": True, "connected": True, "name": "Work"}
    if state == "your-chrome":
        user = {"env": "user", "consented": True, "connected": True, "name": "Work"}
    bs, page = open_tool(browser, scenes, setup, user)
    if state == "your-chrome":
        page.wait_for_selector(".panel .bp-empty .bp-switch", timeout=15000)
    else:
        live(page)
    if state == "log-folded":
        page.locator(f"{ROOT} .bp-log-head").click()
        page.wait_for_selector(f"{ROOT} .bp-log:not(.open)", timeout=5000)
    if state == "idle":
        # The agent clicks the cart in the page's top right corner: its name must turn inward.
        bs.act("g1", "click", "cart", name="Cart", element="the cart button")
        page.wait_for_selector(f"{ROOT} .bv-cursor-tag", timeout=5000)
        page.wait_for_timeout(400)
    if state == "recording":
        page.locator(f"{ROOT} .bp-control.take").click()
        page.wait_for_selector(f"{ROOT} .bv.driving", timeout=8000)
        page.locator(f"{ROOT} .bp-rec-btn").click()
        page.wait_for_selector(".bp-rec-start", timeout=5000)
        page.locator(".bp-rec-start .btn.primary").click()
        page.wait_for_selector(f"{ROOT} .bp-recbar", timeout=5000)
        bs.record_step("g1", action="type", element={"role": "searchbox", "name": "Search"}, slot="search", value="whole rye flour", submit=True)
        bs.record_step("g1", action="click", element={"role": "button", "name": "Add to cart"}, asks=["purchase"])
        page.wait_for_timeout(400)
    if state == "replay":
        row = page.locator(f"{ROOT} .bp-log-row", has=page.locator(".bp-log-frame")).first
        row.wait_for(timeout=10000)
        row.click()
        page.wait_for_selector(f"{ROOT} .bp-replay .bp-replay-img", timeout=5000)
        page.wait_for_timeout(400)
    page.mouse.move(5, 5)

    def measure(label: str) -> None:
        got = page.evaluate(MEASURE, {"column": COLUMN, "log": LOG_COLUMN})
        print(f"[{state}] {label}: panel {got.get('panel')}, picture {got.get('picture')}, {len(got['problems'])} problems")
        for p in got["problems"]:
            say(f"{label}: {p}")

    sizes(page, measure)
    page.context.close()


def run_pip(browser, scenes, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """The corner preview floats in the conversation's column and never over the panel beside it."""
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1, owner_label="Bakery site", acting=True)
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.browser.announced', JSON.stringify(['g1'])); } catch (e) {}")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=ru&panel=overview")
    page.wait_for_selector(".bp-pip", timeout=15000)
    wait_frames(page, ".bp-pip", 1)

    def measure(label: str) -> None:
        if label.endswith("expanded"):
            return
        got = page.evaluate("""() => {
          const pip = document.querySelector('.bp-pip').getBoundingClientRect();
          const panel = document.querySelector('.panel').getBoundingClientRect();
          const frame = document.querySelector('.bp-pip-frame').getBoundingClientRect();
          const canvas = document.querySelector('.bp-pip .bv-canvas');
          const c = canvas ? canvas.getBoundingClientRect() : null;
          return { pip: [pip.left, pip.right], panel: panel.left, frame: [frame.left, frame.right, frame.top, frame.bottom],
            canvas: c && [c.left, c.right, c.top, c.bottom], vw: innerWidth };
        }""")
        print(f"[pip] {label}: pip {[round(x) for x in got['pip']]}, panel at {round(got['panel'])}")
        if got["pip"][1] > got["panel"] + 1:
            problems.append(f"[pip] {label}: the corner preview reaches over the panel ({round(got['pip'][1])} > {round(got['panel'])})")
        if got["pip"][0] < 0:
            problems.append(f"[pip] {label}: the corner preview starts off the window")
        c, f = got["canvas"], got["frame"]
        if c and (c[0] < f[0] - 1 or c[1] > f[1] + 1 or c[2] < f[2] - 1 or c[3] > f[3] + 1):
            problems.append(f"[pip] {label}: the preview's picture is outside its frame")

    sizes(page, measure)
    page.context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for state in ("idle", "log-folded", "handover", "recording", "replay", "chrome-switch", "your-chrome"):
            run_state(browser, scenes, state, problems)
        run_pip(browser, scenes, problems)
        browser.close()
    for problem in problems:
        print("PROBLEM", problem)
    unhandled = UNHANDLED.report()
    if problems or unhandled:
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
