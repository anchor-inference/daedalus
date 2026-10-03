"""Measure the shell's sizes in a real browser and refuse the ones that grew back.

The stylesheet's own test (miniapp/src/density.test.ts) reads the rules; this reads the rendered
page, which is the only place a row's height or a column's width actually exists. Everything the
app asks for is answered by the invented installation in screenshots.py, so the numbers describe
the layout and nothing else. Three windows: a laptop, an ultrawide, a phone; and Settings' column
and rows at the three sizes the owner uses.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_density.py

Exit 0 when every size is within its limit. `MEASURE=path.json` writes what was measured (and, with
`ASSERT=0`, measures without judging — the way a build from before the redesign is read for the
before/after table).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import PROJECTS, S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ASSERT = os.environ.get("ASSERT", "1") != "0"
MEASURE = os.environ.get("MEASURE", "")

OPEN_FOLDERS = "try { " + " ".join(f"localStorage.setItem('daedalus.folder.{k}', '1');" for k in [p["id"] for p in PROJECTS]) + " } catch (e) {}"

# One reading of the page: rectangles and computed sizes of the parts the redesign is measured by.
# Every selector has a fallback to the name the same part had before, so the script reads both builds.
READ = """
() => {
  const px = (el, prop) => el ? parseFloat(getComputedStyle(el)[prop]) : null;
  const box = (el) => el ? { w: Math.round(el.getBoundingClientRect().width), h: Math.round(el.getBoundingClientRect().height) } : null;
  const one = (...sels) => { for (const s of sels) { const el = document.querySelector(s); if (el) return el; } return null; };
  const all = (...sels) => { for (const s of sels) { const list = document.querySelectorAll(s); if (list.length) return [...list]; } return []; };
  const rows = all('.sidebar .erow', '.session-list-pane .erow', '.agents-screen .erow');
  const single = rows.filter((r) => !r.querySelector('.erow-meta') && !r.querySelector('.erow-line2'));
  const double = rows.filter((r) => r.querySelector('.erow-meta') || r.querySelector('.erow-line2'));
  const acts = all('.act:not(.head)');
  // Only what is drawn: the desktop-only buttons are display:none on a phone and measure 0×0.
  const icons = all('.chat-head .iconbtn', '.pagehead .iconbtn').filter((el) => el.getBoundingClientRect().width > 0);
  return {
    body: px(document.body, 'fontSize'),
    left: box(one('.desktop-column')),
    list: box(one('.session-list-pane')),
    aside: box(one('.session-aside')),
    panel: box(one('.panel')),
    panelTabs: box(one('.panel-tabs')),
    headStatus: box(one('.chat-head .head-status')),
    headModel: box(one('.composer .model-select')),
    composerRow: box(one('.composer-row')),
    subMeta: box(one('.chat-head .sub.meta')),
    chat: box(one('.chat')),
    head: box(one('.chat-head')),
    timeline: box(one('.timeline')),
    composerBox: box(one('.composer-box')),
    roundbtn: box(one('.roundbtn')),
    answerFs: px(one('.answer'), 'fontSize'),
    userFs: px(one('.msg.user'), 'fontSize'),
    userW: box(one('.msg.user')),
    rowSingle: single.map((r) => box(r).h),
    rowDouble: double.map((r) => box(r).h),
    rowFs: px(one('.erow-title'), 'fontSize'),
    avatar: box(one('.sidebar .avatar', '.session-list-pane .avatar', '.agents-screen .avatar')),
    act: acts.map((a) => box(a).h),
    actFs: px(acts[0], 'fontSize'),
    iconbtn: icons.map(box),
    chips: all('.chat-head .chip').map(box),
    menuBtn: box(one('.rail [data-rail="menu"]', '.sidebar-menu')),
    rail: box(one('.rail')),
  };
}
"""


def open_page(context, route: str, wait: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang=en")
    page.wait_for_selector(wait, timeout=15000)
    page.wait_for_timeout(600)
    return page


def expand_steps(page: Page) -> None:
    steps = page.get_by_text("8 steps")
    if steps.count():
        steps.first.click()
        page.wait_for_timeout(500)


def measure_session(browser, width: int, height: int, mobile: bool) -> dict:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=mobile, has_touch=mobile)
    context.add_init_script(OPEN_FOLDERS + " try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
    page = open_page(context, f"agents/{S1}", ".chat-scroll .timeline")
    expand_steps(page)
    out = page.evaluate(READ)
    out["vw"] = width
    context.close()
    return out


def measure_agents(browser, width: int, height: int, mobile: bool) -> dict:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=mobile, has_touch=mobile)
    context.add_init_script(OPEN_FOLDERS)
    page = open_page(context, "agents", ".folder")
    out = page.evaluate(READ)
    out["vw"] = width
    context.close()
    return out


# The width of what stands left of the conversation: the rail, plus the sidebar when it is open.
LEFT = """() => {
  const w = (s) => { const el = document.querySelector(s); return el ? Math.round(el.getBoundingClientRect().width) : 0; };
  return { rail: w('.desktop-column'), sidebar: w('nav.sidebar'), main: Math.round(document.querySelector('.main').getBoundingClientRect().left) };
}"""


def check_sidebar(browser) -> dict:  # type: ignore[no-untyped-def]
    """The fold and the menu, which are behaviour rather than sizes: the rail left alone, its persistence, the popover's keyboard."""
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(OPEN_FOLDERS)
    page = open_page(context, f"agents/{S1}", ".sidebar")
    out: dict = {}
    out["open"] = page.evaluate(LEFT)
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)
    out["collapsed"] = page.evaluate(LEFT)
    page.reload()
    page.wait_for_selector(".rail", timeout=15000)
    page.wait_for_timeout(400)
    out["collapsedAfterReload"] = page.evaluate(LEFT)
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)
    out["reopened"] = page.evaluate(LEFT)

    page.locator('.rail [data-rail="menu"]').click()
    page.wait_for_selector(".navmenu[role='menu']", timeout=5000)
    out["menuItems"] = page.locator(".navmenu [role='menuitem']").count()
    out["menuLang"] = page.locator(".navmenu .lang").count()
    out["menuFocusInside"] = page.evaluate("() => !!document.activeElement && !!document.activeElement.closest('.navmenu')")
    out["menuBox"] = page.evaluate("() => { const r = document.querySelector('.navmenu').getBoundingClientRect(); return { x: Math.round(r.x), bottom: Math.round(innerHeight - r.bottom), w: Math.round(r.width) }; }")
    out["menuRow"] = page.evaluate("() => Math.round(document.querySelector('.navmenu [role=menuitem]').getBoundingClientRect().height)")
    page.keyboard.press("ArrowDown")
    out["arrowMoves"] = page.evaluate("() => document.activeElement === document.querySelectorAll('.navmenu [role=menuitem]')[1]")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    out["menuClosed"] = page.locator(".navmenu").count() == 0
    out["focusBack"] = page.evaluate("() => document.activeElement === document.querySelector('.rail [data-rail=\"menu\"]')")
    page.keyboard.press("Control+Shift+M")
    page.wait_for_timeout(300)
    out["shortcutOpens"] = page.locator(".navmenu[role='menu']").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.keyboard.press("g")
    page.keyboard.press("i")
    page.wait_for_timeout(500)
    out["gKeyNavigates"] = page.evaluate("() => location.pathname").endswith("/inbox")
    context.close()
    return out


