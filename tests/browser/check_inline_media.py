"""Inline answer media stays in the prose, opens above the app, and remains usable on a phone."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
UNHANDLED = Unhandled()
SESSION = "media-session"
ALBUM = "11111111-1111-4111-8111-111111111111"
VIDEO = "22222222-2222-4222-8222-222222222222"
AUDIO = "33333333-3333-4333-8333-333333333333"
IMAGE = "44444444-4444-4444-8444-444444444444"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def item(item_id: str, kind: str, filename: str, *, alt: str = "", caption: str = "") -> dict:
    return {"id": item_id, "kind": kind, "mime_type": "image/png" if kind == "image" else f"{kind}/mp4" if kind == "video" else "audio/mpeg", "filename": filename, "byte_size": len(PNG), "width": 640 if kind == "image" else None, "height": 480 if kind == "image" else None, "alt": alt, "caption": caption}


MEDIA = [
    {"id": ALBUM, "layout": "album", "items": [
        item("image-one", "image", "one.png", alt="First chart", caption="Before"),
        item("album-video", "video", "clip.webm", alt="Clip", caption="After"),
        item("image-three", "animation", "three.gif", alt="Third chart", caption="Detail"),
        item("image-four", "image", "four.png", alt="Fourth chart", caption="Result"),
    ]},
    {"id": VIDEO, "layout": "single", "items": [item("video-one", "video", "walkthrough.mp4", caption="Walkthrough")]},
    {"id": AUDIO, "layout": "single", "items": [item("audio-one", "audio", "summary.mp3", caption="Audio summary")]},
    {"id": IMAGE, "layout": "single", "items": [item("image-single", "image", "result.png", caption="Full result")]},
]
ANSWER = f"Before the album.\n\n![Charts](daedalus-media:{ALBUM})\n\nBetween the album and video.\n\n![Video](daedalus-media:{VIDEO})\n\n![Audio](daedalus-media:{AUDIO})\n\n![Result](daedalus-media:{IMAGE})\n\nAfter every attachment."
DETAIL = {
    "id": SESSION, "title": "Media answer", "status": "idle", "run_id": None,
    "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
    "workspace_sessions": [], "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
    "pending": None, "model": "some-model", "mode": "", "brief": "", "tools_off": [], "loop": None,
    "services": [], "subagents": [], "usage": {}, "context": {"tokens": 30, "window": 100000, "messages": 2, "summaries": 0, "operator_turns": 1},
    "messages": [
        {"role": "user", "summary": False, "internal": False, "origin": "operator", "seq": 10, "compaction": None, "archived": None, "headline": "", "text": "Show the result", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-21T00:00:00+00:00", "media": []},
        {"role": "assistant", "summary": False, "internal": False, "origin": "", "seq": 11, "compaction": None, "archived": None, "headline": "", "text": ANSWER, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-21T00:00:02+00:00", "media": MEDIA},
    ],
}


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    path = url.split("?", 1)[0]
    if path.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if "/media/" in path and path.endswith("/content"):
        if "album-video" in path and os.environ.get("MEDIA_TEST_VIDEO"):
            # Without a range response the element learns the duration and then refuses to seek.
            body = Path(os.environ["MEDIA_TEST_VIDEO"]).read_bytes()
            headers = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4"}
            requested = route.request.headers.get("range")
            if requested and requested.startswith("bytes="):
                spec = requested.split("=", 1)[1].split(",", 1)[0]
                start_s, _, end_s = spec.partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else len(body) - 1
                end = min(end, len(body) - 1)
                chunk = body[start:end + 1]
                headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                headers["Content-Length"] = str(len(chunk))
                return route.fulfill(status=206, headers=headers, body=chunk)
            headers["Content-Length"] = str(len(body))
            return route.fulfill(status=200, headers=headers, body=body)
        return route.fulfill(status=200, content_type="image/png", body=PNG)
    if path.endswith("/api/media/access"):
        return route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
    if "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif f"/api/sessions/{SESSION}" in url and "/events" not in url:
        body = json.dumps(DETAIL)
    elif url.rstrip("/").endswith("/api/sessions"):
        body = json.dumps({"sessions": [], "projects": []})
    else:
        rel = path[path.index("/api/"):] if "/api/" in path else ""
        if fulfil_shared(route):
            return
        if rel:
            UNHANDLED.record(rel)
        body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for name, width, height in (("phone", 390, 844), ("desktop", 1440, 900)):
            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=name == "phone", has_touch=name == "phone", permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
            page.wait_for_selector(".inline-media.album img", timeout=15000)
            page.wait_for_function("() => [...document.images].filter(i => i.closest('.inline-media')).every(i => i.complete && i.naturalWidth > 0)")
            album_images = page.locator(".inline-media.album img").count()
            players = (page.locator(".inline-media video").count(), page.locator(".inline-media audio").count())
            order = page.locator(".answer-with-media").evaluate("el => [...el.children].map(x => x.className.includes('inline-media') ? 'media' : x.textContent.trim()).filter(Boolean)")
            overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            geometry = page.locator(".inline-media.album").evaluate("el => { const box=el.getBoundingClientRect(), answer=el.closest('.answer').getBoundingClientRect(), rail=el.querySelector('.inline-media-track'), caption=el.querySelector('figcaption'), media=[...el.closest('.answer').querySelectorAll('.inline-media')], full=[...el.closest('.answer').querySelectorAll('.inline-media.single:has(.inline-media-image),.inline-media.single:has(.inline-media-player:not(.audio))')]; return { centre: Math.abs((box.left+box.right)/2-(answer.left+answer.right)/2), width: Math.abs(box.width-answer.width), siblings: media.map(item => Math.abs(item.getBoundingClientRect().width-answer.width)), content: full.map(item => Math.abs(item.firstElementChild.firstElementChild.getBoundingClientRect().width-answer.width)), scroll: rail.scrollWidth-rail.clientWidth, caption: getComputedStyle(caption).position } }")
            print(f"{name}: album={album_images}, players={players}, overflow={overflow}, geometry={geometry}, order={order}")
            if album_images != 3 or players != (2, 1):
                problems.append(f"{name}: expected three album images, two videos and audio; got {album_images}, {players}")
            frame = page.locator(".inline-media.album").evaluate("el => { const rail = el.querySelector('.inline-media-track'); const box = rail.getBoundingClientRect(); const tiles = [...rail.querySelectorAll('.inline-media-image')].map(t => t.getBoundingClientRect()); const visible = tiles.filter(t => (t.left + t.right) / 2 > box.left && (t.left + t.right) / 2 < box.right).length; const first = tiles[0]; return { visible, ratio: first.width / first.height }; }")
            print(f"{name}: frame={frame}")
            # Three across on a desktop, one card on a phone. The frame is the photo's own ratio
            # (640/480 here), so a video in the album is that tall and a wide picture is not padded.
            expected_visible = 3 if name == "desktop" else 1
            if frame["visible"] != expected_visible or abs(frame["ratio"] - 640 / 480) > 0.08:
                problems.append(f"{name}: album frame is not {expected_visible} tiles at the photo's ratio ({frame})")
            if name == "phone":
                photo = page.locator(".inline-media.single .inline-media-image").evaluate("el => { const box = el.getBoundingClientRect(); return { w: box.width, h: box.height }; }")
                # A landscape file used to be stretched to 70dvh and letterboxed. The box hugs the picture.
                if photo["h"] > photo["w"] * 1.2:
                    problems.append(f"phone: the photo box is taller than the picture ({photo})")
            if order[:3] != ["Before the album.", "media", "Between the album and video."] or order[-1] != "After every attachment.":
                problems.append(f"{name}: media did not retain its position in prose ({order})")
            if overflow > 1:
                problems.append(f"{name}: media made the page {overflow}px wider than the viewport")
            if geometry["centre"] > 2 or geometry["width"] > 2 or any(delta > 2 for delta in geometry["siblings"] + geometry["content"]) or geometry["scroll"] <= 0 or geometry["caption"] != "absolute":
                problems.append(f"{name}: media does not fill the answer column or album is not swipeable and overlaid ({geometry})")

            # Near the tile's corner, clear of its actions in the other one: with the column at 288 px
            # a 1440 window's album tiles are small enough that the centre falls on those buttons.
            page.locator(".inline-media.album .inline-media-surface").first.click(position={"x": 12, "y": 12})
            page.wait_for_selector(".media-viewer")
            page.wait_for_timeout(300)
            viewer = page.locator(".media-viewer").bounding_box()
            controls = page.locator(".media-viewer button, .media-viewer a").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
            page.locator(".media-viewer-stage").dblclick()
            zoom = page.locator(".lightbox-zoom").text_content()
            print(f"{name}: viewer={viewer}, controls={controls}, zoom={zoom}")
            if not viewer or viewer["width"] < width * 0.9 or viewer["height"] < height * 0.8:
                problems.append(f"{name}: viewer is not a full overlay ({viewer})")
            if any(value < 44 for value in controls):
                problems.append(f"{name}: viewer has a touch target below 44px ({controls})")
            if zoom != "200%":
                problems.append(f"{name}: double click did not zoom the image ({zoom})")
            page.keyboard.press("ArrowRight")
            player = page.locator(".media-viewer-stage video")
            assert player.count() == 1
            if os.environ.get("MEDIA_TEST_VIDEO"):
                page.wait_for_function("() => { const v = document.querySelector('.media-viewer-stage video'); return v && v.seekable.length > 0 && v.seekable.end(0) > 5; }")
                page.keyboard.press("ArrowRight")
                page.wait_for_function("() => document.querySelector('.media-viewer-stage video').currentTime >= 4.5")
                page.locator(".media-viewer").get_by_role("button", name="Full screen", exact=True).click()
                page.wait_for_function("() => document.fullscreenElement")
                page.keyboard.press("ArrowRight")
                page.wait_for_function("() => { const v = document.querySelector('.media-viewer-stage video'); return v && v.currentTime >= 9.5 && v.currentTime <= 10.5; }")
                page.evaluate("() => document.exitFullscreen()")
            else:
                page.keyboard.press("ArrowRight")
                if player.count() != 1:
                    problems.append(f"{name}: arrow left the video instead of seeking")
            page.locator(".media-viewer").get_by_role("button", name="Next", exact=True).click()
            assert page.locator(".media-viewer-stage video").count() == 0
            assert page.locator(".media-viewer-stage img").count() == 1
            page.keyboard.press("Escape")

            page.locator(".turn").last.hover()
            page.locator(".msg-actions").last.get_by_role("button", name="Copy", exact=True).click()
            copied = page.evaluate("() => navigator.clipboard.readText()")
            if "daedalus-media:" in copied or not all(label in copied for label in ("Before", "After", "Detail", "Result", "Walkthrough", "Audio summary", "Full result")):
                problems.append(f"{name}: copy exposed internal references or lost labels ({copied!r})")
            context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
