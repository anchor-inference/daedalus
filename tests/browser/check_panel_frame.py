"""The side panel in the place it really runs: framed by an extension, cross-site, under the app's service worker.

``check_side_panel.py`` is a page on its own with its requests answered inside the browser, which is
quick and misses what broke the panel twice: a real Chrome's requests are not the ones assumed. So
this one loads an extension with the real one's key, hence its id and origin, whose page frames
``/app/panel`` exactly as the real panel page does (the same ``sandbox`` and ``allow``), and answers the
handshake. A real HTTP server plays the host, keeping its rule that a panel route takes the panel's
credential and nothing else opens it, and writes down what actually arrived. Then, with the app's
own service worker controlling the frame:

- every picture, GIF and album item is drawn from a blob, with its bytes; an element's own request is
  refused, which is why;
- a document is a card that opens in the panel and saves, and a clip and a voice note play on a press;
- what reached the host for all of that carried ``Authorization: Panel …`` and no cookie, through
  the panel's door only, and the cookie upgrade was never asked for.

What this cannot stand in for: the extension's real relay and a host's real token (the unit tests
hold the door), and Chrome's side panel container, which a script cannot open; the page runs in a tab.

    cd miniapp && npm run build && cd ..
    CHROMIUM=... python3 tests/browser/check_panel_frame.py

Exit 0 when every step holds. A port is the system's choice: nothing here serves on a fixed one.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("APP_DIST", ROOT / "miniapp" / "dist"))
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = Path(os.environ.get("SHOTS", "/tmp/side-panel-shots"))
EXTENSION_ID = "lkhhklicnjlplokpadpccndipomikoaj"
# The real extension's public key, which is what makes this one's id, and so its origin, the real id.
EXTENSION_KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAplP1rwlqrvewb62Pu3s8mFau+6kjNQMrqeOADm8HN+IVbozjE7IEcG8O14jq+aTQQ1V6J0AdcSQ7vqaUaNqY77SH5qJ2M1VQZsNhQB3px/DVg52ZgRlRkcob58YFtGxqcSuVBDgHH+11G05BFuhVtQaRmxHqNZPwwYlE73krKZ+6laIWPtMflDniQZoq0RHEc7RYh4o2iwU6rN3EExd8BQ3QbAzIYU4gsY81OGRaITowWPCWSF+JSfbvzPoFQSnB14T37WAtTznJ+wEMUh5htS+hqZOxlCqR7aE4Cv5yh1W8kKvmAI255Cx1GhVbgJE5u7sUNBOG3Cyg/agy2tCkqQIDAQAB"
TOKEN = "pnl_check"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAEklEQVR4nGNQSFiABzGMSmNDAPCjcIG5l6deAAAAAElFTkSuQmCC")
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
IMAGE, GIFT, ALBUM, CLIP, VOICE, FILE = (f"{n * 8}-{n * 4}-{n * 4}-{n * 4}-{n * 12}" for n in "abcdef")
UNIT = "Подбор модели: условия и цены"


def sound() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as note:
        note.setnchannels(1)
        note.setsampwidth(2)
        note.setframerate(8000)
        note.writeframes(b"".join(struct.pack("<h", (i % 80 - 40) * 200) for i in range(8000)))
    return out.getvalue()


def clip() -> bytes:
    """A second of real video, or nothing where this machine has no encoder."""
    if not shutil.which("ffmpeg"):
        return b""
    made = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10", "-c:v", "libvpx", "-f", "webm", "pipe:1"], capture_output=True, check=False)
    return made.stdout if made.returncode == 0 else b""


def item(item_id: str, kind: str, mime: str, filename: str, body: bytes, *, alt: str = "", caption: str = "") -> dict:
    return {"id": item_id, "kind": kind, "mime_type": mime, "filename": filename, "byte_size": len(body), "width": 10 if kind in ("image", "animation") else None, "height": 10 if kind in ("image", "animation") else None, "alt": alt, "caption": caption, "url": "", "_body": body}


VIDEO = clip()
CARRIED = [
    {"id": IMAGE, "layout": "single", "items": [item("i1", "image", "image/png", "shot.png", PNG, alt="The bill")]},
    {"id": GIFT, "layout": "single", "items": [item("g1", "animation", "image/gif", "wave.gif", GIF, alt="A wave")]},
    {"id": ALBUM, "layout": "album", "items": [item("a1", "image", "image/png", "one.png", PNG, alt="One"), item("a2", "image", "image/png", "two.png", PNG, alt="Two")]},
    {"id": CLIP, "layout": "single", "items": [item("c1", "video", "video/webm", "walk.webm", VIDEO, caption="The walkthrough")]},
    {"id": VOICE, "layout": "single", "items": [item("v1", "audio", "audio/wav", "note.wav", sound(), caption="The voice note")]},
    {"id": FILE, "layout": "single", "items": [item("f1", "file", "text/markdown", "station-pick.md", b"# Station pick\n\nThe whole comparison.\n", alt=UNIT, caption="The whole comparison")]},
]


def public(presentation: dict) -> dict:
    return {**presentation, "items": [{k: v for k, v in i.items() if not k.startswith("_")} for i in presentation["items"]]}


def detail() -> dict:
    lines = "\n\n".join(f"![{p['items'][0]['alt'] or 'x'}](daedalus-media:{p['id']})" for p in CARRIED)
    file = next(p for p in CARRIED if p["id"] == FILE)
    return {
        "id": "s1", "title": "What I found", "status": "idle", "model": "Claude Opus", "workspace": "/w", "folders": [], "pending": None, "thinking": True, "reasoning_effort": "medium", "open_approvals": [],
        "messages": [
            {"role": "user", "text": "Show me", "thinking": "", "created_at": "2026-10-20T10:00:00Z", "seq": 1, "tool_calls": [], "tool_results": []},
            {"role": "assistant", "text": f"Here.\n\n{lines}\n\nThat is all.", "thinking": "", "created_at": "2026-10-20T10:00:02Z", "seq": 2, "tool_calls": [], "tool_results": [], "media": [public(p) for p in CARRIED]},
            {"role": "user", "text": "Add the prices", "thinking": "", "created_at": "2026-10-20T10:01:00Z", "seq": 3, "tool_calls": [], "tool_results": []},
            {"role": "assistant", "text": f"Done.\n\n![{UNIT}](daedalus-media:{FILE})", "thinking": "", "created_at": "2026-10-20T10:01:02Z", "seq": 4, "tool_calls": [], "tool_results": [], "media": [public(file)]},
        ],
    }


class Host(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args: object) -> None:
        pass

    def send(self, status: int, body: bytes, content_type: str, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status: int, body: object) -> None:
        self.send(status, json.dumps(body).encode(), "application/json")

    def do_POST(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path.startswith("/api/"):
            Host.seen.append({"method": self.command, "path": path, "probe": "bare" in url.query, "auth": self.headers.get("authorization", ""), "cookie": self.headers.get("cookie", ""),
                              "site": self.headers.get("sec-fetch-site", ""), "dest": self.headers.get("sec-fetch-dest", "")})
            return self.api(path, parse_qs(url.query))
        file = DIST / path.removeprefix("/app/")
        if not (path.startswith("/app/") and file.is_file()):
            file = DIST / "index.html"
        # As the host answers: only the extension may frame the panel's page.
        policy = f"frame-ancestors chrome-extension://{EXTENSION_ID}" if path.rstrip("/") == "/app/panel" else "frame-ancestors 'self'"
        extra = {"Service_Worker_Allowed": "/app/"} if path.endswith("sw.js") else {}
        return self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream", Content_Security_Policy=policy, **extra)

    def api(self, path: str, query: dict) -> None:
        # Nothing but the panel's door takes the panel's credential, and it takes nothing else.
        if not path.startswith("/api/panel/") or self.headers.get("authorization") != f"Panel {TOKEN}":
            return self.json(401, {"detail": "authentication required"})
        if path == "/api/panel/sessions":
            return self.json(200, {"sessions": [{"id": "s1", "title": "What I found", "status": "idle", "last_message_at": "2026-10-20T10:01:02Z", "needs_you": False, "group": "s-s1-u"}],
                                   "consented": True, "consent": "", "chrome": {"connected": True, "name": "Work"}})
        if path == "/api/panel/settings":
            return self.json(200, {"presets": {}, "providers": {}, "model": ""})
        if path == "/api/panel/asr":
            return self.json(200, {"configured": False, "reason": "", "provider": "", "model": "", "max_seconds": 0, "autosend": False})
        parts = path.split("/")
        if len(parts) < 5 or parts[4] != "s1":
            return self.json(404, {"detail": "no such conversation in this Chrome"})
        tail = "/".join(parts[5:])
        if tail == "":
            return self.json(200, detail())
        if tail == "stream":
            return self.send(200, b'event: hello\ndata: {"event_seq": 0}\n\n', "text/event-stream")
        if tail == "questions":
            return self.json(200, {"questions": []})
        if tail == "steer":
            return self.json(200, [])
        if tail == "browser-approvals":
            return self.json(200, {"mode": "manual", "env": "user", "warning": ""})
        if tail.startswith("media/"):
            found = {p["id"]: {i["id"]: i for i in p["items"]} for p in CARRIED}.get(parts[6], {})
            wanted = found.get(parts[7]) if parts[7] != "download" else found.get(query.get("path", [""])[0].split("/", 1)[0])
            if wanted is None:
                return self.json(404, {"detail": "no such media"})
            disposition = "attachment" if parts[7] == "download" or wanted["kind"] == "file" else "inline"
            return self.send(200, wanted["_body"], wanted["mime_type"], Content_Disposition=f'{disposition}; filename="{wanted["filename"]}"', Cache_Control="private, max-age=31536000, immutable", X_Content_Type_Options="nosniff")
        return self.json(200, {})


# The real panel page's frame, attribute for attribute, and its half of the handshake.
PANEL_HTML = """<!doctype html><meta charset="utf-8"><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block}</style>
<iframe id="frame" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads" allow="clipboard-read; clipboard-write; microphone"></iframe>
<script src="panel.js"></script>"""
PANEL_JS = """
const origin = decodeURIComponent(location.hash.slice(1));
const frame = document.getElementById("frame");
window.__ops = [];
window.addEventListener("message", (event) => {
  if (event.source !== frame.contentWindow || event.origin !== origin) return;
  const data = event.data;
  if (!data || data.t !== "DAEDALUS_PANEL_READY") return;
  const channel = new MessageChannel();
  channel.port1.onmessage = (message) => {
    const { id, op } = message.data;
    window.__ops.push(op);
    channel.port1.postMessage({ id, ok: true, value: op === "tab.current" ? { tab: null, owner: null } : {} });
  };
  const current = { tab: { id: 7, url: "https://shop.example/", title: "Found", readable: true }, owner: { group: "s-s1-u", session: "s1", title: "Found", paused: false } };
  frame.contentWindow.postMessage({ t: "DAEDALUS_PANEL_HELLO", proto: 1, nonce: data.nonce, generation: 1, token: "__TOKEN__", expires: "", lang: "en", current, show: null, profile: "Work", connected: true }, origin, [channel.port2]);
});
frame.src = origin + "/app/panel";
""".replace("__TOKEN__", TOKEN)


def extension(folder: Path) -> None:
    (folder / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "panel check", "version": "1.0", "key": EXTENSION_KEY}))
    (folder / "panel.html").write_text(PANEL_HTML)
    (folder / "panel.js").write_text(PANEL_JS)


def app_frame(page, origin: str):  # type: ignore[no-untyped-def]
    return next(f for f in page.frames if f.url.startswith(origin))


def drawn(page, frame, shot: str) -> None:  # type: ignore[no-untyped-def]
    """Everything the conversation carries is on the screen, as what it is."""
    timeline = frame.locator(".timeline")
    expect(timeline.locator(".inline-media-image img")).to_have_count(4, timeout=15000)
    inside = next(f for f in page.frames if "/app/panel" in f.url)
    # Lazy like any picture in the chat: each is brought into view to be read.
    for index in range(4):
        timeline.locator(".inline-media-image img").nth(index).scroll_into_view_if_needed()
    try:
        inside.wait_for_function("[...document.querySelectorAll('.timeline .inline-media-image img')].every((i) => i.src.startsWith('blob:') && i.naturalWidth > 0)", timeout=8000)
    except Exception:
        print(inside.evaluate("[...document.querySelectorAll('.timeline .inline-media-image img')].map((i) => [i.src, i.complete, i.naturalWidth, i.loading])"))
        raise
    expect(timeline.locator(".inline-file .artifact-name")).to_have_count(2)
    text = timeline.inner_text()
    assert "daedalus-media:" not in text and "![" not in text, text
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / shot), full_page=True)


def run() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Host)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    with tempfile.TemporaryDirectory() as work, sync_playwright() as p:
        folder = Path(work) / "extension"
        folder.mkdir()
        extension(folder)
        context = p.chromium.launch_persistent_context(
            str(Path(work) / "profile"), executable_path=CHROMIUM, headless=False, accept_downloads=True, viewport={"width": 400, "height": 760},
            args=["--headless=new", f"--disable-extensions-except={folder}", f"--load-extension={folder}", "--no-first-run"],
        )
        # A cookie for the host in this browser, as the operator's own app would leave: it must not travel.
        context.add_cookies([{"name": "daedalus_session", "value": "operator", "url": origin, "sameSite": "Lax"}])
        page = context.new_page()
        page.goto(f"chrome-extension://{EXTENSION_ID}/panel.html#{origin}")
        frame = page.frame_locator("#frame")
        expect(frame.locator(".sp-title")).to_contain_text("What I found", timeout=20000)
        inside = app_frame(page, origin)
        assert inside.evaluate("window.parent !== window"), "the app is not framed"
        drawn(page, frame, "panel-frame-first.png")

        # The app's own service worker takes the frame. It registers itself on https only, so here
        # the page asks for it; the frame then reloads, as a panel opened later does, under it.
        inside.evaluate("navigator.serviceWorker.register('/app/sw.js', { scope: '/app/' }).then(() => navigator.serviceWorker.ready).then(() => true)")
        inside.evaluate("location.reload()")
        expect(frame.locator(".sp-title")).to_contain_text("What I found", timeout=20000)
        inside = app_frame(page, origin)
        inside.wait_for_function("!!navigator.serviceWorker.controller")
        del Host.seen[:]
        inside.evaluate("location.reload()")
        expect(frame.locator(".sp-title")).to_contain_text("What I found", timeout=20000)
        inside = app_frame(page, origin)
        inside.wait_for_function("!!navigator.serviceWorker.controller")
        drawn(page, frame, "panel-frame-under-worker.png")
        timeline = frame.locator(".timeline")

        # An element's own request gets nothing: this is why the bytes come through a fetch.
        refused = inside.evaluate("""(url) => new Promise((done) => { const i = new Image(); i.onload = () => done(false); i.onerror = () => done(true); i.src = url; })""", f"/api/panel/sessions/s1/media/{IMAGE}/i1/content?bare=1")
        assert refused, "an <img> reached the media without the credential"

        # A document opens in the panel and saves under its name.
        timeline.locator(".inline-file .artifact-main").first.click()
        expect(frame.locator(".preview")).to_contain_text("The whole comparison")
        page.screenshot(path=str(SHOTS / "panel-frame-preview.png"))
        page.keyboard.press("Escape")
        with page.expect_download() as saved:
            timeline.locator(".inline-file .artifact-actions a").first.click()
        assert saved.value.suggested_filename == "station-pick.md", saved.value.suggested_filename
        assert Path(saved.value.path()).read_bytes().startswith(b"# Station pick"), "the saved file is not the file"
        with page.expect_download() as saved:
            timeline.locator(".inline-media-actions a").first.click()
        assert saved.value.suggested_filename == "shot.png", saved.value.suggested_filename
        assert Path(saved.value.path()).read_bytes() == PNG, "the saved picture is not the picture"

        # The picture opens large from the same blob route.
        timeline.locator(".inline-media-surface").first.click()
        expect(frame.locator(".media-viewer img")).to_be_visible()
        inside.wait_for_function("document.querySelector('.media-viewer img')?.naturalWidth > 0")
        page.keyboard.press("Escape")

        # A clip and a voice note on a press, and they do play.
        loads = timeline.locator(".inline-media-load")
        expect(loads).to_have_count(2)
        before = len(Host.seen)
        audio_press = timeline.locator(".inline-media-load", has_text="The voice note")
        audio_press.click()
        inside.wait_for_function("(() => { const a = document.querySelector('.timeline audio'); return a && a.src.startsWith('blob:') && a.duration > 0; })()", timeout=15000)
        if VIDEO:
            timeline.locator(".inline-media-load", has_text="The walkthrough").click()
            inside.wait_for_function("(() => { const v = document.querySelector('.timeline .video-player video'); return v && v.src.startsWith('blob:') && v.videoWidth > 0; })()", timeout=15000)
        assert len(Host.seen) > before, "the press read nothing"

        sent = [r for r in Host.seen if not r["probe"]]
        media = [r for r in sent if "/media/" in r["path"]]
        assert media, "no media request reached the host"
        print(f"media requests that reached the host: {len(media)}; one of them: {media[0]}")
        assert not [r for r in sent if r["dest"] in ("image", "video", "audio")], "an element asked for media by itself"
        assert all(r["auth"] == f"Panel {TOKEN}" for r in sent), [r for r in sent if r["auth"] != f"Panel {TOKEN}"]
        assert not any(r["cookie"] for r in Host.seen), [r for r in Host.seen if r["cookie"]]
        foreign = [r for r in Host.seen if not r["path"].startswith("/api/panel/")]
        assert not foreign, foreign
        assert not any(r["path"] == "/api/media/access" for r in Host.seen), "the cookie upgrade was asked for"
        wide = inside.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert wide <= 0, f"the panel scrolls sideways by {wide}px"
        context.close()
    server.shutdown()
    print(f"panel frame: ok (pictures in {SHOTS}; clip {'played' if VIDEO else 'not played, no encoder here'})")
    return 0


if __name__ == "__main__":
    sys.exit(run())
