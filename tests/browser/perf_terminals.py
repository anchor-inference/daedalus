"""Measure many terminals at once: the Terminals page with forty rows, and a dock with eight busy tabs.

Two scenes, each held for a while with the page's own probes running:

1. The Terminals screen listing forty terminals with previews, polling as it does. Cards hold no
   sockets; what this measures is the list and its poll.
2. A session's dock with eight tabs, one of them shown, each terminal receiving 50 KB/s over a real
   loopback socket (``terminal_flood.py``). The window is resized a few times meanwhile.

Reported: long tasks (count and worst), DOM nodes, JS heap, frames per second, and for the dock the
WebGL contexts the page holds (the app's own count, the most at once while every tab is shown in
turn, and every context the page ever asked the canvas for) and the RESIZE frames each terminal sent
while only the first tab was on screen. The claims checked: at most 6 WebGL contexts, and no
RESIZE from a tab that is not on screen. The load average is printed with the numbers; above about 8
the long-task and DOM counts are the evidence that does not depend on the machine.

    cd miniapp && npm run build && cd ..
    python3 tests/browser/perf_terminals.py [--seconds 12] [--json out.json]

Exit 1 when a claim fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenshots import S1, stub  # noqa: E402
from terminal_flood import PROBE, Flood, FloodServer, numbered_lines, web_gl_counter  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, dock_state, run, wait_live  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("APP_DIST", ROOT / "miniapp" / "dist"))
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WEBGL_LIMIT = 6
TABS = 8
RATE = 50_000


def load() -> str:
    return " ".join(Path("/proc/loadavg").read_text().split()[:3])


def page_numbers(page, cdp, seconds: float) -> dict:  # type: ignore[no-untyped-def]
    perf = page.evaluate("() => ({ frames: window.__perf.frames, longTasks: window.__perf.longTasks })")
    counters = cdp.send("Memory.getDOMCounters")
    heap = cdp.send("Runtime.getHeapUsage")
    tasks = perf["longTasks"]
    return {
        "fps": round(perf["frames"] / seconds, 1),
        "long_tasks": len(tasks),
        "worst_long_task_ms": round(max(tasks), 1) if tasks else 0,
        "dom_nodes": counters["nodes"],
        "js_heap_mb": round(heap["usedSize"] / 1e6, 1),
    }


def many_rows(browser, server: FloodServer, seconds: float) -> dict:  # type: ignore[no-untyped-def]
    term = TerminalStub(S1)
    for i in range(40):
        term.add(f"card{i:08d}", title=f"bash · job {i}", owner_kind="session", owner_label=f"Session {i}", clients=0,
                 preview=[[run(f"line {r} of job {i}", (r % 6) + 1)] for r in range(5)])
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(PROBE)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.route("**/api/terminals**", term.route)
    page.goto(f"{server.base}/terminals?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".tp-list .tp-row", timeout=20000)
    page.wait_for_timeout(500)
    cdp = context.new_cdp_session(page)
    polls_before = sum(1 for m, p, _ in term.requests if m == "GET" and p == "/api/terminals")
    page.evaluate("() => { window.__perf.longTasks = []; window.__perf.frames = 0; }")
    load_before = load()
    page.wait_for_timeout(seconds * 1000)
    result = page_numbers(page, cdp, seconds)
    polls = sum(1 for m, p, _ in term.requests if m == "GET" and p == "/api/terminals") - polls_before
    cards = page.locator(".tp-list .tp-row").count()
    context.close()
    return {"scene": "terminals screen", "cards": cards, "list_polls": polls, "seconds": seconds, **result, "load_before": load_before, "load_after": load()}


def busy_dock(browser, server: FloodServer, seconds: float) -> dict:  # type: ignore[no-untyped-def]
    ids = [f"tab{i:09d}" for i in range(TABS)]
    term = TerminalStub(S1)
    data, _ = numbered_lines(int(RATE * (seconds + 30)))
    for id_ in ids:
        term.add(id_, title=id_)
        server.floods[id_] = Flood(data=data, rate=RATE)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script(DEBUG)
    context.add_init_script(PROBE)
    context.add_init_script(web_gl_counter())
    context.add_init_script(dock_state(S1, ids, active=ids[0], height=420))
    page = context.new_page()
    page.route("**/api/**", stub)
    page.route("**/api/terminals**", term.route)
    page.goto(f"{server.base}/agents/{S1}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".chat-scroll .timeline", timeout=20000)
    wait_live(page, ids[0])
    page.wait_for_timeout(500)
    for id_ in ids:
        server.floods[id_].go.set()
    cdp = context.new_cdp_session(page)
    page.evaluate("() => { window.__perf.longTasks = []; window.__perf.frames = 0; }")
    load_before = load()
    began = time.monotonic()
    sizes = [(1440, 900), (1280, 820), (1440, 900), (1200, 780)]
    for i in range(int(seconds)):
        if i % 3 == 0:
            width, height = sizes[(i // 3) % len(sizes)]
            page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(1000)
    result = page_numbers(page, cdp, time.monotonic() - began)
    # Taken before any other tab is shown: until then every tab but the first is hidden.
    resizes = {id_: len(server.floods[id_].resizes) for id_ in ids}
    received = {id_: server.floods[id_].acked for id_ in ids}
    attached = [id_ for id_ in ids if server.floods[id_].attached_at is not None]
    # Every tab shown once, then a split: each visible terminal asks for WebGL in turn, and the app
    # must hand it to at most six however many have been shown.
    most_webgl = 0
    for id_ in [*ids[1:], ids[0]]:
        page.locator(f".term-tab[data-tab='{id_}']").click()
        page.wait_for_timeout(250)
        most_webgl = max(most_webgl, page.evaluate("() => window.__terminals.webglContexts()"))
    webgl_app = page.evaluate("() => window.__terminals.webglContexts()")
    webgl_created = page.evaluate("() => window.__webgl.created")
    live_ids = page.evaluate("() => window.__terminals.ids()")
    context.close()
    return {
        "scene": "dock, 8 busy tabs", "seconds": seconds, **result,
        "webgl_contexts_app": webgl_app, "webgl_contexts_most": most_webgl, "webgl_contexts_created": webgl_created,
        "instances": len(live_ids), "attached": len(attached), "bytes_acknowledged": received,
        "resizes": resizes, "load_before": load_before, "load_after": load(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--json")
    args = parser.parse_args()
    problems: list[str] = []
    with FloodServer(DIST) as server, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
        cards = many_rows(browser, server, args.seconds)
        print(json.dumps(cards))
        if cards["cards"] != 40:
            problems.append(f"the Terminals page shows {cards['cards']} rows, not 40")
        dock = busy_dock(browser, server, args.seconds)
        print(json.dumps(dock))
        browser.close()
    if dock["webgl_contexts_most"] > WEBGL_LIMIT:
        problems.append(f"the app held {dock['webgl_contexts_most']} WebGL contexts at once (limit {WEBGL_LIMIT})")
    hidden = {id_: n for id_, n in dock["resizes"].items() if id_ != "tab000000000" and n}
    if hidden:
        problems.append(f"tabs not on screen sent RESIZE: {hidden}")
    if args.json:
        Path(args.json).write_text(json.dumps([cards, dock], indent=1))
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
