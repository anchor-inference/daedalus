"""Resizing the window while a flood is being parsed parses nothing twice.

xterm.js parses output in slices and flushes what is still queued before it resizes; that flush used to
start from chunks it had already parsed, so a resize in the middle of a flood printed them again and
ran their write callbacks again — each one an acknowledgement to the daemon. Here sixteen megabytes of
numbered lines arrive over a real socket (``terminal_flood.py``) while the window changes size every
150 ms, and the daemon's side confirms every size the page sends, so the terminal is resized both by
the page's own fit and by the confirmation. The page runs at a 4x CPU throttle, so xterm.js slices its
parse the way a phone or a loaded machine makes it. Then:

- xterm.js parsed exactly one line feed per line sent (a chunk parsed twice adds its lines again);
- every acknowledgement is at most what was sent, and none goes backwards;
- the scrollback's numbered lines go up by one, with no line twice;
- the last line shown is the last line sent, and the window was never overrun.

    APP_DIST=miniapp/dist python3 tests/browser/check_terminal_resize_flood.py

It serves the build itself (``APP_DIST``, default ``miniapp/dist``) on a free loopback port.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenshots import S1, UNHANDLED, stub  # noqa: E402
from terminal_flood import BATCH, Flood, FloodServer, numbered_lines, sequence_breaks  # noqa: E402
from terminal_stub import DEBUG, WINDOW, TerminalStub, dock_state, text, wait_live  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("APP_DIST", ROOT / "miniapp" / "dist"))
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
FLOOD = "rsflood00000"
TOTAL = 16 * 1024 * 1024
SIZES = [(1440, 900), (1180, 760), (1320, 840), (1100, 700)]
STALL_S = 60
"""Seconds without a new acknowledgement after which the flood is taken as stuck."""


def main() -> int:
    problems: list[str] = []
    data, count = numbered_lines(TOTAL)
    last = f"{count - 1:08d}"
    term = TerminalStub(S1)
    term.add(FLOOD, title="yes")
    with FloodServer(DIST) as server, sync_playwright() as p:
        flood = server.floods[FLOOD] = Flood(data=data)
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        context.add_init_script(DEBUG)
        context.add_init_script(dock_state(S1, [FLOOD], height=420))
        page = context.new_page()
        page.route("**/api/**", stub)
        page.route("**/api/terminals**", term.route)
        page.goto(f"{server.base}/agents/{S1}?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".chat-scroll .timeline", timeout=20000)
        wait_live(page, FLOOD)
        page.wait_for_timeout(300)
        page.evaluate("(id) => window.__terminals.lineFeeds(id)", FLOOD)
        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
        started = time.monotonic()
        flood.go.set()
        turn = 0
        # Given up on only when nothing is acknowledged for a whole minute. A fixed 300 s once ran out
        # at load 47 with 99.6 % acknowledged, and the unparsed tail was then reported as output
        # "parsed twice or lost": a slow machine that is still reading is not a lost stream.
        progress, progressed = flood.acked, time.monotonic()
        while flood.acked < len(data) and time.monotonic() - progressed < STALL_S:
            width, height = SIZES[turn % len(SIZES)]
            page.set_viewport_size({"width": width, "height": height})
            turn += 1
            page.wait_for_timeout(150)
            if flood.acked != progress:
                progress, progressed = flood.acked, time.monotonic()
        took = time.monotonic() - started
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(600)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
        feeds = page.evaluate("(id) => window.__terminals.lineFeeds(id)", FLOOD)
        print(f"line feeds parsed: {feeds} for {count} lines sent")
        finished = flood.acked >= len(data)
        if finished and feeds != count:
            problems.append(f"xterm.js parsed {feeds} line feeds for {count} lines: {feeds - count:+d} (output parsed twice or lost)")
        print(f"flood: {len(data)} bytes in {took:.1f} s, {turn} window sizes, {len(flood.resizes)} RESIZE frames, "
              f"{flood.acks} ACKs, most unacknowledged {flood.max_unacked}")
        if not finished:
            problems.append(f"the flood stalled: {flood.acked} of {len(data)} acknowledged, nothing more for {STALL_S} s")
        if len(flood.resizes) < 4:
            problems.append(f"only {len(flood.resizes)} RESIZE frames reached the daemon: the check did not resize under the flood")
        if flood.ack_past_sent:
            problems.append(f"{flood.ack_past_sent} ACKs acknowledged more than was sent (a chunk's callback ran twice)")
        if flood.ack_backwards:
            problems.append(f"{flood.ack_backwards} ACKs went backwards")
        if flood.max_unacked > WINDOW + BATCH:
            problems.append(f"{flood.max_unacked} bytes went unacknowledged, past the window and one batch")
        held = [x for x in text(page, FLOOD) if x.strip()]
        numbered, breaks, tail = sequence_breaks(held)
        print(f"scrollback: {numbered} numbered lines, {breaks} breaks in the numbering, last {tail:08d}")
        if breaks:
            problems.append(f"the scrollback's numbering breaks {breaks} times: output was parsed twice or lost")
        if finished and tail != count - 1:
            problems.append(f"the last line shown is {tail:08d}, not the last line sent ({last})")
        context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
