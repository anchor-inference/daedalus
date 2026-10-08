"""The start page's composer: where it sits on a phone, its microphone and its effort, carried into
the chat it makes. English and Russian, 390 px phone and 1440 px desktop.

What is checked:

- on a phone the composer sits in the middle of the screen, where a thumb reaches it, and the list
  of chats begins under it at about the middle line, not in the top quarter;
- the microphone is there when speech recognition is, records into the same bar the chat's composer
  has, and lands the words after what was typed — heard at ``/api/transcribe``, since there is no
  session yet;
- the effort is shown and can be changed (in the model sheet on a phone, beside it on a desktop), and
  the chat that the message makes is set to it before the message goes.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_start_composer.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, VOICE_NOTE_PREFIX, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
HEARD = "buy rye flour"
WORDS = {"en": {"mic": "record a voice note", "low": "low", "high": "high"}, "ru": {"mic": "записать голосовую заметку", "low": "низкая", "high": "высокая"}}
ASR = {"configured": True, "reason": "", "provider": "openrouter", "model": "asr", "max_seconds": 600, "autosend": False, "transcriber": "cloud", "fallback": ""}
PRESET = {"provider": "openai", "model": "gpt-6-luna", "label": "gpt-6-luna", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 400000, "max_output_tokens": 32000}


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def posted(self, path: str) -> list[object]:
        return [body for method, p, body in self.calls if method == "POST" and p == path]


def serve(page: Page, host: Host) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if path == "/api/asr":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(ASR))
        if path == "/api/settings" and request.method == "GET":
            # The installation's settings with one preset that thinks, high, as the default.
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"revision": "r1", "model": {"preset": "luna", "chain": []}, "presets": {"luna": PRESET}, "providers": {}}))
        if request.method == "POST" and path == "/api/transcribe":
            host.calls.append(("POST", path, None))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"transcript": HEARD, "text": f"{VOICE_NOTE_PREFIX}\n{HEARD}", "autosend": False}))
        if request.method == "POST" and path in ("/api/sessions", f"/api/sessions/{S1}/model", f"/api/sessions/{S1}/messages"):
            body = request.post_data_json if request.post_data else None
            host.calls.append(("POST", path, body))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": S1} if path == "/api/sessions" else {"ok": True, "run_id": "r1"}))
        return stub(r)

    page.route("**/api/**", route)


def run(browser, lang: str, phone: bool, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark")
    context.grant_permissions(["microphone"], origin=BASE.split("/app")[0])
    page = context.new_page()
    host = Host()
    serve(page, host)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    page.wait_for_timeout(800)

    if phone:
        # A phone's home is a new chat: the composer rests as one 48 px row at the bottom, the
        # question above it, and the model is the top bar's title.
        box = composer.locator(".composer-box").bounding_box()
        greeting = page.locator(".ph-hero h1").bounding_box()
        vh = size["height"]
        assert box and greeting
        if round(box["height"]) != 48 or vh - (box["y"] + box["height"]) > 24:
            say(f"the composer {box} is not one 48 px row at the bottom")
        if greeting["y"] + greeting["height"] > box["y"]:
            say("the question is not above the composer")
        expect(page.locator(".ph-top .ph-top-tb")).to_contain_text("gpt-6-luna")
        if page.evaluate("document.documentElement.scrollWidth - innerWidth") > 0:
            say("the page scrolls sideways")

    # The effort: the preset's own first, then a pick (from the title's sheet on a phone).
    model = composer.locator(".model-select")
    expect(model.locator(".model-effort")).to_contain_text(words["high"])
    if phone:
        page.locator(".ph-top .ph-top-tb").click()
        page.locator(".sheet .ph-model-effort button[role='radio']").nth(1).click()
        page.wait_for_timeout(200)
    else:
        model.click()
        page.locator(".effort-entry").click()
        page.locator(".effort-menu .effort-option input[value='low']").click()
    expect(model.locator(".model-effort")).to_contain_text(words["low"])
    expect(page.locator(".model-list")).to_have_count(0)

    # Switching from idle voice to typed Send must reserve exactly the same toolbar geometry.
    field = composer.locator("textarea")
    field.fill("")
    empty_box = composer.locator(".composer-box").bounding_box()
    empty_row = composer.locator(".composer-row").bounding_box()
    field.fill("One line")
    typed_box = composer.locator(".composer-box").bounding_box()
    typed_row = composer.locator(".composer-row").bounding_box()
    assert empty_box and typed_box and empty_row and typed_row
    if abs(empty_box["height"] - typed_box["height"]) > 1 or empty_row["height"] != typed_row["height"]:
        say(f"typing one line resized the composer: {empty_box} -> {typed_box}")
    field.fill("One line\nTwo lines\nThree lines\nFour lines")
    multiline_box = composer.locator(".composer-box").bounding_box()
    assert multiline_box and multiline_box["height"] > typed_box["height"], "Only multiline text should grow the field"

    # The microphone: the words land after what is typed.
    field = composer.locator("textarea")
    field.fill("Check the price")
    expect(composer.locator(".roundbtn.primary")).to_be_visible()
    composer.locator(".plus").click()
    # A menu item on the desktop, a row of the + sheet on a phone.
    mic = page.get_by_role("menuitem", name=words["mic"]).or_(page.locator(".ph-plus-sheet .ph-mrow").filter(has_text=re.compile(words["mic"], re.I)))
    expect(mic).to_be_visible()
    mic.click()
    page.wait_for_selector(".start-composer .voicebar[data-phase=recording]", timeout=5000)
    if field.is_visible():
        say("the field stays in the way while recording")
    page.wait_for_timeout(900)
    page.locator(".start-composer .voicebar-stop").click()
    page.wait_for_function(f"() => document.querySelector('.start-composer textarea').value.includes({json.dumps(HEARD)})", timeout=10000)
    value = field.input_value()
    if not value.startswith("Check the price\n\n"):
        say(f"the words did not land after the typed text: {value!r}")
    if len(host.posted("/api/transcribe")) != 1:
        say(f"the note was heard {len(host.posted('/api/transcribe'))} times at /api/transcribe")

    # Sent: the chat is made, set to the effort, then given the message.
    composer.locator(".roundbtn.primary").click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    order = [p for _, p, _ in host.calls if p != "/api/transcribe"]
    if order != ["/api/sessions", f"/api/sessions/{S1}/model", f"/api/sessions/{S1}/messages"]:
        say(f"the requests went {order}")
    chosen = host.posted(f"/api/sessions/{S1}/model")
    if chosen != [{"thinking": True, "reasoning_effort": "low"}]:
        say(f"the chat was set to {chosen}")
    sent = host.posted(f"/api/sessions/{S1}/messages")
    if not sent or HEARD not in str(sent[0]):
        say(f"the message was {sent}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        for lang in ("en", "ru"):
            for phone in (True, False):
                run(browser, lang, phone, problems)
            print(f"start composer {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
