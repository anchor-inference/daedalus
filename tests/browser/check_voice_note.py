"""Record a voice note in the composer, in a real browser, and refuse a recording that is lost.

The microphone is Chromium's own fake device (a beep), so MediaRecorder, the level meter and the WAV
conversion are the browser's real ones; the host is a stub in this file that can fail a transcription,
keep the recording under a name and hear it on a retry, the way the real one does. What is asserted:

- the microphone is there with text already typed, and the words land after that text;
- while recording the pill is one bar of cancel, waveform and time, stop, and send;
- ✕ throws the recording away and sends nothing anywhere;
- ↑ stops and sends the typed text and the words together;
- a failed transcription shows why, keeps the draft, and Retry sends the kept name, not the audio;
- "Attach as audio file" puts the recording in the pill and lets the host's copy go;
- a failure survives a reload: the recording waits in the browser until it is settled;
- on a phone every control in the bar is a thumb's size and the bar fits the screen;
- with reduced motion the bar still draws, and nothing scrolls;
- Settings offers an installed local model as the transcriber and as the fallback, and saves the pick.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8183 /tmp/app-root &
    APP_URL=http://127.0.0.1:8183/app python3 tests/browser/check_voice_note.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import DEFAULT_APP, VOICE_NOTE_PREFIX, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SESSION = "sess-voice"
KEPT = "a1b2c3d4e5f6.wav"
HEARD = "buy milk on the way home"


class Host:
    def __init__(self) -> None:
        self.fail = 0
        self.transcribes: list[str] = []
        self.sent: list[str] = []
        self.deleted: list[str] = []

    def reset(self) -> None:
        self.__init__()


HOST = Host()


def detail() -> dict:
    return {
        "id": SESSION, "title": "A session", "status": "idle", "error": "", "run_id": None, "workspace": "/workspace",
        "workspace_name": "ws", "workspace_own": True, "workspace_sessions": [], "pending": None, "model": "Claude Opus 5", "provider": "claude",
        "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
        "configured_model": "opus", "effective_model": "opus", "fallback": None,
        "thinking": False, "reasoning_effort": "", "mode": "", "yagni": False, "brief": "",
        "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": None, "messages": [],
    }


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    path = req.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    reply = lambda body, status=200: route.fulfill(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if rel == f"/api/sessions/{SESSION}/transcribe" and req.method == "POST":
        body = req.post_data_buffer or b""
        kind = "name" if b'name="recording"' in body else "audio" if b'name="audio"' in body and b"RIFF" in body else "other"
        HOST.transcribes.append(kind)
        if HOST.fail:
            HOST.fail -= 1
            return reply({"detail": {"message": "transcription endpoint answered HTTP 400: Provider returned 400", "recording": KEPT}}, 502)
        return reply({"transcript": HEARD, "text": f"{VOICE_NOTE_PREFIX}\n{HEARD}", "autosend": False})
    if rel.startswith(f"/api/sessions/{SESSION}/transcribe/") and req.method == "DELETE":
        HOST.deleted.append(rel.rsplit("/", 1)[1])
        return reply({"deleted": True})
    if rel == f"/api/sessions/{SESSION}/messages" and req.method == "POST":
        data = json.loads(req.post_data or "{}")
        HOST.sent.append(str(data.get("text", "")))
        return reply({"run_id": "r2", "receipt": {"status": "consumed", "run_id": "r2",
                                               "client_message_id": data.get("client_message_id")}})
    if rel == "/api/asr":
        return reply({"configured": True, "reason": "", "provider": "openrouter", "model": "qwen/qwen3-asr-flash", "max_seconds": 600, "autosend": False, "transcriber": "cloud", "fallback": ""})
    if req.method == "GET" and rel == f"/api/sessions/{SESSION}":
        return reply(detail())
    if req.method == "GET" and rel.startswith(f"/api/sessions/{SESSION}/"):
        return reply([])
    if rel == "/api/auth/me":
        return reply({"user_id": 1, "via": "token"})
    if rel == "/api/settings":
        return reply({"model": {"preset": "opus"}, "presets": {"opus": {"provider": "claude", "model": "opus", "label": "Claude Opus 5", "thinking": False, "reasoning_effort": "", "images": True, "context_window": 200000, "max_output_tokens": 32000}}})
    if fulfil_shared(route):
        return None
    UNHANDLED.record(rel)
    return reply([])


def open_page(context) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".composer .mic", timeout=15000)
    return page


def target(page: Page, selector: str) -> dict:
    """What a finger can hit: the control's box, grown by the invisible ::after a 36 px circle carries
    on a phone to reach 44 px."""
    return page.evaluate("""(sel) => {
      const el = document.querySelector(sel);
      if (!el) return { x: -1, width: 0, height: 0 };
      const r = el.getBoundingClientRect(), a = getComputedStyle(el, '::after');
      const px = (v) => (v.endsWith('px') ? parseFloat(v) : 0);
      const grow = a.content !== 'none' && a.position === 'absolute';
      return { x: r.x, width: r.width - (grow ? px(a.left) + px(a.right) : 0), height: r.height - (grow ? px(a.top) + px(a.bottom) : 0) };
    }""", selector)


def new_context(browser, **options):  # type: ignore[no-untyped-def]
    context = browser.new_context(color_scheme="dark", **options)
    context.grant_permissions(["microphone"], origin=BASE.split("/app")[0])
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    return context


def field(page: Page):  # type: ignore[no-untyped-def]
    return page.locator(".composer textarea")


def record(page: Page, seconds: float = 1.3) -> None:
    mic = page.locator(".composer .mic")
    if mic.is_visible():
        mic.click()
    else:
        page.locator(".composer .plus").click()
        page.locator(".plus-menu [role=menuitem]").filter(has=page.locator(".ic-mic")).click()
    page.wait_for_selector(".voicebar[data-phase=recording]", timeout=5000)
    page.wait_for_timeout(int(seconds * 1000))


def run(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            problems.append(what)

    # Desktop: typed text, then a note, then stop.
    HOST.reset()
    context = new_context(browser, viewport={"width": 1440, "height": 900})
    page = open_page(context)
    field(page).fill("Look at the logs first.")
    check(page.locator(".composer .roundbtn.primary").is_visible(), "typed text exposes Send; voice remains in the attachment menu")
    record(page)
    bar = page.locator(".voicebar")
    check(bar.is_visible() and not field(page).is_visible(), "recording, the pill is the bar and the field is out of the way")
    for name, selector in (("cancel", ".voicebar-cancel"), ("stop", ".voicebar-stop"), ("send", ".voicebar-send")):
        control = page.locator(selector)
        check(control.is_visible() and bool(control.get_attribute("aria-label")), f"the bar has a labelled {name} control")
    check(page.locator(".voicebar-wave").is_visible(), "the waveform is drawn")
    check(page.locator(".voicebar-time").inner_text().startswith("0:0"), f"the time runs ({page.locator('.voicebar-time').inner_text()})")
    page.locator(".voicebar-stop").click()
    page.wait_for_function("() => document.querySelector('.composer textarea').value.includes('buy milk')", timeout=10000)
    value = field(page).input_value()
    check(value.startswith("Look at the logs first.\n\n") and value.endswith(HEARD), f"the words land after the typed text ({value!r})")
    check(HOST.transcribes == ["audio"], f"the recording went up as WAV once ({HOST.transcribes})")
    check(not HOST.sent, "stop does not send")

    # ✕ throws the recording away.
    HOST.transcribes.clear()
    record(page, 0.8)
    page.locator(".voicebar-cancel").click()
    page.wait_for_timeout(600)
    check(not bar.is_visible() and field(page).input_value() == value, "cancel closes the bar and leaves the draft as it was")
    check(not HOST.transcribes, "cancel sends nothing to be transcribed")

    # ↑ stops and sends the draft and the words together.
    field(page).fill("Also this.")
    record(page, 0.8)
    page.locator(".voicebar-send").click()
    page.wait_for_function("() => document.querySelector('.composer textarea').value === ''", timeout=10000)
    page.wait_for_timeout(300)
    check(len(HOST.sent) == 1 and HOST.sent[0].startswith("Also this.\n\n") and HOST.sent[0].endswith(HEARD), f"send sends the typed text and the words ({HOST.sent})")

    # A failure keeps everything, and a retry sends the kept name.
    HOST.transcribes.clear()
    HOST.fail = 1
    field(page).fill("Draft that must survive.")
    record(page, 0.8)
    page.locator(".voicebar-stop").click()
    strip = page.locator(".voicenote-failed")
    strip.wait_for(timeout=10000)
    check("HTTP 400" in strip.inner_text(), "the failure says why")
    check(field(page).is_visible() and field(page).input_value() == "Draft that must survive.", "the draft is intact and the field usable")
    check(page.locator(".composer .mic").is_disabled(), "a new recording cannot bury the waiting one")
    page.locator(".voicenote-failed .btn.primary").click()
    page.wait_for_function("() => document.querySelector('.composer textarea').value.includes('buy milk')", timeout=10000)
    check(HOST.transcribes == ["audio", "name"], f"the retry sends the kept recording's name, not the audio ({HOST.transcribes})")
    check(not strip.is_visible(), "a retry that works clears the failure")

    # Attach as audio file.
    HOST.fail = 1
    field(page).fill("")
    record(page, 0.8)
    page.locator(".voicebar-stop").click()
    strip.wait_for(timeout=10000)
    page.locator(".voicenote-failed .btn:not(.primary)").click()
    page.wait_for_timeout(400)
    names = page.locator(".composer .attachment-name").all_inner_texts()
    check(len(names) == 1 and names[0].startswith("voice-note-"), f"the recording is attached as a file ({names})")
    check(HOST.deleted == [KEPT], f"and the host's kept copy is let go ({HOST.deleted})")
    page.locator(".composer .attachment-x").click()

    # A failure survives a reload.
    HOST.fail = 1
    record(page, 0.8)
    page.locator(".voicebar-stop").click()
    strip.wait_for(timeout=10000)
    page.reload()
    page.wait_for_selector(".composer .mic", timeout=15000)
    try:
        strip.wait_for(timeout=5000)
        restored = True
    except Exception:  # noqa: BLE001 - the assertion below says what was missing
        restored = False
    check(restored, "after a reload the recording is still waiting, with Retry")
    if restored:
        page.locator(".voicenote-failed .iconbtn").click()
        page.wait_for_timeout(300)
        check(not strip.is_visible(), "discard settles it")
    context.close()

    # A phone: the bar fits and every control is a thumb's size.
    for label, options in (("phone", {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}), ("tablet", {"viewport": {"width": 1024, "height": 1366}, "is_mobile": True, "has_touch": True})):
        context = new_context(browser, **options)
        page = open_page(context)
        mic = target(page, ".composer .mic")
        check(mic["width"] >= 44 and mic["height"] >= 44, f"{label}: the microphone is a thumb's size ({mic['width']}x{mic['height']})")
        page.locator(".composer .mic").tap()
        page.wait_for_selector(".voicebar[data-phase=recording]", timeout=5000)
        width = options["viewport"]["width"]  # type: ignore[index]
        for selector in (".voicebar-cancel", ".voicebar-stop", ".voicebar-send"):
            box = target(page, selector)
            check(box["width"] >= 44 and box["height"] >= 44 and 0 <= box["x"] and box["x"] + box["width"] <= width, f"{label}: {selector} is 44px or more and on screen ({box})")
        check(page.evaluate("document.documentElement.scrollWidth") <= width, f"{label}: nothing scrolls sideways")
        page.locator(".voicebar-cancel").tap()
        context.close()

    # Reduced motion: the bar draws, and the waveform does not move.
    context = new_context(browser, viewport={"width": 800, "height": 700}, reduced_motion="reduce")
    page = open_page(context)
    record(page, 0.5)
    first = page.evaluate("document.querySelector('.voicebar-wave').toDataURL()")
    page.wait_for_timeout(1200)
    second = page.evaluate("document.querySelector('.voicebar-wave').toDataURL()")
    check(page.locator(".voicebar").is_visible() and first == second, "with reduced motion the waveform is a still line")
    page.locator(".voicebar-cancel").click()
    context.close()

    # Settings: the transcriber and the fallback offer what the Voice page has installed.
    saved: list[dict] = []

    def settings_stub(route) -> None:  # type: ignore[no-untyped-def]
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/api/settings/validate"):
            return shots.respond(route, {"valid": True, "stale": False, "problems": []})
        if path.endswith("/api/settings"):
            if route.request.method == "PUT":
                saved.append(json.loads(route.request.post_data or "{}"))
            # A revision, or the page will not save at all: it guards against overwriting a newer edit.
            return shots.respond(route, {**shots.SETTINGS, "revision": "r1"})
        return shots.stub(route)

    context = new_context(browser, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.route("**/api/**", settings_stub)
    # The card lives on Voice & speech now, its two choices compact pickers in their rows.
    page.goto(f"{BASE}/settings/voice?token=t&scheme=dark&lang=en")
    page.wait_for_selector("#asr-transcriber", timeout=15000)

    def offered_by(picker: str) -> list[str]:
        page.locator(picker).click()
        page.wait_for_selector(".dropdown-list [role=option]", timeout=5000)
        values = page.eval_on_selector_all(".dropdown-list [role=option]", "els => els.map(e => e.dataset.value)")
        return values

    page.wait_for_function("() => document.querySelector('.asr-card') !== null", timeout=10000)
    page.wait_for_timeout(600)
    offered = offered_by("#asr-transcriber")
    check(offered[:1] == ["cloud"] and "gigaam-ru" in offered, f"the transcriber offers the endpoint and the installed model ({offered})")
    page.locator(".dropdown-list [role=option][data-value='gigaam-ru']").click()
    page.wait_for_timeout(400)
    check(bool(saved) and saved[-1].get("asr", {}).get("transcriber") == "gigaam-ru", f"choosing it saves [asr].transcriber ({saved[-1:] if saved else saved})")
    check(not saved or saved[-1]["asr"].get("api_key") == "", "and never sends the key back")
    fallback = offered_by("#asr-fallback")
    check(fallback[:1] == [""] and "gigaam-ru" in fallback, f"the fallback offers nothing, and the installed model ({fallback})")
    page.keyboard.press("Escape")
    context.close()
    return problems


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        try:
            problems = run(browser)
        finally:
            browser.close()
    failed = len(problems) + UNHANDLED.report()
    print(f"\n{'all voice-note checks hold' if not failed else f'{failed} voice-note checks failed'}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
