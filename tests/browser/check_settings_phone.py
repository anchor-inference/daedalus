"""Every Settings page on a phone, 390 and 360 px wide, in both languages: nothing leaves the screen
sideways, an open list stays on it, and each kind of control stands in one place in its row.

On a phone the rows' controls stood wherever their content put them. A long pick (the web search's
fallbacks) grew its control past the row, and the row, aligned to its end, pushed it off the left
edge of the screen, with its list after it; a dropdown ended at one edge, a number field at
another, a number with a unit at a third. What is checked on every page:

- no visible element of the page reaches past either side of the screen (a strip that scrolls
  sideways on purpose, the tabs and chips, is measured by its own box);
- every dropdown opened stays inside the screen, and its trigger inside its row;
- in each row the control is under the words and spans the row: dropdowns, multi-selects, number
  fields and text fields all start at the row's left edge and end at its right, units included,
  so every such control on every page shares one pair of edges;
- a long choice is cut short inside its control rather than widening it.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_settings_phone.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SECTIONS = ["appearance", "notifications", "voice", "security", "models", "rules", "limits", "tools", "environments", "chat", "components", "dependencies", "heartbeat", "about"]

MEASURE = """() => {
  const vw = document.documentElement.clientWidth;
  const root = document.querySelector('.settings-col');
  if (!root) return null;
  const name = (el) => (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : el.tagName.toLowerCase()) + (el.textContent ? ' «' + el.textContent.trim().slice(0, 30) + '»' : '');
  // An element inside a box that scrolls or clips sideways is measured by that box, which is itself measured.
  const clipped = (el) => { for (let p = el.parentElement; p && p !== root.parentElement; p = p.parentElement) { const s = getComputedStyle(p); if (s.overflowX !== 'visible') return true; } return false; };
  const outside = [];
  for (const el of root.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.position === 'fixed') continue;
    if ((r.left < -0.5 || r.right > vw + 0.5) && !clipped(el)) outside.push(`${name(el)} ${Math.round(r.left)}..${Math.round(r.right)}`);
  }
  const rows = [];
  for (const row of root.querySelectorAll('.settings-row')) {
    const ctl = row.querySelector(':scope > .settings-row-ctl');
    if (!ctl || !ctl.getBoundingClientRect().width) continue;
    const kind = ctl.querySelector(':scope > .dropdown') ? (ctl.querySelector(':scope > .dropdown').dataset.multi !== undefined ? 'multi' : 'dropdown')
      : ctl.querySelector(':scope > .settings-num') ? 'number'
      : ctl.querySelector(':scope > input.field, :scope > textarea') ? 'text'
      : null;
    if (!kind || ctl.children.length !== 1) continue;
    const box = row.getBoundingClientRect(), c = ctl.getBoundingClientRect(), text = row.querySelector(':scope > .settings-row-text').getBoundingClientRect();
    const inner = ctl.firstElementChild.getBoundingClientRect();
    rows.push({ kind, title: row.querySelector('.settings-row-title')?.textContent?.trim().slice(0, 40), left: Math.round(inner.left - box.left), right: Math.round(box.right - inner.right), below: c.top >= text.bottom - 0.5 });
  }
  return { outside, rows };
}"""


def long_picks() -> dict:
    """The installation's settings with the web search's three fallbacks picked, under the long names
    the host gives them: the pick that ran off the operator's screen."""
    settings = copy.deepcopy(SETTINGS)
    settings["search_backends"] = [
        {"id": "searxng", "label": "SearXNG (self-hosted metasearch)", "needs_key": False},
        {"id": "duckduckgo", "label": "DuckDuckGo (no install, no key)", "needs_key": False},
        {"id": "keenable", "label": "Keenable (keyword and semantic search)", "needs_key": True, "available": True},
        {"id": "serper", "label": "Serper (Google results through the key proxy)", "needs_key": True, "available": True},
    ]
    settings["tools"]["web"]["search"]["fallback"] = ["keenable", "serper", "duckduckgo"]
    return settings


def page_for(browser, width: int, lang: str) -> Page:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    settings = json.dumps(long_picks())

    def picks(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method != "GET":
            return route.fallback()
        return route.fulfill(status=200, content_type="application/json", body=settings)

    page.route("**/api/settings", picks)
    page.goto(f"{BASE}/settings?token=t&lang={lang}")
    page.wait_for_selector(".settings-col, .settings-link", timeout=20000)
    return page


def sweep(page: Page, width: int, lang: str, problems: list[str]) -> None:
    for section in SECTIONS:
        where = f"{lang} {width}px {section}"
        page.goto(f"{BASE}/settings/{section}?token=t&lang={lang}")
        try:
            page.wait_for_selector(".settings-col .card, .settings-col .settings-row, .settings-col .empty", timeout=15000)
        except Exception:  # noqa: BLE001
            problems.append(f"{where}: the page did not draw")
            continue
        page.wait_for_timeout(700)
        found = page.evaluate(MEASURE)
        if found is None:
            problems.append(f"{where}: no settings column")
            continue
        for item in found["outside"][:5]:
            problems.append(f"{where}: past the screen's edge: {item}")
        for row in found["rows"]:
            if not row["below"]:
                problems.append(f"{where}: the {row['kind']} of «{row['title']}» is beside its words, not under them")
            if abs(row["left"]) > 1 or abs(row["right"]) > 1:
                problems.append(f"{where}: the {row['kind']} of «{row['title']}» stands {row['left']} px from the row's left and {row['right']} px from its right")
        # Every dropdown opened stays on the screen, its trigger in its row.
        buttons = page.locator(".settings-col .dropdown-btn:not(:disabled)")
        for i in range(buttons.count()):
            button = buttons.nth(i)
            if not button.is_visible():
                continue
            label = (button.get_attribute("aria-label") or "")[:40]
            try:
                button.scroll_into_view_if_needed(timeout=3000)
                button.click(timeout=3000)
            except Exception:  # noqa: BLE001
                problems.append(f"{where}: the control «{label}» cannot be pressed: it is off the screen")
                continue
            listbox = page.locator(".settings-col .dropdown-list")
            if not listbox.count():
                continue
            box = listbox.first.bounding_box()
            trigger = button.bounding_box()
            row = button.evaluate("(b) => { const r = (b.closest('.settings-row') || b.parentElement).getBoundingClientRect(); return { x: r.left, r: r.right }; }")
            if box and (box["x"] < -0.5 or box["x"] + box["width"] > width + 0.5):
                problems.append(f"{where}: the list of «{label}» runs off the screen: {round(box['x'])}..{round(box['x'] + box['width'])}")
            if trigger and (trigger["x"] < row["x"] - 0.5 or trigger["x"] + trigger["width"] > row["r"] + 0.5):
                problems.append(f"{where}: the control «{label}» leaves its row: {round(trigger['x'])}..{round(trigger['x'] + trigger['width'])} in {round(row['x'])}..{round(row['r'])}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (390, 360):
                page = page_for(browser, width, lang)
                sweep(page, width, lang, problems)
                page.context.close()
            print(f"settings on a phone {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
