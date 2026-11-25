"""No tab of the right panel is ever cut off or pressed against another: at the panel's default width
and at its narrowest, in English and Russian, with eight tools open, every tab lies whole inside the
strip without scrolling, keeps a gap to its neighbours and to the panel's edge, and every word it
shows is shown whole; the "+" after the tabs offers every tool, and a tool's × closes it.

The Notes tab once made the Russian strip of a session with subagents wider than its panel: the strip
scrolled and its first tab read "…ия". The strip measures which fit it can afford (the icon and the
word, the word on the open tab only, the icons alone); this check holds it to "fits, whole, apart,
named" wherever a panel is drawn: a session, a project's orchestrator, and a phone's sheet.

    APP_URL=http://127.0.0.1:8163/app CHROMIUM=/path/to/chrome python3 tests/browser/check_panel_tabs_fit.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, focus_stub, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
NARROWEST = "try { localStorage.setItem('daedalus.width.panel', '400'); } catch (e) {}"
DEFAULT = "try { localStorage.removeItem('daedalus.width.panel'); } catch (e) {}"
# Eight tools open beside the Overview: the most crowded strip an operator makes by hand.
CROWDED = "subagents,changes,files,jobs,notes,context,usage,details"
OPEN = ("try { localStorage.setItem('daedalus.session.panel', 'details|" + CROWDED + "');"
        " localStorage.setItem('daedalus.session.panel.project', 'details|questions,files,notes,board,brief,wakeups,folders,details'); } catch (e) {}")

MEASURE = """strip => {
  const box = strip.getBoundingClientRect();
  const tabs = [...strip.querySelectorAll('.panel-tab')].map(tab => {
    const r = tab.getBoundingClientRect();
    const words = [...tab.querySelectorAll(':scope > span:not(.count)')].filter(s => s.getClientRects().length);
    const cut = words.filter(s => s.scrollWidth > s.clientWidth + 1 || getComputedStyle(s).textOverflow === 'ellipsis' && s.scrollWidth > s.clientWidth);
    const icon = tab.querySelector('svg.ic');
    const iconShown = !!icon && icon.getClientRects().length > 0;
    return { tab: tab.dataset.tab, on: tab.classList.contains('on'), left: r.left, right: r.right, words: words.map(s => s.textContent), cut: cut.length,
             icon: iconShown, name: tab.getAttribute('aria-label') || '', title: tab.getAttribute('title') || '' };
  });
  const pills = [...strip.querySelectorAll('.panel-tabwrap')].map(w => w.getBoundingClientRect());
  const gaps = pills.slice(1).map((r, i) => Math.round(r.left - pills[i].right));
  const row = strip.closest('.panel-tabs').getBoundingClientRect();
  const add = strip.closest('.panel-tabs').querySelector('.panel-add');
  const addBox = add ? add.getBoundingClientRect() : null;
  return { fade: strip.dataset.fade || '', fit: strip.dataset.fit, scroll: strip.scrollWidth, client: strip.clientWidth, left: box.left, right: box.right, tabs, gaps,
           edge: pills.length ? Math.round(pills[0].left - row.left) : 99, add: addBox ? Math.round(addBox.left - (pills.length ? pills[pills.length - 1].right : 0)) : -1,
           addInside: !!addBox && addBox.right <= row.right + 1 };
}"""


def judge(page: Page, scope: str, where: str, problems: list[str], least: int) -> None:
    strip = page.locator(f"{scope} .panel-tablist")
    seen = strip.evaluate(MEASURE)
    names = [t["tab"] for t in seen["tabs"]]
    print(f"{where}: {seen['client']}px, fit {seen['fit']}, tabs {names}")
    if len(names) < least:
        problems.append(f"{where}: only {len(names)} tabs, expected at least {least}")
    scrolls = seen["scroll"] > seen["client"] + 1
    # Nine tools in a 400 px panel do not fit even as icons: only then may the strip scroll, with the
    # edge that has more behind it faded, and the open tab always whole in view.
    if scrolls and (seen["fit"] != "icons" or not seen["fade"]):
        problems.append(f"{where}: the strip scrolls ({seen['scroll']} > {seen['client']}) at fit {seen['fit']}, fade {seen['fade']!r}")
    for tab in seen["tabs"]:
        outside = tab["left"] < seen["left"] - 1 or tab["right"] > seen["right"] + 1
        if outside and (not scrolls or tab["on"]):
            problems.append(f"{where}: the {tab['tab']} tab lies outside the strip ({tab})")
        if tab["cut"]:
            problems.append(f"{where}: the {tab['tab']} tab's word is cut ({tab})")
        if not tab["words"] and not tab["icon"]:
            problems.append(f"{where}: the {tab['tab']} tab draws neither its word nor its icon")
        if not tab["name"] or not tab["title"]:
            problems.append(f"{where}: the {tab['tab']} tab has no name of its own")
        if tab["on"] and seen["fit"] != "icons" and not tab["words"]:
            problems.append(f"{where}: the open {tab['tab']} tab lost its word")
    if any(gap < 2 for gap in seen["gaps"]):
        problems.append(f"{where}: two tabs touch (gaps {seen['gaps']})")
    if seen["edge"] < 8 and not scrolls:
        problems.append(f"{where}: the first tab is {seen['edge']}px from the panel's edge")
    if seen["add"] < 2 or not seen["addInside"]:
        problems.append(f"{where}: the + after the tabs is not drawn apart from them inside the row ({seen['add']}, {seen['addInside']})")


def menu_and_close(page: Page, scope: str, where: str, problems: list[str]) -> None:
    """The + lists every tool the chat offers; picking one opens its tab; its × closes it again."""
    page.locator(f"{scope} .panel-add").click()
    page.wait_for_selector(".panel-tools [data-tool]", timeout=5000)
    tools = page.locator(".panel-tools [data-tool]").evaluate_all("els => els.map(e => e.dataset.tool)")
    for wanted in ("files", "jobs", "context", "memory", "notes", "canvas", "attachments", "details", "usage"):
        if wanted not in tools:
            problems.append(f"{where}: the + menu does not offer {wanted} ({tools})")
    page.locator(".panel-tools [data-tool='memory']").click()
    page.wait_for_selector(f"{scope} .panel-tab.on[data-tab='memory']", timeout=5000)
    page.locator(f"{scope} .panel-tab-close").click()
    page.wait_for_timeout(200)
    if page.locator(f"{scope} .panel-tab[data-tab='memory']").count():
        problems.append(f"{where}: the memory tab's × left it open")


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for script, label in ((DEFAULT, "default"), (NARROWEST, "narrowest")):
                # A session with subagents and notes: the strip that scrolled.
                for viewport in ({"width": 1440, "height": 900}, {"width": 1280, "height": 800}, {"width": 1024, "height": 768}):
                    context = browser.new_context(viewport=viewport, color_scheme="dark")
                    context.add_init_script(script + OPEN)
                    page = context.new_page()
                    page.route("**/api/**", stub)
                    for tab in ("details", "notes"):
                        page.goto(f"{BASE}/agents/{S1}?panel={tab}&token=t&scheme=dark&lang={lang}")
                        page.wait_for_selector(".panel .panel-tab[data-tab='subagents']", timeout=15000)
                        page.wait_for_timeout(400)
                        judge(page, ".panel", f"{lang} session {viewport['width']} {label} on {tab}", problems, 9)
                    menu_and_close(page, ".panel", f"{lang} session {viewport['width']} {label}", problems)
                    context.close()
                # The orchestrator and a member of its project: the longest strips.
                focus = FocusStub.bakery(lang)
                pid = focus.projects[0]["id"]
                context = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
                context.add_init_script(script + OPEN)
                page = context.new_page()
                page.route("**/api/**", focus_stub(focus))
                page.goto(f"{BASE}/project/{pid}?panel=details&token=t&lang={lang}")
                page.wait_for_selector(".chat.in-project .panel .panel-tab", timeout=15000)
                page.wait_for_timeout(400)
                judge(page, ".panel", f"{lang} orchestrator 1280 {label}", problems, 8)
                context.close()
            # A phone's sheet, at an ordinary phone and the narrowest.
            for width in (390, 320):
                context = browser.new_context(viewport={"width": width, "height": 760}, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
                # A phone keeps four tools open beside the Overview: the sheet's head is the width of the phone.
                context.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'notes|subagents,changes,notes'); } catch (e) {}")
                page = context.new_page()
                page.route("**/api/**", stub)
                page.goto(f"{BASE}/agents/{S1}?panel=notes&token=t&scheme=dark&lang={lang}")
                page.wait_for_selector(".panel-sheet .panel-tab[data-tab='subagents']", timeout=15000)
                page.wait_for_timeout(400)
                judge(page, ".panel-sheet", f"{lang} phone {width}", problems, 4)
                context.close()
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
