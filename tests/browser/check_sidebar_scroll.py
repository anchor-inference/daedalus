"""Scroll the Agents sidebar with every folder open and many chats in it: the page must survive.

    cd miniapp && npm run build
    python3 tests/browser/check_sidebar_scroll.py

A folder with more rows than a screen holds is windowed: only the rows near the viewport are drawn.
The window once measured the row at its edge differently from the same row inside it, so a small
scroll made the window's end and that measurement chase each other until React stopped the page with
"Maximum update depth exceeded". No other check scrolls a long sidebar, which is how it reached a
deployed build. This one scrolls it by small and by large steps, on the installation the sessions
performance harness invents, and fails on any page error.
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_sessions as harness  # noqa: E402


def main() -> int:
    if not (harness.DIST / "index.html").is_file():
        print("build the app first: cd miniapp && npm run build", file=sys.stderr)
        return 2
    harness.Stub.listing = harness.installation(300, 6)
    server = ThreadingHTTPServer(("127.0.0.1", 0), harness.Stub)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    problems: list[str] = []
    try:
        with sync_playwright() as p:
            chromium = os.environ.get("CHROMIUM")
            browser = p.chromium.launch(executable_path=chromium) if chromium else p.chromium.launch()
            for width, height in ((1440, 900), (1920, 1080), (1280, 700)):
                context = browser.new_context(viewport={"width": width, "height": height})
                keys = [project["id"] for project in harness.Stub.listing["projects"]] + ["\\u0000free"]
                context.add_init_script("try { " + " ".join(f"localStorage.setItem('daedalus.folder.{k}', '1');" for k in keys) + " } catch (e) {}")
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/app/agents", wait_until="commit")
                try:
                    page.wait_for_selector(".sb-kids > .sb-row", timeout=60_000)
                except Exception:  # noqa: BLE001
                    problems.append(f"{width}x{height}: the sidebar drew no chat row in a minute; page errors {errors[:1]}; body {page.evaluate('() => document.body.innerText.slice(0, 120)')!r}")
                    context.close()
                    continue
                # A folder draws six chats until asked for all of them; only the whole list is long
                # enough to be windowed, so every folder is opened in full first.
                page.evaluate("() => document.querySelectorAll('.sb-more').forEach((button) => button.click())")
                page.wait_for_timeout(300)
                box = page.locator(".sb-kids > .sb-row").first.bounding_box()
                assert box is not None
                page.mouse.move(box["x"] + 40, box["y"] + 4)
                # Small steps first: the fault needed the window's edge to cross a row, not a long throw.
                for step in [30] * 40 + [7] * 30 + [600] * 12 + [-45] * 40 + [-900] * 10:
                    page.mouse.wheel(0, step)
                    page.wait_for_timeout(16)
                page.wait_for_timeout(500)
                rows = page.locator(".sb-kids > .sb-row").count()
                if errors:
                    problems.append(f"{width}x{height}: the page raised {errors[0][:160]}")
                if rows == 0:
                    problems.append(f"{width}x{height}: no chat row is left in the sidebar after scrolling")
                context.close()
            browser.close()
    finally:
        server.shutdown()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
