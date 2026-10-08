"""Measure the shell's sizes in a real browser and refuse the ones that grew back.

The stylesheet's own test (miniapp/src/density.test.ts) reads the rules; this reads the rendered
page, which is the only place a row's height or a column's width actually exists. Everything the
app asks for is answered by the invented installation in screenshots.py, so the numbers describe
the layout and nothing else. Three windows: a laptop, an ultrawide, a phone; and Settings' column
and rows at the three sizes the owner uses.

The phone has its own claims since the redesign modelled on the chat apps of the operator's phone
(check_phone): six type steps (12 / 13 / 14 / 15 / 16 / 17) on every drawn text of the home, the
Chats page, the drawer and the composer's sheets; a 52 px top bar with no rule under it; 54 px list
rows and 44 px destinations in the drawer; an idle composer of one 44 px row at the bottom of the
screen; no bottom bar outside a project; and a 44 px target for every control, counting the invisible
::after that a 34 px circle or a 28 px chip carries. The numbers are a step under the first cut of the
redesign (12 / 13 / 15 / 16 / 17 / 22, a 56 px bar, 60 px rows), which the operator measured as too
large next to Claude and ChatGPT on the same phone. The desktop claims below are unchanged.

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
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
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
  const phoneRows = all('.ph-row').map((r) => box(r).h);
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
    phoneRows,
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
    # A phone's list of chats is the Chats page now; its home holds only the live ones.
    page = open_page(context, "agents?view=chats", ".ph-row") if mobile else open_page(context, "agents", ".folder")
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


PHONE_STEPS = {12, 13, 14, 15, 16, 17}

# Everything a phone screen draws, read in one pass: the sizes of its text, its targets with their
# ::after hit areas, and the shell's parts. Code, terminals and the icons' own glyphs are not text.
READ_PHONE = """
(scope) => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const root = scope ? document.querySelector(scope) : document.body;
  const sizes = {};
  for (const el of root.querySelectorAll('*')) {
    if (el.closest('svg, code, pre, .mono, .term, .xterm, [inert]')) continue;
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own || !vis(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    (sizes[fs] = sizes[fs] || []).push((el.className && el.className.baseVal === undefined ? '.' + String(el.className).trim().split(/\s+/).join('.') : el.tagName.toLowerCase()) + ' ' + el.textContent.trim().slice(0, 24));
  }
  const targets = [];
  for (const el of root.querySelectorAll('button, a[href], [role="link"], [role="radio"], input, textarea')) {
    if (!vis(el) || el.closest('[inert]') || el.type === 'file') continue;
    const r = el.getBoundingClientRect();
    let w = r.width, h = r.height;
    // The composer's field is 34 px inside its 44 px pill, and a tap on the pill focuses the field.
    const pill = el.tagName === 'TEXTAREA' ? el.closest('.composer[data-shape] .composer-box') : null;
    if (pill) h = Math.max(h, pill.getBoundingClientRect().height);
    const after = getComputedStyle(el, '::after');
    if (after.content !== 'none' && after.position === 'absolute') {
      const px = (v) => (v.endsWith('px') ? parseFloat(v) : 0);
      w = r.width - px(after.left) - px(after.right);
      h = r.height - px(after.top) - px(after.bottom);
    }
    if (Math.min(w, h) < 43.5) targets.push(`${el.tagName.toLowerCase()}.${String(el.className).trim().split(/\s+/).join('.')} ${el.getAttribute('aria-label') || el.textContent.trim().slice(0, 20)} ${Math.round(w)}×${Math.round(h)}`);
  }
  const rect = (s) => { const el = document.querySelector(s); if (!el || !vis(el)) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), b: Math.round(innerHeight - r.bottom) }; };
  const top = document.querySelector('.ph-top, .pagehead');
  return {
    sizes, targets,
    top: rect('.ph-top') || rect('.pagehead-row'),
    topBorder: top ? parseFloat(getComputedStyle(top).borderBottomWidth) : null,
    topBg: top ? getComputedStyle(top).backgroundColor : null,
    pageBg: getComputedStyle(document.body).backgroundColor,
    idle: rect('.composer[data-shape="idle"] .composer-box'),
    open: rect('.composer[data-shape="open"] .composer-box'),
    circle: rect('.composer[data-shape] .roundbtn'),
    rows: [...document.querySelectorAll('.ph-row')].filter(vis).map((r) => Math.round(r.getBoundingClientRect().height)),
    chips: [...document.querySelectorAll('.ph-chip')].filter(vis).map((r) => Math.round(r.getBoundingClientRect().height)),
    nav: [...document.querySelectorAll('.ph-drawer-root.open .ph-nrow')].map((r) => Math.round(r.getBoundingClientRect().height)),
    drawer: rect('.ph-drawer-root.open .ph-drawer'),
    tabbar: document.querySelectorAll('nav.tabbar').length,
    scroll: [document.documentElement.scrollWidth, document.documentElement.clientWidth],
  };
}
"""


def check_phone(browser) -> tuple[list[str], dict]:  # type: ignore[no-untyped-def]
    """The phone redesign's claims at 412 × 915, in both languages: the shell, the home, Chats, the
    drawer and the composer, the Inbox, Terminals and a Settings page, each read where the operator
    meets it."""
    problems: list[str] = []
    seen: dict = {}
    for lang in ("en", "ru"):
        context = browser.new_context(viewport={"width": 412, "height": 915}, color_scheme="dark", is_mobile=True, has_touch=True)
        context.add_init_script(OPEN_FOLDERS)
        page = context.new_page()
        page.route("**/api/**", stub)

        # Defaults bind this window's page and language, not the loop's last ones.
        def go(route: str, wait: str, page: Page = page, lang: str = lang) -> None:
            page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang={lang}")
            page.wait_for_selector(wait, timeout=15000)
            page.wait_for_timeout(500)

        def read(name: str, scope: str | None = None, page: Page = page, lang: str = lang) -> dict:
            m = page.evaluate(READ_PHONE, scope)
            seen[f"{name}-{lang}"] = {k: v for k, v in m.items() if k != "sizes"} | {"sizes": sorted(float(k) for k in m["sizes"])}
            off = {k: v[:3] for k, v in m["sizes"].items() if float(k) not in PHONE_STEPS}
            if off:
                problems.append(f"{name} {lang}: text off the six steps {off}")
            if m["targets"]:
                problems.append(f"{name} {lang}: targets under 44 px {m['targets'][:4]}")
            if m["scroll"][0] > m["scroll"][1]:
                problems.append(f"{name} {lang}: the page scrolls sideways {m['scroll']}")
            return m

        go("agents", ".ph-home .composer")
        m = read("home", ".ph-home")
        if not m["top"] or m["top"]["h"] != 52:
            problems.append(f"home {lang}: the top bar is {m['top']}, not 52 tall")
        if m["topBorder"]:
            problems.append(f"home {lang}: the top bar has a {m['topBorder']} px rule")
        if m["topBg"] != m["pageBg"]:
            problems.append(f"home {lang}: the top bar {m['topBg']} is not the page colour {m['pageBg']}")
        if not m["idle"] or m["idle"]["h"] != 44:
            problems.append(f"home {lang}: the idle composer is {m['idle']}, not one 44 px row")
        elif m["idle"]["b"] > 24:
            problems.append(f"home {lang}: the idle composer stands {m['idle']['b']} px above the bottom")
        if not m["circle"] or (m["circle"]["w"], m["circle"]["h"]) != (34, 34):
            problems.append(f"home {lang}: the white circle is {m['circle']}, not 34 × 34")
        if m["tabbar"]:
            problems.append(f"home {lang}: a bottom bar is still drawn")
        for h in m["rows"]:
            if h != 54:
                problems.append(f"home {lang}: a live row is {h} px, not 54")

        page.locator(".ph-home .composer textarea").fill("Wire the order form to the sheet")
        page.wait_for_timeout(300)
        m = read("composer-open", ".ph-home .composer")
        if not m["open"]:
            problems.append(f"composer {lang}: typing does not open the composer onto its toolbar")
        if page.locator(".ph-home .composer .composer-tools > .mic").count() and page.locator(".ph-home .composer .composer-tools > .mic").is_visible():
            problems.append(f"composer {lang}: the mic stays beside a typed draft")
        page.locator(".ph-home .composer textarea").fill("")

        page.locator(".ph-menu").first.click()
        page.wait_for_selector(".ph-drawer-root.open", timeout=5000)
        page.wait_for_timeout(400)
        m = read("drawer", ".ph-drawer-root.open")
        if not m["drawer"] or m["drawer"]["w"] != 336:
            problems.append(f"drawer {lang}: {m['drawer']}, not 336 wide at 412")
        if any(h != 44 for h in m["nav"]):
            problems.append(f"drawer {lang}: destination rows {m['nav']}, not 44")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        if page.locator(".ph-drawer-root.open").count():
            problems.append(f"drawer {lang}: Escape does not close it")

        go("agents?view=chats", ".ph-row")
        m = read("chats", ".ph-chats")
        if any(h != 54 for h in m["rows"]):
            problems.append(f"chats {lang}: rows {sorted(set(m['rows']))}, not 54")
        if any(h != 28 for h in m["chips"]):
            problems.append(f"chats {lang}: chips {sorted(set(m['chips']))}, not 28")
        page.locator(".ph-row-more").first.click()
        page.wait_for_selector(".ph-actions", timeout=5000)
        page.wait_for_timeout(300)
        read("row-sheet", ".ph-actions")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        # The same sheet on a finger held still, and the quick actions on a swipe to the left.
        row = ".ph-chats .ph-row"
        page.evaluate("""(sel) => { const el = document.querySelector(sel); const r = el.getBoundingClientRect();
          el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', clientX: r.x + 120, clientY: r.y + 20 })); }""", row)
        page.wait_for_timeout(700)
        if not page.locator(".ph-actions").count():
            problems.append(f"chats {lang}: a long press does not open the row's sheet")
        page.evaluate("(sel) => document.querySelector(sel).dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerType: 'touch' }))", row)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.evaluate("""(sel) => { const el = document.querySelector(sel); const r = el.getBoundingClientRect();
          const at = (type, x) => el.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerType: 'touch', clientX: x, clientY: r.y + 20 }));
          at('pointerdown', r.x + 300); at('pointermove', r.x + 280); at('pointermove', r.x + 200); at('pointermove', r.x + 60); at('pointerup', r.x + 60); }""", row)
        page.wait_for_timeout(400)
        if page.locator(".ph-swipe.end .ph-swipe-act").count() < 2:
            problems.append(f"chats {lang}: a swipe to the left does not reveal the row's quick actions")

        # The session: the same 52 px bar with a two-line title and at most two glyphs, the answer's
        # icon row in 44 px targets, the idle composer, and the ⋮ sheet with its tiles.
        go(f"agents/{S1}", ".ph-chat-top")
        m = read("session", ".chat")
        top = page.locator(".ph-chat-top").bounding_box()
        if not top or round(top["height"]) != 52:
            problems.append(f"session {lang}: the bar is {top}, not 52 tall")
        if page.evaluate("parseFloat(getComputedStyle(document.querySelector('.ph-chat-top')).borderBottomWidth)"):
            problems.append(f"session {lang}: the bar has a rule under it")
        glyphs = page.locator(".ph-chat-top .ph-top-actions > *").count()
        if glyphs > 2:
            problems.append(f"session {lang}: {glyphs} glyphs at the right of the bar, more than two")
        if not page.locator(".ph-chat-top .ph-top-sub").count():
            problems.append(f"session {lang}: the title has no second line")
        if not m["idle"] or m["idle"]["h"] != 44:
            problems.append(f"session {lang}: the idle composer is {m['idle']}, not one 44 px row")
        page.locator(".ph-chat-top button[aria-haspopup='menu']").click()
        page.wait_for_selector(".ph-session-menu .ph-tile", timeout=5000)
        page.wait_for_timeout(300)
        read("session-menu", ".ph-session-menu")
        tiles = page.evaluate("[...document.querySelectorAll('.ph-session-menu .ph-tile')].map((e) => Math.round(e.getBoundingClientRect().height))")
        if any(h != 64 for h in tiles):
            problems.append(f"session {lang}: tiles {sorted(set(tiles))}, not 64")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        go("settings", ".settings-link")
        m = read("settings", ".main")
        if m["topBorder"]:
            problems.append(f"settings {lang}: the top bar has a {m['topBorder']} px rule")

        # The Inbox: the phone's bar, 28 px chips, rows at least a row tall, and no white primary on
        # a row — a request in the Inbox is out of its context.
        go("inbox", ".ph-inbox .ph-irow")
        m = read("inbox", ".ph-inbox")
        if not m["top"] or m["top"]["h"] != 52:
            problems.append(f"inbox {lang}: the top bar is {m['top']}, not 52 tall")
        if any(h != 28 for h in m["chips"]):
            problems.append(f"inbox {lang}: chips {sorted(set(m['chips']))}, not 28")
        if any(h < 54 for h in m["rows"]):
            problems.append(f"inbox {lang}: rows {sorted(set(m['rows']))}, some under 54")
        if page.locator(".ph-inbox .ph-irow .ph-btn.primary").count():
            problems.append(f"inbox {lang}: a row's answer is drawn as the white primary")

        # Terminals: one capacity line under the bar, the cards inside the gutter.
        go("terminals", ".ph-terms .ph-page-body")
        m = read("terminals", ".ph-terms")
        if not m["top"] or m["top"]["h"] != 52:
            problems.append(f"terminals {lang}: the top bar is {m['top']}, not 52 tall")

        # A Settings page in grouped cards: its rows are tiles of the phone's settings-row height.
        go("settings/chat", ".settings-col .settings-row")
        read("settings-chat", ".main")
        tiles = page.eval_on_selector_all(".main .settings-col .card > .settings-row", "els => els.map((e) => Math.round(e.getBoundingClientRect().height))")
        if any(h < 48 for h in tiles):
            problems.append(f"settings-chat {lang}: rows {sorted(set(tiles))}, some under 48")
        context.close()
    return problems, seen


def check_phone_project(browser) -> tuple[list[str], dict]:  # type: ignore[no-untyped-def]
    """The same claims over orchestration's phone screens at 412 × 915, in both languages: the list of
    projects, the one Needs-you card and its decision sheet, the project's More sheet and Needs
    decision, the board with its chips and a task's sheet, and the review page's bar and footer."""
    from check_review_merge import reviewed_result
    from screenshots import focus_stub

    problems: list[str] = []
    seen: dict = {}
    for lang in ("en", "ru"):
        context = browser.new_context(viewport={"width": 412, "height": 915}, color_scheme="dark", is_mobile=True, has_touch=True)
        page = context.new_page()
        focus = FocusStub.bakery(lang)
        focus.questions_of_bakery(lang)
        reviewed_result(focus)
        page.route("**/api/**", focus_stub(focus))
        pid = focus.projects[0]["id"]

        def go(route: str, wait: str, page: Page = page, lang: str = lang) -> None:
            page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang={lang}")
            page.wait_for_selector(wait, timeout=15000)
            page.wait_for_timeout(500)

        def read(name: str, scope: str, page: Page = page, lang: str = lang) -> dict:
            m = page.evaluate(READ_PHONE, scope)
            seen[f"{name}-{lang}"] = {"sizes": sorted(float(k) for k in m["sizes"]), "rows": m["rows"], "chips": m["chips"]}
            off = {k: v[:3] for k, v in m["sizes"].items() if float(k) not in PHONE_STEPS}
            if off:
                problems.append(f"{name} {lang}: text off the six steps {off}")
            if m["targets"]:
                problems.append(f"{name} {lang}: targets under 44 px {m['targets'][:4]}")
            if m["scroll"][0] > m["scroll"][1]:
                problems.append(f"{name} {lang}: the page scrolls sideways {m['scroll']}")
            return m

        go("orchestration/projects", ".ph-orch .ph-row")
        m = read("orchestration", ".ph-orch")
        if not m["top"] or m["top"]["h"] != 52:
            problems.append(f"orchestration {lang}: the top bar is {m['top']}, not 52 tall")
        if any(h != 54 for h in m["rows"]):
            problems.append(f"orchestration {lang}: rows {sorted(set(m['rows']))}, not 54")

        go(f"project/{pid}", ".needs-card")
        read("needs card", ".needs-card")
        page.locator(".needs-card .needs-more").tap()
        page.wait_for_selector(".ph-decisions .q-card", timeout=5000)
        page.wait_for_timeout(300)
        read("decision sheet", ".ph-decisions")
        page.keyboard.press("Escape")

        go(f"project/{pid}/board", ".ph-board .ph-row")
        m = read("board", ".ph-board")
        if not m["top"] or m["top"]["h"] != 52:
            problems.append(f"board {lang}: the top bar is {m['top']}, not 52 tall")
        if any(h != 28 for h in m["chips"]):
            problems.append(f"board {lang}: chips {sorted(set(m['chips']))}, not 28")
        if any(h < 54 for h in m["rows"]):
            problems.append(f"board {lang}: a row is shorter than 54: {sorted(set(m['rows']))}")
        page.locator(".ph-board .ph-row.ph-task:not(.need) .ph-row-more").first.tap()
        page.wait_for_selector(".ph-task-menu", timeout=5000)
        page.wait_for_timeout(300)
        read("task sheet", ".ph-task-menu")
        page.keyboard.press("Escape")
        page.locator("nav.project-tabs button[data-tab='more']").tap()
        page.wait_for_selector(".ph-more-sheet", timeout=5000)
        page.wait_for_timeout(300)
        read("more sheet", ".ph-more-sheet")
        page.keyboard.press("Escape")

        go(f"project/{pid}/attention", ".ph-attention .ph-row")
        read("needs decision", ".ph-attention")

        go(f"project/{pid}/board?task=t-endpoint", ".ph-taskpage .ph-decide .ph-btn")
        read("review bar", ".ph-taskpage-top")
        read("review footer", ".ph-decide")
        context.close()
    return problems, seen


def judge(m: dict) -> list[str]:
    problems: list[str] = []
    phone = m["vw"] < 1024
    # The body is the 14 px step on both: a phone moved it to 15 until its scale came down a step and
    # took 14 in.
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
        if h > (54 if phone else 52):
            problems.append(f"{m['vw']}: a two-line row is {h}px")
    if m["avatar"] and m["avatar"]["w"] > 24:
        problems.append(f"{m['vw']}: an avatar in a row is {m['avatar']['w']}px")
    # A phone's top bar is 52 px, one row of 44 px targets with room around them; a desktop's 48.
    if m["head"] and m["head"]["h"] > (52 if phone else 48):
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
    # Diagrams moved from More to a dedicated rail icon, so the menu has one fewer item.
    if s["menuItems"] != 7:
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
            phone_problems, measured["phone"] = check_phone(browser)
            problems += phone_problems
            project_problems, measured["phone-project"] = check_phone_project(browser)
            problems += project_problems
        browser.close()
    for name, m in measured.items():
        print(name, json.dumps(m))
    if ASSERT:
        for name, m in measured.items():
            if name.startswith("session-"):
                problems += judge(m)
        for h in measured["agents-390"]["phoneRows"]:
            if h != 54:
                problems.append(f"390: a row of Chats is {h}px, not 54")
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
