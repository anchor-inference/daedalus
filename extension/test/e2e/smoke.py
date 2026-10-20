"""End-to-end smoke: the built extension in a real Chromium, talking to a stub native host.

Loads `extension/dist` unpacked into a throwaway profile whose `NativeMessagingHosts` names the stub
under the extension's host name, then checks the round trip a person sees: hello and welcome, ping
and pong, the app's check flashing in the side panel, the panel's own check confirmed, a tab created in
the agent's group and driven, a cookie read refused by the extension itself, and the agent's cursor
drawn where it clicks in its own tab, never in the operator's, and left out of the daemon's screenshot.

Unbranded Chromium only: branded Chrome ignores `--load-extension` since 137.

    cd extension && npm run build
    CHROMIUM=<chromium binary> python extension/test/e2e/smoke.py

`SMOKE_SCREENSHOT=<file.png>` also saves a picture of the side panel's settings after the check.
"""

import base64
import http.server
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
EXTENSION = HERE.parent.parent / "dist"
# The id and the host name as the extension itself declares them, so this check cannot drift from it.
PROTOCOL = (HERE.parent.parent / "src" / "protocol.ts").read_text()
EXTENSION_ID = re.search(r'EXTENSION_ID = "([a-p]{32})"', PROTOCOL).group(1)
HOST = re.search(r'NATIVE_HOST = "([a-z0-9_.]+)"', PROTOCOL).group(1)


