"""Drive the terminal sandbox toggle in a real browser and refuse what does not behave.

The dock's menu carries a "Sandbox" checkbox: ticking it keeps the menu open, is remembered on the
device, and makes the next terminal ask the host for the sandbox; an environment that cannot give one
offers no terminal while it is ticked, and says why. A sandboxed terminal wears a shield on its tab,
in the full view and in the phone's list. A running terminal is restarted with or without the
sandbox from the same menu, and what the sandbox left read-only is told in a toast. Where no
environment can sandbox, the checkbox is disabled with the daemon's reason and does not apply.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_sandbox.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, phone_tool, stub  # noqa: E402
from terminal_stub import DEBUG, ENVS, TerminalStub, dock_state, open_session, stub_requests, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
MENU = ".term-tabs button[aria-label='More terminals']"
TOGGLE = ".menu button[role='menuitemcheckbox']"


def open_menu(page) -> None:  # type: ignore[no-untyped-def]
    page.locator(MENU).click()
    page.wait_for_selector(".menu", timeout=5000)


def close_menu(page) -> None:  # type: ignore[no-untyped-def]
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)


def shields(page) -> dict[str, bool]:  # type: ignore[no-untyped-def]
    out = {}
    for tab in page.locator(".term-tab").all():
        out[tab.get_attribute("data-tab") or ""] = tab.locator(".term-shield").count() == 1
    return out


def desktop(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = TerminalStub(S1)
    term.add("t1aaaaaaaaaa", title="bash · bakery", sandbox=True)
    term.add("t2bbbbbbbbbb", title="npm run dev")
    term.emit("t1aaaaaaaaaa", "bakery (main) $ touch /etc/x\r\ntouch: cannot touch '/etc/x': Read-only file system\r\n")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(DEBUG)
    context.add_init_script(dock_state(S1, ["t1aaaaaaaaaa", "t2bbbbbbbbbb"]))
    page = open_session(context, term, stub, BASE, S1)
    wait_live(page, "t1aaaaaaaaaa")

    marks = shields(page)
    print("shields:", marks)
    if marks != {"t1aaaaaaaaaa": True, "t2bbbbbbbbbb": False}:
        problems.append(f"the shield is not on exactly the sandboxed tab: {marks}")

    # The checkbox: off, offered (the container can sandbox), and it keeps the menu open.
    open_menu(page)
    toggle = page.locator(TOGGLE)
    if toggle.count() != 1 or toggle.inner_text().strip() != "Sandbox":
        problems.append(f"the menu has no Sandbox checkbox: {page.locator('.menu button').all_inner_texts()}")
    if toggle.get_attribute("aria-checked") != "false" or toggle.is_disabled():
        problems.append("the Sandbox checkbox is not an enabled, unticked box")
    toggle.click()
    page.wait_for_timeout(150)
    if not page.locator(".menu").count():
        problems.append("ticking the checkbox closed the menu")
    if page.locator(TOGGLE).get_attribute("aria-checked") != "true":
        problems.append("the checkbox did not tick")
    # The host cannot sandbox here: with the box ticked it offers no terminal, and says why.
    host = page.locator(".menu button", has_text="New terminal on the host")
    title = host.get_attribute("title") or ""
    print("host item:", host.is_disabled(), title)
    if not host.is_disabled() or "not available" not in title or "Permission denied" not in title:
        problems.append(f"the host item is not disabled with the daemon's reason: {host.is_disabled()} {title!r}")

    term.skipped = [{"path": "/srv/worktrees/bakery", "reason": "missing"}]
    page.locator(".menu button", has_text="New terminal in the container").click()
    page.wait_for_timeout(400)
    made = stub_requests(term, "POST", "/api/terminals")
    print("created:", made)
    if not made or made[-1].get("sandbox") is not True or made[-1].get("env") != "container":
        problems.append(f"the new terminal did not ask for the sandbox: {made}")
    new_id = [t for t in term.terms if t.startswith("new")]
    if not new_id or not shields(page).get(new_id[-1]):
        problems.append(f"the new sandboxed terminal has no shield: {shields(page)}")
    toast = page.locator(".toast").all_inner_texts()
    print("toast:", toast)
    if not any("Left read-only: /srv/worktrees/bakery (missing)" in t for t in toast):
        problems.append(f"what the sandbox left read-only was not told: {toast}")
    term.skipped = []

    # The "+" button follows the checkbox too.
    before = len(stub_requests(term, "POST", "/api/terminals"))
    page.locator(".term-tabs button[aria-label='New terminal']").click()
    page.wait_for_timeout(400)
    plus = stub_requests(term, "POST", "/api/terminals")[before:]
    if len(plus) != 1 or plus[0].get("sandbox") is not True:
        problems.append(f"+ did not follow the checkbox: {plus}")

    # A new page keeps the choice. (A new page rather than a reload: the stubbed sockets of the old
    # one must close with the page, which Playwright's socket routing does not survive.)
    page.close()
    page = open_session(context, term, stub, BASE, S1)
    page.wait_for_selector(".term-tab", timeout=15000)
    open_menu(page)
    if page.locator(TOGGLE).get_attribute("aria-checked") != "true":
        problems.append("the choice was not remembered across a reload")
    close_menu(page)

    # Restart the sandboxed terminal without the sandbox, then back in it.
    page.locator(".term-tab[data-tab='t1aaaaaaaaaa']").click()
    open_menu(page)
    items = page.locator(".menu button").all_inner_texts()
    if not any(i.strip() == "Restart without the sandbox" for i in items):
        problems.append(f"a sandboxed terminal offers no restart without it: {items}")
    page.locator(".menu button", has_text="Restart without the sandbox").click()
    page.wait_for_timeout(400)
    restarts = stub_requests(term, "POST", "/t1aaaaaaaaaa/restart")
    print("restart:", restarts)
    if restarts != [{"sandbox": False}]:
        problems.append(f"the restart did not switch the sandbox off: {restarts}")
    plain = [t for t in term.terms if t.startswith("rst")]
    if not plain or shields(page).get(plain[-1]) is not False:
        problems.append(f"the restarted terminal still wears the shield: {shields(page)}")
    if "t1aaaaaaaaaa" in shields(page):
        problems.append("the restarted terminal did not take the old tab's place")
    open_menu(page)
    page.locator(".menu button", has_text="Restart in the sandbox").click()
    page.wait_for_timeout(400)
    if stub_requests(term, "POST", f"/{plain[-1]}/restart") != [{"sandbox": True}]:
        problems.append("the restart did not switch the sandbox on")

    # Full screen carries the shield too.
    boxed = [t for t in term.terms if t.startswith("rst")][-1]
    page.locator(".term-tools button[aria-label='Full screen']").click()
    page.wait_for_selector(f".term-full[data-full='{boxed}']", timeout=5000)
    if page.locator(".term-full-head .term-shield").count() != 1:
        problems.append("the full view of a sandboxed terminal has no shield")
    context.close()


def nowhere(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """No environment can sandbox: the box is disabled with the reason, and a stale tick does not apply."""
    envs = copy.deepcopy(ENVS)
    envs[0]["sandbox"] = "bwrap is not installed"
    term = TerminalStub(S1, envs=envs)
    term.add("t1aaaaaaaaaa", title="bash")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(DEBUG)
    context.add_init_script(dock_state(S1, ["t1aaaaaaaaaa"]))
    # Ticked on an earlier visit, when the sandbox was there.
    context.add_init_script("try { localStorage.setItem('daedalus.term.sandbox', '1'); } catch (e) {}")
    page = open_session(context, term, stub, BASE, S1)
    wait_live(page, "t1aaaaaaaaaa")
    open_menu(page)
    toggle = page.locator(TOGGLE)
    title = toggle.get_attribute("title") or ""
    print("toggle:", toggle.is_disabled(), toggle.get_attribute("aria-checked"), title)
    if not toggle.is_disabled() or "bwrap is not installed" not in title or toggle.get_attribute("aria-checked") != "false":
        problems.append(f"the checkbox is not disabled with the reason: {toggle.is_disabled()} {title!r}")
    if not page.locator(".menu button", has_text="Restart in the sandbox").is_disabled():
        problems.append("restarting into a sandbox that is not there is offered")
    close_menu(page)
    # The box shows unavailable, so it does not apply: + still opens an ordinary terminal.
    before = len(stub_requests(term, "POST", "/api/terminals"))
    page.locator(".term-tabs button[aria-label='New terminal']").click()
    page.wait_for_timeout(400)
    made = stub_requests(term, "POST", "/api/terminals")[before:]
    if len(made) != 1 or "sandbox" in made[0]:
        problems.append(f"a stale tick of an unavailable sandbox changed the new terminal: {made}")
    context.close()


def phone(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    term = TerminalStub(S1)
    term.add("t1aaaaaaaaaa", title="bash · bakery", sandbox=True)
    term.add("t2bbbbbbbbbb", title="htop")
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    context.add_init_script(DEBUG)
    page = open_session(context, term, stub, BASE, S1)
    phone_tool(page, "terminal")
    page.wait_for_selector(".term-sheet .term-sheet-row", timeout=5000)
    rows = page.locator(".term-sheet .term-sheet-row")
    marks = [rows.nth(i).locator(".term-shield").count() for i in range(rows.count())]
    print("phone shields:", marks)
    if marks != [1, 0]:
        problems.append(f"the phone's list does not mark the sandboxed terminal: {marks}")
    width = page.evaluate("document.documentElement.scrollWidth")
    if width > 390:
        problems.append(f"the phone's list scrolls sideways: {width}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        desktop(browser, problems)
        nowhere(browser, problems)
        phone(browser, problems)
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
