"""Copying out of a terminal reaches the operator's clipboard, on a page served over plain http too.

The app is usually opened on a LAN address over http, which is not a secure context: the browser
gives such a page no `navigator.clipboard` at all. This check opens the app twice — on the loopback
address the harness serves (a secure context) and on a made-up host name mapped to the same server
(not one) — and reads the clipboard back through a page on the loopback origin, which may.

- a selection is copied with Ctrl+Shift+C and, on a Mac, with Cmd+C; the focus stays in the
  terminal, so the keys typed next still reach it;
- plain Ctrl+C copies a selection and clears it, and with nothing selected is the interrupt;
- over a program that reports the mouse (Claude Code's full-screen view), Shift-drag selects in
  xterm.js — on a Mac too — and sends the program no mouse report; with nothing selected, Cmd+C
  and Ctrl+Shift+C go to the program, which copies its own selection;
- a copy the program asks for (OSC 52) with no key press or click to vouch for it is held: the
  toast's button and the next copy key put it on the clipboard.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_copy.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SPLIT = urlsplit(BASE)
SECURE = f"{SPLIT.scheme}://{SPLIT.netloc}"
# A name only this browser resolves, to the harness's own server: http on it is not a secure context.
INSECURE_HOST = "plain-http.test"
INSECURE = f"{SPLIT.scheme}://{INSECURE_HOST}:{SPLIT.port}"
ID = "copyaaaaaaaa"
# xterm.js and the app both ask the platform; a Mac is a Mac to each of them.
MAC = (
    "Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'MacIntel' });"
    "if ('userAgentData' in navigator) Object.defineProperty(Navigator.prototype, 'userAgentData', { get: () => ({ platform: 'macOS' }) });"
)
MOUSE_PROGRAM = "\x1b[?1000h\x1b[?1006h\x1b[>1u"


def clipboard(context: BrowserContext) -> str:
    reader = context.new_page()
    reader.goto(f"{SECURE}/clipboard.txt")
    value = reader.evaluate("() => navigator.clipboard.readText().catch((e) => 'ERR ' + e)")
    reader.close()
    return value


def set_clipboard(context: BrowserContext, value: str) -> None:
    writer = context.new_page()
    writer.goto(f"{SECURE}/clipboard.txt")
    writer.evaluate("(t) => navigator.clipboard.writeText(t)", value)
    writer.close()


def screen(page: Page) -> dict[str, float]:
    box = page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").bounding_box()
    assert box
    return box


def focused_in_terminal(page: Page) -> bool:
    return page.evaluate("() => !!document.activeElement?.closest('.xterm')")


def selection(page: Page) -> str:
    return page.evaluate(f"() => window.__terminals.selection('{ID}')")


def toast(page: Page) -> str:
    found = page.locator(".toast")
    found.wait_for(timeout=4000)
    return found.inner_text()


def wait_toast_gone(page: Page) -> None:
    page.locator(".toast").wait_for(state="detached", timeout=12000)


def program_copies(page: Page, text: str) -> None:
    """The program asks for a copy (OSC 52) with no key press or click to vouch for it.

    Everything Playwright does in a page counts as a gesture for five seconds, its routed WebSocket
    frames included, so the sequence is fed through the debug hook from a timer that fires after
    that has run out.
    """
    osc = "\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07"
    page.evaluate("([id, osc]) => { window.__fed = false; setTimeout(() => { window.__terminals.write(id, osc); window.__fed = true; }, 6000); }", [ID, osc])
    page.wait_for_timeout(6500)


class Run:
    def __init__(self, browser: Browser, problems: list[str]) -> None:
        self.browser = browser
        self.problems = problems

    def open(self, origin: str, mac: bool) -> tuple[BrowserContext, Page, TerminalStub]:
        term = TerminalStub(S1)
        term.add(ID, title="copy")
        term.emit(ID, "copy me please\r\n$ ")
        context = self.browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=SECURE)
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [ID]))
        if mac:
            context.add_init_script(MAC)
        set_clipboard(context, "before")
        page = open_session(context, term, stub, f"{origin}/app", S1)
        wait_live(page, ID)
        page.wait_for_timeout(300)
        return context, page, term

    def select_word(self, page: Page, shift: bool = False) -> None:
        box = screen(page)
        if shift:
            page.keyboard.down("Shift")
        page.mouse.dblclick(box["x"] + 20, box["y"] + 12)
        if shift:
            page.keyboard.up("Shift")
        page.wait_for_timeout(200)

    def plain_shell(self, origin: str, mac: bool) -> None:
        where = f"{'mac' if mac else 'linux'} on {'plain http' if origin == INSECURE else 'loopback'}"
        say = lambda text: self.problems.append(f"{where}: {text}")  # noqa: E731
        context, page, term = self.open(origin, mac)
        secure = page.evaluate("() => window.isSecureContext")
        if secure != (origin == SECURE):
            say(f"the page is {'' if secure else 'not '}a secure context")
        copy = "Meta+c" if mac else "Control+Shift+C"
        self.select_word(page)
        mark = len(term.inputs(ID))
        page.keyboard.press(copy)
        page.wait_for_timeout(300)
        got = clipboard(context)
        print(f"{where}: {copy} copied {got!r}")
        if got != "copy":
            say(f"{copy} did not copy the selection: {got!r}")
        if term.inputs(ID)[mark:]:
            say(f"{copy} typed into the terminal: {term.inputs(ID)[mark:]!r}")
        if not focused_in_terminal(page):
            say(f"the focus left the terminal after {copy}")
        if "Copied" not in toast(page):
            say("no toast said the selection was copied")
        # The keys typed after a copy still reach the terminal.
        page.keyboard.type("ls")
        page.wait_for_timeout(300)
        if not term.inputs(ID)[mark:].endswith(b"ls"):
            say(f"typing after a copy did not reach the terminal: {term.inputs(ID)[mark:]!r}")
        if not mac:
            # Plain Ctrl+C: copies a selection and clears it; the next one is the interrupt.
            set_clipboard(context, "before")
            self.select_word(page)
            mark = len(term.inputs(ID))
            page.keyboard.press("Control+c")
            page.wait_for_timeout(300)
            got = clipboard(context)
            if got != "copy":
                say(f"Ctrl+C over a selection did not copy it: {got!r}")
            if term.inputs(ID)[mark:]:
                say(f"Ctrl+C over a selection also interrupted: {term.inputs(ID)[mark:]!r}")
            if selection(page):
                say("Ctrl+C left the selection in place, so the next Ctrl+C would copy again")
            page.keyboard.press("Control+c")
            page.wait_for_timeout(300)
            if term.inputs(ID)[mark:] != b"\x03":
                say(f"Ctrl+C with nothing selected did not interrupt: {term.inputs(ID)[mark:]!r}")
        else:
            # Ctrl+C on a Mac is the interrupt, selection or not.
            self.select_word(page)
            mark = len(term.inputs(ID))
            page.keyboard.press("Control+c")
            page.wait_for_timeout(300)
            if term.inputs(ID)[mark:] != b"\x03":
                say(f"Ctrl+C on a Mac did not interrupt: {term.inputs(ID)[mark:]!r}")
        context.close()

    def mouse_program(self, origin: str, mac: bool) -> None:
        where = f"mouse program, {'mac' if mac else 'linux'} on {'plain http' if origin == INSECURE else 'loopback'}"
        say = lambda text: self.problems.append(f"{where}: {text}")  # noqa: E731
        context, page, term = self.open(origin, mac)
        term.emit(ID, MOUSE_PROGRAM)
        page.wait_for_timeout(300)
        copy = "Meta+c" if mac else "Control+Shift+C"
        mark = len(term.inputs(ID))
        self.select_word(page, shift=True)
        if term.inputs(ID)[mark:]:
            say(f"Shift-drag sent the program mouse reports: {term.inputs(ID)[mark:]!r}")
        page.keyboard.press(copy)
        page.wait_for_timeout(300)
        got = clipboard(context)
        print(f"{where}: Shift-select then {copy} copied {got!r}")
        if got != "copy":
            say(f"Shift-drag then {copy} did not copy: {got!r}")
        if term.inputs(ID)[mark:]:
            say(f"{copy} over xterm.js's selection reached the program: {term.inputs(ID)[mark:]!r}")
        # A plain click is the program's: it gets the report, and the selection goes.
        box = screen(page)
        page.mouse.click(box["x"] + 60, box["y"] + 40)
        page.wait_for_timeout(300)
        if b"\x1b[<0;" not in term.inputs(ID)[mark:]:
            say(f"a plain click did not reach the program: {term.inputs(ID)[mark:]!r}")
        # Nothing selected in xterm.js: the copy key is the program's, encoded as the kitty protocol has it.
        mark = len(term.inputs(ID))
        page.keyboard.press(copy)
        page.wait_for_timeout(300)
        want = b"\x1b[99;9u" if mac else b"\x1b[99;6u"
        if term.inputs(ID)[mark:] != want:
            say(f"{copy} with nothing selected did not reach the program as {want!r}: {term.inputs(ID)[mark:]!r}")
        context.close()

    def held_copy(self) -> None:
        say = lambda text: self.problems.append(f"held copy on plain http: {text}")  # noqa: E731
        context, page, term = self.open(INSECURE, True)
        term.emit(ID, MOUSE_PROGRAM)
        program_copies(page, "from the program")
        said = toast(page)
        print("held copy says:", said)
        if "Press ⌘C" not in said:
            say(f"no toast offered the held copy: {said!r}")
        if clipboard(context) != "before":
            say("the program's copy reached the clipboard with nothing to vouch for it; the check proves nothing")
        page.locator(".toast button", has_text="Copy").click()
        page.wait_for_timeout(300)
        if clipboard(context) != "from the program":
            say(f"the toast's button did not copy the held text: {clipboard(context)!r}")
        # The next one is delivered by the copy key, which the program then does not see.
        wait_toast_gone(page)
        page.evaluate("() => document.querySelector('.xterm-helper-textarea').focus()")
        program_copies(page, "second")
        toast(page)
        mark = len(term.inputs(ID))
        page.keyboard.press("Meta+c")
        page.wait_for_timeout(300)
        if clipboard(context) != "second":
            say(f"Cmd+C did not deliver the held copy: {clipboard(context)!r}")
        if term.inputs(ID)[mark:]:
            say(f"Cmd+C delivering a held copy also reached the program: {term.inputs(ID)[mark:]!r}")
        # Delivered once: the next Cmd+C is the program's again.
        mark = len(term.inputs(ID))
        page.keyboard.press("Meta+c")
        page.wait_for_timeout(300)
        if term.inputs(ID)[mark:] != b"\x1b[99;9u":
            say(f"after the held copy, Cmd+C did not go back to the program: {term.inputs(ID)[mark:]!r}")
        context.close()

    def paste(self, origin: str) -> None:
        say = lambda text: self.problems.append(f"paste on {'plain http' if origin == INSECURE else 'loopback'}: {text}")  # noqa: E731
        context, page, term = self.open(origin, False)
        set_clipboard(context, "pasted")
        page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").click()
        page.wait_for_timeout(200)
        mark = len(term.inputs(ID))
        page.keyboard.press("Control+Shift+V")
        page.wait_for_timeout(400)
        print("Ctrl+Shift+V sent:", term.inputs(ID)[mark:])
        if term.inputs(ID)[mark:] != b"pasted":
            say(f"Ctrl+Shift+V did not paste the clipboard: {term.inputs(ID)[mark:]!r}")
        context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=[f"--host-resolver-rules=MAP {INSECURE_HOST} {SPLIT.hostname}"])
        run = Run(browser, problems)
        for origin in (INSECURE, SECURE):
            for mac in (False, True):
                run.plain_shell(origin, mac)
                run.mouse_program(origin, mac)
        run.held_copy()
        run.paste(INSECURE)
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
