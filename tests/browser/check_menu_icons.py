"""A menu row draws its icon and its label on one line: centred on each other, one gap, one text column.

The row was once a block with an inline icon, which put the icon on the text's baseline: three pixels
above the label's middle, touching it, and (for an item with no icon, like "Open") a text column that
started an icon's width further left than the rest. A stylesheet can say all of that and still be
wrong on screen, so this measures it where it is drawn: every menu that pairs an icon with a label,
on a desktop and on a phone, in both colour schemes.

Build the app, serve it with tests/browser/serve_app.py, then:

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_menu_icons.py

Exit 0 when every measured row is within a pixel of centred, shares its menu's gap and text column.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, S2, S6, UNHANDLED, open_tool, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

# One entry per row of a menu: where its label starts relative to the row, and, when the row leads with
# an icon, how far the icon's middle is from the label's middle and how wide the gap between them is.
ROWS = """
([root, rows]) => [...document.querySelectorAll(root)].flatMap((menu) => [...menu.querySelectorAll(rows)].map((row) => {
  const text = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, { acceptNode: (n) => n.textContent.trim() ? 1 : 2 }).nextNode();
  if (!text) return null;
  const range = document.createRange();
  range.selectNodeContents(text);
  const label = [...range.getClientRects()].find((r) => r.width > 0);
  const icon = row.firstElementChild;
  const lead = icon && icon.matches('svg.ic') ? icon.getBoundingClientRect() : null;
  // A row may stack a hint under its title (a phone's, the picker's Free): the icon is centred on both lines, not on the first.
  const block = row.querySelector('.menu-row-main, .model-text');
  const middle = block ? block.getBoundingClientRect() : label;
  return {
    text: text.textContent.trim().slice(0, 24),
    left: Math.round((label.left - row.getBoundingClientRect().left) * 10) / 10,
    dy: lead ? Math.round(((lead.top + lead.height / 2) - (middle.top + middle.height / 2)) * 10) / 10 : null,
    gap: lead ? Math.round((label.left - lead.right) * 10) / 10 : null,
  };
}).filter(Boolean));
"""

# An icon beside its label in a tab or a row of the frame: the same centre line, or, where the label
# is under the icon, the same vertical axis.
TABS = """
(root) => [...document.querySelectorAll(root)].filter((tab) => tab.getClientRects().length).map((tab) => {
  const icon = tab.querySelector('svg.ic'), word = tab.querySelector('span:not(.count)');
  // A strip short of room draws an inactive tab as its icon alone: there is no label to centre on.
  if (!icon || !word || !word.getClientRects().length) return null;
  const i = icon.getBoundingClientRect(), w = word.getBoundingClientRect();
  const under = w.top >= i.bottom - 2;
  return { text: word.textContent.trim(), under, off: Math.round((under ? (i.left + i.width / 2) - (w.left + w.width / 2) : (i.top + i.height / 2) - (w.top + w.height / 2)) * 10) / 10 };
}).filter(Boolean);
"""

MENU_ROWS = ":scope > button, :scope > .menu-row"
# The access menu's rows: its switches and the agent's language under them, in one column of words.
ACCESS_ROWS = ":scope > .menu-row, :scope .access-lang-row > .menu-row"

problems: list[str] = []


def rows(page: Page, root: str, which: str = MENU_ROWS) -> list[dict]:
    return page.evaluate(ROWS, [root, which])


def judge(name: str, rows: list[dict], *, icons: int) -> None:
    """Every icon row is centred on its label; every row's label starts at one x and one gap from its icon."""
    with_icon = [r for r in rows if r["dy"] is not None]
    if len(with_icon) < icons:
        problems.append(f"{name}: only {len(with_icon)} rows with an icon were found, expected at least {icons}")
    for r in with_icon:
        if abs(r["dy"]) > 1:
            problems.append(f"{name}: the icon of '{r['text']}' is {r['dy']}px from the middle of its label")
    gaps = {r["gap"] for r in with_icon}
    if gaps and (max(gaps) - min(gaps) > 1 or not 4 <= min(gaps) <= 16):
        problems.append(f"{name}: the gap between icon and label is {sorted(gaps)}")
    lefts = {r["left"] for r in rows}
    if lefts and max(lefts) - min(lefts) > 1:
        problems.append(f"{name}: the text starts at {sorted(lefts)} px, not in one column: {[(r['text'], r['left']) for r in rows]}")


