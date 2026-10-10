"""Pictures and clips in the agent's steps open in the centred viewer, in a real browser.

A press on the image an ImageView step looked at used to open the file in the side panel, where a
code file belongs and nobody looks for a screenshot. This drives a turn whose steps viewed two images
and sent a clip, and refuses anything but the viewer: the press opens the full-window lightbox and
leaves the panel shut, the arrows page through the conversation's media in order, the clip plays in
the viewer, and the viewer's "Open in the files panel" still reaches the panel. A document the turn
sent keeps opening in the panel. Desktop and phone; on the phone a swipe down closes the viewer.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8701 /tmp/app-root &
    APP_URL=http://127.0.0.1:8701/app python3 tests/browser/check_media_viewer.py

The clip is made with ffmpeg when it is installed, or read from MEDIA_TEST_VIDEO; without either the
play step is reported as skipped. Exit 0 when every step holds.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
UNHANDLED = Unhandled()
SESSION = "viewer-session"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
STAMP = "2026-09-21T00:00:00+00:00"


def message(seq: int, role: str, text: str = "", calls: list | None = None, results: list | None = None) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "archived": None, "headline": "", "text": text, "thinking": "", "tool_calls": calls or [], "tool_results": results or [], "created_at": STAMP, "media": []}


VIEWS = [{"id": "v1", "name": "ImageView", "arguments": {"path": "/workspace/shots/first.png", "question": "what is on it"}},
         {"id": "v2", "name": "ImageView", "arguments": {"path": "/workspace/shots/second.png", "question": "and here"}}]
SENDS = [{"id": "s1", "name": "SendFile", "arguments": {"path": "/workspace/out/clip.webm"}},
         {"id": "s2", "name": "SendFile", "arguments": {"path": "/workspace/out/report.md"}}]
DETAIL = {
    "id": SESSION, "title": "Screens", "status": "idle", "run_id": None,
    "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
    "workspace_sessions": [], "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
    "pending": None, "model": "some-model", "mode": "", "brief": "", "tools_off": [], "loop": None,
    "services": [], "subagents": [], "usage": {}, "context": {"tokens": 30, "window": 100000, "messages": 6, "summaries": 0, "operator_turns": 1},
    "messages": [
        message(10, "user", "Look at the screens and send me the clip"),
        message(11, "assistant", calls=VIEWS),
        message(12, "tool", results=[{"id": c["id"], "content": "a login form", "is_error": False} for c in VIEWS]),
        message(13, "assistant", calls=SENDS),
        message(14, "tool", results=[{"id": "s1", "content": "sent clip.webm (4096 bytes): delivered", "is_error": False}, {"id": "s2", "content": "sent report.md (20 bytes): delivered", "is_error": False}]),
        message(15, "assistant", "Both screens show the login form; the clip is attached."),
    ],
}


def make_clip() -> bytes | None:
    if os.environ.get("MEDIA_TEST_VIDEO"):
        return Path(os.environ["MEDIA_TEST_VIDEO"]).read_bytes()
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "clip.webm"
        made = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10", "-c:v", "libvpx", "-b:v", "200k", str(out)], check=False)
        return out.read_bytes() if made.returncode == 0 and out.exists() else None


def ranged(route, body: bytes, kind: str) -> None:  # type: ignore[no-untyped-def]
    # Without a range response the element learns the duration and then refuses to seek or play on.
    headers = {"Accept-Ranges": "bytes", "Content-Type": kind}
    requested = route.request.headers.get("range")
    if requested and requested.startswith("bytes="):
        start_s, _, end_s = requested.split("=", 1)[1].split(",", 1)[0].partition("-")
        start = int(start_s) if start_s else 0
        end = min(int(end_s) if end_s else len(body) - 1, len(body) - 1)
        headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
        headers["Content-Length"] = str(end - start + 1)
        return route.fulfill(status=206, headers=headers, body=body[start:end + 1])
    headers["Content-Length"] = str(len(body))
    return route.fulfill(status=200, headers=headers, body=body)


def make_stub(clip: bytes | None, fetched: list[str]):  # type: ignore[no-untyped-def]
    def stub(route) -> None:  # type: ignore[no-untyped-def]
        url = route.request.url
        path = url.split("?", 1)[0]
        if path.endswith("/stream"):
            return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
        if path.endswith("/download") and f"/api/sessions/{SESSION}" in path:
            fetched.append(url[url.index("/api/"):])
            if path.endswith("/sent/s1/download"):
                return ranged(route, clip or b"", "video/webm")
            if path.endswith("/sent/s2/download"):
                return route.fulfill(status=200, content_type="text/markdown", body="# Report\n\nAll good.\n")
            return route.fulfill(status=200, content_type="image/png", body=PNG)
        if "auth/me" in url:
            body = json.dumps({"user_id": 1, "via": "token"})
        elif path.endswith(f"/api/sessions/{SESSION}"):
            body = json.dumps(DETAIL)
        elif path.endswith(f"/api/sessions/{SESSION}/checkpoints"):
            body = json.dumps({"checkpoints": [], "total": 0, "pruned": False, "pruned_before": None, "removed": 0, "note": "", "keep_days": 30, "keep_last": 50})
        elif path.endswith(f"/api/sessions/{SESSION}/files"):
            body = json.dumps({"path": "", "entries": []})
        elif url.rstrip("/").endswith("/api/sessions"):
            body = json.dumps({"sessions": [], "projects": []})
        else:
            if fulfil_shared(route):
                return
            rel = path[path.index("/api/"):] if "/api/" in path else ""
            if rel and "/events" not in rel:
                UNHANDLED.record(rel)
            body = "[]"
        route.fulfill(status=200, content_type="application/json", body=body)
    return stub


def swipe(page: Page, direction: int) -> None:
    """A horizontal drag across the stage: -1 to the next item, 1 to the previous one."""
    stage = page.locator(".media-viewer-stage").bounding_box() or {"x": 0, "y": 0, "width": 0, "height": 0}
    y = stage["y"] + stage["height"] / 2
    left, right = stage["x"] + stage["width"] * 0.2, stage["x"] + stage["width"] * 0.8
    page.mouse.move(right if direction < 0 else left, y)
    page.mouse.down()
    page.mouse.move(left if direction < 0 else right, y, steps=6)
    page.mouse.up()


def panel_open(page: Page) -> bool:
    return bool(page.evaluate("() => !!document.querySelector('.panel-crumbs, .preview-sheet, .panel .preview')"))


def check(page: Page, name: str, clip: bytes | None, fetched: list[str]) -> list[str]:
    problems: list[str] = []
    page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
    page.locator(".thinking-head").last.click()
    # Two views in a row fold into one "Called ImageView 2 times" line; its pictures are inside.
    page.locator(".activity .act.head", has_text="ImageView").first.click()
    thumbs = page.locator(".tool-attachment .tool-image")
    thumbs.first.wait_for(timeout=5000)
    page.wait_for_function("() => [...document.querySelectorAll('.tool-attachment img')].length === 2")
    thumbs.first.click()
    viewer = page.locator(".media-viewer.lightbox")
    try:
        viewer.wait_for(timeout=4000)
    except Exception:
        problems.append(f"{name}: the ImageView picture did not open the centred viewer")
        return problems
    page.wait_for_function("() => { const i = document.querySelector('.media-viewer-stage img'); return i && i.complete && i.naturalWidth > 0; }")
    box = viewer.bounding_box() or {"width": 0, "height": 0}
    size = page.viewport_size or {"width": 0, "height": 0}
    count = page.locator(".lightbox-count").inner_text()
    title = page.locator(".lightbox-title").inner_text()
    print(f"{name}: viewer={box} count={count!r} title={title!r} panel={panel_open(page)}")
    if box["width"] < size["width"] * 0.95 or box["height"] < size["height"] * 0.95:
        problems.append(f"{name}: the viewer does not cover the window ({box})")
    if panel_open(page):
        problems.append(f"{name}: the side panel opened as well as the viewer")
    # The two viewed images, then the sent clip; the sent document is not media.
    if count != "1/3" or title != "first.png":
        problems.append(f"{name}: the viewer did not start at the first of three media ({count!r}, {title!r})")
    if not any("/download?path=shots%2Ffirst.png" in url for url in fetched):
        problems.append(f"{name}: the viewer did not read the file where the panel does ({fetched})")

    if name == "desktop":
        page.keyboard.press("ArrowRight")
    else:
        swipe(page, -1)
        page.wait_for_timeout(200)
        swipe(page, 1)
        page.wait_for_timeout(200)
        if page.locator(".lightbox-title").inner_text() != "first.png":
            problems.append(f"{name}: a swipe to the right did not return to the first picture")
        swipe(page, -1)
    page.wait_for_timeout(200)
    if page.locator(".lightbox-title").inner_text() != "second.png":
        problems.append(f"{name}: paging did not move to the second picture")
    page.locator(".media-viewer").get_by_role("button", name="Next", exact=True).click()
    video = page.locator(".media-viewer-stage video")
    if video.count() != 1 or page.locator(".lightbox-title").inner_text() != "clip.webm":
        problems.append(f"{name}: the sent clip is not the third item of the viewer")
    elif clip is None:
        print(f"{name}: no clip to play (no ffmpeg, no MEDIA_TEST_VIDEO): play step skipped")
    else:
        page.locator(".media-viewer").get_by_role("button", name="Play", exact=True).first.click()
        try:
            page.wait_for_function("() => { const v = document.querySelector('.media-viewer-stage video'); return v && !v.paused && v.currentTime > 0.3; }", timeout=6000)
        except Exception:
            state = page.evaluate("() => { const v = document.querySelector('.media-viewer-stage video'); return v && { paused: v.paused, t: v.currentTime, error: v.error && v.error.code, src: v.currentSrc }; }")
            problems.append(f"{name}: the clip did not play in the viewer ({state})")
    if name == "desktop":
        page.locator(".media-viewer").get_by_role("button", name="Previous", exact=True).click()
    else:
        # A phone has no "previous" edge button, and the clip's stage belongs to its player: the
        # way back to a picture is to close the clip and tap the picture.
        page.keyboard.press("Escape")
        page.locator(".tool-attachment .tool-image").nth(1).click()
        page.locator(".media-viewer.lightbox").wait_for(timeout=4000)
    page.wait_for_timeout(200)
    if page.locator(".lightbox-title").inner_text() != "second.png":
        problems.append(f"{name}: did not come back to the second picture")

    # The old route stays one press away.
    page.locator(".media-viewer").get_by_role("button", name="Open in the files panel", exact=True).click()
    page.wait_for_timeout(600)
    if viewer.count():
        problems.append(f"{name}: the viewer stayed open after asking for the panel")
    crumbs = page.evaluate("() => document.body.innerText")
    if not panel_open(page) or "second.png" not in crumbs:
        problems.append(f"{name}: 'Open in the files panel' did not open the picture in the panel")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if name == "phone":
        back = page.locator(".preview-sheet button[aria-label='Close'], .sheet button[aria-label='Close']")
        if back.count():
            back.first.click()
            page.wait_for_timeout(300)
    return problems


def phone_swipe_close(page: Page) -> list[str]:
    page.locator(".tool-attachment .tool-image").first.click()
    page.locator(".media-viewer.lightbox").wait_for(timeout=4000)
    stage = page.locator(".media-viewer-stage").bounding_box() or {"x": 0, "y": 0, "width": 0, "height": 0}
    x = stage["x"] + stage["width"] / 2
    page.mouse.move(x, stage["y"] + stage["height"] * 0.3)
    page.mouse.down()
    page.mouse.move(x, stage["y"] + stage["height"] * 0.75, steps=8)
    page.mouse.up()
    page.wait_for_timeout(300)
    return ["phone: a swipe down did not close the viewer"] if page.locator(".media-viewer.lightbox").count() else []


def document_stays_in_panel(page: Page, name: str) -> list[str]:
    card = page.locator(".artifact", has_text="report.md").locator(".artifact-main")
    card.click()
    page.wait_for_timeout(600)
    if page.locator(".media-viewer.lightbox").count():
        return [f"{name}: a document opened in the media viewer"]
    if not panel_open(page):
        return [f"{name}: a sent document no longer opens in the panel"]
    return []


def run() -> int:
    problems: list[str] = []
    clip = make_clip()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, args=["--autoplay-policy=no-user-gesture-required"])
        for name, width, height in (("desktop", 1440, 900), ("phone", 390, 844)):
            fetched: list[str] = []
            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=name == "phone", has_touch=name == "phone")
            page = context.new_page()
            page.route("**/api/**", make_stub(clip, fetched))
            page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
            found = check(page, name, clip, fetched)
            if name == "phone" and not found:
                found += phone_swipe_close(page)
            if not found:
                page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
                page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
                found += document_stays_in_panel(page, name)
            problems += found
            context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