def frames(log: Path):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def wait_for(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def pixels(png: bytes):
    """The RGB(A) rows of a PNG as Chromium writes one: 8 bits, not interlaced. Enough to look for a colour."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    at, idat, width, height, kind = 8, b"", 0, 0, 0
    while at < len(png):
        (length,) = struct.unpack(">I", png[at:at + 4])
        name, body = png[at + 4:at + 8], png[at + 8:at + 8 + length]
        if name == b"IHDR":
            width, height, depth, kind = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and kind in (2, 6), (depth, kind)
        elif name == b"IDAT":
            idat += body
        at += 12 + length
    step = 4 if kind == 6 else 3
    raw = zlib.decompress(idat)
    rows, prev, stride = [], bytearray(width * step), width * step
    for y in range(height):
        kind_of_filter, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - step] if i >= step else 0
            up, corner = prev[i], prev[i - step] if i >= step else 0
            if kind_of_filter == 1:
                line[i] = (line[i] + left) & 255
            elif kind_of_filter == 2:
                line[i] = (line[i] + up) & 255
            elif kind_of_filter == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind_of_filter == 4:
                guess = left + up - corner
                near = min((abs(guess - left), left), (abs(guess - up), up), (abs(guess - corner), corner), key=lambda pair: pair[0])[1]
                line[i] = (line[i] + near) & 255
        rows.append([tuple(line[i:i + 3]) for i in range(0, stride, step)])
        prev = line
    return rows


def blue(png: bytes) -> int:
    """How many pixels are the agent's blue (the border's and the cursor's #1f6feb, blended or not)."""
    return sum(1 for row in pixels(png) for r, g, b in row if b > 180 and b - r > 90 and b - g > 40)


def drawn(root: dict) -> dict:
    """The agent's marks in a document as DevTools returns it with ``pierce``, which sees into the closed
    shadow root that the page's own scripts cannot: the host element, its style, the cursor's attributes."""
    found = {"host": 0, "style": "", "cursor": None}

    def walk(node):
        attrs = node.get("attributes") or []
        named = dict(zip(attrs[::2], attrs[1::2]))
        if node.get("localName") == "daedalus-agent-border":
            found["host"] += 1
            found["style"] = named.get("style", "")
        if "data-daedalus-cursor" in named:
            found["cursor"] = named
        for child in (node.get("children") or []) + (node.get("shadowRoots") or []):
            walk(child)

    walk(root)
    return found


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — the standard library's name
        # No text: a glyph's coloured anti-aliased edge reads as blue to the colour test. "/blue" is the
        # operator's page, with a square of the agent's blue for the colour test to find.
        square = "<div style='width:60px;height:60px;background:#1f6feb'></div>" if self.path.endswith("/blue") else ""
        body = f"<!doctype html><title>smoke</title><body style='margin:0;background:#fff'>{square}</body>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    if not (EXTENSION / "manifest.json").exists():
        print("build the extension first: cd extension && npm run build", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        profile = Path(scratch) / "profile"
        log = Path(scratch) / "stub.jsonl"
        hosts = profile / "NativeMessagingHosts"
        hosts.mkdir(parents=True)
        wrapper = Path(scratch) / "host.sh"
        # A plain page on loopback for the agent's tab: the content script is not let into data: pages.
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        page = f"http://127.0.0.1:{server.server_address[1]}/"
        wrapper.write_text(f'#!/bin/sh\nSTUB_LOG="{log}" STUB_PAGE="{page}" exec "{sys.executable}" "{HERE / "stub_host.py"}" "$@"\n')
        wrapper.chmod(0o755)
        (hosts / f"{HOST}.json").write_text(json.dumps({
            "name": HOST,
            "description": "relay smoke stub",
            "path": str(wrapper),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
        }))
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile),
                channel="chromium",
                executable_path=os.environ.get("CHROMIUM") or None,
                headless=True,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
            )
            try:
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                assert EXTENSION_ID in worker.url, f"the key did not pin the id: {worker.url}"

                def received(kind):
                    return [f["value"] for f in frames(log) if f["kind"] == "frame" and f["value"].get("t") == kind]

                hello = wait_for(lambda: received("hello"), "hello")[0]
                origin = [f["value"] for f in frames(log) if f["kind"] == "origin"][0]
                assert origin == f"chrome-extension://{EXTENSION_ID}/", origin
                assert hello["proto"] == 2 and hello["profile_name"] == "Chrome", hello
                wait_for(lambda: received("pong"), "pong")

                def results():
                    return {r["id"]: r for r in received("result")}

                wait_for(lambda: 1 in results(), "the notify result")
                assert results()[1]["ok"] and results()[1]["value"] == {}

                wait_for(lambda: {2, 3, 4, 5} <= results().keys(), "the tab, evaluate and refused results")
                assert results()[2]["ok"], results()[2]
                assert results()[3]["value"]["result"]["value"] == 42, results()[3]
                assert results()[4]["ok"] is False and results()[4]["error"]["code"] == "refused", results()[4]
                assert results()[5]["ok"], results()[5]

                # The agent's click drew its cursor in the agent's tab, and the daemon's screenshot did not
                # show it, nor the border: the agent never sees its own marks as the page's.
                wait_for(lambda: 7 in results(), "the screenshot after the click")
                assert results()[6]["ok"] and results()[7]["ok"], (results()[6], results()[7])
                shot = base64.b64decode(results()[7]["value"]["data"])
                assert blue(shot) == 0, f"the daemon's screenshot shows {blue(shot)} pixels of the agent's marks"
                wait_for(lambda: 8 in results(), "the page's document after the screenshot")
                marks = drawn(results()[8]["value"]["root"])
                assert marks["host"] == 1 and marks["cursor"] and "on" in marks["cursor"].get("class", ""), marks
                assert "translate(120px, 80px)" in marks["cursor"].get("style", "").replace(",80", ", 80"), marks
                assert "opacity: 1" in marks["style"], f"the marks did not come back after the screenshot: {marks}"
                # The operator's own tab (the page the browser started with, outside the agent's window)
                # gets neither, and the colour test does find the agent's blue where it is.
                mine = context.pages[0] if context.pages else context.new_page()
                mine.goto(f"{page}blue")
                mine.wait_for_timeout(500)
                assert drawn(context.new_cdp_session(mine).send("DOM.getDocument", {"depth": -1, "pierce": True})["root"])["host"] == 0
                assert blue(mine.screenshot()) > 1000, "the colour test cannot see the agent's blue"

                # The page the browser started with, not a new one: a page Playwright creates opens in
                # the last window, the agent's, beside an agent tab that Chrome then names its opener.
                popup = context.pages[0] if context.pages else context.new_page()
                popup.goto(f"chrome-extension://{EXTENSION_ID}/panel.html")
                # The panel's policy forbids evaluating strings, so it is read through the DOM only. With
                # no panel credential (the stub hands none), the panel shows its settings.
                wait_for(lambda: popup.inner_text("#install") == "Stub", "the panel to show the installation")
                popup.click("#check")
                popup.wait_for_selector("#check-result[data-ok='true']", timeout=6000)
                text = popup.inner_text("#check-result")
                assert "✓" in text, text
                assert received("check"), "the panel's check never reached the host"
                # The operator's page stayed the operator's.
                joined = [f for f in received("tabs") if f["op"] in ("created", "joined")]
                assert not joined, joined
                if os.environ.get("SMOKE_DEBUG"):
                    print(json.dumps(frames(log), indent=1))
                if os.environ.get("SMOKE_SCREENSHOT"):
                    popup.set_viewport_size({"width": 340, "height": 760})
                    popup.screenshot(path=os.environ["SMOKE_SCREENSHOT"], full_page=True)
                print("smoke: hello/welcome, ping/pong, notify, tab.create, cdp allow and refuse, cursor and screenshot, check/check_ok: ok")
                print("panel:", text)
            finally:
                context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
