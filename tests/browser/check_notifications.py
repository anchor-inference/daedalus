"""Toasts, the bell and the Inbox as the notification centre, driven by the host's event stream.

Everything but the stream and the notifications is the screenshot stub's installation. The stream is
a small server here that holds the connection open and writes the frames this script tells it to
(the pattern of ``check_event_stream.py``); the notifications are a list kept here, so that answering
one changes what the next read returns, the way the host does.

What is checked, in order:

- five notifications arrive: three toasts are shown with a "+2 more" line and Close all, and all of
  them leave after their time, the counted ones with them;
- a toast closed under the mouse does not freeze the next one, which still leaves by itself;
- Close all empties the stack; "Don't show pop-ups" switches them off for this device, after which
  none is drawn, and the switch in Settings → Notifications turns them back on;
- in a conversation the stack stays clear of the composer and its send button;
- the bell's badge counts them;
- a permission request: the popover shows it under "Needs you" with Allow and Deny; Allow posts
  ``…/act {action: "allow"}``, and the ``notify.resolved`` that follows takes it off the list;
- a notification about the session on screen raises no toast, and one about another session does;
- on a phone: one banner at the top, the Inbox tab's badge, and "Needs you" first on the Inbox.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_notifications.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from screenshots import BASE, CHROMIUM, P1, S1, S2, S3, UNHANDLED, stub  # noqa: E402

TOAST_S = 7.0
"""The app's time for a toast that asks nothing; the checks allow a second on either side."""


class Streams:
    lock = threading.Lock()
    open: list[queue.Queue[str | None]] = []
    seq = 100


class EventServer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        feed: queue.Queue[str | None] = queue.Queue()
        with Streams.lock:
            Streams.open.append(feed)
            head = Streams.seq
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.wfile.write(f'event: hello\ndata: {json.dumps({"head": head, "oldest": 1, "server_time": now, "client": ""})}\n\n'.encode())
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


class Centre:
    """The notifications the host holds, and the answers it was sent."""

    entries: list[dict] = []
    acted: list[tuple[int, dict]] = []

    @classmethod
    def summary(cls) -> dict:
        return {"unseen": sum(1 for e in cls.entries if not e["seen"] and e["level"] != "quiet"), "needs_you": sum(1 for e in cls.entries if e["needs_you"])}

    @classmethod
    def view(cls, name: str) -> list[dict]:
        rows = sorted(cls.entries, key=lambda e: -e["id"])
        if name == "needs_you":
            return [e for e in rows if e["needs_you"]]
        if name == "unseen":
            return [e for e in rows if not e["seen"] and e["level"] != "quiet"]
        return rows


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def notification(id_: int, title: str, *, session: str | None, category: str = "run_finished", level: str = "normal", tone: str = "ok", project: str | None = None, request: str | None = None) -> dict:
    actions = [{"id": "allow", "label": "Allow", "style": "primary", "quick": True}, {"id": "deny", "label": "Deny", "style": "default", "quick": True}, {"id": "open", "label": "Open", "style": "ghost", "quick": False}] if request else []
    return {
        "id": id_, "at": now_iso(), "updated_at": now_iso(), "category": category, "kind": category, "level": level, "tone": tone, "title": title,
        "body": "Exec: npm install sharp" if request else "", "link": f"/app/agents/{session}" if session else "", "session_id": session, "run_id": None,
        "project_id": project, "staff_id": None, "terminal_id": None, "source": "check", "dedupe_key": request, "request_ref": request, "count": 1,
        "actions": actions, "seen": False, "resolved": None, "needs_you": request is not None, "delivered": {},
    }


def send(kind: str, payload: dict, *, session: str | None = None, project: str | None = None) -> None:
    with Streams.lock:
        Streams.seq += 1
        event = {"seq": Streams.seq, "at": now_iso(), "type": kind, "project_id": project, "session_id": session, "staff_id": None, "terminal_id": None, "payload": payload}
        frame = f"id: {Streams.seq}\nevent: {kind}\ndata: {json.dumps(event)}\n\n"
        for feed in Streams.open:
            feed.put(frame)


def arrive(entry: dict, *, toast: bool = True) -> None:
    Centre.entries.append(entry)
    send("notify", {"notification": entry, "toast": toast, "deliver": {"push": False, "desktop": False, "telegram": "none"}, "merged": False, "summary": Centre.summary()}, session=entry["session_id"], project=entry["project_id"])


