"""Measure the shell's sizes in a real browser and refuse the ones that grew back.

The stylesheet's own test (miniapp/src/density.test.ts) reads the rules; this reads the rendered
page, which is the only place a row's height or a column's width actually exists. Everything the
app asks for is answered by the invented installation in screenshots.py, so the numbers describe
the layout and nothing else. Three windows: a laptop, an ultrawide, a phone; and Settings' column
and rows at the three sizes the owner uses.

The phone has its own claims since the redesign modelled on the chat apps of the operator's phone
(check_phone): six type steps (12 / 13 / 14 / 15 / 16 / 17) on every drawn text of the home, the
Chats page, the drawer and the composer's sheets; a 52 px top bar with no rule under it; 54 px list
rows; a drawer that is the sidebar, 330 px wide at 412 with 44 px rows; a composer card whose
controls are one row of 44 px targets, at rest as while typing; no bottom bar outside a project; and a
44 px target for every control, counting the invisible ::after that a 40 px circle or a 28 px chip
carries. The numbers are a step under the first cut of the
redesign (12 / 13 / 15 / 16 / 17 / 22, a 56 px bar, 60 px rows), which the operator measured as too
large next to Claude and ChatGPT on the same phone.

On every window nothing is drawn under 12 px, the floor the approved design sets for meta and
labels (the session, the agents list and the phone's Chats page are read for it).

The desktop's session header is two lines inside its 52 px (48 until every header took 52), the title and
then the project, the model and the live state; the start screen and the projects page that came with
it have their own claims (check_start).

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
  const phoneRows = all('.list-row').map((r) => box(r).h);
  const single = rows.filter((r) => !r.querySelector('.erow-meta') && !r.querySelector('.erow-line2'));
  const double = rows.filter((r) => r.querySelector('.erow-meta') || r.querySelector('.erow-line2'));
  const acts = all('.act:not(.head)');
  // The sidebar's own rows: every one a 32 px line, a chat, a chat inside a project and a project alike;
  // the pinned destinations too.
  const sbChat = all('.sidebar .sb-chat:not(.nested)').map((r) => box(r).h);
  const sbNested = all('.sidebar .sb-chat.nested').map((r) => box(r).h);
  const sbProject = all('.sidebar .sb-prow').map((r) => box(r).h);
  const sbNav = all('.sidebar .sb-pinned-nav .sb-nav').map((r) => box(r).h);
  const sbHead = box(one('.sidebar .sidebar-head'));
  const sbFoot = box(one('.sidebar .sidebar-foot'));
  const radius = (el) => el ? getComputedStyle(el).borderTopLeftRadius : null;
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
    headSub: box(one('.chat-head .chat-sub')),
    headTitle: box(one('.chat-head .chat-title')),
    // How far the run line's middle is from the title's: 0 when they share the line, as they must.
    subOffset: (() => { const s = one('.chat-head .chat-sub'), t = one('.chat-head .chat-title'); if (!s || !t) return null; const a = s.getBoundingClientRect(), b = t.getBoundingClientRect(); return a.height ? Math.round((a.top + a.height / 2) - (b.top + b.height / 2)) : null; })(),
    // Where the title's words start, from the main area's edge (the sidebar is draggable, so the window's
    // x would hold only at its default width), and how much room the scroller gives the stripe.
    titleX: (() => { const t = one('.chat-head .chat-title'); const m = one('.main'); if (!t || !m) return null; const r = document.createRange(); r.selectNodeContents(t); return Math.round(r.getBoundingClientRect().x - m.getBoundingClientRect().x); })(),
    scrollRoom: (() => { const s = one('.chat-scroll'); if (!s) return null; const cs = getComputedStyle(s); return Math.round(s.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)); })(),
    chat: box(one('.chat')),
    head: box(one('.chat-head')),
    timeline: box(one('.timeline')),
    composerBox: box(one('.composer-box')),
    roundbtn: box(one('.roundbtn')),
    answerFs: px(one('.answer'), 'fontSize'),
    userFs: px(one('.msg.user'), 'fontSize'),
    userW: box(one('.msg.user')),
    sbChat, sbNested, sbProject, sbNav, sbHead, sbFoot,
    rowSingle: single.map((r) => box(r).h),
    rowDouble: double.map((r) => box(r).h),
    rowFs: px(one('.erow-title'), 'fontSize'),
    avatar: box(one('.sidebar .avatar', '.session-list-pane .avatar', '.agents-screen .avatar')),
    act: acts.map((a) => box(a).h),
    actFs: px(acts[0], 'fontSize'),
    iconbtn: icons.map(box),
    chips: all('.chat-head .chip').map(box),
    phoneRows,
    small: smallText(),
  };
  // Every drawn text under the 12 px floor: an element that holds words of its own, is on the
  // screen and is not hidden. The smallest size drawn, and up to eight offenders by class and words.
  function smallText() {
    const out = [];
    let min = Infinity;
    for (const el of document.querySelectorAll('body *')) {
      if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      if (r.width === 0 || r.height === 0 || s.visibility === 'hidden' || s.display === 'none' || el.closest('[aria-hidden="true"], .xterm')) continue;
      const size = parseFloat(s.fontSize);
      min = Math.min(min, size);
      if (size < 12 && out.length < 8) out.push(`${size}px ${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${el.textContent.trim().slice(0, 24)}"`);
    }
    return { min: Number.isFinite(min) ? min : null, under: out };
  }
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
    page = open_page(context, "agents?view=chats", ".list-row") if mobile else open_page(context, "agents", ".sb-row")
    out = page.evaluate(READ)
    out["vw"] = width
    context.close()
    return out


# The width of what stands left of the conversation: the sidebar's column when it is open, nothing when folded.
LEFT = """() => {
  const w = (s) => { const el = document.querySelector(s); return el ? Math.round(el.getBoundingClientRect().width) : 0; };
  return { column: w('.desktop-column'), sidebar: w('nav.sidebar'), main: Math.round(document.querySelector('.main').getBoundingClientRect().left) };
}"""


def check_sidebar(browser) -> dict:  # type: ignore[no-untyped-def]
    """The fold and the menu, which are behaviour rather than sizes: the sidebar gone, its persistence, the popover's keyboard."""
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(OPEN_FOLDERS)
    page = open_page(context, f"agents/{S1}", ".sidebar")
    out: dict = {}
    out["open"] = page.evaluate(LEFT)
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)
    out["collapsed"] = page.evaluate(LEFT)
    page.reload()
    page.wait_for_selector(".sidebar-unfold", timeout=15000)
    page.wait_for_timeout(400)
    out["collapsedAfterReload"] = page.evaluate(LEFT)
    page.keyboard.press("Control+\\")
    page.wait_for_timeout(400)
    out["reopened"] = page.evaluate(LEFT)

    page.locator('.sb-pinned-nav [data-nav="menu"]').click()
    page.wait_for_selector(".navmenu[role='dialog']", timeout=5000)
    out["menuItems"] = page.locator(".navmenu [data-launch]").count()
    out["menuLang"] = page.locator(".navmenu .lang").count()
    out["menuFocusInside"] = page.evaluate("() => !!document.activeElement && document.activeElement.matches('.navmenu .launch-field')")
    out["menuBox"] = page.evaluate("() => { const r = document.querySelector('.navmenu').getBoundingClientRect(); return { x: Math.round(r.x), bottom: Math.round(innerHeight - r.bottom), w: Math.round(r.width) }; }")
    out["menuRow"] = page.evaluate("() => Math.round(document.querySelector('.navmenu .launch-catalogue [data-launch]').getBoundingClientRect().height)")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowRight")
    out["arrowMoves"] = page.evaluate("() => document.activeElement === document.querySelectorAll('.navmenu [data-launch]')[1]")
    # Esc in a search with words in it empties the field and leaves the launcher open.
    page.keyboard.press("ArrowUp")
    page.keyboard.type("usa")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    out["escClears"] = page.locator(".navmenu").count() == 1 and page.locator(".navmenu .launch-field").input_value() == ""
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    out["menuClosed"] = page.locator(".navmenu").count() == 0
    out["focusBack"] = page.evaluate("() => document.activeElement === document.querySelector('.sb-pinned-nav [data-nav=\"menu\"]')")
    page.keyboard.press("Control+Shift+M")
    page.wait_for_timeout(300)
    out["shortcutOpens"] = page.locator(".navmenu[role='dialog']").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.keyboard.press("g")
    page.keyboard.press("i")
    page.wait_for_timeout(500)
    out["gKeyNavigates"] = page.evaluate("() => location.pathname").endswith("/inbox")
    context.close()
    return out


