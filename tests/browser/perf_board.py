"""Measure the Board under a stubbed API: what a board of 300 cards costs to open, with its finished
cards shown, and with a card's pane open.

The board of a real installation holds a few hundred cards, most of them finished, and the page reads
them all at once (the filter bar counts the finished ones). A long column draws its first sixty cards
and offers the rest, which is the claim this measures rather than asserts. Run against the app being
served, as the checks are:

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/perf_board.py              # 300 cards, 4x throttle
    APP_URL=... python3 tests/browser/perf_board.py --cards 300 --rate 4 --json out.json

Reported per case: time to the first card, DOM nodes, cards in the DOM, the long tasks and the time
they blocked, and the worst one. The machine is shared: the DOM and the long-task count are the
numbers that do not depend on its load.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECTS = [{"id": f"p{i}", "name": name, "folders": [], "created_at": "2026-11-01T00:00:00Z", "settings": {"snapshots": True}, "system": "", "sessions": []}
            for i, name in enumerate(("Bakery site", "Support", "Expenses", "Courier rates"))]
CHATS = [{"id": f"s{i}", "title": f"Chat {i}", "status": "running" if i % 3 == 0 else "idle", "created_at": "2026-11-01T00:00:00Z",
          "last_message_at": f"2026-11-2{i % 5}T10:00:00Z", "run_id": None, "project_id": f"p{i % 4}" if i % 5 else "", "project": ""} for i in range(10)]
# Where the cards stand, as on a board most of whose work is done.
SPREAD = (("todo", 40), ("doing", 15), ("review", 10), ("blocked", 15), ("done", 200), ("dropped", 20))

# Installed before the app's first script, so opening the page is counted from its first long task.
PROBE = """
window.__perf = { tasks: 0, blocked: 0, worst: 0 };
new PerformanceObserver((list) => {
  for (const e of list.getEntries()) {
    window.__perf.tasks++;
    window.__perf.blocked += e.duration;
    window.__perf.worst = Math.max(window.__perf.worst, e.duration);
  }
}).observe({ type: "longtask", buffered: true });
"""
RESET = "() => { window.__perf = { tasks: 0, blocked: 0, worst: 0 }; }"
READ = """
() => ({
  tasks: window.__perf.tasks, blocked: Math.round(window.__perf.blocked), worst: Math.round(window.__perf.worst),
  nodes: document.getElementsByTagName("*").length, cards: document.querySelectorAll(".task-card").length,
})
"""


def board(count: int) -> BoardStub:
    tasks = []
    n = 0
    scale = count / sum(k for _, k in SPREAD)
    for status, share in SPREAD:
        for _ in range(round(share * scale)):
            chat = CHATS[n % len(CHATS)]
            tasks.append(BoardStub.task(f"t{n:04d}", f"Card {n}: a task with a title long enough to wrap", status=status, priority=1 + n % 5,
                                        origin_session_id=chat["id"], session_id=chat["id"], project_id=chat["project_id"] or None,
                                        checklist=[{"text": "one", "done": True}, {"text": "two", "done": n % 2 == 0}] if n % 3 == 0 else [],
                                        due_at="2026-12-01T23:59:59Z" if n % 4 == 0 else None))
            n += 1
    stub = BoardStub(PROJECTS[0], tasks=tasks)
    names = {p["id"]: p["name"] for p in PROJECTS}
    for task in stub.tasks:
        task["project_name"] = names.get(task["project_id"] or "")
    return stub


def run_case(browser, case: str, stub: BoardStub, unhandled: Unhandled, rate: float, width: int, height: int) -> dict:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        answered = stub.answer(request.method, path, url.query, None)
        if answered is not None:
            return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        if path == "/api/projects":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(PROJECTS))
        if path == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": CHATS, "projects": []}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", handle)
    page.add_init_script(PROBE)
    cdp = context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})
    t0 = time.time()
    page.goto(f"{BASE}/board?token=t&lang=en", wait_until="commit")
    page.wait_for_selector(".task-card", timeout=120_000)
    open_ms = round((time.time() - t0) * 1000)
    time.sleep(2)
    # The other cases count only what their own action costs.
    if case == "finished":
        page.evaluate(RESET)
        page.locator(".board-filter[data-filter='done']").click()
        page.wait_for_selector(".kanban-col[data-column='finished'] .task-card", timeout=60_000)
        time.sleep(2)
    elif case == "pane":
        page.evaluate(RESET)
        page.locator(".task-card").first.click()
        page.wait_for_selector(".task-pane .task-log, .task-pane .task-none", timeout=60_000)
        time.sleep(2)
    out = page.evaluate(READ)
    context.close()
    return {"case": case, "cards": len(stub.tasks), "throttle": rate, "open_ms": open_ms, "dom_nodes": out["nodes"], "cards_in_dom": out["cards"],
            "long_tasks": out["tasks"], "blocked_ms": out["blocked"], "worst_task_ms": out["worst"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=int, default=300)
    parser.add_argument("--rate", type=float, default=4.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    expect_app(BASE)
    unhandled = Unhandled()
    rows = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for case in ("open", "finished", "pane"):
            rows.append(run_case(browser, case, board(args.cards), unhandled, args.rate, args.width, args.height))
        browser.close()
    keys = ("case", "cards", "open_ms", "dom_nodes", "cards_in_dom", "long_tasks", "blocked_ms", "worst_task_ms")
    width = max(len(k) for k in keys)
    for key in keys:
        print(key.rjust(width), " ".join(str(r[key]).rjust(10) for r in rows))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return unhandled.report()


if __name__ == "__main__":
    sys.exit(main())
