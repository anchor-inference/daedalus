"""A shell's commands are marked in its terminal, and the marks are good for something.

The stub plays a shell with integration: each prompt and command is marked in the stream and reported
as the daemon reports it, one `command` event per mark and a `marks` list after every snapshot. The
check asserts:

- **Snapshot.** Commands that ran before the page attached come back from the `marks` after the
  snapshot, each on its prompt's line, with a dot in the colour of how it ended (ok, failed).
- **Live.** A command that fails while the page watches gets a red dot and turns the tab's dot red;
  one that runs shows the accent until it ends. One of them is reported *ahead* of its bytes, as
  the daemon does when output is still batched, and still lands on its prompt's line.
- **Jumps.** Ctrl+↑ scrolls to the last command's prompt, again to the one before; Ctrl+↓ comes
  back; neither is typed into the shell.
- **Copy.** The toolbar's "copy last command output" and the terminal's own menu put exactly the
  last finished command's output on the clipboard; when that output has left the terminal's history,
  the text comes from the host (`GET …/commands?last=5&output=1`).
- **Reattach.** After a snapshot reattach the marks are where they were; after a tail reattach, a
  command that ran while the page was away gets its mark from the host.
- **Phone.** The full-screen terminal on a 390 px phone offers the copy too.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_marks.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, stub_requests, text, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ID = "marksaaaaaaa"


def wait_for(page: Page, predicate, timeout_ms: int = 10000) -> bool:  # type: ignore[no-untyped-def]
    for _ in range(timeout_ms // 100):
        if predicate():
            return True
        page.wait_for_timeout(100)
    return predicate()


def marks(page: Page) -> list[dict[str, Any]]:
    return page.evaluate("(id) => window.__terminals ? window.__terminals.marks(id) : []", ID)


def viewport_y(page: Page) -> int:
    return int(page.evaluate("(id) => window.__terminals.viewportY(id)", ID))


def base_y(page: Page) -> int:
    return int(page.evaluate("(id) => window.__terminals.baseY(id)", ID))


def line_of(page: Page, n: int) -> int:
    for m in marks(page):
        if m["n"] == n:
            return int(m["line"])
    return -2


def result_of(page: Page, n: int) -> str:
    for m in marks(page):
        if m["n"] == n:
            return str(m["result"])
    return ""


def clipboard(page: Page) -> str:
    return str(page.evaluate("() => navigator.clipboard.readText().catch((e) => 'ERR ' + e)"))


def clear_clipboard(page: Page) -> None:
    page.evaluate("() => navigator.clipboard.writeText('')")


def focus(page: Page) -> None:
    page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").click()
    page.wait_for_timeout(200)


def tab_dot(page: Page) -> str:
    return page.locator(f".term-tab[data-tab='{ID}'] .term-dot").get_attribute("class") or ""


def dot_colours(page: Page) -> dict[str, str]:
    """The colour each visible mark's dot is painted, by result, against the tokens it should use."""
    return page.evaluate(
        """() => {
          const probe = (token) => {
            const el = document.createElement('div');
            el.style.background = `var(${token})`;
            document.body.appendChild(el);
            const colour = getComputedStyle(el).backgroundColor;
            el.remove();
            return colour;
          };
          const out = { tokens: { ok: probe('--ok'), failed: probe('--bad'), running: probe('--accent') } };
          for (const el of document.querySelectorAll('.term-mark')) out[el.dataset.result] = getComputedStyle(el, '::before').backgroundColor;
          return out;
        }"""
    )


