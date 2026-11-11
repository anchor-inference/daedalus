"""The Agents tab and the approval of a message to another agent, in both languages, schemes and widths.

- the approval over the composer has the host, why the message waits, the whole text, and Send, Edit and
  Don't send; Edit opens the text, and what is sent then is the operator's words;
- the card in the tab holds the same approval, and every limit (messages, turns, dollars) has its own
  "No limit" switch that sends the host's word for none (0 for messages, -1 for waking);
- nothing scrolls sideways, on a phone either.

    APP_URL=http://127.0.0.1:8293/app SHOTS=/tmp/shots python3 tests/browser/check_agents_tab.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeTalks, expect_app  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"edit": "Edit", "edited": "Send edited", "refuse": "Don't send", "free": "No limit", "sends": "Messages to send", "turns": "Turns", "usd": "Dollars", "wake": "Wake on a reply"},
    "ru": {"edit": "Изменить", "edited": "Отправить с правкой", "refuse": "Не отправлять", "free": "Без лимита", "sends": "Сообщений отправить", "turns": "Ходы", "usd": "Доллары", "wake": "Будить при ответе"},
}


def snap(page: Page, name: str) -> None:
    if os.environ.get("SHOTS"):
        page.wait_for_timeout(500)  # a phone's sheet slides in; a picture taken in the middle of it shows both layers
        page.screenshot(path=f"{os.environ['SHOTS']}/{name}.png")


def fits(page: Page, where: str) -> None:
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert over <= 0, f"{over}px of horizontal scroll: {where}"


def check(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    talks = FakeTalks(S1)
    talks.add("t0", live=False)
    api_stub.TALKS = talks
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")

    above = page.locator(".composer [data-talk-held='3']")
    expect(above).to_be_visible()
    expect(above).to_contain_text("Can I order a cake for Sunday?")
    for button in ("send", "edit", "refuse"):
        expect(above.locator(f"[data-action='{button}']")).to_be_visible()
    snap(page, f"approval-{name}")
    fits(page, f"approval {name}")

    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}&panel=agents", wait_until="domcontentloaded")
    card = page.locator("[data-agents-tab] [data-talk='t1']")
    expect(card).to_be_visible()
    expect(card.locator("[data-talk-held='3']")).to_contain_text("Can I order a cake for Sunday?")
    for limit in ("sends", "wake-turns", "wake-usd"):
        expect(card.locator(f"[data-talk-limit='{limit}']")).to_contain_text(words["free"])
    snap(page, f"tab-{name}")
    fits(page, f"tab {name}")

    # Every limit's own "No limit": the cap on sends goes to 0, and waking, once on, to -1 for turns and dollars.
    def switch(label: str):  # type: ignore[no-untyped-def]
        found = card.get_by_role("switch", name=label, exact=True)
        found.click()
        expect(found).to_have_attribute("aria-checked", "true")

    switch(f"{words['sends']}: {words['free']}")
    switch(words["wake"])
    switch(f"{words['turns']}: {words['free']}")
    switch(f"{words['usd']}: {words['free']}")
    expect(card.get_by_role("switch", name=f"{words['usd']}: {words['free']}", exact=True)).to_have_attribute("aria-checked", "true")
    settings = [body for verb, _, body in talks.calls if verb == "PUT"]
    assert {"sends_limit": 0} in settings and {"wake_turns": -1, "wake_usd": -1} in settings, settings
    expect(card.locator(".talk-counts")).not_to_contain_text(" 30")
    snap(page, f"limits-{name}")
    fits(page, f"limits {name}")

    # Edit, then send the operator's own words.
    held = card.locator("[data-talk-held='3']")
    held.locator("[data-action='edit']").click()
    held.locator("[data-talk-edit]").fill("Can I order a cake for Sunday at noon?")
    expect(held.locator("[data-action='send']")).to_have_text(words["edited"])
    snap(page, f"edit-{name}")
    held.locator("[data-action='send']").click()
    expect(page.locator("[data-talk-held]")).to_have_count(0)
    sent = [body for verb, path, body in talks.calls if verb == "POST" and path.endswith("/messages/3/send")]
    assert sent == [{"text": "Can I order a cake for Sunday at noon?"}], sent
    expect(card.locator(".talk-msg[data-role='us']").last).to_contain_text("at noon")
    context.close()


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang in WORDS:
            for scheme in ("light", "dark"):
                for width, height in ((1280, 800), (390, 844)):
                    check(browser, lang, scheme, width, height)
        browser.close()
    if UNHANDLED.report():
        raise SystemExit(1)
    print("agents tab: ok")


if __name__ == "__main__":
    main()
