"""A focused terminal keeps its keys; the app takes back only the few it reserves.

Ctrl+K kills a line in a shell and Ctrl+\\ quits a program: with the terminal focused they must reach
it, and the command palette and the sidebar must stay as they are. Ctrl+` belongs to the app in any
keyboard layout — matched by the physical key, so a Russian layout's "ё" on the same key works — and
sends nothing. Ctrl+Shift+C copies the selection. A multi-line paste into a shell without bracketed
paste asks first; with bracketed paste on, it goes straight through, marked.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_keys.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, text, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ID = "keysaaaaaaaa"


def since(term: TerminalStub, mark: int) -> bytes:
    return term.inputs(ID)[mark:]


def focus(page: Page) -> None:
    page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").click()
    page.wait_for_timeout(200)


def ctrl_yo(page: Page) -> None:
    """Ctrl+` as a Russian layout sends it: the same physical key, typing "ё"."""
    page.evaluate(
        """() => {
          const field = document.querySelector('.xterm-helper-textarea');
          field.dispatchEvent(new KeyboardEvent('keydown', { key: 'ё', code: 'Backquote', ctrlKey: true, bubbles: true, cancelable: true }));
        }"""
    )


def paste(page: Page, data: str) -> None:
    page.evaluate(
        """(data) => {
          const field = document.querySelector('.xterm-helper-textarea');
          const dt = new DataTransfer();
          dt.setData('text/plain', data);
          field.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        }""",
        data,
    )


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    term.add(ID, title="keys")
    term.emit(ID, "copy me please\r\n$ ")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE.split("/app")[0])
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [ID]))
        page = open_session(context, term, stub, BASE, S1)
        wait_live(page, ID)
        page.wait_for_timeout(300)
        focus(page)

        sidebar = page.evaluate("() => document.querySelector('.sidebar')?.className ?? ''")
        mark = len(term.inputs(ID))
        page.keyboard.press("Control+k")
        page.wait_for_timeout(300)
        got = since(term, mark)
        if got != b"\x0b":
            problems.append(f"Ctrl+K sent {got!r}, not \\x0b")
        if page.locator(".palette, .palette-list").count():
            problems.append("Ctrl+K opened the palette from inside the terminal")
        mark = len(term.inputs(ID))
        page.keyboard.press("Control+Backslash")
        page.wait_for_timeout(300)
        got = since(term, mark)
        if got != b"\x1c":
            problems.append(f"Ctrl+\\ sent {got!r}, not \\x1c")
        if page.evaluate("() => document.querySelector('.sidebar')?.className ?? ''") != sidebar:
            problems.append("Ctrl+\\ toggled the sidebar from inside the terminal")
        mark = len(term.inputs(ID))
        page.keyboard.press("Control+m")
        page.wait_for_timeout(300)
        if since(term, mark) != b"\r":
            problems.append(f"Ctrl+M did not reach the terminal as Enter: {since(term, mark)!r}")
        if page.locator(".models-menu").count():
            problems.append("Ctrl+M opened the composer's model menu from inside the terminal")
        panel_before = page.locator(".panel").count()
        page.keyboard.press("Control+Period")
        page.wait_for_timeout(300)
        if page.locator(".panel").count() != panel_before:
            problems.append("Ctrl+. toggled the panel from inside the terminal")

        # Ctrl+`: the dock goes, nothing is typed; and in a Russian layout the same.
        mark = len(term.inputs(ID))
        page.keyboard.press("Control+Backquote")
        page.wait_for_timeout(300)
        if page.locator(".term-dock.open").count():
            problems.append("Ctrl+` did not hide the dock from inside the terminal")
        page.keyboard.press("Control+Backquote")
        page.wait_for_timeout(500)
        focus(page)
        ctrl_yo(page)
        page.wait_for_timeout(300)
        if page.locator(".term-dock.open").count():
            problems.append("Ctrl+ё (the same key in a Russian layout) did not hide the dock")
        if since(term, mark):
            problems.append(f"Ctrl+` typed into the terminal: {since(term, mark)!r}")
        page.keyboard.press("Control+Backquote")
        page.wait_for_timeout(500)

        # Ctrl+Shift+C copies what is selected.
        row = page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").bounding_box()
        assert row
        page.mouse.dblclick(row["x"] + 20, row["y"] + 12)
        page.wait_for_timeout(200)
        mark = len(term.inputs(ID))
        page.keyboard.press("Control+Shift+C")
        page.wait_for_timeout(300)
        copied = page.evaluate("() => navigator.clipboard.readText().catch((e) => 'ERR ' + e)")
        print("copied:", repr(copied))
        if copied not in ("copy", "copy me please"):
            problems.append(f"Ctrl+Shift+C did not copy the selection: {copied!r}")
        if since(term, mark):
            problems.append(f"Ctrl+Shift+C typed into the terminal: {since(term, mark)!r}")

        # A multi-line paste without bracketed paste asks; declined, nothing is sent.
        focus(page)
        mark = len(term.inputs(ID))
        paste(page, "echo one\necho two\n")
        page.wait_for_selector(".dialog", timeout=3000)
        title = page.locator(".dialog h3").inner_text()
        print("paste asks:", title)
        if "2" not in title:
            problems.append(f"the paste question does not count the lines: {title!r}")
        page.locator(".dialog .dialog-actions .btn").first.click()
        page.wait_for_timeout(300)
        if since(term, mark):
            problems.append(f"a declined paste was sent: {since(term, mark)!r}")
        # Accepted, it is sent as it was pasted.
        paste(page, "echo one\necho two\n")
        page.wait_for_selector(".dialog", timeout=3000)
        page.locator(".dialog .btn.primary").click()
        page.wait_for_timeout(300)
        if since(term, mark) != b"echo one\recho two\r":
            problems.append(f"an accepted paste did not arrive whole: {since(term, mark)!r}")
        # With bracketed paste on, the shell tells a paste from typing: no question, the markers sent.
        term.emit(ID, "\x1b[?2004h")
        for _ in range(20):
            if any("$" in line for line in text(page, ID)):
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(200)
        mark = len(term.inputs(ID))
        paste(page, "echo three\necho four")
        page.wait_for_timeout(400)
        if page.locator(".dialog").count():
            problems.append("a paste into a shell with bracketed paste still asked")
            page.locator(".dialog .dialog-actions .btn").first.click()
        got = since(term, mark)
        if not (got.startswith(b"\x1b[200~") and got.endswith(b"\x1b[201~")):
            problems.append(f"a bracketed paste was not marked: {got!r}")
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