PAGE_ROUTES = ["inbox", "board", "usage", "health", "memory", "services", "changes", "schedules", "canvas", "harnesses", "terminals"]


def check_page_header(browser) -> tuple[list[str], dict]:  # type: ignore[no-untyped-def]
    """Every full page's header is one 52 px row with its title 24 px in from the main area's left
    edge, at a laptop and an ultrawide width. Measured from the main area, not the window: the sidebar
    is draggable, so "x = 324" holds only at its default width. The header once followed the page's
    column (padded in to its edge), which put the titles at six different positions."""
    problems: list[str] = []
    measured: dict = {}
    for width in (1366, 1920):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark")
        for route in PAGE_ROUTES:
            page = open_page(context, route, ".pagehead h1")
            m = page.evaluate("""() => {
              const main = document.querySelector('.main').getBoundingClientRect();
              const head = document.querySelector('.pagehead').getBoundingClientRect();
              const range = document.createRange();
              range.selectNodeContents(document.querySelector('.pagehead h1'));
              return { titleX: Math.round(range.getBoundingClientRect().x - main.x), height: Math.round(head.height) };
            }""")
            measured[f"{route}-{width}"] = m
            if m["titleX"] != 24:
                problems.append(f"{route} at {width}: the title starts {m['titleX']} px from the main area's edge, not 24")
            if m["height"] != 52:
                problems.append(f"{route} at {width}: the header is {m['height']} px high, not 52")
            page.close()
        context.close()
    return problems, measured


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
    """Settings' column and rows as drawn: inside the dialog the section takes the pane's whole width
    (the dialog is already the reading width; the page's centred 960 px column went with the page), a
    row is at least the touch height, and a phone does not scroll sideways."""
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
            want = m["pane"]["w"]
            if abs(m["col"]["w"] - want) > 1:
                problems.append(f"settings {width}: the column is {m['col']['w']}px, not the pane's {want}")
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
    // The form shadow a Base UI switch keeps beside itself: a 1 px input, hidden from the reader and
    // out of the tab order. The switch itself is the target, and is measured as one.
    if (el.tagName === 'INPUT' && el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0) continue;
    const r = el.getBoundingClientRect();
    let w = r.width, h = r.height;
    // The composer's field is one line inside its card, and a tap on the card focuses the field.
    const card = el.tagName === 'TEXTAREA' ? el.closest('.composer[data-shape] .composer-box') : null;
    if (card) h = Math.max(h, card.getBoundingClientRect().height);
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
    card: rect('.composer[data-shape] .composer-box'),
    composerRow: rect('.composer[data-shape] .composer-row'),
    circle: rect('.composer[data-shape] .roundbtn'),
    rows: [...document.querySelectorAll('.list-row')].filter(vis).map((r) => Math.round(r.getBoundingClientRect().height)),
    chips: [...document.querySelectorAll('.ph-chip')].filter(vis).map((r) => Math.round(r.getBoundingClientRect().height)),
    nav: [...document.querySelectorAll('.ph-drawer-root.open .sb-pinned-nav .sb-nav')].map((r) => Math.round(r.getBoundingClientRect().height)),
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
        if not m["card"] or not m["composerRow"] or m["composerRow"]["h"] != 44:
            problems.append(f"home {lang}: the composer is {m['card']} with a row of {m['composerRow']}, not a card over one 44 px row")
        elif m["card"]["b"] > 24:
            problems.append(f"home {lang}: the composer stands {m['card']['b']} px above the bottom")
        if not m["circle"] or (m["circle"]["w"], m["circle"]["h"]) != (40, 40):
            problems.append(f"home {lang}: the send circle is {m['circle']}, not 40 × 40")
        if m["tabbar"]:
            problems.append(f"home {lang}: a bottom bar is still drawn")
        for h in m["rows"]:
            if h != 54:
                problems.append(f"home {lang}: a live row is {h} px, not 54")

        page.locator(".ph-home .composer .composer-field").fill("Wire the order form to the sheet")
        page.wait_for_timeout(300)
        read("composer-typed", ".ph-home .composer")
        # The mic stays beside Send once something is typed: hiding it left no way to dictate the rest.
        mic = page.locator(".ph-home .composer .composer-tools > .mic")
        if mic.count() and not mic.is_visible():
            problems.append(f"composer {lang}: the mic is gone once a draft is typed")
        page.locator(".ph-home .composer .composer-field").fill("")

        page.locator(".ph-menu").first.click()
        page.wait_for_selector(".ph-drawer-root.open", timeout=5000)
        page.wait_for_timeout(400)
        m = read("drawer", ".ph-drawer-root.open")
        if not m["drawer"] or m["drawer"]["w"] != 330:
            problems.append(f"drawer {lang}: {m['drawer']}, not 330 wide at 412")
        if not m["nav"]:
            problems.append(f"drawer {lang}: no pinned rows of the sidebar in it")
        if any(h != 44 for h in m["nav"]):
            problems.append(f"drawer {lang}: destination rows {m['nav']}, not 44")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        if page.locator(".ph-drawer-root.open").count():
            problems.append(f"drawer {lang}: Escape does not close it")

        go("agents?view=chats", ".list-row")
        m = read("chats", ".ph-chats")
        if any(h != 54 for h in m["rows"]):
            problems.append(f"chats {lang}: rows {sorted(set(m['rows']))}, not 54")
        if any(h != 28 for h in m["chips"]):
            problems.append(f"chats {lang}: chips {sorted(set(m['chips']))}, not 28")
        page.locator(".list-row-more").first.click()
        page.wait_for_selector(".menu-sheet", timeout=5000)
        page.wait_for_timeout(300)
        read("row-sheet", ".menu-sheet")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        # The same sheet on a finger held still, and the quick actions on a swipe to the left.
        row = ".ph-chats .list-row"
        page.evaluate("""(sel) => { const el = document.querySelector(sel); const r = el.getBoundingClientRect();
          el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', clientX: r.x + 120, clientY: r.y + 20 })); }""", row)
        page.wait_for_timeout(700)
        if not page.locator(".menu-sheet").count():
            problems.append(f"chats {lang}: a long press does not open the row's sheet")
        page.evaluate("(sel) => document.querySelector(sel).dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerType: 'touch' }))", row)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.evaluate("""(sel) => { const el = document.querySelector(sel); const r = el.getBoundingClientRect();
          const at = (type, x) => el.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerType: 'touch', clientX: x, clientY: r.y + 20 }));
          at('pointerdown', r.x + 300); at('pointermove', r.x + 280); at('pointermove', r.x + 200); at('pointermove', r.x + 60); at('pointerup', r.x + 60); }""", row)
        page.wait_for_timeout(400)
        if page.locator(".list-row-swipe.end .list-row-swipe-act").count() < 2:
            problems.append(f"chats {lang}: a swipe to the left does not reveal the row's quick actions")

        # The session: the same 52 px bar with a two-line title and at most two glyphs, the answer's
        # icon row in 44 px targets, the idle composer, and the ⋮ sheet with its tiles.
        go(f"agents/{S1}", ".ph-chat-top")
        m = read("session", ".chat")
        if not m["card"] or not m["composerRow"] or m["composerRow"]["h"] != 44:
            problems.append(f"session {lang}: the composer is {m['card']} with a row of {m['composerRow']}, not a card over one 44 px row")
        if not m["circle"] or (m["circle"]["w"], m["circle"]["h"]) != (40, 40):
            problems.append(f"session {lang}: the send circle is {m['circle']}, not 40 × 40")
        before = m["card"]
        page.locator(".chat .composer .composer-field").first.click()
        page.wait_for_timeout(200)
        after = page.evaluate(READ_PHONE, ".chat")["card"]
        if before and after and before["h"] != after["h"]:
            problems.append(f"session {lang}: the composer is {before['h']} px at rest and {after['h']} px once the field has the reader")
        page.keyboard.press("Escape")
        top = page.locator(".ph-chat-top").bounding_box()
        if not top or round(top["height"]) != 52:
            problems.append(f"session {lang}: the bar is {top}, not 52 tall")
        if page.evaluate("parseFloat(getComputedStyle(document.querySelector('.ph-chat-top')).borderBottomWidth)"):
            problems.append(f"session {lang}: the bar has a rule under it")
        glyphs = page.locator(".ph-chat-top .pagehead-actions > *").count()
        if glyphs > 2:
            problems.append(f"session {lang}: {glyphs} glyphs at the right of the bar, more than two")
        # The title is 15 px with the 12 px line of where the chat lives over it, when it lives
        # somewhere of another name than its own.
        title = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.ph-chat-top .pagehead-t')).fontSize)")
        if title != 15:
            problems.append(f"session {lang}: the title is {title} px, not 15")
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

        go("orchestration/projects", ".ph-orch .list-row")
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

        go(f"project/{pid}/board", ".ph-board .list-row")
        m = read("board", ".ph-board")
        if not m["top"] or m["top"]["h"] != 52:
            problems.append(f"board {lang}: the top bar is {m['top']}, not 52 tall")
        if any(h != 28 for h in m["chips"]):
            problems.append(f"board {lang}: chips {sorted(set(m['chips']))}, not 28")
        if any(h < 54 for h in m["rows"]):
            problems.append(f"board {lang}: a row is shorter than 54: {sorted(set(m['rows']))}")
        page.locator(".ph-board .list-row.ph-task:not(.need) .list-row-more").first.tap()
        page.wait_for_selector(".ph-task-menu", timeout=5000)
        page.wait_for_timeout(300)
        read("task sheet", ".ph-task-menu")
        page.keyboard.press("Escape")
        page.locator("nav.project-tabs button[data-tab='more']").tap()
        page.wait_for_selector(".ph-more-sheet", timeout=5000)
        page.wait_for_timeout(300)
        read("more sheet", ".ph-more-sheet")
        page.keyboard.press("Escape")

        go(f"project/{pid}/attention", ".ph-attention .list-row")
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
    # Nothing drawn under 12 px, the design's floor for meta and labels: 11 px was the smallest step
    # until then, and a fifth of the drawn text used it.
    if m["small"]["under"]:
        problems.append(f"{m['vw']}: text under the 12 px floor: {m['small']['under']}")
    if not phone:
        # The 300 px column the design opens the sidebar at, alone: the 52 px rail of icons beside it
        # is gone (288 before, and 272 until the rows went to two lines). The operator's drag still
        # moves it; this is the default.
        if not m["left"] or m["left"]["w"] != 300:
            problems.append(f"{m['vw']}: the left column is {m['left']}, not 300 wide")
        # Every row is the 32 px one-line row every list shares: a chat was two lines of 44 (40 under a
        # project) with its model and host on the second line, which went to the row's tooltip.
        for name, rows in (("chat", m["sbChat"]), ("nested chat", m["sbNested"]), ("project", m["sbProject"]), ("pinned", m["sbNav"])):
            for h in rows:
                if h != 32:
                    problems.append(f"{m['vw']}: a sidebar {name} row is {h}px, not 32")
        if not m["sbChat"] or not m["sbProject"] or not m["sbNav"]:
            problems.append(f"{m['vw']}: the sidebar drew {len(m['sbChat'])} chat rows, {len(m['sbProject'])} project rows and {len(m['sbNav'])} pinned rows")
        for name in ("sbHead", "sbFoot"):
            if not m[name] or m[name]["h"] != 52:
                problems.append(f"{m['vw']}: the sidebar's {name[2:].lower()} is {m[name]}, not 52 high")
        if m["list"]:
            problems.append(f"{m['vw']}: a second list column is still there ({m['list']})")
    for h in m["rowSingle"]:
        if h > (44 if phone else 32):
            problems.append(f"{m['vw']}: a one-line row is {h}px")
    for h in m["rowDouble"]:
        if h > (54 if phone else 52):
            problems.append(f"{m['vw']}: a two-line row is {h}px")
    if m["avatar"] and m["avatar"]["w"] > 24:
        problems.append(f"{m['vw']}: an avatar in a row is {m['avatar']['w']}px")
    # A phone's top bar is 52 px, one row of 44 px targets with room around them, and so is a
    # desktop's header since every header took --head-h's 52 (it was 48).
    if m["head"] and m["head"]["h"] > 52:
        problems.append(f"{m['vw']}: the chat header is {m['head']['h']}px")
    if m["subMeta"]:
        problems.append(f"{m['vw']}: the header still has its second row of chips")
    # The desktop header is one line, as every page's: the place and the title 24 px in from the main
    # area's edge, then the run in the meta size on the same line. It was two lines in the same 52 px,
    # the model and the state under the title, which read as a second toolbar.
    if not phone:
        if m["titleX"] != 24:
            problems.append(f"{m['vw']}: the chat's title starts {m['titleX']} px from the main area's edge, not 24")
        if m["subOffset"] is not None and abs(m["subOffset"]) > 3:
            problems.append(f"{m['vw']}: the header's run line sits {m['subOffset']} px off the title's line")
    if m["chips"]:
        problems.append(f"{m['vw']}: {len(m['chips'])} chip(s) are still in the header")
    if not phone:
        if not m["panel"]:
            problems.append(f"{m['vw']}: the panel is not open beside the conversation")
        else:
            if m["panel"]["w"] < 360 or m["panel"]["w"] > 0.65 * m["vw"]:
                problems.append(f"{m['vw']}: the panel is {m['panel']['w']}px, outside 360..65%")
            # 52, the header's height: the tabs draw their icon over their word, so six named tabs fit
            # the panel's width where a 40 px row of "icon word" pairs fell back to icons alone.
            if not m["panelTabs"] or m["panelTabs"]["h"] != 52:
                problems.append(f"{m['vw']}: the panel's tab row is {m['panelTabs']}, not 52")
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
    # The operator's bubble reads at the answer's size and stops at 560 px, three quarters of the stripe.
    if not phone and m["userW"] and (m["userFs"] != 15 or m["userW"]["w"] > 560):
        problems.append(f"{m['vw']}: the operator's bubble is {m['userW']['w']}px wide at {m['userFs']}px, not 15 px within 560")
    if m["timeline"]:
        # The conversation and the field that answers it are one column, 760 px at every width (the
        # owner's measure; it widened to 1120 and 1320 on wide windows before). Three claims: the stripe
        # is never exceeded, it is the whole 760 wherever the scroller has the room, and the field is
        # exactly as wide as the conversation above it (they were offset against each other once).
        if m["timeline"]["w"] > 760:
            problems.append(f"{m['vw']}: the timeline is {m['timeline']['w']}px, over the 760 stripe")
        if not phone and m["scrollRoom"] is not None and m["scrollRoom"] >= 760 and m["timeline"]["w"] != 760:
            problems.append(f"{m['vw']}: the timeline is {m['timeline']['w']}px with {m['scrollRoom']}px of room, not 760")
        if m["vw"] >= 1024 and m["composerBox"] and abs(m["composerBox"]["w"] - m["timeline"]["w"]) > 2:
            problems.append(f"{m['vw']}: the composer is {m['composerBox']['w']}px against a {m['timeline']['w']}px timeline")
        # Beside the 42 % panel a 1440 window keeps a conversation column: 1440 less the 300 px
        # sidebar is a 1140 px chat area, 58 % of it the conversation. The floor of 578 was set while a
        # 52 px rail stood beside the sidebar as well; without it the column is wider still.
        if m["vw"] == 1440 and m["timeline"]["w"] < 578:
            problems.append(f"1440: the timeline is {m['timeline']['w']}px beside the panel, narrower than 578")
    return problems


