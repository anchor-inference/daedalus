"""Standing contacts, keys and the operator's own words, in both languages, schemes and widths.

- the Agents tab opens with the Contacts block: this side's own fingerprint, an active contact with its verified key,
  level and spend, and an offer that waits, its words drawn as plain text, accepted from there;
- a contact's conversation says whose key its door proved, and a message the other side's owner wrote is marked as
  theirs; "Write as me" sends the operator's own words;
- a link for several guests lists them in its reader, each with its key or "Unverified", the guest owner's message
  marked, and the operator writes to a guest as themselves;
- nothing scrolls sideways, on a phone either.

    APP_URL=http://127.0.0.1:8293/app SHOTS=/tmp/shots python3 tests/browser/check_contacts.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeContacts, FakeGuests, FakeTalks, expect_app  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {
        "contacts": "Contacts", "mine": "Your key: 41F0-9A2C-D7B3-6E15-08CB", "verified": "Verified key 7C1E-04A9-B3D2-5F80-E6A1", "offer": "Offer", "accept": "Accept",
        "their_owner": "Their owner", "write": "Write as me", "you": "You", "unverified": "Unverified", "guest_owner": "Guest's owner", "actions": "Session actions",
        "menu": "Agent link…", "read": "Read", "guest1": "Guest 1", "guest2": "Guest 2", "trusted": "Trusted",
    },
    "ru": {
        "contacts": "Контакты", "mine": "Ваш ключ: 41F0-9A2C-D7B3-6E15-08CB", "verified": "Проверенный ключ 7C1E-04A9-B3D2-5F80-E6A1", "offer": "Предложение", "accept": "Принять",
        "their_owner": "Их владелец", "write": "Написать от себя", "you": "Вы", "unverified": "Не проверено", "guest_owner": "Владелец гостя", "actions": "Действия с сессией",
        "menu": "Ссылка для агента…", "read": "Читать", "guest1": "Гость 1", "guest2": "Гость 2", "trusted": "Доверенный",
    },
}
OFFER = "We work on the menu every week: <b>shall we</b> keep a standing line? See http://elsewhere.example/x"


def snap(page: Page, name: str) -> None:
    if os.environ.get("SHOTS"):
        page.wait_for_timeout(400)
        page.screenshot(path=f"{os.environ['SHOTS']}/{name}.png")


def fits(page: Page, where: str) -> None:
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert over <= 0, f"{over}px of horizontal scroll: {where}"


def guests_with_many() -> FakeGuests:
    """The chat's links, and one direct link that took two guests: one came with a key, one without."""
    guests = FakeGuests()
    now = time.time()
    first = {**guests.conversation("cm1", "open"), "fingerprint": "2B9D-E310-55AC-0F47-C8D6", "verified": True, "spent_usd": 0.31, "turns_used": 4}
    second = {**guests.conversation("cm2", "open"), "fingerprint": "", "verified": False, "spent_usd": 0.02, "turns_used": 1}
    guests.link("mg", state="waiting", mode="direct", tools="read", label="Partners", topic="Weekly menu", conversation=second, max_guests=0, total_usd=5.0, conversations=[first, second])
    guests.messages["mg"] = [
        {"id": "g1", "role": "guest", "text": "This is Tomas himself, not my agent.", "at": now - 300, "state": "answered", "cost_usd": 0.0, "by": "owner"},
        {"id": "g2", "role": "agent", "text": "Noted, Tomas.", "at": now - 280, "state": "answered", "cost_usd": 0.01},
    ]
    return guests