def wait(page, predicate, seconds: float) -> bool:  # type: ignore[no-untyped-def]
    """Poll through the page's clock: the sync API runs route handlers only inside its own calls."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        page.wait_for_timeout(50)
    return predicate()


def toasts(page) -> list[str]:  # type: ignore[no-untyped-def]
    return page.eval_on_selector_all(".notice-toasts .notice-toast", "els => els.map(e => e.dataset.notice)")


def overlap(a: dict, b: dict) -> bool:
    return a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]


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
        query = request.url.split("?", 1)[1] if "?" in request.url else ""
        if rel == "/api/events":
            return route.continue_(url=f"{events_url}?{query}")
        if rel == "/api/notifications/summary":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(Centre.summary()))
        if rel == "/api/notifications":
            view = next((p.split("=", 1)[1] for p in query.split("&") if p.startswith("view=")), "all")
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"entries": Centre.view(view), "next_before": None, "summary": Centre.summary()}))
        if rel == "/api/notifications/seen" and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            for e in Centre.entries:
                if body.get("all") or e["id"] in (body.get("ids") or []):
                    e["seen"] = True
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"marked": 1, "summary": Centre.summary()}))
        if rel.startswith("/api/notifications/") and rel.endswith("/act") and request.method == "POST":
            id_ = int(rel.split("/")[3])
            body = json.loads(request.post_data or "{}")
            Centre.acted.append((id_, body))
            entry = next(e for e in Centre.entries if e["id"] == id_)
            entry.update(resolved=body["action"], needs_you=False, seen=True)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"resolution": body["action"], "notification": entry}))
            # The host announces the resolution to every client after it answered this one.
            send("notify.resolved", {"id": id_, "request_ref": entry["request_ref"], "resolution": body["action"], "via": "notification", "summary": Centre.summary()}, session=entry["session_id"])
            return None
        return stub(route)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            context.route("**/api/**", route_api)
            page = context.new_page()
            page.goto(f"{BASE}/agents?token=t&lang=en", wait_until="commit")
            page.wait_for_selector(".sidebar .bell", timeout=20000)
            if not wait(page, lambda: len(Streams.open) == 1, 10):
                problems.append("the app did not open the event stream")
                raise RuntimeError
            page.mouse.move(700, 300)
            page.wait_for_timeout(500)

            # Five at once: three shown, two counted, all gone after their time.
            for i in range(1, 6):
                arrive(notification(i, f"Run {i} finished", session=S3))
            if not wait(page, lambda: len(toasts(page)) == 3, 3):
                problems.append(f"five notifications raised {len(toasts(page))} toasts, not three")
            shown = toasts(page)
            print("toasts shown:", shown)
            if shown != ["3", "2", "1"]:
                problems.append(f"the stack is {shown}, not the first three newest on top")
            more = page.locator(".notice-toasts .notice-more")
            if not more.count() or more.inner_text() != "+2 more":
                problems.append(f"the two waiting toasts are not counted: {more.inner_text() if more.count() else 'no line'}")
            if not page.locator(".notice-toasts [data-close-all]").count():
                problems.append("a stack of three has no Close all")
            badge = page.locator(".sidebar .bell .bell-badge")
            if not wait(page, lambda: badge.count() and badge.inner_text() == "5", 3):
                problems.append(f"the bell's badge says {badge.inner_text() if badge.count() else 'nothing'}, not 5")
            page.wait_for_timeout(int((TOAST_S - 1) * 1000))
            if len(toasts(page)) != 3:
                problems.append("a toast left before its time")
            if not wait(page, lambda: toasts(page) == [] and not page.locator(".notice-toasts").count(), 3):
                problems.append(f"after their time the toasts did not leave: {toasts(page)}")

            # Closed under the mouse: the stack leaves the page with the pointer inside it, and the
            # next toast must still run its own clock.
            arrive(notification(11, "Run 11 finished", session=S3))
            if not wait(page, lambda: toasts(page) == ["11"], 3):
                problems.append(f"a lone notification raised {toasts(page)}")
            close = page.locator(".notice-toast[data-notice='11'] button[aria-label='Dismiss']")
            close.hover()
            close.click()
            arrive(notification(12, "Run 12 finished", session=S3))
            if not wait(page, lambda: toasts(page) == ["12"], 3):
                problems.append(f"the next notification raised {toasts(page)}")
            if not wait(page, lambda: toasts(page) == [], TOAST_S + 2):
                problems.append("a toast after one closed under the mouse stayed up past its time")
            page.mouse.move(700, 300)

            # A mouse moved onto a toast holds it past its time; moved away, the toast goes.
            arrive(notification(19, "Run 19 finished", session=S3))
            if not wait(page, lambda: toasts(page) == ["19"], 3):
                problems.append(f"a lone notification raised {toasts(page)}")
            held = page.locator(".notice-toast[data-notice='19']").bounding_box()
            if held:
                page.mouse.move(held["x"] + 40, held["y"] + 20, steps=4)
                page.mouse.move(held["x"] + 60, held["y"] + 24, steps=4)
            page.wait_for_timeout(int((TOAST_S + 1.5) * 1000))
            if toasts(page) != ["19"]:
                problems.append("a toast under a moving mouse left while it was held")
            page.mouse.move(700, 300, steps=4)
            if not wait(page, lambda: toasts(page) == [], TOAST_S + 2):
                problems.append("a toast the mouse let go of did not leave")

            # Close all, then "Don't show pop-ups", then nothing at all until Settings turns them on.
            for i in (13, 14):
                arrive(notification(i, f"Run {i} finished", session=S3))
            if not wait(page, lambda: len(toasts(page)) == 2, 3):
                problems.append(f"two notifications raised {toasts(page)}")
            page.locator(".notice-toasts [data-close-all]").click()
            if not wait(page, lambda: not page.locator(".notice-toasts").count(), 2):
                problems.append("Close all left the stack up")
            arrive(notification(15, "Run 15 finished", session=S3))
            if not wait(page, lambda: toasts(page) == ["15"], 3):
                problems.append(f"after Close all a new one raised {toasts(page)}")
            page.locator(".notice-toasts [data-popups-off]").click()
            if not wait(page, lambda: not page.locator(".notice-toasts").count() and page.locator(".toast").count() == 1, 2):
                problems.append("Don't show pop-ups did not take the stack down and say how to bring it back")
            stored = page.evaluate("() => localStorage.getItem('daedalus.notice.popups')")
            if stored != "off":
                problems.append(f"the device did not remember the switch: {stored}")
            arrive(notification(16, "Run 16 finished", session=S3))
            page.wait_for_timeout(1500)
            if page.locator(".notice-toast").count():
                problems.append("a pop-up was drawn with pop-ups switched off")
            if not wait(page, lambda: badge.count() and badge.inner_text() == str(Centre.summary()["unseen"]), 3):
                problems.append("with pop-ups off the bell stopped counting")
            page.goto(f"{BASE}/settings/notifications?token=t&lang=en", wait_until="commit")
            switch = page.locator("[data-popups-card] button[aria-pressed]")
            switch.wait_for(timeout=10000)
            if switch.get_attribute("aria-pressed") != "false":
                problems.append("Settings does not show pop-ups as off")
            switch.click()
            if page.evaluate("() => localStorage.getItem('daedalus.notice.popups')") is not None:
                problems.append("the switch in Settings did not turn pop-ups back on")
            page.goto(f"{BASE}/agents?token=t&lang=en", wait_until="commit")
            page.wait_for_selector(".sidebar .bell", timeout=20000)
            if not wait(page, lambda: len(Streams.open) >= 1, 10):
                problems.append("the stream did not come back after Settings")
            page.wait_for_timeout(1500)
            page.mouse.move(700, 300)

            # A permission request, answered from the bell's popover.
            arrive(notification(6, "Bakery site: photos is waiting for permission", session=S2, category="permission", level="urgent", tone="warning", project=P1, request=f"policy:{S2}:exec"))
            if not wait(page, lambda: toasts(page) == ["6"], 3):
                problems.append("the permission request raised no toast")
            page.locator(".notice-toast[data-notice='6'] button[aria-label='Dismiss']").click()
            page.locator(".sidebar .bell").click()
            needs = page.locator(".bell-pop .needs-you .notice-row[data-notice='6']")
            try:
                needs.wait_for(timeout=5000)
            except Exception:  # noqa: BLE001
                problems.append("the popover does not show the permission under Needs you")
                raise RuntimeError from None
            if needs.locator("[data-action='allow']").count() != 1 or needs.locator("[data-action='deny']").count() != 1:
                problems.append("the permission in the popover has no Allow and Deny")
            needs.locator("[data-action='allow']").click()
            if not wait(page, lambda: Centre.acted == [(6, {"action": "allow"})], 3):
                problems.append(f"Allow posted {Centre.acted}, not one allow for entry 6")
            if not wait(page, lambda: page.locator(".bell-pop .needs-you").count() == 0, 4):
                problems.append("the answered permission is still under Needs you")
            page.keyboard.press("Escape")
            if page.locator(".bell-pop").count():
                problems.append("Escape did not close the popover")

            # The conversation on screen says it itself; another one still gets a toast.
            page.goto(f"{BASE}/agents/{S1}?token=t&lang=en", wait_until="commit")
            page.wait_for_selector(".chat-scroll .timeline", timeout=20000)
            if not wait(page, lambda: len(Streams.open) >= 1, 10):
                problems.append("the stream did not come back after the page changed")
            page.wait_for_timeout(2500)
            arrive(notification(7, "Bakery site finished", session=S1))
            page.wait_for_timeout(1500)
            if toasts(page):
                problems.append(f"a notification about the session on screen raised a toast: {toasts(page)}")
            arrive(notification(8, "Support inbox finished", session=S3))
            if not wait(page, lambda: toasts(page) == ["8"], 3):
                problems.append(f"a notification about another session raised {toasts(page)}")
            for i in (17, 18):
                arrive(notification(i, f"Run {i} finished", session=S3))
            wait(page, lambda: len(toasts(page)) == 3, 3)
            page.wait_for_timeout(300)
            stack = page.locator(".notice-toasts").bounding_box()
            composer = page.locator(".composer-box").first.bounding_box()
            print("stack:", stack, "composer:", composer)
            if not stack or not composer or overlap(stack, composer):
                problems.append(f"the stack covers the composer: {stack} over {composer}")
            # A small laptop puts the composer under the corner: the stack must rise above it.
            page.set_viewport_size({"width": 1100, "height": 720})
            page.wait_for_timeout(400)
            stack = page.locator(".notice-toasts").bounding_box()
            composer = page.locator(".composer-box").first.bounding_box()
            print("small laptop, stack:", stack, "composer:", composer)
            if not stack or not composer or overlap(stack, composer):
                problems.append(f"on a small laptop the stack covers the composer: {stack} over {composer}")
            page.set_viewport_size({"width": 1440, "height": 900})
            context.close()

            # The phone: one banner, the More tab's badge, and Needs you first on the Inbox.
            Centre.entries[:] = [e for e in Centre.entries if e["id"] < 6]
            phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
            phone.route("**/api/**", route_api)
            page = phone.new_page()
            before = len(Streams.open)
            page.goto(f"{BASE}/agents?token=t&lang=en", wait_until="commit")
            page.wait_for_selector(".tabbar", timeout=20000)
            if not wait(page, lambda: len(Streams.open) > before, 10):
                problems.append("the phone did not open the event stream")
            page.wait_for_timeout(500)
            arrive(notification(9, "Expense tracker finished", session=S3))
            arrive(notification(10, "Weekly digest asks something", session=S3, category="permission", level="urgent", tone="warning", request=f"policy:{S3}:x"))
            if not wait(page, lambda: page.locator(".notice-toasts.banner .notice-toast").count() == 1, 3):
                problems.append(f"the phone shows {page.locator('.notice-toast').count()} toasts, not one banner")
            else:
                box = page.locator(".notice-toasts.banner").bounding_box()
                if not box or box["y"] > 40:
                    problems.append(f"the phone's banner is not at the top: {box}")
                for name in (".composer-box", ".tabbar"):
                    other = page.locator(name).first.bounding_box() if page.locator(name).count() else None
                    if box and other and overlap(box, other):
                        problems.append(f"the phone's banner covers {name}: {box} over {other}")
            # The Inbox is in More on a phone (Terminals took its tab); More carries its unseen count.
            tab = page.locator(".tabbar button[aria-haspopup='dialog'] .tab-badge")
            if not wait(page, lambda: tab.count() and tab.inner_text() == str(Centre.summary()["unseen"]), 3):
                problems.append(f"the More tab's badge is {tab.inner_text() if tab.count() else 'missing'}, not {Centre.summary()['unseen']}")
            page.locator(".tabbar button[aria-haspopup='dialog']").click()
            page.locator(".more-sheet .more-item[href$='/inbox']").click()
            page.wait_for_selector(".needs-you .notice-row", timeout=10000)
            first = page.eval_on_selector(".screen", "el => el.firstElementChild && el.firstElementChild.className")
            print("the Inbox starts with:", first)
            if first != "needs-you":
                problems.append(f"the Inbox does not start with Needs you: {first}")
            phone.close()
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
