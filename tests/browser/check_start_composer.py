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
        box = composer.bounding_box()
        head = page.locator(".start-list-head").bounding_box()
        greeting = page.locator(".start-greeting").bounding_box()
        vh = size["height"]
        assert box and head and greeting
        if not 0.42 * vh <= head["y"] <= 0.55 * vh:
            say(f"the chats begin at {head['y']:.0f} px of {vh}, not about the middle")
        if box["y"] < 0.2 * vh or box["y"] + box["height"] > head["y"] + 0.5:
            say(f"the composer {box} is not in the middle, above the chats at {head['y']:.0f}")
        if greeting["y"] + greeting["height"] > box["y"]:
            say("the question is not above the composer")
        if page.evaluate("document.documentElement.scrollWidth - innerWidth") > 0:
            say("the page scrolls sideways")

    # The effort: the preset's own first, then a pick.
    model = composer.locator(".model-select")
    if phone:
        expect(model.locator(".model-effort")).to_contain_text(words["high"])
        model.tap()
        page.locator(".model-sheet .effort-option input[value='low']").check()
        expect(model.locator(".model-effort")).to_contain_text(words["low"])
        page.keyboard.press("Escape")
        page.wait_for_selector(".model-sheet", state="detached", timeout=3000)
    else:
        effort = composer.locator(".effort-select")
        expect(effort).to_contain_text(words["high"])
        effort.click()
        page.locator(".effort-menu .effort-option input[value='low']").click()
        expect(effort).to_contain_text(words["low"])

    # The microphone: the words land after what is typed.
    field = composer.locator("textarea")
    field.fill("Check the price")
    mic = composer.get_by_role("button", name=words["mic"])
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
