"""Questions of the Questions tab in the operator's language, in a real browser.

A side question the host translated (a translated session) is drawn in the operator's language — its
heading, its text and its option chips — with "Show original" giving back the agent's words; a chip
picked from the translation still answers with the agent's own option. A question nobody translated
offers "Translate" under its title, which asks the host once and then reads like the first. In English
and Russian; screenshots into ``SHOTS`` when set.

    APP_URL=http://127.0.0.1:8371/app python3 tests/browser/check_question_translate.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_side_questions import BASE, CHROMIUM, SID, UNHANDLED, SideStub, card, side  # noqa: E402

SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None
ACCESS = "If a subscription expires, should recipes that were opened stay readable?"
ACCESS_RU = "Если подписка истекает, должны ли открытые рецепты оставаться доступными?"
OPTIONS = ["Opened ones stay", "Only while subscribed"]
OPTIONS_RU = ["Открытые остаются", "Только пока есть подписка"]
TABS = "Tabs or spaces in the new files?"
TABS_RU = "Табы или пробелы в новых файлах?"
WORDS = {
    "en": {"original": "Show original", "translation": "Show translation", "translate": "Translate", "send": "Send"},
    "ru": {"original": "Показать оригинал", "translation": "Показать перевод", "translate": "Перевести", "send": "Отправить"},
}


class Stub(SideStub):
    def __init__(self, lang: str) -> None:
        super().__init__(lang)
        done = {"state": "done", "from": "en", "to": "ru", "title": "", "text": ACCESS_RU, "options": OPTIONS_RU}
        access = side("ask-access", "qacc01", ACCESS, OPTIONS, "10:00")
        access["detail"]["translation"] = done
        tabs = side("ask-tabs", "qt4bs4", TABS, ["Tabs", "Spaces"], "10:01")
        self.open = [{**access, "translate_into": "ru"}, {**tabs, "translate_into": "ru"}]
        self.translated: list[str] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(route.request.url).path
        if path.startswith("/api/asks/") and path.endswith("/translate") and route.request.method == "POST":
            ask_id = path.split("/")[3]
            self.translated.append(ask_id)
            mark = {"state": "done", "from": "", "to": "ru", "title": "", "text": TABS_RU, "options": ["Табы", "Пробелы"], "asked": True}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"translation": mark}))
        return super().route(route)


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / name))


def walk(browser, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    stub = Stub(lang)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/api/**", stub.route)
    page.goto(f"{BASE}/agents/{SID}?token=t&lang={lang}")
    page.locator(".chat .questions-line").click()
    access = card(page, "qacc01")
    expect(access).to_be_visible()

    # Translated by the host: heading and chips in Russian, the agent's words one click away.
    expect(access.locator(".q-title")).to_have_text(ACCESS_RU)
    expect(access.locator(".q-chip").first).to_contain_text(OPTIONS_RU[0])
    expect(access.locator("[data-translate='offer']")).to_have_count(0)
    shot(page, f"question-translated-{lang}.png")
    access.get_by_role("button", name=words["original"]).click()
    expect(access.locator(".q-title")).to_have_text(ACCESS)
    expect(access.locator(".q-chip").first).to_contain_text(OPTIONS[0])
    access.get_by_role("button", name=words["translation"]).click()
    expect(access.locator(".q-title")).to_have_text(ACCESS_RU)

    # A chip picked from the translation answers with the agent's own option.
    access.locator(".q-chip").first.click()
    page.locator(".questions-panel").get_by_role("button", name=words["send"]).first.click()
    page.wait_for_timeout(500)
    sent = stub.sends[-1]["items"][0] if stub.sends else {}
    assert sent.get("ask_id") == "ask-access" and sent.get("selected") == [OPTIONS[0]], stub.sends

    # Not translated: "Translate" under the title, once.
    tabs = card(page, "qt4bs4")
    expect(tabs.locator(".q-title")).to_have_text(TABS)
    offer = tabs.locator("[data-translate='offer']")
    expect(offer).to_have_text(words["translate"])
    shot(page, f"question-offer-{lang}.png")
    offer.click()
    expect(tabs.locator(".q-title")).to_have_text(TABS_RU)
    expect(tabs.locator(".q-chip").first).to_contain_text("Табы")
    expect(offer).to_have_count(0)
    assert stub.translated == ["ask-tabs"], stub.translated
    tabs.get_by_role("button", name=words["original"]).click()
    expect(tabs.locator(".q-title")).to_have_text(TABS)
    assert stub.translated == ["ask-tabs"], "showing the original asks the host nothing"
    page.close()


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        try:
            for lang in ("en", "ru"):
                walk(browser, lang)
                print(f"ok {lang}")
        finally:
            browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
