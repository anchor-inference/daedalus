"""What agents do together over a link, in both languages, schemes and widths.

- the answers to one question put to several agents gather over the composer as a table: one row an agent, its
  state, and its answer as plain text even where a stranger wrote markup; Put away posts and the table goes, and
  the Agents tab keeps it under its own heading;
- a conversation's thread draws a card beside a message (free times), and the conversation keeps the other side's
  summary, split into what we and they promised; Summary asks for ours, To notes and To board post;
- a link's conversation in the Agent link sheet shows our summary, the guest's subscription with End, the actions
  its turns asked for with Close access on a service shown, and a meeting card in the thread;
- nothing scrolls sideways, on a phone either.

    APP_URL=http://127.0.0.1:8293/app SHOTS=/tmp/shots python3 tests/browser/check_link_flows.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeGuests, FakeTalks, expect_app  # noqa: E402
from check_link_files import fits, open_sheet, snap, snap_element  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"answered": "Answered", "waiting": "Waiting", "line": "2 of 3 answered", "ours": "We promised", "theirs": "They promised", "actions": "Session actions", "menu": "Agent link…",
           "read": "Read", "booked": "Booked", "subs": "Tell me when", "revoke": "Close access", "polls": "Questions to several"},
    "ru": {"answered": "Ответил", "waiting": "Ждём", "line": "Ответили 2 из 3", "ours": "Мы обещали", "theirs": "Они обещали", "actions": "Действия с сессией", "menu": "Ссылка для агента…",
           "read": "Читать", "booked": "В календаре", "subs": "Сообщить, когда", "revoke": "Закрыть доступ", "polls": "Вопросы нескольким"},
}


def calls() -> list[tuple[str, str]]:
    return list(api_stub.FLOW_CALLS)


def check_chat(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    api_stub.GUESTS = FakeGuests()
    api_stub.TALKS = FakeTalks(S1, flows=True)
    api_stub.FLOW_CALLS.clear()
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)

    # Over the composer: the comparison, filling as the answers come.
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")
    poll = page.locator(".composer [data-poll='p-cake']")
    expect(poll).to_be_visible()
    rows = poll.locator("tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator(".lf-chip")).to_have_text(words["answered"])
    expect(rows.nth(2).locator(".lf-chip")).to_have_text(words["waiting"])
    expect(poll.locator(".lf-poll-line")).to_contain_text(words["line"])
    answer = rows.nth(0).locator(".lf-answer")
    expect(answer).to_contain_text("<b>Order now</b>")
    assert answer.locator("b, a").count() == 0, "a stranger's answer is drawn as text"
    # An answer shows a few lines and gives the rest on hover: it cannot push the other rows out of the table.
    assert answer.evaluate("e => e.scrollHeight > e.clientHeight") or width >= 1024, "a long answer is clamped on a phone"
    snap_element(page, ".composer [data-poll='p-cake']", f"poll-{name}")
    fits(page, f"poll {name}")

    poll.locator("[data-poll-dismiss]").click()
    expect(page.locator(".composer [data-poll]")).to_have_count(0)
    assert ("agent-polls", "p-cake/dismiss") in calls(), calls()

    # The Agents tab keeps every question under its own heading, and a conversation its cards and summaries.
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}&panel=agents", wait_until="domcontentloaded")
    tab = page.locator("[data-agents-tab]")
    group = tab.locator("[data-group='polls']")
    expect(group).to_contain_text(words["polls"])
    expect(group.locator("[data-poll='p-cake']")).to_be_visible()
    card = tab.locator("[data-talk='t1']")
    expect(card.locator("[data-talk-messages] [data-card='slots'] li")).to_have_count(2)
    theirs = card.locator("[data-summary][data-direction='theirs']")
    expect(theirs.locator("[data-side='us']")).to_contain_text(words["ours"])
    expect(theirs.locator("[data-side='them']")).to_contain_text("Confirm the order by Friday noon.")
    assert theirs.locator("b, a").count() == 1, "only the card's own title is bold; the summary's words are text"
    card.locator("[data-summary-make]").click()
    expect(card.locator("[data-summary][data-direction='mine']")).to_have_count(1)
    card.locator("[data-summary][data-direction='mine'] [data-summary-note]").click()
    card.locator("[data-summary][data-direction='mine'] [data-summary-board]").click()
    page.wait_for_timeout(200)
    assert ("link-cards", "2/note") in calls() and ("link-cards", "2/board") in calls(), calls()
    assert any(path.endswith("/summary") for verb, path, _ in api_stub.TALKS.calls if verb == "POST"), api_stub.TALKS.calls
    card.locator("[data-summaries]").scroll_into_view_if_needed()
    snap(page, f"tab-{name}")
    snap_element(page, "[data-agents-tab] [data-talk='t1'] [data-summaries]", f"summary-{name}")
    fits(page, f"tab {name}")
    context.close()


def check_link(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    """The owner's side: a link's conversation with our summary, the guest's subscription and the actions."""
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    guests = FakeGuests(flows=True)
    api_stub.GUESTS = guests
    api_stub.TALKS = FakeTalks(S1)
    api_stub.FLOW_CALLS.clear()
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")
    open_sheet(page, words)
    page.locator("[data-guest-link='1a2b']").get_by_role("button", name=words["read"], exact=True).click()
    reader = page.locator("[data-guest-reader]")
    expect(reader).to_be_visible()
    expect(reader.locator(".guest-msg [data-card='meeting'] .lf-chip")).to_have_text(words["booked"])
    ours = reader.locator("[data-summary][data-direction='mine']")
    expect(ours.locator("[data-side='us']")).to_contain_text("Confirm the order by Friday noon.")
    subs = reader.locator("[data-link-subs]")
    expect(subs).to_contain_text(words["subs"])
    expect(reader.locator("[data-link-actions] [data-action-kind='patch']")).to_contain_text("fix/prices")
    reader.locator("[data-summaries]").scroll_into_view_if_needed()
    snap(page, f"reader-{name}")
    fits(page, f"reader {name}")

    reader.locator("[data-summary-make]").click()
    expect(reader.locator("[data-summary][data-direction='mine']")).to_have_count(2)
    assert any(path.endswith("/1a2b/summary") for verb, path, _ in guests.calls if verb == "POST"), guests.calls
    subs.locator("[data-sub-end]").click()
    expect(subs.locator("[data-sub-end]")).to_have_count(0)
    reader.locator("[data-action-kind='service']").get_by_role("button", name=words["revoke"], exact=True).click()
    expect(reader.locator("[data-action-kind='service'] button")).to_have_count(0)
    assert ("link-subscriptions", "sub_1/end") in calls() and ("link-actions", "act-svc/revoke") in calls(), calls()
    fits(page, f"reader after {name}")
    context.close()


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang in WORDS:
            for scheme in ("light", "dark"):
                for width, height in ((1280, 800), (390, 844)):
                    check_chat(browser, lang, scheme, width, height)
                    check_link(browser, lang, scheme, width, height)
        browser.close()
    if UNHANDLED.report():
        raise SystemExit(1)
    print("link flows: ok")


if __name__ == "__main__":
    main()