def check_colours(page: Page, results: list[str], problems: list[str], when: str) -> None:
    # A decoration is drawn on the frame after its mark is placed.
    wait_for(page, lambda: all(page.locator(f".term-mark[data-result='{r}']").count() for r in results), 3000)
    colours = dot_colours(page)
    tokens = colours.get("tokens", {})
    for result in results:
        if result not in colours:
            problems.append(f"{when}: no {result} dot is drawn")
        elif colours[result] != tokens.get(result):
            problems.append(f"{when}: the {result} dot is {colours[result]}, not {tokens.get(result)}")


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    term.add(ID, title="bash", rows=24)
    # Before the page attaches: two commands, which reach it through the snapshot's marks.
    term.shell_prompt(ID)
    term.shell_command(ID, "make build", ["compiling", "built dist/app"], 0)
    term.shell_prompt(ID)
    term.shell_command(ID, "grep TODO src", [], 1)
    term.shell_prompt(ID)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE.split("/app")[0])
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [ID], height=420))
        errors: list[str] = []
        context.on("page", lambda pg: pg.on("pageerror", lambda e: errors.append(str(e))))
        page = open_session(context, term, stub, BASE, S1)
        wait_live(page, ID)

        # Snapshot: both commands on their prompt lines, with their colours.
        if not wait_for(page, lambda: len(marks(page)) == 2):
            problems.append(f"snapshot: the marks never came: {marks(page)}")
        lines = text(page, ID)
        for n, prompt, result in ((1, "$ make build", "ok"), (2, "$ grep TODO src", "failed")):
            at = line_of(page, n)
            if not (0 <= at < len(lines) and lines[at] == prompt):
                problems.append(f"snapshot: command {n} is marked on line {at} ({lines[at] if 0 <= at < len(lines) else '-'!r}), not on {prompt!r}")
            if result_of(page, n) != result:
                problems.append(f"snapshot: command {n} is {result_of(page, n)!r}, not {result}")
        check_colours(page, ["ok", "failed"], problems, "snapshot")
        if "cmd-failed" not in tab_dot(page):
            problems.append(f"snapshot: the tab's dot does not say the last command failed: {tab_dot(page)!r}")

        # Live: a running command shows the accent, and its end (sent ahead of its bytes) the result.
        term.shell_command(ID, "npm test", ["PASS unit"], finish=False)
        if not wait_for(page, lambda: result_of(page, 3) == "running"):
            problems.append(f"live: the running command has no running mark: {marks(page)}")
        check_colours(page, ["running"], problems, "live")
        if not wait_for(page, lambda: "cmd-running" in tab_dot(page), 3000):
            problems.append(f"live: the tab's dot does not show a command running: {tab_dot(page)!r}")
        rec = term.terms[ID].commands[-1]
        term.print_lines(ID, rec, ["FAIL e2e", "2 failed"])
        term.finish_command(ID, 1, early=True)
        term.shell_prompt(ID, early=True)
        if not wait_for(page, lambda: result_of(page, 3) == "failed"):
            problems.append(f"live: the failed command is not marked failed: {marks(page)}")
        lines = text(page, ID)
        at = line_of(page, 3)
        if not (0 <= at < len(lines) and lines[at] == "$ npm test"):
            problems.append(f"live: the command is marked on line {at} ({lines[at] if 0 <= at < len(lines) else '-'!r}), not on its prompt")
        term.shell_command(ID, "git status", ["nothing to commit"], 0, early=True)
        term.shell_prompt(ID)
        if not wait_for(page, lambda: result_of(page, 4) == "ok"):
            problems.append(f"live: the command reported ahead of its bytes is not marked: {marks(page)}")
        lines = text(page, ID)
        at = line_of(page, 4)
        if not (0 <= at < len(lines) and lines[at] == "$ git status"):
            problems.append(f"live: the command reported ahead of its bytes is on line {at} ({lines[at] if 0 <= at < len(lines) else '-'!r})")
        if not wait_for(page, lambda: "cmd-ok" in tab_dot(page), 3000):
            problems.append(f"live: the tab's dot does not show the last command's success: {tab_dot(page)!r}")

        # Enough output to scroll, then the jumps.
        term.shell_command(ID, "seq 60", [str(i) for i in range(1, 61)], 0)
        term.shell_prompt(ID)
        wait_for(page, lambda: result_of(page, 5) == "ok")
        page.wait_for_timeout(300)
        focus(page)
        typed = len(term.inputs(ID))
        page.keyboard.press("Control+ArrowUp")
        page.wait_for_timeout(300)
        seq_line = line_of(page, 5)
        if viewport_y(page) != min(seq_line, base_y(page)):
            problems.append(f"jump: Ctrl+↑ scrolled to {viewport_y(page)}, not to the last command's prompt at {seq_line}")
        page.keyboard.press("Control+ArrowUp")
        page.wait_for_timeout(300)
        status_line = line_of(page, 4)
        if viewport_y(page) != status_line:
            problems.append(f"jump: a second Ctrl+↑ scrolled to {viewport_y(page)}, not to the prompt before at {status_line}")
        if not page.locator(".term-mark.current").count():
            problems.append("jump: the prompt jumped to is not marked as the current one")
        page.keyboard.press("Control+ArrowDown")
        page.wait_for_timeout(300)
        if viewport_y(page) != min(seq_line, base_y(page)):
            problems.append(f"jump: Ctrl+↓ scrolled to {viewport_y(page)}, not back to {seq_line}")
        page.keyboard.press("Control+ArrowDown")
        page.wait_for_timeout(300)
        if viewport_y(page) != base_y(page):
            problems.append(f"jump: Ctrl+↓ past the last prompt did not return to the bottom ({viewport_y(page)} of {base_y(page)})")
        if term.inputs(ID)[typed:]:
            problems.append(f"jump: the keys were typed into the shell: {term.inputs(ID)[typed:]!r}")

        # Copy from the toolbar: the last finished command's output, exactly.
        clear_clipboard(page)
        page.locator(".term-tools .term-copy-output").click()
        expected = "\n".join(str(i) for i in range(1, 61))
        if not wait_for(page, lambda: clipboard(page) == expected, 3000):
            problems.append(f"copy: the toolbar copied {clipboard(page)[:60]!r}…, not the output of seq 60")
        if stub_requests(term, "GET", "/commands"):
            problems.append("copy: the output was in the terminal, yet the host was asked")
        # And from the terminal's own menu, after another command.
        term.shell_command(ID, "echo done", ["done"], 0)
        term.shell_prompt(ID)
        wait_for(page, lambda: result_of(page, 6) == "ok")
        clear_clipboard(page)
        box = page.locator(f".term-view[data-terminal-view='{ID}'] .term-screen").bounding_box()
        assert box
        page.mouse.click(box["x"] + 80, box["y"] + 60, button="right")
        menu = page.locator(".term-menu")
        if not wait_for(page, lambda: menu.count() == 1, 3000):
            problems.append("menu: a right click opened no terminal menu")
        else:
            labels = [b.inner_text() for b in menu.locator("button").all()]
            print("menu:", labels)
            menu.locator("button", has_text="Copy last command output").click()
            if not wait_for(page, lambda: clipboard(page) == "done", 3000):
                problems.append(f"menu: copy last output put {clipboard(page)!r} on the clipboard")

        # Snapshot reattach: another screen resized the PTY while this one was away.
        term.drop(ID)
        term.resize_by_other(ID, 80, 24)
        before = len([c for c in term.of(ID)])
        if not wait_for(page, lambda: len(term.of(ID)) > before and bool(marks(page)), 15000):
            problems.append("reattach: the page never came back")
        wait_live(page, ID)
        page.wait_for_timeout(500)
        lines = text(page, ID)
        for n, prompt in ((4, "$ git status"), (5, "$ seq 60"), (6, "$ echo done")):
            at = line_of(page, n)
            if not (0 <= at < len(lines) and lines[at] == prompt):
                problems.append(f"snapshot reattach: command {n} is on line {at} ({lines[at] if 0 <= at < len(lines) else '-'!r}), not on {prompt!r}")

        # Tail reattach: a command that runs while the page is away is filled in from the host.
        term.drop(ID)
        page.wait_for_timeout(100)
        term.shell_command(ID, "ls -la", ["total 0"], 2)
        term.shell_prompt(ID)
        if not wait_for(page, lambda: result_of(page, 7) == "failed", 15000):
            problems.append(f"tail reattach: the command that ran while away has no mark: {marks(page)}")
        else:
            lines = text(page, ID)
            at = line_of(page, 7)
            if not (0 <= at < len(lines) and lines[at] == "$ ls -la"):
                problems.append(f"tail reattach: the filled-in command is on line {at} ({lines[at] if 0 <= at < len(lines) else '-'!r})")
        attaches = [f["json"] for c in term.of(ID) for f in c.of("attach")]
        if not attaches or not attaches[-1].get("haveState"):
            problems.append(f"tail reattach: the last attach did not ask for a tail: {attaches[-1] if attaches else None}")

        # Output longer than the terminal's history: the copy comes from the host.
        term.shell_command(ID, "cat huge.log", [f"row {i:05d}" for i in range(10_500)], 0)
        term.shell_prompt(ID)
        if not wait_for(page, lambda: result_of(page, 8) == "ok", 20000):
            problems.append("fallback: the long command never ended in the page")
        clear_clipboard(page)
        page.locator(".term-tools .term-copy-output").click()
        if not wait_for(page, lambda: clipboard(page).endswith("row 10499"), 5000):
            problems.append(f"fallback: the copy put {clipboard(page)[-40:]!r} on the clipboard")
        asked = [path for method, path, _ in term.requests if method == "GET" and "/commands" in path]
        if not any(ID in path for path in asked):
            problems.append(f"fallback: the host was never asked for the output ({asked})")
        problems.extend(f"the page threw: {e}" for e in errors)
        context.close()

        # Phone: the full-screen terminal's bar has the jumps between commands, and its menu the copy
        # of the last output (the bar holds at most the two jumps and the menu).
        phone = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
        phone.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE.split("/app")[0])
        phone.add_init_script(DEBUG)
        page = open_session(phone, term, stub, BASE, S1)
        page.locator(".term-button").first.click()
        page.locator(".term-sheet-row").first.click()
        page.wait_for_selector(".term-full", timeout=10000)
        if not wait_for(page, lambda: page.locator(".term-full .term-phone-jump").count() == 2, 10000):
            problems.append("phone: the full-screen terminal's bar has no jumps between commands")
        else:
            for box in (b.bounding_box() for b in page.locator(".term-full .term-phone-jump").all()):
                if not box or box["x"] + box["width"] > 390:
                    problems.append(f"phone: a jump is off the screen ({box})")
            page.locator(".term-phone-head button[aria-haspopup]").last.click()
            item = page.get_by_role("menuitem", name="Copy last command output")
            if not item.count():
                problems.append("phone: the terminal's menu has no copy of the last output")
        phone.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
