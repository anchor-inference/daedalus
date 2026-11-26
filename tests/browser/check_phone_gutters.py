"""On a phone every page and every tool of the chat's panel keeps one side gutter: 16 px.

At about 390 px the operator saw blocks against the screen's edges: in Settings → Security the
section's "Add secret" button stood past the right edge of the cards above it, and the header's
back button and the blocks did not share one left edge; in the chat's panel the Context tool drew
its percentage, its bar, its legend and its two figures about 6 px from the sheet's edge while the
tab row above had its normal inset. This opens, at 390 and 360 px, every page the drawer reaches
(each Settings section among them) and every tool of the chat's panel in its phone sheet, and
measures what is drawn: the glyphs of every line of text, every icon, every field, and every box
with a surface of its own (a background or a side border). It refuses:

- a left content edge other than 16 px from the screen's edge (one left line for the header, the
  titles, the labels, the cards and the buttons), unless everything drawn is centred (an empty
  state) and inside the gutters;
- anything closer than 16 px to the right edge;
- a box with a surface that is not full-bleed and ends past the 16 px line on either side.

Allowed to reach the edges, by name: a full-width frame (the page, a sheet, a bar: a surface as wide
as the screen), a divider (2 px or thinner), a picture, a table or code scroller (it scrolls inside
its own frame), a strip that scrolls sideways (its own box is measured, what scrolls in it is not),
and the sheet's head with its tab row, which is chrome in the place of the page header.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_phone_gutters.py

Exit 0 when every page and tool keeps the gutter at both widths.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
GUTTER = 16
WIDTHS = [390, 360]
SECTIONS = [s["id"] for s in json.loads((Path(__file__).resolve().parents[2] / "miniapp/src/screens/settings-index.json").read_text(encoding="utf-8"))["sections"] if s["id"] not in ("desktop", "address")]
PAGES = ["agents", "inbox", "board", "calendar", "changes", "terminals", "schedules", "canvas", "services", "memory", "usage", "health", "harnesses", "settings"] + [f"settings/{s}" for s in SECTIONS]
TOOLS = ["overview", "details", "context", "usage", "board", "changes", "files", "preview", "notes", "attachments", "canvas", "memory", "browser", "browser-live", "jobs", "subagents", "questions"]
TARGETS = ["chat"] + PAGES + [f"tool/{t}" for t in TOOLS]
SCENES: dict = {}

# What may reach past the gutter on purpose. Each is measured by its own box where it has one.
ALLOW = ", ".join([
    ".sheet-head", ".panel-tabs",                     # the sheet's head: chrome in the page header's place
    "img", "video", "canvas", ".bv", ".preview-image",  # pictures
    ".preview-grid", ".tablewrap",                    # tables that scroll inside their own frame
    ".drawer", ".notice-toast", ".toast",             # what floats over the page
])

MEASURE = """([gutter, allow, rootSel]) => {
  const vw = document.documentElement.clientWidth;
  const root = document.querySelector(rootSel) || document.body;
  const name = (el) => (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : el.tagName.toLowerCase());
  const label = (el) => name(el) + (el.textContent && el.textContent.trim() ? ' «' + el.textContent.trim().slice(0, 24) + '»' : '');
  // A text is clipped by its own box too (a title cut with an ellipsis); an element only by its ancestors.
  const clip = (el, r, self = false) => {
    let L = r.left, R = r.right;
    for (let a = self ? el : el.parentElement; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.overflowX !== 'visible') { const ar = a.getBoundingClientRect(); L = Math.max(L, ar.left); R = Math.min(R, ar.right); }
    }
    return [L, R];
  };
  const transparent = (c) => !c || c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c);
  const items = [];
  // What scrolls sideways in a strip (chips, tabs) is not measured: it is meant to pass under the
  // strip's edges. The strip itself is, where it draws a surface.
  const scrolled = (el) => { for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) { const s = getComputedStyle(a); if ((s.overflowX === 'auto' || s.overflowX === 'scroll') && a.scrollWidth > a.clientWidth + 1 && a.clientHeight < 120) return true; } return false; };
  const skip = (el) => el.closest(allow) || el.closest('[aria-hidden="true"]:not(svg)') || el.closest('[inert]') || scrolled(el);
  const visible = (el) => { const s = getComputedStyle(el); return s.visibility !== 'hidden' && parseFloat(s.opacity) > 0.05; };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || skip(el) || !visible(el)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      const [L, R] = clip(el, r, true);
      if (R - L > 0.5 && R > 0 && L < vw) items.push({ L, R, what: label(el), kind: 'text' });
    }
  }
  for (const el of root.querySelectorAll('*')) {
    if (skip(el) || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height <= 2) continue;
    const s = getComputedStyle(el);
    if (s.position === 'fixed' && r.width >= vw - 1) continue;
    const ink = ['svg', 'INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
    const surface = !ink && r.width < vw - 1 && (!transparent(s.backgroundColor) || (parseFloat(s.borderLeftWidth) > 0 && !transparent(s.borderLeftColor)) || (parseFloat(s.borderRightWidth) > 0 && !transparent(s.borderRightColor)));
    if (!ink && !surface) continue;
    if (el.tagName !== 'svg' && el.closest('svg')) continue;
    const [L, R] = clip(el, r);
    if (R - L > 0.5 && R > 0 && L < vw) items.push({ L, R, what: label(el), kind: ink ? 'ink' : 'surface' });
  }
  if (!items.length) return { left: null, right: null, near: [] };
  const left = Math.min(...items.map((i) => i.L));
  const right = vw - Math.max(...items.map((i) => i.R));
  const near = items.filter((i) => i.L < gutter - 1 || vw - i.R < gutter - 1).slice(0, 8).map((i) => `${i.kind} ${i.what} ${Math.round(i.L)}..${Math.round(i.R)}`);
  const leftmost = items.filter((i) => i.L < left + 0.5).slice(0, 3).map((i) => `${i.kind} ${i.what}`);
  return { left: Math.round(left * 10) / 10, right: Math.round(right * 10) / 10, near, leftmost, vw };
}"""


def phone_context(browser, width: int):  # type: ignore[no-untyped-def]
    return browser.new_context(viewport={"width": width, "height": 844}, device_scale_factor=1, is_mobile=True, has_touch=True, color_scheme="dark")


def install(page) -> None:  # type: ignore[no-untyped-def]
    page.route("**/api/**", stub)


def browser_stub(page):  # type: ignore[no-untyped-def]
    """The chat's browser on a page the agent is acting on, for the Browser tool with something in it."""
    from browser_stub import BrowserStub, render_scenes

    if not SCENES:
        SCENES.update(render_scenes(page.context.browser, sizes=((page.viewport_size["width"], 600),)))
    bs = BrowserStub(SCENES)
    bs.add("g1", scene="shop", owner_id=S1, owner_label="Bakery site", acting=True)
    bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")
    return bs