def judge_sidebar(s: dict) -> list[str]:
    problems: list[str] = []
    # The sidebar is the first column alone, 300 wide, the design's default (288 before, 272 before
    # that); folded, nothing of it is left and the conversation starts at the window's edge.
    want = {"open": (300, 300, 300), "collapsed": (0, 0, 0), "collapsedAfterReload": (0, 0, 0), "reopened": (300, 300, 300)}
    for key, (column, sidebar, main) in want.items():
        got = s[key]
        if (got["column"], got["sidebar"], got["main"]) != (column, sidebar, main):
            problems.append(f"{key}: column/sidebar/conversation start {got['column']}/{got['sidebar']}/{got['main']}, not {column}/{sidebar}/{main}")
    # The launcher lists every destination, the pinned ones included, as Frequent tiles and the catalogue
    # (the stub's thirteen screens and its one published page), then Settings, Customise and About.
    if s["menuItems"] != 17:
        problems.append(f"the launcher has {s['menuItems']} items")
    # The language and the companion went to Settings -> Appearance.
    if s["menuLang"]:
        problems.append("the launcher still has a language switch")
    # It opens from the sidebar's More row, beside the sidebar rather than over it, 600 wide while the
    # window allows.
    if not 296 <= s["menuBox"]["x"] <= 306 or s["menuBox"]["bottom"] < 8 or s["menuBox"]["w"] != 600:
        problems.append(f"the launcher is not beside its More row at 600 wide: {s['menuBox']}")
    if s["menuRow"] != 32:
        problems.append(f"a catalogue row is {s['menuRow']}px")
    for key in ("menuFocusInside", "arrowMoves", "escClears", "menuClosed", "focusBack", "shortcutOpens", "gKeyNavigates"):
        if not s[key]:
            problems.append(f"{key} is false")
    return problems


