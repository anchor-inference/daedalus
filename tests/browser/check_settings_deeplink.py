"""A link to one setting opens its row, from wherever the link comes, driven in a real browser.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- the address ``/app/settings/voice#asr.transcriber`` opened directly scrolls Settings → Voice & speech
  to the speech-recognition row and makes it blink, and the fragment leaves the address afterwards;
- an answer in a conversation that links the row — as a Markdown link, and as a bare address — is a
  link the app opens itself (the page is not reloaded) and lands on the blinking row; Back returns to
  the conversation;
- the Settings search finds the row by a word that is not in its title ("STT", "диктовка") and goes
  straight to it.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import MESSAGES_S2, S2, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")
ROW = "asr.transcriber"
ADDRESS = f"/app/settings/voice#{ROW}"
ANSWER = {
    "en": f"Speech recognition for the chat is in [Voice & speech → Speech recognition]({ADDRESS}). The same row: {ADDRESS}.",
    "ru": f"Распознавание речи в чате — в [Голос и речь → Распознавание речи]({ADDRESS}). Та же строка: {ADDRESS}.",
}
SEARCH = {"en": "STT", "ru": "диктовка"}


def route_for(lang: str):  # type: ignore[no-untyped-def]
    answer = {"role": "assistant", "seq": 16, "text": ANSWER[lang], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=1)}

    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = r.request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel == f"/api/sessions/{S2}" and r.request.method == "GET":
            return respond(r, {**detail(S2), "messages": [*MESSAGES_S2, answer]})
        return stub(r)

    return route


def row_state(page) -> dict:  # type: ignore[no-untyped-def]
    return page.evaluate(
        """(key) => {
            const el = document.querySelector(`[data-setting="${key}"]`);
            if (!el) return { found: false, path: location.pathname };
            const r = el.getBoundingClientRect();
            return { found: true, flash: el.classList.contains("setting-flash"), inView: r.top >= 0 && r.bottom <= window.innerHeight && r.height > 0,
                     path: location.pathname, hash: location.hash, marker: window.__stayed === true };
        }""",
        ROW,
    )


def landed(page, say, how: str, *, marker: bool) -> None:  # type: ignore[no-untyped-def]
    try:
        page.wait_for_selector(f'[data-setting="{ROW}"].setting-flash', timeout=8000)
    except Exception:  # noqa: BLE001
        say(f"{how}: the row never blinked ({row_state(page)})")
        return
    page.wait_for_timeout(800)  # the smooth scroll settles, the fragment is taken off
    state = row_state(page)
    print(f"  {how}: {state}")
    if not state.get("inView"):
        say(f"{how}: the row is not in view: {state}")
    if not state.get("path", "").endswith("/settings/voice"):
        say(f"{how}: opened {state.get('path')}")
    if state.get("hash"):
        say(f"{how}: the address still names the row: {state.get('hash')}")
    if marker and not state.get("marker"):
        say(f"{how}: the page was reloaded instead of opened by the app")


def run(browser, lang: str, viewport: dict, problems: list[str], **context) -> None:  # type: ignore[no-untyped-def]
    where = f"[{lang} {viewport['width']}]"
    say = lambda text: problems.append(f"{where} {text}")  # noqa: E731
    print(where)
    page = browser.new_context(viewport=viewport, color_scheme="dark", **context).new_page()
    page.route("**/api/**", route_for(lang))

    # Straight to the address.
    page.goto(f"{BASE}/settings/voice?token=t&lang={lang}#{ROW}")
    landed(page, say, "direct address", marker=False)

    # From an answer in a conversation: the Markdown link, then the bare address.
    for index, how in ((0, "markdown link"), (1, "bare address")):
        page.goto(f"{BASE}/agents/{S2}?token=t&lang={lang}")
        page.wait_for_selector(".composer textarea", timeout=15000)
        links = page.locator(f'a[data-app-link][href="{ADDRESS}"]')
        try:
            links.nth(1).wait_for(timeout=8000)
        except Exception:  # noqa: BLE001
            say(f"the answer's addresses are not links ({links.count()} found)")
            break
        page.evaluate("window.__stayed = true")
        links.nth(index).click()
        landed(page, say, how, marker=True)
        page.go_back()
        try:
            page.wait_for_selector(".composer textarea", timeout=8000)
        except Exception:  # noqa: BLE001
            say(f"{how}: Back did not return to the conversation ({page.evaluate('location.pathname')})")

    # The search on the Settings page, by a word the row's title does not hold.
    page.goto(f"{BASE}/settings?token=t&lang={lang}")
    field = page.locator(".settings-search").first
    field.wait_for(timeout=15000)
    field.fill(SEARCH[lang])
    hit = page.locator(f'.settings-field-hit[data-field-hit="{ROW}"]')
    try:
        hit.wait_for(timeout=5000)
    except Exception:  # noqa: BLE001
        say(f"the search for {SEARCH[lang]!r} did not offer the row")
    else:
        print(f"  search hit: {hit.inner_text()!r}")
        page.evaluate("window.__stayed = true")
        hit.click()
        landed(page, say, "search", marker=True)
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
