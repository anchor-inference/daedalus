"""Drive the session's terminal dock in a real browser and refuse what does not behave.

Tabs and the split; a tab's ✕ only detaches (no request reaches the host); End on a busy terminal
asks first and then POSTs the kill; a new terminal is made in the session's folder in the default
environment; maximise and back moves the same terminal instead of reattaching it; the header button
and the session's row both count the running terminals; and on a phone the header opens a list, a
row of which opens the terminal full screen.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_dock.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots  # noqa: E402
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, stub_requests, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def attaches(term: TerminalStub, id_: str) -> list[dict]:
    return [f["json"] for c in term.of(id_) for f in c.of("attach")]


def desktop(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = TerminalStub(S1)
    term.add("t1aaaaaaaaaa", title="bash · bakery", busy=True)
    term.add("t2bbbbbbbbbb", title="npm run dev")
    term.add("t3cccccccccc", title="htop")
    term.emit("t1aaaaaaaaaa", "bakery (main) $ npm test -- checkout\r\n")
    term.emit("t2bbbbbbbbbb", "VITE v7.1.4  ready in 412 ms\r\n")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(DEBUG)
    context.add_init_script(dock_state(S1, ["t1aaaaaaaaaa", "t2bbbbbbbbbb"]))
    page = open_session(context, term, stub, BASE, S1)
    wait_live(page, "t1aaaaaaaaaa")
    page.wait_for_timeout(400)

    tabs = page.locator(".term-tab")
    print("tabs:", tabs.count(), [t.strip() for t in tabs.all_inner_texts()])
    if tabs.count() != 2:
        problems.append(f"the dock opened with {tabs.count()} tabs, not the two it saved")
    if page.locator(".term-tab.on[data-tab='t1aaaaaaaaaa']").count() != 1:
        problems.append("the saved active tab is not the one shown")
    pill = page.locator(".term-tools .term-env").inner_text().strip()
    print("environment pill:", pill)
    if pill.lower() != "container":
        problems.append(f"the environment pill says {pill!r}")

    # The header counts the session's running terminals (three: one of them has no tab).
    count = page.locator(".chat-head .term-button .term-count").inner_text().strip()
    if count != "3":
        problems.append(f"the header button counts {count!r} running terminals, not 3")
    # The session's row in the sidebar carries the same count (the listing says 3).
    row_pill = page.locator(f".erow[data-session='{S1}'] .erow-terms")
    if not row_pill.count() or row_pill.first.inner_text().strip() != "3":
        problems.append("the session's row in the sidebar does not show its terminal count")

    # The hidden tab is connected but has never sent a size.
    wait_live(page, "t2bbbbbbbbbb")
    if term.resizes("t2bbbbbbbbbb"):
        problems.append(f"the hidden tab sent a size: {term.resizes('t2bbbbbbbbbb')}")

    # Split: the second tab joins on the right and sends its size once it is shown.
    page.locator(".term-tools button[aria-label='Split']").click()
    page.wait_for_selector(".term-panes.split .term-slot.right .term-view:not(.hidden)", timeout=5000)
    page.wait_for_timeout(600)
    widths = page.evaluate("() => [...document.querySelectorAll('.term-slot.left, .term-slot.right')].map((e) => Math.round(e.getBoundingClientRect().width))")
    print("split widths:", widths)
    if len(widths) != 2 or not (1.1 < widths[0] / max(1, widths[1]) < 1.4):
        problems.append(f"the split is not 1.25 : 1 ({widths})")
    if not term.resizes("t2bbbbbbbbbb"):
        problems.append("the right pane never sent its size once shown")

    # ✕ on the right tab detaches: the tab goes, nothing is asked of the host.
    before = len(term.requests)
    page.locator(".term-tab[data-tab='t2bbbbbbbbbb'] .term-tab-x").click()
    page.wait_for_timeout(300)
    after = [(m, p) for m, p, _ in term.requests[before:] if m != "GET"]
    if page.locator(".term-tab[data-tab='t2bbbbbbbbbb']").count():
        problems.append("✕ left the tab in place")
    if after:
        problems.append(f"✕ asked the host for something: {after}")
    if page.locator(".term-panes.split").count():
        problems.append("the split stayed after its right tab was closed")

    # The menu offers the terminals that have no tab; picking one opens it.
    page.locator(".term-tabs button[aria-label='More terminals']").click()
    items = page.locator(".menu button").all_inner_texts()
    print("menu:", items)
    if not any("htop" in i for i in items) or not any("npm run dev" in i for i in items):
        problems.append(f"the menu does not offer the terminals without a tab: {items}")
    if not any(i.strip() == "New terminal on the host" for i in items):
        problems.append("the menu has no host terminal")
    page.locator(".menu button", has_text="htop").click()
    page.wait_for_selector(".term-tab.on[data-tab='t3cccccccccc']", timeout=5000)
    page.locator(".term-tab[data-tab='t1aaaaaaaaaa']").click()

    # End on the busy terminal asks, and only then kills.
    page.locator(".term-tabs button[aria-label='More terminals']").click()
    page.locator(".menu button", has_text="End").click()
    page.wait_for_selector(".dialog", timeout=5000)
    question = page.locator(".dialog").inner_text()
    print("end asks:", question.replace("\n", " | "))
    if "stops" not in question or not stub_requests(term, "POST", "/kill") == []:
        problems.append("End on a busy terminal did not ask first")
    page.locator(".dialog .btn.danger").click()
    page.wait_for_timeout(600)
    if len(stub_requests(term, "POST", "/t1aaaaaaaaaa/kill")) != 1:
        problems.append("End did not POST the kill once confirmed")
    page.wait_for_selector(".term-view[data-terminal-view='t1aaaaaaaaaa'] .term-exit", timeout=5000)
    banner = page.locator(".term-view[data-terminal-view='t1aaaaaaaaaa'] .term-exit").inner_text()
    print("exit banner:", banner.replace("\n", " | "))
    if "129" not in banner or "Restart" not in banner:
        problems.append(f"the exit banner does not say the code and offer Restart: {banner!r}")

    # Maximise and back: the same instance moves, no attach asks for a snapshot.
    page.locator(".term-tab[data-tab='t3cccccccccc']").click()
    wait_live(page, "t3cccccccccc")
    page.wait_for_timeout(300)
    snapshot_attaches = [a for a in attaches(term, "t3cccccccccc") if not a.get("haveState")]
    n_attaches = len(attaches(term, "t3cccccccccc"))
    page.locator(".term-tools button[aria-label='Full screen']").click()
    page.wait_for_selector(".term-full .term-view[data-terminal-view='t3cccccccccc']", timeout=5000)
    page.wait_for_timeout(500)
    full_box = page.locator(".term-full").bounding_box()
    if not full_box or full_box["width"] < 1400 or full_box["height"] < 880:
        problems.append(f"the full-screen view does not cover the window: {full_box}")
    page.locator(".term-full-head button[aria-label='Back to the dock']").click()
    page.wait_for_selector(".term-dock .term-view[data-terminal-view='t3cccccccccc']:not(.hidden)", timeout=5000)
    page.wait_for_timeout(500)
    later = attaches(term, "t3cccccccccc")
    print("attaches before/after maximise:", n_attaches, len(later))
    if len(later) != n_attaches or [a for a in later if not a.get("haveState")] != snapshot_attaches:
        problems.append(f"maximise and back reattached the terminal: {later}")

    # "+" makes a terminal for this session in the default environment and shows it.
    page.locator(".term-tabs button[aria-label='New terminal']").click()
    page.wait_for_timeout(800)
    made = stub_requests(term, "POST", "/api/terminals")
    print("created:", made)
    if made != [{"env": "container", "owner_kind": "session", "owner_id": S1}]:
        problems.append(f"+ did not ask for a container terminal of this session: {made}")
    new_ids = [i for i in term.terms if i.startswith("new")]
    if not new_ids or not page.locator(f".term-tab.on[data-tab='{new_ids[0]}']").count():
        problems.append("the new terminal did not open as the active tab")

    # Ctrl+` hides the dock and brings it back with its tabs.
    page.keyboard.press("Control+Backquote")
    page.wait_for_timeout(300)
    if page.locator(".term-dock.open").count():
        problems.append("Ctrl+` did not hide the dock")
    page.keyboard.press("Control+Backquote")
    page.wait_for_timeout(300)
    if not page.locator(".term-dock.open").count() or page.locator(".term-tab").count() < 2:
        problems.append("Ctrl+` did not bring the dock back with its tabs")

    # The delete dialog says how many terminals end with the session.
    page.locator(".chat-head button[aria-label='Session actions']").click()
    page.locator(".menu button", has_text="Delete").click()
    page.wait_for_selector(".dialog", timeout=5000)
    body = page.locator(".dialog").inner_text()
    print("delete says:", body.replace("\n", " | "))
    if "terminals will end" not in body:
        problems.append("the delete dialog does not say the terminals end")
    page.locator(".dialog .dialog-actions .btn").first.click()
    context.close()


def phone(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = TerminalStub(S1)
    term.add("t1aaaaaaaaaa", title="bash · bakery")
    term.emit("t1aaaaaaaaaa", "bakery (main) $ ls\r\nsrc  package.json\r\n")
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    context.add_init_script(DEBUG)
    page = open_session(context, term, stub, BASE, S1)
    page.wait_for_timeout(500)
    if page.locator(".term-dock").count():
        problems.append("a phone got the desktop dock")
    button = page.locator(".chat-head .term-button")
    if not button.count() or button.get_attribute("aria-pressed") is not None:
        problems.append("the phone's header button is missing or pretends to be a toggle")
    button.click()
    page.wait_for_selector(".term-sheet .term-sheet-row", timeout=5000)
    page.locator(".term-sheet .term-sheet-row").first.click()
    page.wait_for_selector(".term-full .term-view[data-terminal-view='t1aaaaaaaaaa']", timeout=5000)
    wait_live(page, "t1aaaaaaaaaa")
    page.wait_for_timeout(500)
    box = page.locator(".term-full .term-screen").bounding_box()
    print("phone terminal box:", box)
    if not box or box["width"] < 370 or box["height"] < 600:
        problems.append(f"the phone's terminal does not fill the screen: {box}")
    sizes = term.resizes("t1aaaaaaaaaa")
    print("phone sizes:", sizes)
    if not sizes or sizes[-1][2] < 20 or sizes[-1][2] > 60:
        problems.append(f"the phone sent no size, or one that does not fit 390 px: {sizes}")
    page.locator(".term-full-head button[aria-label='Back']").click()
    page.wait_for_timeout(300)
    if page.locator(".term-full").count():
        problems.append("Back did not close the phone's terminal")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    # The session's row counts three running terminals, as the host's listing would.
    screenshots.SESSIONS[0]["terminals"] = 3
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desktop(browser, problems)
        phone(browser, problems)
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