START = """
() => {
  const box = (el) => el ? { w: Math.round(el.getBoundingClientRect().width), h: Math.round(el.getBoundingClientRect().height) } : null;
  const circle = document.querySelector('.start-composer .composer-tools .roundbtn.primary');
  return {
    composer: box(document.querySelector('.start-composer .composer-box')),
    circle: box(circle),
    radius: circle ? getComputedStyle(circle).borderRadius : null,
    chips: [...document.querySelectorAll('.start-composer .composer-row > button.composer-project, .start-composer .composer-row > button.composer-mode, .start-composer .composer-row > button.composer-access')].map((el) => box(el).h),
    cards: [...document.querySelectorAll('.resume-card')].map((el) => box(el).h),
    heading: (() => { const el = document.querySelector('h1.start-greeting'); return el ? { size: getComputedStyle(el).fontSize, weight: getComputedStyle(el).fontWeight } : null; })(),
    projectRows: [...document.querySelectorAll('.projects-row')].map((el) => box(el).h),
    projectList: (() => { const el = document.querySelector('.projects-table'); return el ? Math.round(el.getBoundingClientRect().width) : 0; })(),
  };
}
"""


def check_start(browser) -> tuple[list[str], dict]:  # type: ignore[no-untyped-def]
    """The desktop's start screen and projects page, at 1440: the question in 28 px over a composer in
    the conversation's own 760 px column, the primary a 32 px circle, the chips of the composer's row
    28 px like every small chip, the chats to continue and the project rows 56 px cards, in the page
    chrome's 880 px list."""
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, "agents", ".resume-card")
    # An empty field shows the mic alone in the trailing slot; Send, the circle measured, joins it once
    # the field holds words. It was the voice circle on an empty field until that left ordinary chats.
    page.locator(".start-composer .composer-field").fill("Bake the menu")
    start = page.evaluate(START)
    page.goto(f"{BASE}/agents?view=projects&token=t&scheme=dark&lang=en")
    page.wait_for_selector(".projects-row", timeout=15000)
    projects = page.evaluate(START)
    start["projectRows"] = projects["projectRows"]
    start["projectList"] = projects["projectList"]
    context.close()
    if not start["composer"] or start["composer"]["w"] != 760:
        problems.append(f"start: the composer is {start['composer']}, not 760 wide")
    if not start["heading"] or start["heading"]["size"] != "28px" or start["heading"]["weight"] != "500":
        problems.append(f"start: the question is {start['heading']}, not 28/500")
    if not start["circle"] or start["circle"]["w"] != 32 or start["circle"]["h"] != 32 or start["radius"] != "50%":
        problems.append(f"start: the primary is {start['circle']} with radius {start['radius']}, not a 32 px circle")
    if not start["chips"] or any(h != 28 for h in start["chips"]):
        problems.append(f"start: the chips of the row are {start['chips']}, not 28")
    if not start["cards"] or any(h != 56 for h in start["cards"]):
        problems.append(f"start: the cards to continue are {start['cards']}, not 56")
    if not start["projectRows"] or any(h != 56 for h in start["projectRows"]):
        problems.append(f"projects: the rows are {start['projectRows']}, not 56")
    if start["projectList"] != 880:
        problems.append(f"projects: the list is {start['projectList']} wide, not the page chrome's 880")
    return problems, start


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
            head_problems, measured["page-header"] = check_page_header(browser)
            problems += head_problems
            problems += check_settings(browser)
            phone_problems, measured["phone"] = check_phone(browser)
            problems += phone_problems
            project_problems, measured["phone-project"] = check_phone_project(browser)
            problems += project_problems
            start_problems, measured["start"] = check_start(browser)
            problems += start_problems
        browser.close()
    for name, m in measured.items():
        print(name, json.dumps(m))
    if ASSERT:
        for name, m in measured.items():
            if name.startswith("session-"):
                problems += judge(m)
        for name in ("agents-1440", "agents-390"):
            if measured[name]["small"]["under"]:
                problems.append(f"{name}: text under the 12 px floor: {measured[name]['small']['under']}")
        for h in measured["agents-390"]["phoneRows"]:
            if h != 54:
                problems.append(f"390: a row of Chats is {h}px, not 54")
        problems += judge_sidebar(measured["sidebar"])
    if MEASURE:
        Path(MEASURE).write_text(json.dumps(measured, indent=1) + "\n")
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
