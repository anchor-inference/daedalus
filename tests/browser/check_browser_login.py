"""The agent's request to be signed in is answered in a private form, in the conversation and the Inbox.

- a group whose "needs you" carries a sign-in request draws the form under the session's conversation:
  the site, the agent's reason, a username field, a masked password field and a code field, none of
  which offers to remember what is typed (``autocomplete="off"``);
- signing in posts the values once to ``POST /api/browsers/{group}/login`` with the request's id, says
  where the page ended in a toast, and the form goes when the request is over;
- a site with a saved sign-in offers it first; "Always in this session" posts ``use_saved: always``;
- the Inbox's "needs you" entry for a sign-in draws the same form;
- every word is in English and in Russian.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_login.py

Exit 0 when every step holds.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PASSWORD = "FAKE-PASSWORD-check-5"
WORDS = {
    "en": {"title": "Sign in to acme.example", "submit": "Sign in", "done": "Signed in to acme.example", "always": "Always in this session", "saved": "A sign-in is saved for acme.example"},
    "ru": {"title": "Вход на acme.example", "submit": "Войти", "done": "Вход на acme.example выполнен", "always": "Всегда в этой сессии", "saved": "Для acme.example сохранён вход"},
}


def in_conversation(browser, scenes, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    bs.login_ask("g1", "acme.example")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}")
    card = page.locator(".chat-scroll .login-card")
    expect(card).to_be_visible(timeout=10000)
    expect(card).to_contain_text(words["title"])
    expect(card).to_contain_text("read the invoices")
    assert card.get_attribute("autocomplete") == "off", f"{lang}: the form offers to remember"
    fields = card.locator("input.field")
    assert fields.count() == 3, f"{lang}: {fields.count()} fields"
    password = card.locator("input[type=password]")
    assert password.count() == 1, f"{lang}: the password is not masked"
    for i in range(3):
        assert fields.nth(i).get_attribute("autocomplete") == "off", f"{lang}: field {i} offers to remember"
    fields.nth(0).fill("someone@example.com")
    password.fill(PASSWORD)
    card.get_by_role("button", name=words["submit"], exact=True).click()
    expect(page.get_by_text(words["done"])).to_be_visible(timeout=5000)
    sent = bs.posted("/g1/login")
    assert len(sent) == 1 and sent[0] == {"login_id": "l1", "username": "someone@example.com", "password": PASSWORD, "save": False}, f"{lang}: sent {sent!r}"
    expect(page.locator(".login-card")).to_have_count(0, timeout=10000)
    assert PASSWORD not in page.content(), f"{lang}: the password stayed in the page"
    context.close()


def saved_offered(browser, scenes, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    bs.login_ask("g1", "acme.example", saved=True, login_id="l2")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}")
    card = page.locator(".chat-scroll .login-card")
    expect(card).to_contain_text(words["saved"], timeout=10000)
    assert card.locator("input[type=password]").count() == 0, f"{lang}: the saved sign-in did not come first"
    card.get_by_role("button", name=words["always"]).click()
    expect(page.get_by_text(words["done"])).to_be_visible(timeout=5000)
    assert bs.posted("/g1/login") == [{"login_id": "l2", "use_saved": "always"}], f"{lang}: sent {bs.posted('/g1/login')!r}"
    context.close()


def in_inbox(browser, scenes, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    bs.login_ask("g1", "acme.example", login_id="l3")
    entry = {
        "id": 71, "at": "2026-10-13T10:00:00Z", "updated_at": "2026-10-13T10:00:00Z", "category": "question", "kind": "browser_login", "level": "urgent", "tone": "warning",
        "title": "Invoices asks you to sign in to acme.example", "body": "sign in to acme.example: read the invoices", "link": f"/app/agents/{S1}?panel=browser", "session_id": S1,
        "run_id": None, "project_id": None, "staff_id": None, "terminal_id": None, "source": "browser", "dedupe_key": "browser:g1", "request_ref": "browser:g1", "count": 1,
        "actions": [{"id": "open", "label": "Open", "style": "primary", "quick": False}], "seen": False, "resolved": None, "needs_you": True, "delivered": {},
    }

    def notices(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method != "GET":
            return stub(route)
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/api/notifications"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"entries": [entry], "next_before": None, "summary": {"unseen": 1, "needs_you": 1}}))
        if path.endswith("/api/notifications/summary"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"unseen": 1, "needs_you": 1}))
        return stub(route)

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", stub)
    bs.install(page)
    page.route("**/api/notifications**", notices)
    page.goto(f"{BASE}/inbox?token=t&scheme=dark&lang={lang}")
    card = page.locator(".needs-you .login-card")
    expect(card).to_be_visible(timeout=15000)
    expect(card).to_contain_text(words["title"])
    card.locator("input.field").nth(0).fill("someone@example.com")
    card.locator("input[type=password]").fill(PASSWORD)
    card.get_by_role("button", name=words["submit"], exact=True).click()
    expect(page.get_by_text(words["done"])).to_be_visible(timeout=5000)
    assert [b["login_id"] for b in bs.posted("/g1/login")] == ["l3"], f"{lang}: sent {bs.posted('/g1/login')!r}"
    context.close()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            in_conversation(browser, scenes, lang)
            saved_offered(browser, scenes, lang)
            in_inbox(browser, scenes, lang)
        browser.close()
    print("browser private sign-in holds")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
