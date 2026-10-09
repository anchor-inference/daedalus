"""An error about a setting takes the operator to that setting, driven in a real browser.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- a session's ``setting_notice`` (the vision model was not set, the session's model looked instead) is
  a warning line in the conversation, with the button to the row;
- a /compact the host refuses about the summary model (409 with ``setting``) raises a red toast in
  the corner — the banner on a phone — with the button, and the same line in the conversation;
- the toast's button opens Settings → Limits, where the summary model's row is in view and blinks
  (it carries the highlight class), and the address no longer names the row afterwards;
- the conversation's warning opens Settings → Models with the vision model's row in view and blinking;
- the fallback switch sits on the models page, on by default.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, setting_notice_frame, setting_refusal  # noqa: E402
from screenshots import S2, UNHANDLED, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")
REFUSED = "no model could summarise the history: opencode/deepseek-flash: Go usage limit exceeded"
COMMANDS = [{"name": "compact", "args": "", "description": "Summarise the history", "scope": "session", "confirm": False}]
GO = {"en": "Go to setting", "ru": "К настройке"}


def route(r) -> None:  # type: ignore[no-untyped-def]
    path = r.request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == "/api/commands":
        return respond(r, COMMANDS)
    if rel == f"/api/sessions/{S2}/stream":
        frame = setting_notice_frame("vision", "fallback", "no vision model is configured", "models", "vision.preset", model="main")
        return respond(r, "event: hello\ndata: {}\n\n" + frame, content_type="text/event-stream")
    return stub(r)


def row_state(page, key: str) -> dict:  # type: ignore[no-untyped-def]
    """Whether the row is on screen (inside the viewport) and whether it carries the highlight."""
    return page.evaluate(
        """(key) => {
            const el = document.querySelector(`[data-setting="${key}"]`);
            if (!el) return { found: false };
            const r = el.getBoundingClientRect();
            return { found: true, flash: el.classList.contains("setting-flash"), top: r.top, bottom: r.bottom,
                     inView: r.top >= 0 && r.bottom <= window.innerHeight && r.height > 0, search: location.search, path: location.pathname };
        }""",
        key,
    )


def wait_for_flash(page, key: str) -> dict:  # type: ignore[no-untyped-def]
    page.wait_for_selector(f'[data-setting="{key}"].setting-flash', timeout=8000)
    page.wait_for_timeout(700)  # the smooth scroll settles
    return row_state(page, key)


def run(browser, lang: str, viewport: dict, problems: list[str], **context) -> None:  # type: ignore[no-untyped-def]
    where = f"[{lang} {viewport['width']}]"
    say = lambda text: problems.append(f"{where} {text}")  # noqa: E731
    stub.command_refusal = setting_refusal(REFUSED, "limits", "compaction.preset")  # type: ignore[attr-defined]
    page = browser.new_context(viewport=viewport, color_scheme="dark", **context).new_page()
    page.route("**/api/**", route)
    page.goto(f"{BASE}/agents/{S2}?token=t&lang={lang}")
    page.wait_for_selector(".composer textarea", timeout=15000)

    # The session's own notice: a warning line with the way to the vision model.
    inline = page.locator('.setting-notice[data-setting-notice="vision.preset"]')
    try:
        inline.wait_for(timeout=8000)
    except Exception:  # noqa: BLE001
        say("the session's setting notice never appeared in the conversation")
    else:
        cls = inline.get_attribute("class") or ""
        print(f"{where} inline: {inline.inner_text()!r} ({cls})")
        if "tone-warning" not in cls:
            say(f"the fallback notice is not a warning: {cls}")
        if GO[lang] not in inline.inner_text():
            say(f"the line has no button to the setting: {inline.inner_text()!r}")

    # A /compact refused about the summary model.
    field = page.locator(".composer textarea").first
    field.click()
    field.fill("/compact")
    # Enter is a new line on a phone; the circle sends there, and on a desktop as well.
    page.locator(".composer .roundbtn.primary").first.click()
    toast = page.locator(".notice-toast.tone-error")
    try:
        toast.wait_for(timeout=8000)
    except Exception:  # noqa: BLE001
        say("the refused /compact raised no setting toast")
        page.context.close()
        return
    text = toast.inner_text()
    print(f"{where} toast: {text!r}")
    if "usage limit exceeded" not in text or GO[lang] not in text:
        say(f"the toast reads {text!r}")
    if page.locator('.setting-notice.tone-error[data-setting-notice="compaction.preset"]').count() != 1:
        say("the refused /compact left no line in the conversation")
    stack = page.locator(".notice-toasts")
    box = stack.bounding_box() or {"x": 0, "y": 0, "width": 0, "height": 0}
    wide = viewport["width"] >= 1024
    # Bottom-right on a desktop, the banner at the top of a phone.
    if wide and (box["x"] + box["width"] < viewport["width"] - 40 or box["y"] < viewport["height"] / 2):
        say(f"the toast is not in the bottom-right corner: {box}")
    if not wide and box["y"] > viewport["height"] / 3:
        say(f"the phone's banner is not at the top: {box}")

    toast.locator('[data-setting-go="compaction.preset"]').click()
    state = wait_for_flash(page, "compaction.preset")
    print(f"{where} summary row: {state}")
    if not state.get("found") or not state.get("inView") or not state.get("flash"):
        say(f"the summary model's row is not in view and highlighted: {state}")
    if not state.get("path", "").endswith("/settings/limits"):
        say(f"the button opened {state.get('path')}")
    page.wait_for_timeout(600)
    if "setting=" in page.evaluate("location.search"):
        say(f"the address still names the row: {page.evaluate('location.search')}")
    if page.locator(".notice-toast.tone-error").count():
        say("the toast stayed after its button was pressed")

    # Back to the conversation; its warning line goes to the vision model's row.
    page.go_back()
    page.wait_for_selector(".composer textarea", timeout=15000)
    inline = page.locator('.setting-notice[data-setting-notice="vision.preset"]')
    inline.wait_for(timeout=8000)
    inline.locator('[data-setting-go="vision.preset"]').click()
    state = wait_for_flash(page, "vision.preset")
    print(f"{where} vision row: {state}")
    if not state.get("found") or not state.get("inView") or not state.get("flash"):
        say(f"the vision model's row is not in view and highlighted: {state}")
    switch = page.locator('[data-setting="model.fallback_to_session"] [role=switch], [data-setting="model.fallback_to_session"] input[type=checkbox]').first
    checked = switch.get_attribute("aria-checked") or str(switch.is_checked()).lower()
    print(f"{where} fallback switch: {checked}")
    if checked != "true":
        say(f"the fallback switch is not on by default: {checked}")
    widths = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    if widths[0] > widths[1]:
        say(f"the page scrolls sideways: {widths}")
    page.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ([LANG] if LANG else ["en", "ru"]):
            run(browser, lang, {"width": 1440, "height": 900}, problems)
            run(browser, lang, {"width": 390, "height": 844}, problems, is_mobile=True, has_touch=True)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
