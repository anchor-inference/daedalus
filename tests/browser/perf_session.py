"""Measure the session screen under a stubbed API: frame rate, blocked main thread, refetch traffic.

Everything the app asks for is answered here with invented data, so the numbers describe the
client and nothing else. A session of N messages is served once, then an event stream pushes a
text delta every 20 ms (and a message_stop every few seconds, the way a real run does), while the
page runs at a CPU throttle that stands in for a mid-range phone.

    cd miniapp && npm run build
    python3 tests/browser/perf_session.py                 # 600 and 1200 messages, 4x throttle
    python3 tests/browser/perf_session.py --messages 600 --seconds 8 --rate 4 --json out.json
    python3 tests/browser/perf_session.py --live null     # the run's newest message with no row number

Reported per case: frames per second while streaming, the total and the worst long task (a task
over 50 ms blocks the main thread visibly), DOM nodes, JS heap, and what the app pulled from the
API during the window — the count and the bytes, which is what a refetch per event costs.

The tail of a running session is the case that decides whether any of the rest matters: the
newest message or two are in the engine and not in the transcript yet, and how the API describes
them is what ``--live`` selects. Read ``session_bytes`` for the answer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import EVENTS, GATES, Unhandled, event_stream_hello, folders, serve_shared_post  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "miniapp" / "dist"
SESSION_ID = "a1b2c3d4e5f6"
NOW = datetime.now(UTC)

LOREM = (
    "The deploy went out at the top of the hour and the health check came back green on both "
    "replicas. I read the log around the restart and the only thing out of the ordinary is a "
    "single slow query, which I have written down below with the plan it took."
)
# A tool-result preview the size the API ships today (TOOL_RESULT_PREVIEW_CHARS).
TOOL_OUT = "".join(f"line {i:04d}: ok, 0.4ms, cache hit, worker-3, run/1a2b3c\n" for i in range(72))


def ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def message(seq: int, total: int) -> dict:
    """One message of the synthetic history: user, a tool step, its result, then an answer."""
    at = ago((total - seq) * 12)
    slot = seq % 4
    if slot == 0:
        return {"role": "user", "seq": seq, "text": f"Check run {seq // 4} and tell me what the log says.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": at, "origin": "operator"}
    if slot == 1:
        return {
            "role": "assistant",
            "seq": seq,
            "text": "",
            "thinking": "The log for that run is on the box; reading the tail is enough to tell.",
            "tool_calls": [{"id": f"c{seq}", "name": "Exec", "arguments": {"command": f"tail -n 40 /srv/log/run-{seq // 4}.log"}}],
            "tool_results": [],
            "created_at": at,
        }
    if slot == 2:
        return {"role": "tool", "seq": seq, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": f"c{seq - 1}", "content": TOOL_OUT, "is_error": False, "length": len(TOOL_OUT)}], "created_at": at}
    return {
        "role": "assistant",
        "seq": seq,
        "text": f"### Run {seq // 4}\n\n{LOREM}\n\n- restart at `{seq}s`, no error\n- `SELECT` over `events` took 40ms\n\n```sql\nSELECT seq FROM events WHERE run_id = '{seq}' ORDER BY seq DESC LIMIT 20;\n```\n",
        "thinking": "",
        "tool_calls": [],
        "tool_results": [],
        "created_at": at,
    }


def live_row(seq: int) -> dict | None:
    """The message the run is in the middle of, as the API returns it before the transcript has it.

    The transcript is written by a task of its own, so a read that lands during a run routinely
    catches one message the engine holds and the rows do not. ``--live seq`` is what the API returns
    for it now: the number the row is going to get, and a mark saying the number is not settled yet.
    ``--live null`` is what it returned before, a row with no number at all, which the app has to
    read as a hole in the history — the case this harness could not see, and the one that costs a
    re-read of the whole session on every event.
    """
    if Stub.live == "off":
        return None
    text = Stub.streaming
    if not text:
        return None
    row = {"role": "assistant", "seq": seq, "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": NOW.isoformat()}
    if Stub.live == "null":
        row["seq"] = None
    else:
        row["live"] = True
    return row


def detail(messages: int, tail: int, before: int | None) -> dict:
    """The session as the API returns it. The cursor is answered only with --cursor: today's API has none."""
    rows = [message(i + 1, messages) for i in range(messages)] + list(Stub.written)
    live = live_row(len(rows) + 1)
    if live is not None:
        rows = rows + [live]
    if before is not None and Stub.cursor:
        rows = [r for r in rows if (r["seq"] or 0) < before]
    rows = rows[-tail:] if tail > 0 else rows
    return {
        "id": SESSION_ID,
        "title": "Bakery site",
        "status": "running",
        "compacting": None,
        "run_id": "run1",
        "workspace": "/srv/work/bakery",
        "workspace_name": "bakery",
        "workspace_own": True,
        "workspace_sessions": [],
        "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
        "pending": None,
        "model": "Claude Opus 5",
        "provider": "claude",
        "messages": rows,
        "usd_cap": None,
        "brief": "",
        "spawned_by": None,
        "tools_off": [],
        "loop": None,
        "services": [],
        "subagent_of": None,
        "subagent_name": None,
        "leader_title": None,
        "subagents": [],
        "context": {"tokens": 41200, "window": 200000, "messages": messages, "summaries": 0, "operator_turns": messages // 4},
        "usage": {"c": 12, "i": 412000, "o": 9100, "ch": 380000, "usd": 1.42},
    }


class Stub(BaseHTTPRequestHandler):
    """The app's whole API surface, invented, plus the built app under /app."""

    messages = 600
    cursor = False  # whether `before=<seq>` is answered; today's API ignores it
    initial_tail = 0  # when set, the app's full read is answered with this many rows only
    written = []  # the messages the run has finished, as the transcript would hold them
    streaming = ""  # the message the run is in the middle of, which the transcript does not hold yet
    live = "seq"  # how an unpersisted message is returned: "seq" (today), "null" (before), "off"
    delta_ms = 20
    stop_every = 200
    served = []  # (path, bytes) of every /api read, so a refetch storm is visible as a number
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

    def _api(self, path: str, query: dict[str, str]) -> None:
        if path.endswith("/stream"):
            return self._stream()
        body = json.dumps(self._payload(path, query)).encode()
        with Stub.lock:
            Stub.served.append((path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(query.items())) if query else ""), len(body)))
        self._send(body)

    def _payload(self, path: str, query: dict[str, str]) -> object:
        if path.startswith("/api/usage/provider/"):
            return {"provider": "claude", "today": {"calls": 12, "cost_usd": 1.42}, "subscription": None, "balance": None}
        if path == f"/api/sessions/{SESSION_ID}":
            tail = int(query.get("tail", "0"))
            # The app's full read asks for the API default; here that stands for the whole synthetic
            # history, so a case of N messages really puts N on the screen. A smaller tail is a tail
            # read and is answered as one.
            if tail in (0, 600):
                tail = Stub.initial_tail or Stub.messages
            before = int(query["before"]) if query.get("before") else None
            return detail(Stub.messages, tail, before)
        if path == f"/api/sessions/{SESSION_ID}/steer":
            # The composer asks what the host holds for the next step of a running session: nothing here.
            return []
        if path == f"/api/sessions/{SESSION_ID}/checkpoints":
            # Which turns can still put the files back. The screen asks once, when it opens.
            return {"checkpoints": [], "total": 0, "pruned": False, "pruned_before": None, "removed": 0, "note": "", "keep_days": 14, "keep_last": 40}
        if path in GATES:
            return GATES[path]
        # Answering {} here is how this harness came to measure the "Add a model" screen for two
        # minutes and then time out. An unrecognised path is reported and fails the run instead.
        Stub.unhandled.record(path)
        return {}

    def _stream(self) -> None:
        """A run in progress: a text delta every `delta_ms`, a message_stop every `stop_every` deltas."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        words = ("The log ", "shows ", "one ", "slow ", "query ", "on ", "the ", "events ", "table, ", "which ")
        n = 0
        written = ""
        try:
            self.wfile.write(b"event: message_start\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                text = words[n % len(words)]
                frame = f'event: content_block_delta\ndata: {{"delta": {{"type": "text_delta", "text": "{text}"}}}}\n\n'
                self.wfile.write(frame.encode())
                self.wfile.flush()
                n += 1
                written += text
                Stub.streaming = written
                if n % Stub.stop_every == 0:
                    # The message the stream just finished is in the transcript from now on, the way
                    # the real one is by the time the app reads the end of the conversation.
                    with Stub.lock:
                        seq = Stub.messages + len(Stub.written) + 1
                        Stub.written.append({"role": "assistant", "seq": seq, "text": written, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": NOW.isoformat()})
                    written = ""
                    Stub.streaming = ""
                    self.wfile.write(b"event: message_stop\ndata: {}\n\n")
                    self.wfile.write(b'event: state_changed\ndata: {"status": "running"}\n\n')
                    self.wfile.flush()
                time.sleep(Stub.delta_ms / 1000)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            return

    def do_POST(self) -> None:  # noqa: N802
        serve_shared_post(self, Stub.unhandled)

    def do_GET(self) -> None:  # noqa: N802
        raw, _, qs = self.path.partition("?")
        query = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if raw == EVENTS:
            return self._send(event_stream_hello().encode(), "text/event-stream")
        if raw.startswith("/api/"):
            return self._api(raw, query)
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
      // A frame the browser could not paint for 50 ms is a delta the main thread sat on.
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
  turns: document.querySelectorAll(".timeline .turn").length,
  heap: Math.round((performance.memory ? performance.memory.usedJSHeapSize : 0) / 1048576),
})
"""


