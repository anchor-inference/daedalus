"""No ring around a region after a message is sent: the focus ring is a control's, not a container's.

A violet border appeared around the whole conversation after a message went out. It was the app-wide
`:focus-visible` outline landing on a container: pressing Enter to send leaves the browser in keyboard
mode, and any focus that lands afterwards on a region (the conversation's scroller, which Chromium makes
focusable when nothing in it is, the panel's body, a sheet, the phone's drawer) counts as keyboard focus
and drew the ring. Now only controls draw it; a region draws it only when the reader reached it with Tab.

Claims, at 1280x500 (the conversation scrolls), dark, en:

- sending by Enter and by a click on Send leaves no element that is not a control with a visible outline
  or a box-shadow in the ring's colour;
- after a keyboard send, the conversation's scroller, the panel's body and the sidebar's list focused
  from a script draw no ring, and neither does the phone's drawer or a sheet's frame;
- a control still draws its ring when Tab reaches it, and a region the reader tabs to (a scroller with
  nothing focusable in it) draws it too.

    APP_URL=http://127.0.0.1:<port>/app python tests/browser/check_focus_ring.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None

# What counts as a control, as the app's stylesheet says it: anything native, anything put in the tab
# order on purpose, and the widget roles. Everything else is a region.
CONTROL = ("a[href], button, input, select, textarea, summary, [contenteditable]:not([contenteditable='false']), [tabindex]:not([tabindex='-1']), "
           "[role=button], [role=link], [role=tab], [role=treeitem], [role=option], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], "
           "[role=checkbox], [role=radio], [role=switch], [role=slider], [role=separator], [role=gridcell], [role=row], [role=listbox], [role=combobox], "
           "[role=textbox], [role=spinbutton], [role=searchbox]")

# Every element drawing a ring now: an outline, or a box-shadow in the ring's colour.
RINGS = """(control) => {
  const probe = document.createElement('i');
  probe.style.color = getComputedStyle(document.documentElement).getPropertyValue('--ring');
  document.body.append(probe);
  const ring = getComputedStyle(probe).color;
  probe.remove();
  const out = [];
  for (const e of document.querySelectorAll('*')) {
    const s = getComputedStyle(e);
    const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0 && s.outlineColor !== 'rgba(0, 0, 0, 0)';
    const shadow = s.boxShadow !== 'none' && s.boxShadow.includes(ring);
    if (outline || shadow) out.push({ what: e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\\s+/).join('.') : ''), control: e.matches(control), tabbed: e.hasAttribute('data-tabbed') });
  }
  return out;
}"""


def stray(page, where: str) -> list[str]:  # type: ignore[no-untyped-def]
    """A ring on anything but a control, or on a region the reader did not tab to."""
    return [f"{where}: {ring['what']} draws a ring" for ring in page.evaluate(RINGS, CONTROL) if not ring["control"] and not ring["tabbed"]]


def focus_quietly(page, selector: str) -> bool:  # type: ignore[no-untyped-def]
    """Focus a region from a script, as code restoring focus would: a tabindex of -1 if it has none."""
    return bool(page.evaluate("""(sel) => { const e = document.querySelector(sel); if (!e) return false;
      if (!e.hasAttribute('tabindex') && e.tabIndex < 0) { e.setAttribute('tabindex', '-1'); e.setAttribute('data-quiet', ''); } e.focus(); return document.activeElement === e; }""", selector))


def unfocus_quietly(page) -> None:  # type: ignore[no-untyped-def]
    """Take back the tabindex `focus_quietly` gave, so the page is as the app drew it."""
    page.evaluate("() => document.querySelectorAll('[data-quiet]').forEach((e) => { e.removeAttribute('tabindex'); e.removeAttribute('data-quiet'); })")


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    page = browser.new_page(viewport={"width": 1280, "height": 500}, color_scheme="dark")
    page.route("**/api/**", shots.stub)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme=dark&lang=en&panel=overview")
    page.wait_for_selector(".composer .composer-field", timeout=15000)
    page.wait_for_timeout(800)
    field = page.locator(".composer .composer-field")

    field.click()
    page.keyboard.type("a message sent with Enter")
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    problems += stray(page, "sent by Enter")
    if SHOTS is not None:
        page.screenshot(path=str(SHOTS / "focus-after-enter.png"))
    # Still in keyboard mode: every region a script might focus now stays without a ring.
    for region in (".chat-scroll", ".timeline", ".panel-body", ".sidebar-body"):
        page.keyboard.press("Shift")
        if focus_quietly(page, region):
            problems += stray(page, f"{region} focused after a keyboard send")
        if SHOTS is not None and region == ".chat-scroll":
            page.screenshot(path=str(SHOTS / "focus-scroller-after-enter.png"))
        field.focus()
        unfocus_quietly(page)

    field.click()
    page.keyboard.type("a message sent with a click")
    page.locator(".composer .roundbtn.primary").click()
    page.wait_for_timeout(600)
    problems += stray(page, "sent by a click")

    # A control keeps its ring under Tab, and a region the reader tabs to shows one as well.
    field.click()
    page.keyboard.press("Tab")
    active = page.evaluate("(control) => { const a = document.activeElement; return { control: !!a && a.matches(control), outline: a ? getComputedStyle(a).outlineStyle : '' }; }", CONTROL)
    if not active["control"] or active["outline"] == "none":
        problems.append(f"Tab from the composer reaches no control with a ring ({active})")
    page.evaluate("() => document.querySelectorAll('.chat-scroll button, .chat-scroll a, .chat-scroll [tabindex], .chat-scroll [role=button], .chat-scroll pre').forEach((e) => e.remove())")
    field.focus()
    page.keyboard.press("Shift+Tab")
    reached = page.evaluate("() => { const a = document.activeElement; return { scroller: !!a && a.classList.contains('chat-scroll'), outline: a ? getComputedStyle(a).outlineStyle : '', active: a ? a.tagName + '.' + a.className : '' }; }")
    if not reached["scroller"]:
        problems.append(f"Shift+Tab from the composer did not reach the conversation with nothing focusable in it ({reached})")
    elif reached["outline"] == "none":
        problems.append("the conversation reached with Shift+Tab shows no ring")
    elif SHOTS is not None:
        page.screenshot(path=str(SHOTS / "focus-scroller-tabbed.png"))
    page.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    page = browser.new_page(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page.route("**/api/**", shots.stub)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme=dark&lang=en&panel=overview")
    page.wait_for_selector(".panel-sheet [data-tool-page='overview']", timeout=15000)
    page.wait_for_timeout(600)
    page.keyboard.press("Shift")
    for region in (".panel-sheet", ".panel-sheet .panel-body"):
        if focus_quietly(page, region):
            problems += stray(page, f"phone {region} focused from a script")
    page.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += desktop(browser)
        problems += phone(browser)
        browser.close()
    print("problems:", "\n  ".join(problems) if problems else "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or shots.UNHANDLED.report())
