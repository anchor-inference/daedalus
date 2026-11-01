"""The pictures an MCP tool returned show under its step, are fetched by the call, and open in the viewer."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")
UNHANDLED = Unhandled()
SESSION = "tool-pictures"
CALL = "call_shot_1"


def _screenshot() -> bytes:
    """A page-sized picture, so the thumbnails take the size a real screenshot would."""
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (640, 400), (242, 238, 230))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 0, 640, 56), fill=(34, 74, 92))
    draw.rectangle((40, 110, 400, 150), fill=(60, 60, 60))
    draw.rectangle((40, 170, 300, 190), fill=(150, 150, 150))
    draw.rounded_rectangle((40, 230, 200, 276), radius=10, fill=(214, 120, 60))
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _screenshot()


def message(role: str, seq: int, **fields: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "archived": None,
            "headline": "", "text": "", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-11-01T10:00:0{seq % 10}+00:00", "media": [], **fields}


DETAIL = {
    "id": SESSION, "title": "Page check", "status": "idle", "run_id": None,
    "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
    "workspace_sessions": [], "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
    "pending": None, "model": "some-model", "brief": "", "tools_off": [], "loop": None,
    "services": [], "subagents": [], "usage": {}, "context": {"tokens": 30, "window": 100000, "messages": 4, "summaries": 0, "operator_turns": 1},
    "messages": [
        message("user", 1, text="How does the landing page look?"),
        message("assistant", 2, tool_calls=[{"id": CALL, "name": "Mcp_Browser_screenshot", "arguments": {"url": "https://example.com"}}]),
        message("tool", 3, tool_results=[{"id": CALL, "content": "[image image/png, 68 bytes, returned by the tool as picture 1]", "is_error": False,
                                          "images": [{"mime_type": "image/png"}, {"mime_type": "image/png", "uri": "file:///hero.png"}]}]),
        message("assistant", 4, text="The hero section renders as expected."),
    ],
}


def stub(fetched: list[str]):  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url
        path = url.split("?", 1)[0]
        if path.endswith("/stream"):
            return r.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
        if path.endswith(f"/api/sessions/{SESSION}/tool-results/{CALL}/images/download"):
            fetched.append(url.split("path=", 1)[1].split("&", 1)[0])
            return r.fulfill(status=200, content_type="image/png", body=PNG)
        if "auth/me" in url:
            body = json.dumps({"user_id": 1, "via": "token"})
        elif f"/api/sessions/{SESSION}" in url and "/events" not in url:
            body = json.dumps(DETAIL)
        elif url.rstrip("/").endswith("/api/sessions"):
            body = json.dumps({"sessions": [], "projects": []})
        else:
            if fulfil_shared(r):
                return None
            if "/api/" in path:
                UNHANDLED.record(path[path.index("/api/"):])
            body = "[]"
        return r.fulfill(status=200, content_type="application/json", body=body)
    return route


def run() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for name, width, height in (("desktop", 1440, 900), ("phone", 390, 844)):
            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=name == "phone", has_touch=name == "phone")
            page = context.new_page()
            page.set_default_timeout(10000)
            fetched: list[str] = []
            page.route("**/api/**", stub(fetched))
            page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en")
            pictures = page.locator("[data-tool-images] img.tool-image")
            # A finished turn folds its steps; opened, the step shows its pictures without being
            # opened itself, the way ImageView's picture is shown.
            page.get_by_text("Worked for", exact=False).first.click()
            expect(pictures).to_have_count(2)
            page.wait_for_function("() => [...document.querySelectorAll('[data-tool-images] img')].every(i => i.complete && i.naturalWidth > 0)")
            assert sorted(fetched) == ["image-1.png", "image-2.png"], fetched
            expect(pictures.nth(1)).to_have_attribute("alt", "file:///hero.png")
            expect(pictures.nth(0)).to_have_attribute("alt", "Picture 1 the tool returned")
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 1, f"{name}: the page scrolls sideways by {overflow}px"
            if SHOTS:
                Path(SHOTS).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(SHOTS) / f"tool-images-{name}.png"))
            pictures.first.click()
            expect(page.locator(".media-viewer")).to_be_visible()
            context.close()
            print(f"{name}: two pictures under the step, fetched by the call, opened in the viewer")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
