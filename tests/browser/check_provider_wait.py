"""A run waiting for its model endpoint says so, instead of looking hung.

A deploy restarts the key proxy, and for a few seconds to two minutes every request a run makes is
refused before it is sent. The host waits that out and tells the screen on each pause with a
``provider_wait`` event; the live line under the run must read "Waiting for the provider to come back"
meanwhile, in both languages, on a desktop and on a phone, and go back to the ordinary line once
the endpoint answers.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_provider_wait.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub  # noqa: E402
from check_step_order import STREAM  # noqa: E402

WAITING = {"en": "Waiting for the provider to come back", "ru": "Ждём, пока провайдер снова ответит"}


def check(browser, phone: bool, lang: str, scheme: str) -> list[str]:  # type: ignore[no-untyped-def]
    where = f"{'phone' if phone else 'desktop'}/{lang}/{scheme}"
    problems: list[str] = []
    HOST.status = "running"
    HOST.messages = [message(101, "user", "Carry on with the migration.", run_id="r1")]
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, is_mobile=phone, has_touch=phone, color_scheme=scheme)
    page = context.new_page()
    page.add_init_script(STREAM)
    page.route("**/api/**", stub)

    def event(name: str, payload: dict) -> None:
        page.evaluate(f"window.sendEvent({json.dumps(name)}, {json.dumps({'run_id': 'r1', **payload})})")

    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme={scheme}&lang={lang}")
    page.wait_for_function("typeof window.sendEvent === 'function'", timeout=15000)
    page.wait_for_selector(".msg.user", timeout=15000)
    event("message_start", {})
    event("provider_wait", {"provider": "anthropic", "retry_in": 4.0})
    try:
        page.get_by_text(WAITING[lang]).first.wait_for(timeout=5000)
    except Exception:  # noqa: BLE001 — reported as the check's finding
        problems.append(f"{where}: the waiting line did not appear")
    if os.environ.get("SHOTS"):
        page.screenshot(path=f"{os.environ['SHOTS']}/provider-wait-{where.replace('/', '-')}.png", full_page=False)
    event("provider_wait", {"provider": "anthropic", "retry_in": None})
    event("content_block_delta", {"delta": {"type": "text_delta", "text": "Back on it."}})
    page.wait_for_timeout(400)
    if page.get_by_text(WAITING[lang]).count():
        problems.append(f"{where}: the waiting line stayed after the endpoint answered")
    context.close()
    print(f"{where}: {'ok' if not problems else 'FAILED'}")
    return problems


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for phone, lang, scheme in ((False, "en", "light"), (False, "ru", "dark"), (True, "en", "dark"), (True, "ru", "light")):
            problems += check(browser, phone, lang, scheme)
        browser.close()
    if UNHANDLED.report():
        problems.append("the app asked the stub for a route it does not know")
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