def check_browser_preview(browser) -> list[str]:  # type: ignore[no-untyped-def]
    """The browser's corner preview: a 240 px card inside the conversation's column, never over the
    panel beside it, at 1440 and 1280 (check_browser_pip.py drives the rest of it)."""
    from browser_stub import BrowserStub, open_page, render_scenes

    problems: list[str] = []
    bs = BrowserStub(render_scenes(browser))
    bs.add("g1", scene="shop", owner_id=S1)
    for width in (1440, 1280):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark")
        page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?panel=details&token=t&lang=en", wait=".bp-pip")
        page.wait_for_timeout(400)
        m = page.evaluate("""() => {
          const r = (el) => { const b = el.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom, w: b.width }; };
          const pip = document.querySelector('.bp-pip');
          return { pip: r(pip), column: r(pip.parentElement), column_is_main: pip.parentElement.classList.contains('chat-main'), panel: r(document.querySelector('.panel')) };
        }""")
        print(f"preview-{width}", json.dumps(m))
        p, c, panel = m["pip"], m["column"], m["panel"]
        if not m["column_is_main"]:
            problems.append(f"{width}: the preview is not in the conversation's column")
        if p["x"] < c["x"] or p["r"] > c["r"] + 0.5 or p["y"] < c["y"]:
            problems.append(f"{width}: the preview {p} leaves its column {c}")
        if p["r"] > panel["x"] + 0.5:
            problems.append(f"{width}: the preview reaches over the panel ({p['r']} > {panel['x']})")
        if abs(p["w"] - 240) > 1:
            problems.append(f"{width}: the preview is {p['w']} px, not --pip-w")
        context.close()
    return problems


