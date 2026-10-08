"""Sharing a dialog: the menu opens the same choices a service has, and the link is a page.

The page is not the app. It shows the messages, with no login, and a link that was turned off
says so instead of the conversation.

    cd miniapp && npm run build && cd ..
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8273 /tmp/app-root
    APP_URL=http://127.0.0.1:8273/app python3 tests/browser/check_dialog_share.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402
from screenshots import S1, detail, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SLUG = "demo-share-link"

WORDS = {
    "en": {
        "actions": "Session actions",
        "share": "Share…",
        "private": "Private link",
        "key": "share key",
        "public": "Public",
        "confirm": "Make public",
        "cancel": "Cancel",
        "kicker": "Shared conversation",
        "gone": "This conversation is not shared.",
        "chip": "Public",
    },
    "ru": {
        "actions": "Действия с сессией",
        "share": "Поделиться…",
        "private": "Частная ссылка",
        "key": "ключ для доступа",
        "public": "Публично",
        "confirm": "Открыть всем",
        "cancel": "Отмена",
        "kicker": "Общий диалог",
        "gone": "Этот диалог не открыт.",
        "chip": "Публично",
    },
}


def share_body(mode: str) -> dict:
    url = None if mode == "local" else f"http://127.0.0.1:8273/c/{SLUG}" + ("?key=test-key-value" if mode == "key" else "")
    return {"mode": mode, "slug": SLUG, "key": "test-key-value" if mode == "key" else None, "url": url, "public_base": "http://127.0.0.1:8273"}


def install(page: Page, transcript: dict | None, status: int = 200) -> None:
    # The session's own detail carries its share, as the host's does. Served by the general stub it
    # carried none, so a reload of the session in the middle of the sheet reset the mode the sheet had
    # just set, and about one run in five lost the private link's key a moment after it appeared.
    shared = {"mode": "local"}

    def answer_share(route) -> None:  # type: ignore[no-untyped-def]
        body = json.loads(route.request.post_data or "{}")
        shared["mode"] = body.get("mode") or "local"
        route.fulfill(status=200, content_type="application/json", body=json.dumps(share_body(shared["mode"])))

    def answer_detail(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method != "GET":
            return stub(route)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({**detail(S1), "share": share_body(shared["mode"])}))

    def answer_transcript(route) -> None:  # type: ignore[no-untyped-def]
        if status != 200:
            route.fulfill(status=status, content_type="application/json", body=json.dumps({"detail": "no"}))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(transcript))

    # Registered after the general stub, so these two win where they match.
    page.route("**/api/**", stub)
    page.route(f"**/api/sessions/{S1}", answer_detail)
    page.route("**/api/sessions/*/share", answer_share)
    page.route(f"**/c/{SLUG}/transcript", answer_transcript)


def open_share(page: Page, words: dict[str, str]) -> None:
    page.get_by_role("button", name=words["actions"], exact=True).click()
    # A phone's actions are rows of the bar's ⋮ sheet; a desktop's, a menu under its header button.
    page.wait_for_selector(".ph-session-menu, [role='menu']", timeout=5000)
    sheet = page.locator(".ph-session-menu")
    if sheet.count():
        sheet.get_by_role("button", name=words["share"], exact=True).click()
    else:
        page.get_by_role("menuitem", name=words["share"], exact=True).click()
    expect(page.locator(".access-options")).to_be_visible()


def check_sheet(page: Page, words: dict[str, str], width: int) -> None:
    open_share(page, words)
    public = page.locator("label.access-option", has_text=words["public"])
    public.click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    page.get_by_role("button", name=words["cancel"], exact=True).click()
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(page.locator(".share-field")).to_have_count(0)
    public.click()
    page.get_by_role("button", name=words["confirm"], exact=True).click()
    link = page.locator(".share-field input").first
    expect(link).to_be_visible()
    assert f"/c/{SLUG}" in (link.input_value() or "")
    expect(page.locator(".chat-head .chip", has_text=words["chip"])).to_be_visible()
    page.locator("label.access-option", has_text=words["private"]).click()
    key = page.get_by_label(words["key"])
    expect(key).to_be_visible()
    assert "key=" in (link.input_value() or "") and (key.input_value() or "") in (link.input_value() or "")
    box = page.locator(".sheet, .dialog").last.bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
    assert box["y"] >= 0
    if os.environ.get("SHOTS"):
        tag = "ru" if words["kicker"].startswith("О") else "en"
        page.screenshot(path=f"{os.environ['SHOTS']}/sheet-{tag}-{width}.png")
    page.keyboard.press("Escape")


def check_page(page: Page, words: dict[str, str], width: int) -> None:
    expect(page.locator(".shared-kicker")).to_have_text(words["kicker"])
    expect(page.get_by_text("How does the gate work?")).to_be_visible()
    expect(page.get_by_text("It opens when the slug is known.")).to_be_visible()
    expect(page.locator(".composer")).to_have_count(0)
    expect(page.locator("nav.sidebar, nav.rail")).to_have_count(0)
    user = page.locator(".msg.user").bounding_box()
    answer = page.locator(".answer").bounding_box()
    assert user and answer
    assert user["x"] > answer["x"]
    assert user["x"] + user["width"] <= width
    assert answer["x"] >= 0
    if os.environ.get("SHOTS"):
        tag = "ru" if words["kicker"].startswith("О") else "en"
        page.screenshot(path=f"{os.environ['SHOTS']}/page-{tag}-{width}.png")


TRANSCRIPT = {
    "title": "A shared dialog",
    "older": False,
    "messages": [
        {"role": "user", "text": "How does the gate work?", "at": "2026-09-27T10:00:00Z", "via": "", "media": []},
        {"role": "assistant", "text": "It opens when the slug is known.", "at": "2026-09-27T10:00:05Z", "via": "", "media": []},
    ],
}


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang, words in WORDS.items():
            for width, height in ((1280, 800), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                install(page, TRANSCRIPT)
                page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme=dark", wait_until="domcontentloaded")
                expect(page.locator(".chat-title")).to_be_visible()
                check_sheet(page, words, width)
                page.goto(f"{BASE}/c/{SLUG}?lang={lang}&scheme=dark", wait_until="domcontentloaded")
                check_page(page, words, width)
                page.close()
            locked = browser.new_page(viewport={"width": 1280, "height": 800})
            install(locked, None, status=404)
            locked.goto(f"{BASE}/c/{SLUG}?lang={lang}&scheme=dark", wait_until="domcontentloaded")
            expect(locked.get_by_text(words["gone"])).to_be_visible()
            expect(locked.locator(".composer")).to_have_count(0)
            locked.close()
        browser.close()
    print("dialog share: ok")


if __name__ == "__main__":
    main()