def check(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    contacts = FakeContacts(seeded=True)
    api_stub.CONTACTS = contacts
    talks = FakeTalks(S1)
    talks.contact_talk()
    api_stub.TALKS = talks
    guests = guests_with_many()
    api_stub.GUESTS = guests
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}&panel=agents", wait_until="domcontentloaded")

    block = page.locator("[data-agents-tab] [data-contacts]")
    expect(block).to_be_visible()
    expect(block.locator(".dt-label")).to_contain_text(words["contacts"])
    expect(block.locator("[data-contacts-mine]")).to_have_text(words["mine"])
    active = block.locator("[data-contact='k1']")
    expect(active.locator("[data-key='verified']")).to_have_text(words["verified"])
    incoming = block.locator("[data-contact='k2']")
    expect(incoming.locator("[data-contact-chip]")).to_have_text(words["offer"])
    # The other side's words, character for character: no tag or link came out of them.
    expect(incoming.locator(".contact-quote .talk-text")).to_have_text(OFFER)
    assert incoming.locator(".contact-quote").locator("b, a").count() == 0
    fits(page, f"contacts {name}")
    snap(page, f"contacts-{name}")

    card = page.locator("[data-agents-tab] [data-talk='t1']")
    expect(card.locator("[data-key='verified']")).to_have_text(words["verified"])
    expect(card.locator("[data-talk-contact='k1']")).to_be_visible()
    marked = card.locator(".talk-msg[data-author='owner'][data-role='them']")
    expect(marked.locator(".talk-msg-head > b")).to_have_text(words["their_owner"])
    card.locator("[data-owner-say] textarea").fill("I will pick it up myself on Monday.")
    card.locator("[data-owner-send]").click()
    expect(card.locator(".talk-msg[data-author='owner'][data-role='us']")).to_contain_text("I will pick it up myself on Monday.")
    expect(card.locator(".talk-msg[data-author='owner'][data-role='us'] .talk-msg-head > b")).to_have_text(words["you"])
    assert any(verb == "POST" and path.endswith("/agent-talks/t1/say") for verb, path, _ in talks.calls)
    card.scroll_into_view_if_needed()
    snap(page, f"contact-talk-{name}")
    fits(page, f"talk {name}")

    # The level of an active contact, changed in place, and an offer taken up in this chat.
    active.get_by_role("radio", name=words["trusted"], exact=True).click()
    incoming.locator("[data-contact-accept]").click()
    expect(block.locator("[data-contact='k2'] [data-contact-chip]")).not_to_have_text(words["offer"])
    puts = [body for verb, path, body in contacts.calls if verb == "PUT" and path == "/api/contacts/k1"]
    accepts = [body for verb, path, body in contacts.calls if verb == "POST" and path == "/api/contacts/k2/accept"]
    assert puts == [{"tools": "trusted"}], puts
    assert accepts and accepts[0]["session_id"] == S1 and accepts[0]["tools"] == "read", accepts

    # A link of several guests, read from the chat's link sheet.
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")
    page.get_by_role("button", name=words["actions"], exact=True).click()
    page.wait_for_selector(".ph-session-menu, [role='menu']", timeout=5000)
    sheet = page.locator(".ph-session-menu")
    if sheet.count():
        sheet.get_by_role("button", name=words["menu"], exact=True).click()
    else:
        page.get_by_role("menuitem", name=words["menu"], exact=True).click()
    link = page.locator("[data-guest-link='mg']")
    expect(link).to_be_visible()
    link.get_by_role("button", name=words["read"], exact=True).click()
    reader = page.locator("[data-guest-reader]")
    picks = reader.locator("[data-guest-guests] [data-guest-pick]")
    expect(picks).to_have_count(2)
    expect(picks.nth(0)).to_contain_text(words["guest1"])
    expect(picks.nth(1)).to_contain_text(words["guest2"])
    expect(reader.locator("[data-key='unverified']")).to_have_text(words["unverified"])
    picks.nth(0).click()
    expect(reader.locator("[data-key='verified']")).to_contain_text("2B9D-E310-55AC-0F47-C8D6")
    expect(reader.locator(".guest-msg[data-author='owner'] .guest-msg-head > b")).to_have_text(words["guest_owner"])
    reader.locator("[data-owner-say] textarea").fill("Hello Tomas, it is me.")
    reader.locator("[data-owner-send]").click()
    page.wait_for_timeout(300)
    said = [body for verb, path, body in guests.calls if verb == "POST" and path.endswith("/say")]
    assert said and said[0] == {"text": "Hello Tomas, it is me."}, said
    snap(page, f"guests-{name}")
    fits(page, f"guests {name}")
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
    print("contacts: ok")


if __name__ == "__main__":
    main()