def check_settings(browser) -> list[str]:  # type: ignore[no-untyped-def]
    """Settings' column and rows as drawn: the column is min(960, pane - 96) and centred, a row is at
    least the touch height, its control ends at the column's right edge, and a phone does not scroll
    sideways. Widened from 760 on the owner's word that the column was too narrow."""
    problems: list[str] = []
    for width, height, mobile in ((1920, 1080, False), (1180, 820, False), (390, 844, True)):
        context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=mobile, has_touch=mobile)
        page = open_page(context, "settings/chat", ".settings-row")
        m = page.evaluate("""() => {
          const col = document.querySelector('.settings-col').getBoundingClientRect();
          const main = document.querySelector('.settings-main');
          const pane = main ? main.getBoundingClientRect() : null;
          const rows = [...document.querySelectorAll('.settings-row')].map((r) => {
            const b = r.getBoundingClientRect();
            const ctl = r.querySelector('.settings-row-ctl');
            return { h: Math.round(b.height), right: Math.round(b.right), ctlRight: ctl ? Math.round(ctl.getBoundingClientRect().right) : null };
          });
          return { col: { x: Math.round(col.x), w: Math.round(col.width), r: Math.round(col.right) }, pane: pane ? { x: Math.round(pane.x), w: Math.round(pane.width) } : null, rows,
                   scroll: [document.documentElement.scrollWidth, document.documentElement.clientWidth] };
        }""")
        print(f"settings-{width}", json.dumps({k: v for k, v in m.items() if k != "rows"}), f"{len(m['rows'])} rows")
        if m["pane"]:
            want = min(960, m["pane"]["w"] - 96)
            if abs(m["col"]["w"] - want) > 1:
                problems.append(f"settings {width}: the column is {m['col']['w']}px, not {want}")
            if abs((m["col"]["x"] - m["pane"]["x"]) - (m["pane"]["x"] + m["pane"]["w"] - m["col"]["r"])) > 2:
                problems.append(f"settings {width}: the column is off centre {m['col']} in {m['pane']}")
        for row in m["rows"]:
            if row["h"] < 44:
                problems.append(f"settings {width}: a row is {row['h']}px, under the touch height")
                break
        # Every control ends in one lane: the row's right edge (the number fields keep a units' slot inside it).
        lanes = {row["ctlRight"] for row in m["rows"] if row["ctlRight"] is not None}
        if lanes and max(lanes) - min(lanes) > 1:
            problems.append(f"settings {width}: the controls end at {sorted(lanes)}, not in one lane")
        if m["scroll"][0] > m["scroll"][1]:
            problems.append(f"settings {width}: the page scrolls sideways {m['scroll']}")
        context.close()
    return problems


def judge(m: dict) -> list[str]:
    problems: list[str] = []
    phone = m["vw"] < 1024
    if m["body"] != 14:
        problems.append(f"{m['vw']}: body is {m['body']}px, not 14")
    if not phone:
        if not m["left"] or m["left"]["w"] != 324:
            problems.append(f"{m['vw']}: the left column is {m['left']}, not 324 wide")
        if m["list"]:
            problems.append(f"{m['vw']}: a second list column is still there ({m['list']})")
    for h in m["rowSingle"]:
        if h > (44 if phone else 36):
            problems.append(f"{m['vw']}: a one-line row is {h}px")
    for h in m["rowDouble"]:
        if h > (60 if phone else 52):
            problems.append(f"{m['vw']}: a two-line row is {h}px")
    if m["avatar"] and m["avatar"]["w"] > 24:
        problems.append(f"{m['vw']}: an avatar in a row is {m['avatar']['w']}px")
    if m["head"] and m["head"]["h"] > 48:
        problems.append(f"{m['vw']}: the chat header is {m['head']['h']}px")
    if m["subMeta"]:
        problems.append(f"{m['vw']}: the header still has its second row of chips")
    if m["chips"]:
        problems.append(f"{m['vw']}: {len(m['chips'])} chip(s) are still in the header")
    if not phone:
        if not m["panel"]:
            problems.append(f"{m['vw']}: the panel is not open beside the conversation")
        else:
            if m["panel"]["w"] < 360 or m["panel"]["w"] > 0.65 * m["vw"]:
                problems.append(f"{m['vw']}: the panel is {m['panel']['w']}px, outside 360..65%")
            if not m["panelTabs"] or m["panelTabs"]["h"] != 40:
                problems.append(f"{m['vw']}: the panel's tab row is {m['panelTabs']}, not 40")
    elif m["panel"]:
        problems.append(f"{m['vw']}: a phone shows the panel as a column")
    # Running phones reserve the composer for steering; idle settings use a full touch target.
    if not m["headModel"] and not phone:
        problems.append(f"{m['vw']}: the composer has no model selector")
    elif m["headModel"] and m["headModel"]["h"] > (44 if phone else 32):
        problems.append(f"{m['vw']}: the model selector in the composer is {m['headModel']}")
    # The field grows independently; the controls remain one compact row.
    if m["composerRow"] and m["composerRow"]["h"] > 44:
        problems.append(f"{m['vw']}: the composer controls are {m['composerRow']['h']}px at rest")
    for h in m["act"]:
        if h > 28:
            problems.append(f"{m['vw']}: a step row is {h}px")
    want = 44 if phone else 32
    for b in m["iconbtn"]:
        if b["w"] != want or b["h"] != want:
            problems.append(f"{m['vw']}: an icon button is {b['w']}×{b['h']}, not {want}")
    if m["answerFs"] != (16 if phone else 15):
        problems.append(f"{m['vw']}: the answer is {m['answerFs']}px")
    if m["timeline"]:
        # The conversation and the field that answers it are one column. Two claims, both of them
        # about what went wrong before: the field is exactly as wide as the conversation above it
        # (they were offset against each other), and the stripe is never exceeded. How much room a
        # panel leaves is not arithmetic worth pinning here; the widening claim is made below.
        stripe = 920 if m["vw"] < 1600 else (1120 if m["vw"] < 2100 else 1320)
        if m["timeline"]["w"] > stripe:
            problems.append(f"{m['vw']}: the timeline is {m['timeline']['w']}px, over the {stripe} stripe")
        if m["vw"] >= 1024 and m["composerBox"] and abs(m["composerBox"]["w"] - m["timeline"]["w"]) > 2:
            problems.append(f"{m['vw']}: the composer is {m['composerBox']['w']}px against a {m['timeline']['w']}px timeline")
        # Beside the 42 % panel a 1440 window keeps a 647 px conversation column: 1440 less the 52 px
        # rail and the 272 px sidebar is a 1116 px chat area, 58 % of it the conversation, and the
        # timeline is that less the gutters. The rail took 52 px the old 677 px figure did not count
        # (the sidebar's strip then stood only where the column was folded), so the floor is 590.
        if m["vw"] == 1440 and m["timeline"]["w"] < 590:
            problems.append(f"1440: the timeline is {m['timeline']['w']}px beside the panel, narrower than 590")
    return problems


