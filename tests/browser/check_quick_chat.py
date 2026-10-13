"""The desktop's Quick Chat page, its appshot, its settings and the tray's status, against a stubbed API.

The shell opens /app/quick in a small frameless window and binds window.daedalus.quick there; the main
window's app gets window.daedalus.desk. Both bridges are faked here, recording what the page asks of
the shell, and the API answers what the companion's contract says. What is checked:

- dictation is offered where the host can transcribe;
- the companion is the default target: Enter sends the words to POST /api/companion/message, the
  confirmation line names it, and "Open" asks the shell to show its conversation;
- `$` picks a skill and the message asks for it in words; `@` attaches a conversation, and a message
  to the last chat goes to its own messages route as a follow-up, with the context listed by id;
- the appshot button asks the shell for a picture, which is previewed and uploaded through the
  companion's session; a window can be chosen from thumbnails;
- Esc closes an open list first and hides the window only then;
- with GET /api/companion answering 404 the companion is no target at all;
- signed out, the page says so and offers the main window; the words follow ?lang=ru;
- Settings → Desktop app is listed only in the desktop window, records a shortcut from the physical
  key and shows the Wayland note; the app in the main window reports what runs to the tray, does the
  tray's Pause, and moves to the conversation Quick Chat's "open" names.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_quick_chat.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import COMPANION, DEFAULT_APP, expect_app  # noqa: E402
from screenshots import INBOX_SUMMARY, SESSIONS as KNOWN, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = Path(os.environ.get("SHOTS", "/tmp/quick-chat-shots"))

PNG = "iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA4UlEQVR42u3dsQmAMBBAUSMuYB8wkGUcw9Ecw2WEbOAKbhBtBDnea9Pd59KFpJyXgbhGIxAYgREYgREYgRFYYKKY+sfzsZvR/13rZoNd0QiMwAiMwAiMwAgsMAIjMAIjMAIjsMAIjMAIzPeS56M2GIERGIERGIERWGCieHhdWEo1o/daO20wAiMwAiOwwAiMwAiMwAiMwAIjMAIjMAIjMAILjMAIjMAIjMAIjMACIzACIzACIzACC4zACIzACIzACCywEQiMwAiMwAiMwAgsMFH4u9AGIzACIzACIzACC0wYN66lB8/TbcQGAAAAAElFTkSuQmCC"

# What the shell's preload binds in Quick Chat's window and the main one, recording every request.
BRIDGE = """
window.__calls = [];
window.__shown = null;
window.__action = null;
window.__navigate = null;
const png = Uint8Array.from(atob('%s'), (c) => c.charCodeAt(0));
const shot = (name) => ({ ok: true, name, type: 'image/png', bytes: png, width: 160, height: 100 });
window.__desk = { settings: { quickChat: 'Alt+Space', appshot: 'Alt+Shift+Space', hideToTray: true } };
window.daedalus = {
  window: true,
  shell: true,
  quick: {
    hide: () => window.__calls.push(['hide']),
    resize: (h) => window.__calls.push(['resize', h]),
    open: (path) => window.__calls.push(['open', path]),
    pending: async () => [],
    windows: async () => { window.__calls.push(['windows']); return { ok: true, windows: [{ id: 'window:7:0', name: 'Editor', thumbnail: 'data:image/png;base64,%s' }] }; },
    capture: async (id) => { window.__calls.push(['capture', id || '']); return shot(id ? 'appshot-window.png' : 'appshot-screen.png'); },
    onShown: (cb) => { window.__shown = cb; return () => {}; },
  },
  desk: {
    settings: async () => info({}),
    setSettings: async (patch) => { window.__calls.push(['setSettings', patch]); Object.assign(window.__desk.settings, patch); return info({}); },
    status: (status) => window.__calls.push(['status', status]),
    openScreenSettings: () => window.__calls.push(['screenSettings']),
    onAction: (cb) => { window.__action = cb; return () => {}; },
    onNavigate: (cb) => { window.__navigate = cb; return () => {}; },
  },
};
function info(errors) {
  return { settings: { ...window.__desk.settings }, defaults: { quickChat: 'Alt+Space', appshot: 'Alt+Shift+Space', hideToTray: true },
    report: { quickChat: { accelerator: window.__desk.settings.quickChat, ok: true, reason: '' }, appshot: { accelerator: window.__desk.settings.appshot, ok: false, reason: 'taken' } },
    errors, platform: 'linux', wayland: true, tray: { ready: true, note: '' }, screen: 'granted' };
}
""" % (PNG, PNG)

SESSIONS = {
    "sessions": [
        {"id": "s-deploy", "title": "Deploy the site", "status": "running", "created_at": "2026-10-13T08:00:00Z", "last_message_at": "2026-10-13T09:00:00Z", "run_id": "r1", "project_id": "p1", "project": "Website"},
        {"id": "s-old", "title": "Older chat", "status": "idle", "created_at": "2026-10-10T08:00:00Z", "last_message_at": "2026-10-10T09:00:00Z", "run_id": None, "project_id": "p1", "project": "Website"},
    ],
    "projects": [],
}


class Api:
    """The stub, with the routes Quick Chat writes to recorded."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, object]] = []
        self.no_companion = False

    def __call__(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel == "/api/sessions" and request.method == "GET":
            return respond(route, SESSIONS)
        if rel == "/api/asr":
            # Speech recognition is set up, so the box offers dictation through the host.
            return respond(route, {"configured": True, "reason": "", "provider": "local", "model": "small", "max_seconds": 120, "autosend": False})
        if rel == "/api/companion" and request.method == "GET" and self.no_companion:
            return respond(route, {"detail": "Not Found"}, status=404)
        if request.method in ("POST", "PATCH") and (rel.startswith("/api/companion") or rel.startswith("/api/sessions")):
            content_type = request.headers.get("content-type", "")
            # An upload is multipart with a PNG in it: read as bytes, and kept as text only to look in.
            raw = request.post_data_buffer or b""
            body: object = json.loads(raw) if "json" in content_type and raw else raw.decode("utf-8", "replace")
            self.posts.append((request.method, rel, body))
            if rel == "/api/sessions":
                return respond(route, {"id": "s-new"})
            if rel.endswith("/messages") or rel.endswith("/upload"):
                return respond(route, {"run_id": "r9", "receipt": {"status": "queued"}})
        return stub(route)

    def last(self, rel: str):  # type: ignore[no-untyped-def]
        for method, path, body in reversed(self.posts):
            if path == rel:
                return method, body
        raise AssertionError(f"nothing was sent to {rel}; sent: {[p for _, p, _ in self.posts]}")


def calls(page, name: str) -> list:  # type: ignore[no-untyped-def]
    return [c for c in page.evaluate("window.__calls") if c[0] == name]


def run() -> int:
    expect_app(BASE)
    SHOTS.mkdir(parents=True, exist_ok=True)
    api = Api()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        ctx = browser.new_context(viewport={"width": 680, "height": 420})
        ctx.add_init_script(BRIDGE)
        page = ctx.new_page()
        page.route("**/api/**", api)
        page.goto(f"{BASE}/quick?lang=en")
        target = page.locator(".quick-target")
        expect(target).to_contain_text("Daedalus")
        field = page.locator(".quick-field")
        expect(field).to_be_focused()
        assert calls(page, "resize"), "the page never told the window its height"
        expect(page.locator(".quick-actions .mic")).to_be_visible()

        # Words to the companion.
        field.fill("hello there")
        field.press("Enter")
        expect(page.locator(".quick-status")).to_contain_text("Sent to Daedalus.")
        _, body = api.last("/api/companion/message")
        assert body == {"text": "hello there", "source": "quick_chat"}, body
        page.screenshot(path=str(SHOTS / "quick-sent.png"))
        page.locator(".quick-status").get_by_role("button", name="Open").click()
        assert calls(page, "open")[-1] == ["open", f"/app/agents/{COMPANION['session_id']}"], calls(page, "open")

        # A skill by $.
        field.press_sequentially("$web")
        listbox = page.get_by_role("listbox", name="Skills")
        expect(listbox).to_contain_text("web-design-reviewer")
        field.press("Enter")
        expect(page.locator(".quick-chip.skill")).to_contain_text("web-design-reviewer")
        field.press_sequentially("check the page")
        field.press("Enter")
        expect(page.locator(".quick-status")).to_contain_text("Sent to Daedalus.")
        _, body = api.last("/api/companion/message")
        assert body["text"] == "Use the web-design-reviewer skill: check the page", body

        # A conversation by @, sent to the last chat.
        target.click()
        page.get_by_role("option", name="Last chat").click()
        expect(target).to_contain_text("Deploy the site")
        field.press_sequentially("@Older")
        mention = page.get_by_role("listbox", name="Add context")
        expect(mention).to_contain_text("Older chat")
        page.screenshot(path=str(SHOTS / "quick-mention.png"))
        field.press("Enter")
        expect(page.locator(".quick-chip.session")).to_contain_text("Older chat")
        field.press_sequentially("compare with this")
        field.press("Enter")
        expect(page.locator(".quick-status")).to_contain_text("Sent to Deploy the site.")
        _, body = api.last("/api/sessions/s-deploy/messages")
        assert body["follow_up"] is True and body["client_message_id"], body
        assert body["text"].startswith("compare with this\n\nContext:\n- the conversation “Older chat” (session s-old)"), body

        # Esc closes the list before it hides the window.
        field.press_sequentially(" @Dep")
        expect(page.get_by_role("listbox", name="Add context")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("listbox", name="Add context")).to_have_count(0)
        assert not calls(page, "hide"), "Esc hid the window while a list was open"
        field.fill("")

        # The appshot: a picture of the screen, previewed, sent through the companion's session.
        target.click()
        page.get_by_role("option", name="Companion").click()
        page.get_by_role("button", name="Appshot of the screen under the pointer").click()
        chip = page.locator(".quick-chip.image")
        expect(chip).to_contain_text("appshot-screen.png")
        assert calls(page, "capture")[-1] == ["capture", ""]
        chip.locator(".quick-thumb").click()
        expect(page.locator(".quick-preview img")).to_be_visible()
        page.screenshot(path=str(SHOTS / "quick-appshot.png"))
        page.keyboard.press("Escape")
        expect(page.locator(".quick-preview")).to_have_count(0)
        assert not calls(page, "hide"), "Esc hid the window while the preview was open"
        # A window chosen from thumbnails.
        page.get_by_role("button", name="Appshot of a window").click()
        page.get_by_role("option", name="Editor").click()
        assert calls(page, "capture")[-1] == ["capture", "window:7:0"], calls(page, "capture")
        expect(chip).to_have_count(2)
        field.fill("what is wrong here?")
        field.press("Enter")
        expect(page.locator(".quick-status")).to_contain_text("Sent to Daedalus.")
        _, body = api.last(f"/api/sessions/{COMPANION['session_id']}/upload")
        assert "what is wrong here?" in str(body) and "appshot-screen.png" in str(body) and "appshot-window.png" in str(body), str(body)[:300]
        expect(chip).to_have_count(0)

        # Esc with nothing open hides the window.
        page.keyboard.press("Escape")
        assert calls(page, "hide"), "Esc did not hide the window"

        # Without the companion it is no target.
        api.no_companion = True
        page.reload()
        expect(page.locator(".quick-target")).to_contain_text("Deploy the site")
        page.locator(".quick-target").click()
        expect(page.get_by_role("option", name="Companion")).to_have_count(0)
        expect(page.get_by_role("option", name="New chat")).to_be_visible()
        page.get_by_role("option", name="New chat").click()
        page.locator(".quick-field").fill("start something")
        page.locator(".quick-field").press("Enter")
        expect(page.locator(".quick-status")).to_contain_text("Sent to New chat.")
        method, created = api.last("/api/sessions")
        assert method == "POST" and created["title"] == "start something" and created["autotitle"] is True, created
        api.last("/api/sessions/s-new/messages")
        api.no_companion = False

        # Russian, and signed out.
        page.goto(f"{BASE}/quick?lang=ru")
        expect(page.locator(".quick-field")).to_have_attribute("placeholder", "Спросите что угодно — @ добавит контекст, $ выберет навык")
        page.screenshot(path=str(SHOTS / "quick-ru.png"))
        stub.signedout = True  # type: ignore[attr-defined]
        try:
            page.goto(f"{BASE}/quick?lang=en")
            expect(page.locator(".quick-signedout")).to_contain_text("Sign in to Daedalus in its main window first.")
            page.get_by_role("button", name="Open Daedalus").click()
            assert calls(page, "open")[-1] == ["open", "/app/"]
        finally:
            stub.signedout = False  # type: ignore[attr-defined]

        # The main window: settings, the tray's status, its Pause and Quick Chat's "open".
        main = ctx.new_page()
        main.set_viewport_size({"width": 1180, "height": 820})
        main.route("**/api/**", api)
        main.goto(f"{BASE}/settings?lang=en")
        expect(main.locator(".settings-index")).to_contain_text("Desktop app")
        main.goto(f"{BASE}/settings/desktop?lang=en")
        panel = main.locator(".settings-desktop")
        expect(panel).to_contain_text("Alt+Space")
        expect(panel).to_contain_text("Another program holds this shortcut")
        expect(panel).to_contain_text("daedalus --quick-chat")
        main.screenshot(path=str(SHOTS / "settings-desktop.png"))
        panel.get_by_role("button", name="Change").first.click()
        expect(panel.locator("kbd.recording")).to_be_visible()
        main.keyboard.press("Control+Shift+KeyK")
        expect(panel).to_contain_text("Ctrl+Shift+K")
        assert calls(main, "setSettings")[-1] == ["setSettings", {"quickChat": "Ctrl+Shift+K"}], calls(main, "setSettings")
        statuses = calls(main, "status")
        # One conversation running in the list above; what waits is the Inbox's "needs you".
        assert statuses and statuses[-1][1] == {"running": 1, "waiting": INBOX_SUMMARY["needs_you"], "companion": {"paused": False}}, statuses
        main.evaluate("window.__action('pause-companion')")
        main.wait_for_timeout(300)
        assert api.last("/api/companion") == ("PATCH", {"paused": True})
        # A conversation the stub can draw, so the move lands on a screen rather than an error.
        known = KNOWN[0]["id"]
        main.evaluate(f"window.__navigate('/app/agents/{known}')")
        main.wait_for_url(f"**/app/agents/{known}*")

        # A plain browser lists no desktop section.
        plain = browser.new_context(viewport={"width": 1180, "height": 820})
        other = plain.new_page()
        other.route("**/api/**", api)
        other.goto(f"{BASE}/settings?lang=en")
        expect(other.locator(".settings-index")).to_contain_text("Appearance")
        expect(other.locator(".settings-index")).not_to_contain_text("Desktop app")
        plain.close()
        browser.close()
    print("quick chat: sends to the companion, the last chat and a new one; $ and @; appshot; Esc; settings; tray status")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
