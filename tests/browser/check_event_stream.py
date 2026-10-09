"""The app follows the host's event stream: one connection per browser, and the lists move with it.

Everything but the stream is the screenshot stub's installation. The stream itself cannot be a
fulfilled route, because a fulfilled body arrives whole and at once, so the page's request for
``/api/events`` is sent on to a small server here that holds the connection open and writes the
frames this script tells it to, when it tells it to.

What is checked, in order:

- the app opens the stream with its tab id and window kind, so the host can tie presence to it;
- ``session.unread_result`` and ``run.finished`` for a listed agent: the list is read again within
  half a second (not at the next poll) and the row gets its unread dot;
- while the stream is up the agent list is not polled every five seconds;
- the server drops the stream: the reconnect carries ``Last-Event-ID`` equal to the last id sent;
- a ``resync`` reads the lists again;
- a second page in the same browser does not open a stream of its own, and both pages follow the
  events the one stream carries.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_event_stream.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from screenshots import BASE, CHROMIUM, P1, S2, UNHANDLED, listing, stub  # noqa: E402

REREAD_BUDGET_S = 0.5
"""The debounce is 300 ms; the rest is the request and a loaded machine."""


class Streams:
    """The open ``/api/events`` connections and what each one asked with."""

    lock = threading.Lock()
    open: list[queue.Queue[str | None]] = []
    opened: list[dict[str, str]] = []
    head = 100


class EventServer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        feed: queue.Queue[str | None] = queue.Queue()
        with Streams.lock:
            Streams.open.append(feed)
            Streams.opened.append({"at": str(time.monotonic()), "last_event_id": self.headers.get("Last-Event-ID", ""), **query})
            head = Streams.head
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        server_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.wfile.write(f'event: hello\ndata: {json.dumps({"head": head, "oldest": 1, "server_time": server_time, "client": query.get("client", "")})}\n\n'.encode())
            self.wfile.flush()
            while True:
                try:
                    item = feed.get(timeout=1.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    return
                self.wfile.write(item.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            with Streams.lock:
                if feed in Streams.open:
                    Streams.open.remove(feed)


def send(kind: str, payload: dict, *, seq: int = 0, session: str | None = None) -> None:
    """One frame to every open stream, the way the host writes it."""
    event = {"seq": seq, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "type": kind, "project_id": None, "session_id": session, "staff_id": None, "terminal_id": None, "payload": payload}
    head = f"id: {seq}\n" if seq else ""
    frame = f"{head}event: {kind}\ndata: {json.dumps(event)}\n\n" if kind != "resync" else f"event: resync\ndata: {json.dumps(payload)}\n\n"
    with Streams.lock:
        if seq:
            Streams.head = seq
        for feed in Streams.open:
            feed.put(frame)


def drop() -> None:
    with Streams.lock:
        for feed in Streams.open:
            feed.put(None)


class Api:
    """The agent list with one agent's unread flag under the script's control, and when it was read."""

    unread = False
    reads: list[float] = []
    summary_reads: list[float] = []


def serve_list() -> dict:
    page = listing()
    page["sessions"] = [{**s, "unread_result": Api.unread} if s["id"] == S2 else s for s in page["sessions"]]
    return page


def wait(page, predicate, seconds: float) -> bool:  # type: ignore[no-untyped-def]
    """Poll ``predicate`` through the page's own clock: Playwright's sync API runs the route handlers
    only while a call of its own is in progress, so a plain ``time.sleep`` here would stall every
    request the page makes and the check would wait for its own deadlock."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        page.wait_for_timeout(20)
    return predicate()