def judge_sidebar(s: dict) -> list[str]:
    problems: list[str] = []
    # A 52 px icon rail stands beside the contextual list, whose boundary reserves one pixel.
    # Folding retains a 52 px icon column; the conversation starts at its edge.
    want = {"open": (324, 271, 324), "collapsed": (52, 0, 52), "collapsedAfterReload": (52, 0, 52), "reopened": (324, 271, 324)}
    for key, (rail, sidebar, main) in want.items():
        got = s[key]
        if (got["rail"], got["sidebar"], got["main"]) != (rail, sidebar, main):
            problems.append(f"{key}: rail/sidebar/conversation start {got['rail']}/{got['sidebar']}/{got['main']}, not {rail}/{sidebar}/{main}")
    # The companion toggle lives beside the other secondary destinations.
    # Diagrams live in More while Calendar has a dedicated rail icon.
    if s["menuItems"] != 8:
        problems.append(f"the menu has {s['menuItems']} items")
    if not s["menuLang"]:
        problems.append("the menu has no language switch")
    # The menu opens from the rail's foot, beside the rail rather than over it.
    if not 48 <= s["menuBox"]["x"] <= 58 or s["menuBox"]["bottom"] < 8 or s["menuBox"]["w"] != 300:
        problems.append(f"the secondary menu is not over the list beside its rail trigger at 300 wide: {s['menuBox']}")
    if s["menuRow"] != 36:
        problems.append(f"a menu row is {s['menuRow']}px")
    for key in ("menuFocusInside", "arrowMoves", "menuClosed", "focusBack", "shortcutOpens", "gKeyNavigates"):
        if not s[key]:
            problems.append(f"{key} is false")
    return problems


def run() -> int:
    problems: list[str] = []
    measured: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for name, (w, h, mobile) in {"session-1440": (1440, 900, False), "session-2560": (2560, 1400, False), "session-390": (390, 844, True)}.items():
            measured[name] = measure_session(browser, w, h, mobile)
        for name, (w, h, mobile) in {"agents-1440": (1440, 900, False), "agents-390": (390, 844, True)}.items():
            measured[name] = measure_agents(browser, w, h, mobile)
        if ASSERT:
            measured["sidebar"] = check_sidebar(browser)
            problems += check_browser_preview(browser)
            problems += check_settings(browser)
        browser.close()
    for name, m in measured.items():
        print(name, json.dumps(m))
    if ASSERT:
        for name, m in measured.items():
            if name.startswith("session-"):
                problems += judge(m)
        problems += judge_sidebar(measured["sidebar"])
    if MEASURE:
        Path(MEASURE).write_text(json.dumps(measured, indent=1) + "\n")
    if ASSERT:
        # A wider window must read wider, not pad the sides: the owner's standing complaint.
        wide = {m["vw"]: m["timeline"]["w"] for n, m in measured.items() if n.startswith("session-") and m.get("timeline") and m["vw"] >= 1024}
        if len(wide) > 1 and wide[max(wide)] <= wide[min(wide)]:
            problems.append(f"the conversation does not widen with the window: {wide}")
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
