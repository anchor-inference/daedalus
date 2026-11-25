"""The start page's account: a new chat on a subscription with several accounts starts on the one picked
before its first message. English and Russian, 390 px phone and 1440 px desktop.

What is checked:

- with the default model going through the Claude subscription (three accounts in the stub), the model
  list offers the Account row, the desktop beside the effort and the phone in the model sheet;
- the account picked there is what the chat is set to, in the same change as the model, before the
  message goes, so the first turn already runs on it;
- a model whose client has no accounts shows no row, and the chat it makes names no account.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_start_account.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORK = "k3v9q2ma"
"""The stub's second Claude account, named "work"."""
PRESETS = {
    "opus": {"provider": "claude", "model": "claude-opus-5", "label": "claude-opus-5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 400000, "max_output_tokens": 32000},
    "luna": {"provider": "openai", "model": "gpt-6-luna", "label": "gpt-6-luna", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 400000, "max_output_tokens": 32000},
}


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def posted(self, path: str) -> list[object]:
        return [body for p, body in self.calls if p == path]


def serve(page: Page, host: Host, default: str) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if path == "/api/settings" and request.method == "GET":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"revision": "r1", "model": {"preset": default, "chain": []}, "presets": PRESETS, "providers": {}}))
        if request.method == "POST" and path in ("/api/sessions", f"/api/sessions/{S1}/model", f"/api/sessions/{S1}/messages"):
            host.calls.append((path, request.post_data_json if request.post_data else None))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": S1} if path == "/api/sessions" else {"ok": True, "run_id": "r1"}))
        return stub(r)

    page.route("**/api/**", route)


def run(browser, lang: str, phone: bool, default: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {'390' if phone else '1440'}px {default}"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark")
    page = context.new_page()
    host = Host()
    serve(page, host, default)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    page.wait_for_timeout(800)
    subscription = default == "opus"

    # The account is a chip on the picker's reasoning line, the same in its sheet on a phone.
    if phone:
        page.locator(".ph-top .ph-top-tb").click()
    else:
        composer.locator(".model-select").click()
    rows = page.locator(".model-picker [data-branch='account']")
    page.wait_for_timeout(500)
    if not subscription:
        if rows.count():
            say("a model with no accounts offers an Account row")
        page.keyboard.press("Escape")
    else:
        expect(rows).to_be_visible()
        rows.locator(".model-account-chip").click()
        page.locator(f".account-menu [data-account='{WORK}']").click()
        page.wait_for_timeout(200)
        if page.locator(".model-picker").count():
            page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    composer.locator(".composer-field").fill("Plan the week")
    composer.locator(".roundbtn.primary").click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    order = [p for p, _ in host.calls]
    chosen = host.posted(f"/api/sessions/{S1}/model")
    if subscription:
        if order != ["/api/sessions", f"/api/sessions/{S1}/model", f"/api/sessions/{S1}/messages"]:
            say(f"the requests went {order}")
        if chosen != [{"account": WORK}]:
            say(f"the chat was set to {chosen}")
    elif any("account" in (body or {}) for body in chosen if isinstance(body, dict)):
        say(f"a chat with no accounts was set to {chosen}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (True, False):
                for default in ("opus", "luna"):
                    run(browser, lang, phone, default, problems)
            print(f"start account {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