def run() -> int:
    problems: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), EventServer)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    events_url = f"http://127.0.0.1:{server.server_address[1]}/api/events"

    def route_api(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel == "/api/events":
            query = request.url.split("?", 1)[1] if "?" in request.url else ""
            return route.continue_(url=f"{events_url}?{query}")
        if rel == "/api/sessions" and request.method == "GET":
            Api.reads.append(time.monotonic())
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(serve_list()))
        if rel == "/api/notifications/summary":
            Api.summary_reads.append(time.monotonic())
        return stub(route)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            context.route("**/api/**", route_api)
            page = context.new_page()
            page.goto(f"{BASE}/agents?token=t&lang=en", wait_until="commit")
            row = f'.sidebar [data-session="{S2}"]'
            # The folder opens once and stays open: the second page below finds it open too.
            page.click(f'.sidebar [data-project="{P1}"] .sb-prow', timeout=20000)
            page.wait_for_selector(row, timeout=10000)
            if not wait(page, lambda: len(Streams.open) == 1, 10):
                problems.append(f"the app did not open the event stream (open: {len(Streams.open)})")
                raise RuntimeError
            first = Streams.opened[0]
            print("opened:", first)
            if not first.get("client", "").startswith("tab-") or first.get("kind") != "browser":
                problems.append(f"the stream does not say which tab and kind it is: {first}")

            # A finished run nobody watched: the row's dot, from a fresh read of the list.
            page.wait_for_timeout(500)
            Api.unread = True
            sent = time.monotonic()
            send("session.unread_result", {"unread": True, "run_id": "r9"}, seq=101, session=S2)
            send("run.finished", {"run_id": "r9", "status": "completed", "duration_s": 42, "watched": False, "origin": "operator", "operator_facing": True, "telegram": False, "title": "Bakery site: photos"}, seq=102, session=S2)
            if not wait(page, lambda: any(t > sent for t in Api.reads), 3):
                problems.append("the list was not read again after the run finished")
            else:
                took = min(t for t in Api.reads if t > sent) - sent
                print(f"list read again {took * 1000:.0f} ms after the events")
                if took > REREAD_BUDGET_S:
                    problems.append(f"the list was read {took * 1000:.0f} ms after the events (budget {REREAD_BUDGET_S * 1000:.0f} ms)")
            try:
                page.wait_for_selector(f"{row}.unread .unread-dot", timeout=3000)
            except Exception:  # noqa: BLE001
                problems.append("the unread dot did not appear on the finished agent's row")

            # Up and quiet: no five-second poll of the list.
            quiet = time.monotonic()
            page.wait_for_timeout(7000)
            polled = [t for t in Api.reads if t > quiet]
            print(f"list reads in 7 s with the stream up: {len(polled)}")
            if polled:
                problems.append(f"the list was polled {len(polled)} times in 7 s while the stream was up")

            # Dropped: the reconnect resumes past the last id.
            before = len(Streams.opened)
            drop()
            if not wait(page, lambda: len(Streams.opened) > before and len(Streams.open) == 1, 10):
                problems.append("the app did not reconnect after the stream dropped")
            else:
                again = Streams.opened[before]
                print("reconnected:", again)
                if again.get("last_event_id") != "102":
                    problems.append(f"the reconnect resumed from {again.get('last_event_id')!r}, not '102'")

            # A resync: every mounted list is read again.
            page.wait_for_timeout(300)
            sent = time.monotonic()
            send("resync", {"reason": "too_far", "head": 500})
            if not wait(page, lambda: any(t > sent for t in Api.reads) and any(t > sent for t in Api.summary_reads), 3):
                problems.append("a resync did not read the lists and the badge again")

            # A second page of the same browser follows the first page's stream.
            opened = len(Streams.opened)
            other = context.new_page()
            other.goto(f"{BASE}/agents?token=t&lang=en", wait_until="commit")
            other.wait_for_selector(f"{row}.unread", timeout=20000)
            other.wait_for_timeout(2500)
            print(f"streams open with two pages: {len(Streams.open)}, opened since the second page: {len(Streams.opened) - opened}")
            if len(Streams.open) != 1 or len(Streams.opened) != opened:
                problems.append(f"two pages hold {len(Streams.open)} streams and opened {len(Streams.opened) - opened} more; one browser holds one")
            Api.unread = False
            send("session.unread_result", {"unread": False}, seq=103, session=S2)
            for name, where in (("first", page), ("second", other)):
                try:
                    where.wait_for_selector(f"{row}:not(.unread)", timeout=3000)
                except Exception:  # noqa: BLE001
                    problems.append(f"the {name} page did not follow the event carried by the other page's stream")

            # The first page goes away: the second takes the stream over and resumes past 103.
            opened = len(Streams.opened)
            page.close()
            if not wait(other, lambda: len(Streams.opened) > opened, 5):
                problems.append("nobody took the stream over when the leading page closed")
            else:
                taken = Streams.opened[opened]
                print("taken over:", taken)
                if taken.get("last_event_id") != "103":
                    problems.append(f"the page that took over resumed from {taken.get('last_event_id')!r}, not '103'")
            browser.close()
    except RuntimeError:
        pass
    finally:
        server.shutdown()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
