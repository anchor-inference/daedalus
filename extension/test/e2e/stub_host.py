#!/usr/bin/env python3
"""A stand-in for `browserd bridge` that plays the daemon's side of the relay protocol.

Chrome starts it through native messaging, exactly as it will start the bridge. It answers hello
with welcome and check with check_ok, drives a short script once welcomed (a ping, the app's own
check, a tab, an allowed and a refused DevTools command, a click and a screenshot), and writes every frame it receives,
and the origin Chrome named, as JSON lines to the file in `STUB_LOG`.
"""

import json
import os
import struct
import sys
import time

LOG = open(os.environ.get("STUB_LOG", "/dev/null"), "a", buffering=1)


def log(kind, value):
    LOG.write(json.dumps({"kind": kind, "value": value}) + "\n")


def read():
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    return json.loads(sys.stdin.buffer.read(length))


def send(frame):
    body = json.dumps(frame).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(body)) + body)
    sys.stdout.buffer.flush()


def main():
    log("origin", sys.argv[1] if len(sys.argv) > 1 else "")
    tab = None
    while True:
        frame = read()
        if frame is None:
            return
        log("frame", frame)
        kind = frame.get("t")
        if kind == "hello":
            send({"t": "welcome", "proto": 2, "daemon_version": "0.0.0-stub", "install_name": "Stub", "install_kind": "native"})
            send({"t": "ping", "n": 1})
            send({"t": "call", "id": 1, "op": "notify", "args": {"text": "stub"}})
            send({"t": "call", "id": 2, "op": "tab.create", "args": {"group": "g1", "title": "Smoke"}})
        elif kind == "check":
            send({"t": "check_ok", "n": frame["n"], "host_seen": True})
        elif kind == "result" and frame.get("id") == 2 and frame.get("ok"):
            tab = frame["value"]["tab"]["id"]
            session = f"x{tab}"
            send({"t": "cdp", "id": 3, "session": session, "method": "Runtime.evaluate", "params": {"expression": "6*7", "returnByValue": True}})
            send({"t": "cdp", "id": 4, "session": session, "method": "Network.getCookies", "params": {}})
            # A page of a real origin when the smoke serves one: Chrome keeps extensions out of data: pages,
            # so the border and the cursor are drawn only there.
            page = os.environ.get("STUB_PAGE") or "data:text/html,<title>smoke</title>ok"
            send({"t": "cdp", "id": 5, "session": session, "method": "Page.navigate", "params": {"url": page}})
        elif kind == "result" and frame.get("id") == 5 and frame.get("ok"):
            # The agent clicks: the cursor is drawn there. Then a screenshot, which must not show it. A
            # moment first: the content script goes into the page as it loads, after the navigation's answer.
            time.sleep(1.0)
            send({"t": "cdp", "id": 6, "session": f"x{tab}", "method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed", "x": 120, "y": 80, "button": "left", "clickCount": 1}})
        elif kind == "result" and frame.get("id") == 6:
            send({"t": "cdp", "id": 7, "session": f"x{tab}", "method": "Page.captureScreenshot", "params": {"format": "png"}})
        elif kind == "result" and frame.get("id") == 7:
            # The page as DevTools sees it, into the closed shadow root: where the cursor is now.
            send({"t": "cdp", "id": 8, "session": f"x{tab}", "method": "DOM.getDocument", "params": {"depth": -1, "pierce": True}})


if __name__ == "__main__":
    main()