def desktop(page: Page, scheme: str) -> None:
    def fresh(route: str) -> None:
        page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&lang=en&scheme={scheme}")
        page.wait_for_selector(".sidebar .sb-chat", timeout=15000)

    fresh(f"agents/{S1}")
    row = page.locator(f'.sidebar .sb-chat[data-session="{S2}"]')
    row.click(button="right")
    page.wait_for_selector(".context-menu")
    judge(f"{scheme} chat right click", rows(page, ".context-menu"), icons=7)
    page.keyboard.press("Escape")
    row.hover()
    row.locator(".sb-acts button[aria-haspopup='menu']").click()
    judge(f"{scheme} chat overflow", rows(page, ".menu[role=menu]"), icons=7)
    page.keyboard.press("Escape")
    page.locator(".chat button[aria-label='Session actions']").first.click()
    judge(f"{scheme} session menu", rows(page, ".menu[role=menu]"), icons=10)
    page.keyboard.press("Escape")
    page.locator(".sb-pinned-nav a.sb-nav[data-nav]").nth(1).click(button="right")
    page.wait_for_selector(".context-menu")
    judge(f"{scheme} pinned row right click", rows(page, ".context-menu"), icons=3)
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Add to the message").click()
    judge(f"{scheme} composer +", rows(page, ".menu.pop"), icons=3)
    page.keyboard.press("Escape")
    field = page.locator(".composer-field").first
    field.fill("A draft with a selection")
    field.evaluate("""el => { el.focus(); const text = el.querySelector('[data-line]').firstChild; const range = document.createRange(); range.setStart(text, 2); range.setEnd(text, 7);
      getSelection().removeAllRanges(); getSelection().addRange(range); const r = el.getBoundingClientRect(); el.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:r.left + 60, clientY:r.top + 10 })); }""")
    page.wait_for_selector(".context-menu")
    judge(f"{scheme} text right click", rows(page, ".context-menu"), icons=4)
    page.keyboard.press("Escape")
    page.locator(".composer .composer-access").click()
    page.wait_for_selector(".menu.access-menu")
    judge(f"{scheme} access menu", rows(page, ".menu.access-menu", ACCESS_ROWS), icons=0)
    page.keyboard.press("Escape")
    # The panel's "+" menu: every tool the chat offers, under its group's heading.
    page.locator(".panel .panel-add").click()
    page.wait_for_selector(".menu.panel-tools .panel-tools-row")
    judge(f"{scheme} panel tools", rows(page, ".menu.panel-tools", ".panel-tools-row"), icons=8)
    page.keyboard.press("Escape")
    # A checkbox row has no icon of its own and has to stand in the column of the rows around it.
    open_tool(page, "files")
    page.locator(".explorer-more").click()
    judge(f"{scheme} files tools", rows(page, ".menu[role=menu]"), icons=2)
    page.keyboard.press("Escape")
    # The strip holds the Overview and what was opened: open enough tools that it has several tabs to
    # measure, in a panel at its widest so that each of them still draws its word beside its icon.
    # In a chat with no open tasks: with some, the Board tab stands beside the opened ones, four tabs
    # do not all fit with their words in 520 px, and the strip steps down to the open tab's word alone.
    fresh(f"agents/{S6}")
    for tool in ("details", "context"):
        open_tool(page, tool)
    tabs = page.evaluate(TABS, ".panel-tab")
    if len(tabs) < 4:
        problems.append(f"{scheme} panel tabs: only {len(tabs)} found")
    problems.extend(f"{scheme} panel tab '{t['text']}' is {t['off']}px off its icon's axis" for t in tabs if abs(t["off"]) > 1)


def phone(page: Page, scheme: str) -> None:
    page.goto(f"{BASE}/agents/{S2}?token=t&lang=en&scheme={scheme}")
    page.wait_for_selector(".composer-field", timeout=15000)
    field = page.locator(".composer-field").first
    field.fill("A draft with a selection")
    field.evaluate("""el => { el.focus(); const text = el.querySelector('[data-line]').firstChild; const range = document.createRange(); range.setStart(text, 2); range.setEnd(text, 7);
      getSelection().removeAllRanges(); getSelection().addRange(range); const r = el.getBoundingClientRect(); el.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:r.left + 60, clientY:r.top + 10 })); }""")
    page.wait_for_selector(".context-menu")
    judge(f"{scheme} phone text right click", rows(page, ".context-menu"), icons=4)
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Add to the message").click()
    page.wait_for_selector(".sheet .menu.attach-menu .menu-row")
    judge(f"{scheme} phone + sheet", rows(page, ".sheet .menu.attach-menu"), icons=3)


def run() -> None:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for scheme in ("dark", "light"):
            context = browser.new_context(viewport=DESK, color_scheme=scheme)
            context.add_init_script("try { localStorage.setItem('daedalus.width.panel', '520'); } catch (e) {}")
            page = context.new_page()
            page.set_default_timeout(8000)
            page.route("**/api/**", stub)
            desktop(page, scheme)
            context.close()
            context = browser.new_context(viewport=PHONE, color_scheme=scheme, is_mobile=True, has_touch=True)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.route("**/api/**", stub)
            phone(page, scheme)
            context.close()
        browser.close()
    assert not UNHANDLED.report()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print("menu icons: centred on their labels, one gap and one text column in every menu, both schemes, desktop and phone")


if __name__ == "__main__":
    run()
