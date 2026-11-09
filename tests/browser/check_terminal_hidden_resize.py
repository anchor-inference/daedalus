"""The webterm failure, reproduced on purpose: hidden terminals must never size the PTY.

The old web terminal fitted every pane, hidden ones included; a pane in a background tab measured a
few pixels, became 9×5, and the shell every view shared was squeezed to nine columns. Here a dock with
three tabs (two hidden) goes through twenty window resizes, a collapse and an expand, tab switches, a
maximise and back, and every RESIZE the page sends is attributed to the terminal whose socket sent it.

Refused: a RESIZE from a terminal that was not on screen at the time; any RESIZE below 20×4; a tab
shown for the first time that sends anything but exactly one; any RESIZE while the dock is collapsed.
A tab shown again at the size it already sent sends nothing, which is the rule working, not failing.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_hidden_resize.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, open_session, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
IDS = ["h1aaaaaaaaaa", "h2bbbbbbbbbb", "h3cccccccccc"]


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    term = TerminalStub(S1)
    for i, id_ in enumerate(IDS):
        term.add(id_, title=f"shell {i + 1}")
        term.emit(id_, f"terminal {i + 1}\r\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, IDS))
        page = open_session(context, term, stub, BASE, S1)
        for id_ in IDS:
            wait_live(page, id_)
        page.wait_for_timeout(600)

        seen = 0

        def since(label: str) -> list[tuple[str, int, int, int]]:
            nonlocal seen
            every = term.resizes()
            new = every[seen:]
            seen = len(every)
            print(f"{label}: {[(i[:2], c, r) for i, _, c, r in new]}")
            return new

        def only(label: str, new: list[tuple[str, int, int, int]], allowed: set[str]) -> None:
            strangers = [r for r in new if r[0] not in allowed]
            if strangers:
                problems.append(f"{label}: a terminal that was not on screen sent a size: {strangers}")

        first = since("open")
        only("open", first, {IDS[0]})
        if len([r for r in first if r[0] == IDS[0]]) != 1:
            problems.append(f"the visible terminal sent {len(first)} sizes on open, not one")

        # Twenty resizes of the window: only the visible terminal speaks, never below 20×4.
        for i in range(20):
            page.set_viewport_size({"width": 1100 + (i % 5) * 70, "height": 700 + (i % 4) * 60})
            page.wait_for_timeout(160)
        page.wait_for_timeout(400)
        only("window resizes", since("window resizes"), {IDS[0]})

        # Collapsed: nobody is on screen, so nobody speaks, whatever the window does.
        page.locator(".term-tools button[aria-label='Hide the terminal panel']").click()
        page.wait_for_timeout(200)
        since("collapse")
        for i in range(5):
            page.set_viewport_size({"width": 1000 + i * 90, "height": 760 + i * 20})
            page.wait_for_timeout(160)
        page.wait_for_timeout(300)
        collapsed = since("while collapsed")
        if collapsed:
            problems.append(f"sizes were sent while the dock was collapsed: {collapsed}")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(300)
        page.keyboard.press("Control+Backquote")
        page.wait_for_timeout(600)
        only("expand", since("expand"), {IDS[0]})

        # Each tab shown for the first time sends exactly one size, from its own socket.
        for id_ in IDS[1:]:
            page.locator(f".term-tab[data-tab='{id_}']").click()
            page.wait_for_timeout(700)
            new = since(f"switch to {id_[:2]}")
            only(f"switch to {id_[:2]}", new, {id_})
            if len(new) != 1:
                problems.append(f"showing {id_[:2]} for the first time sent {len(new)} sizes, not one")
        # Back to the first: shown again, at most one, and only from it.
        page.locator(f".term-tab[data-tab='{IDS[0]}']").click()
        page.wait_for_timeout(700)
        back = since("switch back")
        only("switch back", back, {IDS[0]})
        if len(back) > 1:
            problems.append(f"showing the first tab again sent {len(back)} sizes")

        # Maximise and back: the one terminal, at its full-window size and then the dock's.
        page.locator(".term-tools button[aria-label='Full screen']").click()
        page.wait_for_timeout(700)
        page.locator(".term-full-head button[aria-label='Back to the dock']").click()
        page.wait_for_timeout(700)
        only("maximise and back", since("maximise and back"), {IDS[0]})

        # The drag of the dock's edge: the visible terminal follows it in rows alone.
        grip = page.locator(".term-grip").bounding_box()
        assert grip
        page.mouse.move(grip["x"] + 200, grip["y"] + 4)
        page.mouse.down()
        for dy in range(0, 200, 20):
            page.mouse.move(grip["x"] + 200, grip["y"] + 4 - dy)
            page.wait_for_timeout(30)
        page.mouse.up()
        page.wait_for_timeout(500)
        only("drag", since("drag"), {IDS[0]})

        every = term.resizes()
        small = [r for r in every if r[2] < 20 or r[3] < 4]
        if small:
            problems.append(f"sizes below 20×4 were sent: {small}")
        print("every size:", [(i[:2], c, r) for i, _, c, r in every])
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
