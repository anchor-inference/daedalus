#!/usr/bin/env python3
"""A stand-in for `browserd bridge` that plays the daemon's side of the relay protocol.

Chrome starts it through native messaging, exactly as it will start the bridge. It answers hello
with welcome and check with check_ok, drives a short script once welcomed (a ping, the app's own
check, a tab, an allowed and a refused DevTools command), and writes every frame it receives,
and the origin Chrome named, as JSON lines to the file in `STUB_LOG`.
"""

import json
import os
import struct
import sys

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
            send({"t": "welcome", "proto": 1, "daemon_version": "0.0.0-stub", "install_name": "Stub", "install_kind": "native"})
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
            send({"t": "cdp", "id": 5, "session": session, "method": "Page.navigate", "params": {"url": "data:text/html,<title>smoke</title>ok"}})


if __name__ == "__main__":
    main()
