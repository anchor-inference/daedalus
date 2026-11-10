"""A message sent with pictures while the agent works is drawn once, mid-turn and after the turn.

The app draws a bubble while a message is being sent and drops it once a shown row names the send's
receipt id. A steer is put into the run's history before the host has finished labelling it, so the
first read of it can carry the words and the pictures and no receipt id yet. The screen used to keep
that first copy for the rest of the run, because the later reads looked like "the same message": the
steer showed in the turn and a second time just above the composer. Here the host's reads happen in
that order, through a real event stream, and the conversation must hold one bubble with the words.

The other path too: a message the run takes only once the turn is over opens the next turn, and that
row must retire the bubble as well.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_steer_once.py

Exit 0 when every step holds; ``SHOTS=<dir>`` keeps a picture of each width.
"""
from __future__ import annotations

import json
import os
import sys
import time

from check_composer import BASE, CHROMIUM, HOST, PIXEL, SESSION, UNHANDLED, field, message, posts, reached, stub
from event_feed import EventFeed
from playwright.sync_api import Page, sync_playwright

WORDS = "and the first two slides on the main domain repeat each other"
SHOTS_NAMES = ["slide-1.png", "slide-2.png"]


class SessionFeed(EventFeed):
    """The session's own stream: the same server, with the session's envelope instead of the global one."""

    def emit(self, kind: str, payload: dict, run_id: str = "r1") -> None:
        with self.lock:
            self.seq += 1
            frame = f"event: {kind}\ndata: {json.dumps({'kind': kind, 'payload': payload, 'run_id': run_id, 'event_seq': self.seq, 'history_revision': 0})}\n\n"
            for lane in self.open:
                lane.put(frame)


def drawn(page: Page) -> int:
    return page.locator(".msg.user", has_text=WORDS).count()


def wait_until(page: Page, check, seconds: float = 8.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            return False
        page.wait_for_timeout(50)
    return True


def send_with_pictures(page: Page, problems: list[str], where: str) -> str:
    """Words and two pictures, sent while the run is on: queued for after the turn, its card above the pill."""
    before = len(posts("/upload"))
    page.locator(".composer input[type=file][multiple]").first.set_input_files([{"name": name, "mimeType": "image/png", "buffer": PIXEL} for name in SHOTS_NAMES])
    field(page).click()
    field(page).type(WORDS)
    # The circle, not Enter: on a phone Enter starts a new line.
    page.locator(".composer .roundbtn.primary").click()
    reached(page, "/upload", before, f"{where}: sending with pictures", problems)
    sent = posts("/upload")[-1][2] or {}
    cid = str(sent.get("client_message_id") or "")
    for item in HOST.queue:
        if item["id"] == cid:
            item["text"] = WORDS
    page.wait_for_selector(".composer .steer", timeout=5000)
    return cid


def row(seq: int, cid: str | None, **over: object) -> dict:
    attachments = [{"name": name, "mime": "image/png", "size": len(PIXEL), "file": name} for name in SHOTS_NAMES]
    return message(seq, "user", WORDS, run_id="r1", attachments=attachments, **({"client_message_ids": [cid]} if cid else {}), **over)


def mid_turn(page: Page, feed: SessionFeed, where: str, problems: list[str], shots: str | None) -> None:
    cid = send_with_pictures(page, problems, where)
    page.locator(".composer .steer .steer-now").click()
    reached(page, f"/steer/{cid}", 0, f"{where}: the card's Steer", problems, method="POST")
    feed.emit("steer_changed", {"session_id": SESSION, "reason": "steered", "count": 1, "queued": HOST.queue})
    # The core has placed it; the host has said whose words they are but not yet which send they were.
    HOST.queue = []
    base = list(HOST.messages)
    HOST.messages = base + [row(103, None, delivery="steer", live=True)]
    feed.emit("message_stop", {"turn_id": "t1", "stop_reason": "tool_use"})
    if not wait_until(page, lambda: page.locator(".turn .msg.user", has_text=WORDS).count() >= 1):
        problems.append(f"{where}: the placed steer never reached the conversation")
    # The labelling finishes; the next read names the send, and the queue says it was read. Nothing
    # else joins the history until the model's next message ends, which can be minutes away: the
    # stream draws that message meanwhile ("Writing file"), and this is the read the screen lives on.
    HOST.messages = base + [row(103, cid, delivery="steer", live=True)]
    feed.emit("queue_update", {"placed": [cid], "kind": "steer", "remaining": 0})
    feed.emit("steer_changed", {"session_id": SESSION, "reason": "consumed", "count": 0, "queued": []})
    feed.emit("message_start", {"turn_id": "t2", "model": "claude-opus-5", "role": "assistant"})
    feed.emit("tool_use_start", {"turn_id": "t2", "block_idx": 1, "tool_call_id": "w1", "tool_name": "Write"})
    page.wait_for_timeout(1200)
    if drawn(page) != 1:
        problems.append(f"{where}: a steer with pictures is drawn {drawn(page)} times mid-turn, not once")
    if page.locator(".composer .steer").count():
        problems.append(f"{where}: the card stayed after the run read the steer")
    if shots:
        page.screenshot(path=os.path.join(shots, f"steer-once-{where}.png"))


def after_turn(page: Page, feed: SessionFeed, where: str, problems: list[str]) -> None:
    """The run ends before reading it: the message opens the next turn, a settled row naming the send."""
    cid = send_with_pictures(page, problems, where)
    HOST.queue = []
    HOST.messages = list(HOST.messages) + [row(103, cid, delivery="drained")]
    feed.emit("steer_changed", {"session_id": SESSION, "reason": "consumed", "count": 0, "queued": []})
    feed.emit("run_settled", {"status": "completed"})
    if not wait_until(page, lambda: page.locator(".turn .msg.user", has_text=WORDS).count() >= 1):
        problems.append(f"{where}: the message the next turn opened with never reached the conversation")
    page.wait_for_timeout(1200)
    if drawn(page) != 1:
        problems.append(f"{where}: a message read after the turn is drawn {drawn(page)} times, not once")


def scenario(browser, width: int, height: int, phone: bool, path: str, shots: str | None) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    where = f"{'phone' if phone else 'desktop'} {path}"
    feed = SessionFeed()
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=phone, has_touch=phone)
    try:
        HOST.status = "running"
        HOST.queue = []
        HOST.messages = [message(101, "user", "Polish the landing page.", run_id="r1"), message(102, "assistant", "", run_id="r1", tool_calls=[{"id": "x1", "name": "Exec", "arguments": {"command": "ls"}}])]
        page = context.new_page()
        page.route("**/api/**", stub)
        page.route(f"**/api/sessions/{SESSION}/stream**", feed.route)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".composer .roundbtn.primary[data-action='stop']", timeout=15000)
        if not wait_until(page, lambda: feed.connected() > 0):
            problems.append(f"{where}: the session stream never connected")
            return problems
        if path == "mid-turn":
            mid_turn(page, feed, where.replace(" ", "-"), problems, shots)
        else:
            after_turn(page, feed, where.replace(" ", "-"), problems)
    finally:
        context.close()
        feed.close()
        HOST.status = "idle"
        HOST.queue = []
    return problems


def main() -> int:
    shots = os.environ.get("SHOTS")
    if shots:
        os.makedirs(shots, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        try:
            for path in ("mid-turn", "after-turn"):
                problems += scenario(browser, 1280, 860, False, path, shots)
                problems += scenario(browser, 390, 844, True, path, shots)
        finally:
            browser.close()
    for problem in problems:
        print("FAIL:", problem)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