def run_case(browser, port: int, messages: int, seconds: float, rate: float, width: int, height: int, settle: float) -> dict:
    Stub.messages = messages
    with Stub.lock:
        Stub.served.clear()
        Stub.written.clear()
        Stub.streaming = ""
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2)
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})
    t0 = time.time()
    page.goto(f"http://127.0.0.1:{port}/app/agents/{SESSION_ID}", wait_until="commit")
    page.wait_for_selector(".timeline .turn", timeout=120_000)
    open_ms = round((time.time() - t0) * 1000)
    with Stub.lock:
        open_bytes = sum(b for _, b in Stub.served)
        open_calls = len(Stub.served)
        Stub.served.clear()
    # The opening of the screen is reported separately; the frame rate is the steady state after it.
    time.sleep(settle)
    page.evaluate(PROBE)
    time.sleep(seconds)
    out = page.evaluate(READ)
    with Stub.lock:
        calls = [p for p, _ in Stub.served if p.startswith(f"/api/sessions/{SESSION_ID}")]
        refetch_bytes = sum(b for p, b in Stub.served if p.startswith(f"/api/sessions/{SESSION_ID}"))
    context.close()
    return {
        "messages": messages,
        "live": Stub.live,
        "throttle": rate,
        "viewport": f"{width}x{height}",
        "open_ms": open_ms,
        "open_bytes": open_bytes,
        "open_calls": open_calls,
        "fps": round(out["fps"] / seconds, 1),
        "blocked_ms": out["blocked"],
        "blocked_pct": round(100 * out["blocked"] / (seconds * 1000)),
        "worst_task_ms": out["worst"],
        "long_tasks": out["tasks"],
        "frames_over_50ms": out["longFrames"],
        "worst_frame_ms": out["worstFrame"],
        "dom_nodes": out["nodes"],
        "turns_in_dom": out["turns"],
        "heap_mb": out["heap"],
        "session_calls": len(calls),
        "session_bytes": refetch_bytes,
        "session_paths": sorted(set(re.sub(r"=\d+", "=N", c) for c in calls)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, action="append", help="history length (repeatable; default 600 and 1200)")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--settle", type=float, default=3.0, help="seconds to let the screen settle before measuring")
    ap.add_argument("--rate", type=float, default=4.0, help="CPU throttle, 4 = mid-range phone")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--cursor", action="store_true", help="answer before=<seq> (the paginated API)")
    ap.add_argument("--live", choices=["seq", "null", "off"], default="seq", help="how an unpersisted message is returned: seq (today), null (before it was numbered), off")
    ap.add_argument("--initial-tail", type=int, default=0, help="answer the app's full read with this many rows")
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()
    if not DIST.joinpath("index.html").is_file():
        print(f"no build at {DIST}: run `npm run build` in miniapp first", file=sys.stderr)
        return 2
    sizes = args.messages or [600, 1200]
    Stub.cursor = args.cursor
    Stub.initial_tail = args.initial_tail
    Stub.live = args.live
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Stub)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM"), args=["--enable-precise-memory-info"])
        for n in sizes:
            row = run_case(browser, args.port, n, args.seconds, args.rate, args.width, args.height, args.settle)
            rows.append(row)
            print(json.dumps(row))
        browser.close()
    server.shutdown()
    head = ["messages", "live", "fps", "frames_over_50ms", "worst_frame_ms", "blocked_ms", "worst_task_ms", "dom_nodes", "turns_in_dom", "heap_mb", "open_ms", "session_calls", "session_bytes"]
    print("\n| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(str(r[h]) for h in head) + " |")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
    return Stub.unhandled.report()


if __name__ == "__main__":
    raise SystemExit(main())
