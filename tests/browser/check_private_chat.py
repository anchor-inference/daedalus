"""A private chat: chosen in the new chat's + menu, and marked wherever the chat is named afterwards.
English and Russian, 1440 px desktop and 390 px phone.

What is checked:

- the start composer's + menu (a desktop popover, a phone sheet) offers "Private chat"; choosing it puts
  a chip in the composer, dashes the composer's edge and changes the placeholder, and the chip turns it
  off again;
- a chat sent while private is created with ``private: true``, and one sent after the chip turned it
  off is created without the field;
- the New agent sheet offers the same choice and sends it;
- a private session carries the mark on its sidebar row (desktop), on its Chats row and in the drawer
  (phone), and the badge in its header; an ordinary session carries none.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_private_chat.py
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
from screenshots import S1, S4, UNHANDLED, detail, listing, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = {
    "en": {"menu": "Private chat", "badge": "Private", "placeholder": "not remembered"},
    "ru": {"menu": "Приватный чат", "badge": "Приватный", "placeholder": "не запоминается"},
}


class Host:
    def __init__(self) -> None:
        self.created: list[dict] = []


def serve(page: Page, host: Host) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if path in ("/api/sessions", "/api/panel/sessions") and request.method == "POST":
            body = request.post_data_json or {}
            host.created.append(body)
            if not isinstance(body.get("private", False), bool):
                return r.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": "private is true or false"}))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": S1}))
        if request.method == "POST" and path.startswith(f"/api/sessions/{S1}/"):
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "r1"}))
        if path == "/api/sessions" and request.method == "GET":
            # One session is private; the rest are ordinary.
            answer = listing()
            answer["sessions"] = [{**s, "metadata": {**(s.get("metadata") or {}), **({"private": True} if s["id"] == S4 else {})}} for s in answer["sessions"]]
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        if path == f"/api/sessions/{S4}" and request.method == "GET":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({**detail(S4), "private": True}))
        return stub(r)

    page.route("**/api/**", route)


def start(browser, phone: bool):  # type: ignore[no-untyped-def]
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark")
    page = context.new_page()
    host = Host()
    serve(page, host)
    return context, page, host


def choose_private(page: Page, composer, phone: bool) -> None:  # type: ignore[no-untyped-def]
    composer.locator(".plus").click()
    item = page.locator(".ph-plus-sheet [data-private]" if phone else ".plus-menu [data-private]")
    expect(item).to_be_visible()
    item.click()
    if phone:
        composer.locator("textarea").click()


def run(browser, lang: str, phone: bool, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731

    # The choice, its chip, the chip turning it off, and what is sent either way.
    context, page, host = start(browser, phone)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    composer.locator(".plus").click()
    item = page.locator(".ph-plus-sheet [data-private]" if phone else ".plus-menu [data-private]")
    expect(item).to_contain_text(words["menu"])
    if item.get_attribute("aria-checked") != "false":
        say("the private choice starts switched on")
    item.click()
    if phone:
        # The idle pill shares its one row with the field and leaves the chip for the open toolbar.
        expect(composer.locator(".private-chip")).to_be_hidden()
        composer.locator("textarea").click()
    chip = composer.locator(".private-chip")
    expect(chip).to_be_visible()
    expect(chip).to_contain_text(words["badge"])
    if composer.get_attribute("data-private") is None:
        say("the composer does not show that the chat will be private")
    if words["placeholder"] not in (composer.locator("textarea").get_attribute("placeholder") or ""):
        say("the placeholder does not say the chat is private")
    row = composer.locator(".composer-row").bounding_box()
    if row and row["height"] > 44:
        say(f"the chip makes the composer row {row['height']} px tall")
    chip.click()
    expect(chip).to_have_count(0)
    composer.locator("textarea").fill("An ordinary question")
    composer.locator(".roundbtn.primary").click()
    page.wait_for_timeout(600)
    if not host.created or "private" in host.created[-1]:
        say(f"a chat sent after the chip turned it off was not ordinary: {host.created}")
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    choose_private(page, composer, phone)
    expect(composer.locator(".private-chip")).to_be_visible()
    composer.locator("textarea").fill("Something between us")
    composer.locator(".roundbtn.primary").click()
    page.wait_for_timeout(600)
    if not host.created or host.created[-1].get("private") is not True:
        say(f"the private chat was not created private: {host.created}")
    if page.evaluate("document.documentElement.scrollWidth - innerWidth") > 0:
        say("the page scrolls sideways")
    # The New agent sheet, where the other options of a new chat are.
    page.goto(f"{BASE}/agents?new=1&token=t&lang={lang}")
    toggle = page.locator(".sheet [data-private]")
    expect(toggle).to_be_visible(timeout=15000)
    expect(toggle).to_contain_text(words["menu"])
    page.locator(".sheet input.field").first.fill("Kept quiet")
    toggle.locator("input").check()
    page.locator(".sheet .sheet-foot .btn.primary").click()
    page.wait_for_timeout(600)
    if not host.created or host.created[-1].get("private") is not True or host.created[-1].get("title") != "Kept quiet":
        say(f"the New agent sheet did not create a private chat: {host.created[-1:]}")
    context.close()

    # The marks: the private session's row and header, none on an ordinary one.
    context, page, host = start(browser, phone)
    if phone:
        page.goto(f"{BASE}/agents?view=chats&token=t&lang={lang}")
        row = page.locator(f"[data-session='{S4}']").first
        expect(row).to_be_visible(timeout=15000)
        expect(row.locator(".private-mark")).to_be_visible()
        if page.locator(f"[data-session='{S1}'] .private-mark").count():
            say("an ordinary chat is marked private on the Chats page")
        page.goto(f"{BASE}/agents/{S4}?token=t&lang={lang}")
        badge = page.locator(".ph-chat-top .private-badge")
        expect(badge).to_be_visible(timeout=15000)
        expect(badge).to_contain_text(words["badge"])
    else:
        page.goto(f"{BASE}/agents/{S4}?token=t&lang={lang}")
        row = page.locator(f".sidebar [data-session='{S4}']").first
        expect(row).to_be_visible(timeout=15000)
        expect(row.locator(".private-mark")).to_be_visible()
        if page.locator(f".sidebar [data-session='{S1}'] .private-mark").count():
            say("an ordinary chat is marked private in the sidebar")
        badge = page.locator(".chat-title-line .private-badge")
        expect(badge).to_be_visible(timeout=15000)
        expect(badge).to_contain_text(words["badge"])
        page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}")
        expect(page.locator(f".sidebar [data-session='{S1}']").first).to_be_visible(timeout=15000)
        page.wait_for_timeout(400)
        if page.locator(".chat-title-line .private-badge").count():
            say("an ordinary chat carries the private badge")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (False, True):
                try:
                    run(browser, lang, phone, problems)
                except Exception as exc:  # noqa: BLE001 — one failure must not hide the rest
                    problems.append(f"{lang} {'390' if phone else '1440'}px: {exc}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: the private chat choice and its marks")
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
