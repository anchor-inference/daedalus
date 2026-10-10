"""Measure the Agents list under a stubbed API: what 300 agents cost to open, collapsed and expanded.

The screen is folders now, and a folder that is closed is not in the DOM — which is the claim this
measures rather than asserts. Two cases over the same invented installation: the folders as the
screen opens them (all closed, which is the default) and every folder expanded, which is the worst
case a reader can ask for.

    cd miniapp && npm run build
    python3 tests/browser/perf_sessions.py                      # 300 agents, 4x throttle, both cases
    python3 tests/browser/perf_sessions.py --agents 300 --seconds 8 --rate 4 --json out.json

Reported per case: time to the first folder on screen, DOM nodes, frames per second while the list
polls, the long tasks and the worst frame, and what the list pulled from the API during the window.
A run whose stub was asked for a path it does not know fails, the way every harness here does.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import EVENTS, GATES, Unhandled, event_stream_hello, serve_shared_post  # noqa: E402
from api_stub import folders as folder_rows  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "miniapp" / "dist"
NOW = datetime.now(UTC)

MODELS = ("Claude Opus 5", "GPT-5.6 Luna", "DeepSeek Flash", "Local Qwen3.8")
TITLES = ("Bakery site", "Support inbox", "Weekly digest", "Expense tracker", "Courier quotes", "Invoice run", "Menu page", "Photo crop")
LOOP = {"mode": "interval", "interval_seconds": 5400, "status": "active", "run_count": 14, "max_runs": None, "next_run_at": None, "last_run_at": None, "last_reason": None, "stop_reason": None, "pause_note": None, "instruction": "Read the inbox."}


def ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def installation(agents: int, folders: int) -> dict:
    """``folders`` projects, with every agent in one and a fifth using private children.

    Every eleventh agent is a subagent of the one before it and every seventeenth a fork of it, so
    the nesting the screen does is part of what is being measured and not a case it never meets.
    """
    projects = [
        {
            "id": f"p{n:02d}",
            "name": f"Project {n:02d}",
            "folders": folder_rows(f"/home/operator/work/project-{n:02d}"),
            "created_at": ago(86_400 * 30),
            "settings": {"snapshots": False, "system": "voice" if n == 0 else ""},
            "system": "voice" if n == 0 else "",
            "total": 0,
            "active": 0,
            "loops": 0,
            "last_message_at": ago(60 * n + 30),
        }
        for n in range(folders)
    ]
    sessions = []
    for i in range(agents):
        sid = f"{i:012x}"
        own = i % 5 == 0
        project_row = projects[i % folders]
        project = project_row["id"]
        meta: dict = {}
        if i % 11 == 10 and i:
            meta = {"subagent_of": f"{i - 1:012x}", "subagent_name": f"helper {i}"}
        elif i % 17 == 16 and i:
            meta = {"forked_from": {"session_id": f"{i - 1:012x}", "seq": 100 + i}}
        elif i % 13 == 0:
            meta = {"loop": LOOP}
        status = "running" if i % 9 == 0 else "waiting" if i % 23 == 0 else "idle"
        sessions.append(
            {
                "id": sid,
                "title": f"{TITLES[i % len(TITLES)]} {i}",
                "status": status,
                "created_at": ago(86_400),
                "last_message_at": ago(60 * (i % 600) + 20),
                "run_id": "r1" if status == "running" else None,
                "model": MODELS[i % len(MODELS)],
                "workspace": sid,
                "workspace_path": f"{project_row['folders'][0]['path']}/.agents/{sid}" if own else project_row["folders"][0]["path"],
                "workspace_own": own,
                "metadata": meta,
                "project_id": project,
                "project": project_row["name"],
            }
        )
    for p in projects:
        mine = [s for s in sessions if s["project_id"] == p["id"]]
        p["total"] = len(mine)
        p["active"] = sum(1 for s in mine if s["status"] in ("running", "waiting"))
        p["loops"] = sum(1 for s in mine if s["metadata"].get("loop"))
    return {"sessions": sessions, "projects": projects}


class Stub(BaseHTTPRequestHandler):
    listing: dict = {}
    served: list[tuple[str, int]] = []
    unhandled = Unhandled()
    lock = threading.Lock()

    def log_message(self, *args: object) -> None:  # quiet
        pass

    def _send(self, body: bytes, kind: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _payload(self, path: str) -> object:
        if path == "/api/sessions":
            return Stub.listing
        if path == "/api/projects":
            return [{**p, "sessions": []} for p in Stub.listing["projects"]]
        if path == "/api/settings":
            return {"presets": {}, "model": {}}
        if path == "/api/imports/harnesses":
            # No other program's sessions on this machine: the start screen draws no import card.
            return {"harnesses": []}
        if path in GATES:
            return GATES[path]
        Stub.unhandled.record(path)
        return {}

    def do_POST(self) -> None:  # noqa: N802
        serve_shared_post(self, Stub.unhandled)

    def do_GET(self) -> None:  # noqa: N802
        raw, _, _qs = self.path.partition("?")
        if raw.endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            time.sleep(600)
            return
        if raw == EVENTS:
            return self._send(event_stream_hello().encode(), "text/event-stream")
        if raw.startswith("/api/"):
            body = json.dumps(self._payload(raw)).encode()
            with Stub.lock:
                Stub.served.append((raw, len(body)))
            return self._send(body)
        rel = raw[len("/app/"):] if raw.startswith("/app/") else raw.lstrip("/")
        target = DIST / rel
        if not target.is_file() or "." not in target.name:
            target = DIST / "index.html"
        kind = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json", ".png": "image/png", ".webmanifest": "application/manifest+json"}.get(target.suffix, "application/octet-stream")
        self._send(target.read_bytes(), kind)


PROBE = """
() => {
  window.__perf = { frames: 0, blocked: 0, worst: 0, tasks: 0, longFrames: 0, worstFrame: 0, last: 0 };
  const tick = (t) => {
    const p = window.__perf;
    if (p.last) {
      const gap = t - p.last;
      if (gap > 50) p.longFrames++;
      if (gap > p.worstFrame) p.worstFrame = gap;
    }
    p.last = t;
    p.frames++;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      window.__perf.tasks++;
      window.__perf.blocked += e.duration;
      window.__perf.worst = Math.max(window.__perf.worst, e.duration);
    }
  }).observe({ entryTypes: ["longtask"] });
}
"""

READ = """
() => ({
  fps: window.__perf.frames,
  blocked: Math.round(window.__perf.blocked),
  worst: Math.round(window.__perf.worst),
  tasks: window.__perf.tasks,
  longFrames: window.__perf.longFrames,
  worstFrame: Math.round(window.__perf.worstFrame),
  nodes: document.getElementsByTagName("*").length,
  folders: document.querySelectorAll("[data-project]").length,
  rows: document.querySelectorAll("[data-session]").length,
  heap: Math.round((performance.memory ? performance.memory.usedJSHeapSize : 0) / 1048576),
})
"""


def run_case(browser, port: int, *, expanded: bool, seconds: float, rate: float, width: int, height: int, settle: float) -> dict:
    with Stub.lock:
        Stub.served.clear()
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2)
    if expanded:
        # Every folder open before the first paint, so the case measures the list and not the click.
        keys = [p["id"] for p in Stub.listing["projects"]] + ["\\u0000free"]
        context.add_init_script("try { " + " ".join(f"localStorage.setItem('daedalus.folder.{k}', '1');" for k in keys) + " } catch (e) {}")
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})
    t0 = time.time()
    page.goto(f"http://127.0.0.1:{port}/app/agents", wait_until="commit")
    # The first project or chat row of the desktop's column or the phone's list; the list's old
    # ".folder" class is gone, and waiting on it timed every run out.
    page.wait_for_selector("[data-project], [data-session]", timeout=120_000)
    open_ms = round((time.time() - t0) * 1000)
    with Stub.lock:
        open_bytes = sum(b for _, b in Stub.served)
        Stub.served.clear()
    time.sleep(settle)
    page.evaluate(PROBE)
    time.sleep(seconds)
    out = page.evaluate(READ)
    with Stub.lock:
        list_bytes = sum(b for p, b in Stub.served if p == "/api/sessions")
        list_calls = sum(1 for p, _ in Stub.served if p == "/api/sessions")
    context.close()
    return {
        "case": "expanded" if expanded else "collapsed",
        "agents": len(Stub.listing["sessions"]),
        "throttle": rate,
        "viewport": f"{width}x{height}",
        "open_ms": open_ms,
        "open_bytes": open_bytes,
        "fps": round(out["fps"] / seconds, 1),
        "blocked_ms": out["blocked"],
        "worst_task_ms": out["worst"],
        "long_tasks": out["tasks"],
        "frames_over_50ms": out["longFrames"],
        "worst_frame_ms": out["worstFrame"],
        "dom_nodes": out["nodes"],
        "folders_in_dom": out["folders"],
        "rows_in_dom": out["rows"],
        "heap_mb": out["heap"],
        "list_calls": list_calls,
        "list_bytes": list_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=300)
    parser.add_argument("--folders", type=int, default=6)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--settle", type=float, default=3.0)
    parser.add_argument("--rate", type=float, default=4.0)
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    if not (DIST / "index.html").is_file():
        print("build the app first: cd miniapp && npm run build", file=sys.stderr)
        return 2

    Stub.listing = installation(args.agents, args.folders)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rows = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.chromium) if args.chromium else p.chromium.launch()
            for expanded in (False, True):
                rows.append(run_case(browser, port, expanded=expanded, seconds=args.seconds, rate=args.rate, width=args.width, height=args.height, settle=args.settle))
            browser.close()
    finally:
        server.shutdown()

    keys = ("case", "agents", "open_ms", "fps", "frames_over_50ms", "worst_frame_ms", "long_tasks", "blocked_ms", "dom_nodes", "folders_in_dom", "rows_in_dom", "heap_mb", "list_calls", "list_bytes")
    width = max(len(k) for k in keys)
    for key in keys:
        print(key.rjust(width), " ".join(str(r[key]).rjust(12) for r in rows))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return Stub.unhandled.report()


if __name__ == "__main__":
    sys.exit(main())
