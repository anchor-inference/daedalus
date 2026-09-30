"""A staff member's column laid over its terminal, full width, and back, at 1440 px in both languages.

The column beside a command-line member (Session, Details, Changes, Notes, Browser) could only be
dragged wider, and a wider column is a narrower terminal: the member's TUI hears the resize and
reflows. The column now opens over the whole body instead. What is checked:

- the Browser tab, expanded, covers the body from edge to edge and shows the page live;
- the terminal underneath keeps its box to the pixel, and no RESIZE reaches the member's PTY
  while the column is over it, nor after it comes back;
- the button and Escape both bring the terminal back, the column at the width it had;
- any tab expands the same way (Details), and closing the column drops the expansion;
- a phone, whose column is a sheet already, offers no such button.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_staff_overlay.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, render_scenes  # noqa: E402
from check_staff_view import open_page, stand  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

BOX = """(sel) => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; }"""
WORDS = {"en": {"expand": "Open over the terminal", "restore": "Back to the terminal"}, "ru": {"expand": "Открыть поверх терминала", "restore": "Вернуться к терминалу"}}


def run(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    words = WORDS[lang]
    focus, term, pid = stand(lang)
    bs = BrowserStub(scenes)
    bs.add("gs", scene="shop", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=pid, acting=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.staff.view', 'terminal'); localStorage.removeItem('daedalus.staff.side'); } catch {}")
    page = open_page(context, focus, term, EventFeed(), "about:blank")
    bs.install(page)
    page.goto(f"{BASE}/project/{pid}/staff/st-ira?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".staff-cli .staff-term .xterm", timeout=20000)
    page.wait_for_selector(".staff-aside .panel-tab[data-tab='browser']", timeout=20000)
    page.wait_for_timeout(1500)
    terminal = page.evaluate(BOX, ".staff-term")
    aside = page.evaluate(BOX, ".staff-aside")
    body = page.evaluate(BOX, ".staff-cli .chat-body")
    resizes = len(term.resizes("tm-ira"))

    page.locator(".staff-aside .panel-tab[data-tab='browser']").click()
    page.wait_for_selector(".staff-aside .bp .bv[data-state='live']", timeout=10000)
    expand = page.get_by_role("button", name=words["expand"])
    expand.click()
    page.wait_for_selector(".staff-aside.expanded", timeout=3000)
    page.wait_for_timeout(900)
    over = page.evaluate(BOX, ".staff-aside")
    if not over or abs(over["x"] - body["x"]) > 1 or abs(over["w"] - body["w"]) > 1 or abs(over["h"] - body["h"]) > 1:
        say(f"the expanded column {over} does not cover the body {body}")
    if page.evaluate(BOX, ".staff-term") != terminal:
        say(f"the terminal moved under the column: {page.evaluate(BOX, '.staff-term')} not {terminal}")
    if len(term.resizes("tm-ira")) != resizes:
        say(f"the PTY was resized while the column covered it: {term.resizes('tm-ira')[resizes:]}")
    if page.locator(".staff-aside .bp .bv[data-state='live']").count() != 1:
        say("the browser is not live in the expanded column")
    view = page.evaluate(BOX, ".staff-aside .bp")
    if not view or view["w"] < body["w"] - 40:
        say(f"the browser is {view} wide, not the body's {body['w']}")
    if page.locator(".bp-pip").count():
        say("the corner card shows over the expanded browser")
    if os.environ.get("SHOTS"):
        page.screenshot(path=f"{os.environ['SHOTS']}/staff-overlay-{lang}.png")
    # Tapping the terminal is impossible: it is under the column, which is what the operator asked for.
    top = page.evaluate("(p) => document.elementFromPoint(p.x, p.y)?.closest('.staff-aside') !== null", {"x": terminal["x"] + 40, "y": terminal["y"] + 40})
    if not top:
        say("the column is not on top of the terminal")

    page.get_by_role("button", name=words["restore"]).click()
    page.wait_for_selector(".staff-aside:not(.expanded)", timeout=3000)
    page.wait_for_timeout(600)
    if page.evaluate(BOX, ".staff-aside") != aside:
        say(f"the column came back at {page.evaluate(BOX, '.staff-aside')}, not {aside}")

    # Any tab, and Escape inside the column brings the terminal back.
    page.locator(".staff-aside .panel-tab[data-tab='details']").click()
    page.get_by_role("button", name=words["expand"]).click()
    page.wait_for_selector(".staff-aside.expanded", timeout=3000)
    page.locator(".staff-aside .panel-tab[data-tab='details']").focus()
    page.keyboard.press("Escape")
    page.wait_for_selector(".staff-aside:not(.expanded)", timeout=3000)
    page.wait_for_timeout(300)
    if len(term.resizes("tm-ira")) != resizes:
        say(f"the PTY was resized while a tab was expanded: {term.resizes('tm-ira')[resizes:]}")
    before_close = len(term.resizes("tm-ira"))
    # Closing the column while it covers the body drops the expansion with it.
    page.get_by_role("button", name=words["expand"]).click()
    page.wait_for_selector(".staff-aside.expanded", timeout=3000)
    page.locator(".staff-cli .chat-head .head-actions button[aria-pressed]").last.click()
    page.wait_for_selector(".staff-aside", state="detached", timeout=3000)
    page.locator(".staff-cli .chat-head .head-actions button[aria-pressed]").last.click()
    page.wait_for_selector(".staff-aside:not(.expanded)", timeout=3000)
    page.wait_for_timeout(600)
    if page.evaluate(BOX, ".staff-term") != terminal:
        say(f"after all that the terminal is {page.evaluate(BOX, '.staff-term')}, not {terminal}")
    # A closed column is a wider terminal, and the PTY may be told so: whether the width in between
    # goes out depends on whether the column is opened again within the terminal's 100 ms resize
    # debounce, which a loaded machine does not keep ("156 then 114 columns"). What must hold is that
    # the PTY ends at the size it had.
    after = term.resizes("tm-ira")[before_close:]
    had = term.resizes("tm-ira")[:resizes]
    if after and (not had or after[-1][2:] != had[-1][2:]):
        say(f"the PTY was left at another size: {after}, not {had[-1] if had else 'its first size'}")
    context.close()

    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
    page = open_page(context, focus, term, EventFeed(), f"{BASE}/project/{pid}/staff/st-ira?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".staff-cli .chat-head", timeout=20000)
    page.locator(".staff-cli .chat-head .head-actions button[aria-pressed]").last.tap()
    page.wait_for_selector(".staff-sheet .staff-panel", timeout=5000)
    if page.locator(".staff-sheet .staff-expand").count():
        say("the phone's sheet offers to expand")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            run(browser, scenes, lang, problems)
            print(f"staff overlay {lang}: {'ok' if not any(x.startswith(f'[{lang}]') for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
