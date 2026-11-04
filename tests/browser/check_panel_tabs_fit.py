"""No tab of the right panel is ever cut off: at the panel's default width and at its narrowest, in
English and Russian, every tab lies whole inside the strip without scrolling, and every word it shows
is shown whole.

The Notes tab made the Russian strip of a session with subagents wider than its panel: the strip
scrolled and its first tab read "…ия". The strip now measures which fit it can afford (the icon and
the word, the word alone, the word on the open tab only, the icons alone); this check holds it to
"fits, whole, named" wherever a panel is drawn: a session, a project's orchestrator and a member
beside it (the longest strips), and a phone's sheet.

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
NARROWEST = "try { localStorage.setItem('daedalus.width.panel', '1'); } catch (e) {}"
DEFAULT = "try { localStorage.removeItem('daedalus.width.panel'); } catch (e) {}"

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
  return { fit: strip.dataset.fit, scroll: strip.scrollWidth, client: strip.clientWidth, left: box.left, right: box.right, tabs };
}"""


def judge(page: Page, scope: str, where: str, problems: list[str], least: int) -> None:
    strip = page.locator(f"{scope} .panel-tablist")
    seen = strip.evaluate(MEASURE)
    names = [t["tab"] for t in seen["tabs"]]
    print(f"{where}: {seen['client']}px, fit {seen['fit']}, tabs {names}")
    if len(names) < least:
        problems.append(f"{where}: only {len(names)} tabs, expected at least {least}")
    if seen["scroll"] > seen["client"] + 1:
        problems.append(f"{where}: the strip scrolls ({seen['scroll']} > {seen['client']}), so a tab is cut off")
    for tab in seen["tabs"]:
        if tab["left"] < seen["left"] - 1 or tab["right"] > seen["right"] + 1:
            problems.append(f"{where}: the {tab['tab']} tab lies outside the strip ({tab})")
        if tab["cut"]:
            problems.append(f"{where}: the {tab['tab']} tab's word is cut ({tab})")
        if not tab["words"] and not tab["icon"]:
            problems.append(f"{where}: the {tab['tab']} tab draws neither its word nor its icon")
        if not tab["name"] or not tab["title"]:
            problems.append(f"{where}: the {tab['tab']} tab has no name of its own")
        if tab["on"] and seen["fit"] != "icons" and not tab["words"]:
            problems.append(f"{where}: the open {tab['tab']} tab lost its word")


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
                    context.add_init_script(script)
                    page = context.new_page()
                    page.route("**/api/**", stub)
                    for tab in ("details", "notes"):
                        page.goto(f"{BASE}/agents/{S1}?panel={tab}&token=t&scheme=dark&lang={lang}")
                        page.wait_for_selector(".panel .panel-tab[data-tab='subagents']", timeout=15000)
                        page.wait_for_timeout(400)
                        judge(page, ".panel", f"{lang} session {viewport['width']} {label} on {tab}", problems, 6)
                    context.close()
                # The orchestrator and a member of its project: the longest strips.
                focus = FocusStub.bakery(lang)
                pid = focus.projects[0]["id"]
                context = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
                context.add_init_script(script)
                page = context.new_page()
                page.route("**/api/**", focus_stub(focus))
                page.goto(f"{BASE}/project/{pid}?panel=details&token=t&lang={lang}")
                page.wait_for_selector(".chat.in-project .panel .panel-tab", timeout=15000)
                page.wait_for_timeout(400)
                judge(page, ".panel", f"{lang} orchestrator 1280 {label}", problems, 7)
                context.close()
            # A phone's sheet, at an ordinary phone and the narrowest.
            for width in (390, 320):
                context = browser.new_context(viewport={"width": width, "height": 760}, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
                page = context.new_page()
                page.route("**/api/**", stub)
                page.goto(f"{BASE}/agents/{S1}?panel=notes&token=t&scheme=dark&lang={lang}")
                page.wait_for_selector(".panel-sheet .panel-tab[data-tab='subagents']", timeout=15000)
                page.wait_for_timeout(400)
                judge(page, ".panel-sheet", f"{lang} phone {width}", problems, 6)
                context.close()
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