def visit(page, target: str) -> dict:  # type: ignore[no-untyped-def]
    """Open a page or a tool and measure it."""
    if target.startswith("tool/"):
        tool = target.split("/", 1)[1]
        if tool == "browser-live":
            tool = "browser"
            ctx = page.context
            page = ctx.new_page()
            bs = browser_stub(page)
            bs.install(page)
            page.route("**/api/**", lambda r: bs.route(r) if "/api/browsers" in r.request.url else stub(r))
            page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=ru&panel=browser")
            page.wait_for_selector(".panel-sheet .bp .bv[data-state='live']", timeout=15000)
            page.wait_for_timeout(900)
            got = page.evaluate(MEASURE, [GUTTER, ALLOW, ".panel-sheet"])
            page.close()
            return got
        page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=ru&panel={tool}")
        page.wait_for_selector(f".panel-sheet .panel-tab.on[data-tab='{tool}']", timeout=15000)
        page.wait_for_timeout(900)
        root = ".panel-sheet"
    elif target == "chat":
        page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=ru&panel=0")
        page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
        page.wait_for_timeout(900)
        root = "#root"
    else:
        page.goto(f"{BASE}/{target}?token=t&scheme=dark&lang=ru")
        page.wait_for_selector(".screen, .settings-page, .main, main", timeout=15000)
        page.wait_for_timeout(900)
        root = "#root"
    return page.evaluate(MEASURE, [GUTTER, ALLOW, root])


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for width in WIDTHS:
            context = phone_context(browser, width)
            page = context.new_page()
            install(page)
            for target in TARGETS:
                got = visit(page, target)
                print(f"{width} {target}: left {got['left']} right {got['right']}")
                if got["left"] is None:
                    problems.append(f"{width} {target}: nothing was drawn")
                    continue
                # Content centred in the screen (an empty state) has no left line of its own: it only
                # has to stay inside the gutters on both sides.
                centred = abs(got["left"] - got["right"]) <= 2 and got["left"] >= GUTTER - 1
                if abs(got["left"] - GUTTER) > 1 and not centred:
                    problems.append(f"{width} {target}: the left content edge is {got['left']} px, not {GUTTER} ({'; '.join(got['leftmost'])})")
                if got["right"] < GUTTER - 1:
                    problems.append(f"{width} {target}: content reaches {got['right']} px from the right edge ({'; '.join(got['near'])})")
            context.close()
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
