"""A browser tool's permission answered for its whole MCP server, and the grants it leaves revocable.

At 1440 px, in English and in Russian, on Ira (Claude Code) with two requests open: the browser's
BrowserNavigate, a tool of the ``daedalus_browser`` MCP server, offers "Always, all daedalus_browser
tools" beside "Always", and pressing it sends ``allow``, ``always`` and ``server``; the Bash request
beside it offers "Always" and no server-wide answer, since a built-in tool's only rule would be all
of it. The Session tab lists her standing grants — the one given earlier and the server's, once
granted — each with Revoke, which takes it off the list. The Questions tab offers the same choice
for the same request and sends the same body.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_staff_permissions.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_staff_view import DESK, Check, open_page, sideways, stand  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from terminal_stub import DEBUG  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"always": "Always", "server": "Always, all daedalus_browser tools", "rules": "Standing permissions", "snapshot": "BrowserSnapshot of daedalus_browser",
           "whole": "all daedalus_browser tools", "revoke": "Revoke", "revoked": "Revoked"},
    "ru": {"always": "Всегда", "server": "Всегда, все инструменты daedalus_browser", "rules": "Постоянные разрешения", "snapshot": "BrowserSnapshot из daedalus_browser",
           "whole": "все инструменты daedalus_browser", "revoke": "Отозвать", "revoked": "Отозвано"},
}


def permissions(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    focus, term, pid = stand(lang)
    focus.browser_permission_of_ira()
    feed = EventFeed()
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    context.add_init_script(DEBUG)
    page = open_page(context, focus, term, feed, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-request[data-ask='b9w4rx'] .ask-answers-row .btn", timeout=15000)

    browse = page.locator(".staff-request[data-ask='b9w4rx']")
    bash = page.locator(".staff-request[data-ask='k7m2qd']")
    expect(browse.locator("[data-answer='always']")).to_have_text(words["always"])
    expect(browse.locator("[data-answer='always-server']")).to_have_text(words["server"])
    expect(bash.locator("[data-answer='always']")).to_have_count(1)
    check.that(bash.locator("[data-answer='always-server']").count() == 0, f"{lang}: the Bash request offers a server-wide always")

    # The standing grant given earlier, in the operator's words, with its Revoke.
    side = page.locator(".staff-aside")
    rules = side.locator(".staff-rules-section")
    expect(rules.locator(".staff-aside-label")).to_have_text(words["rules"], timeout=5000)
    expect(rules.locator(".staff-rule")).to_have_count(1)
    expect(rules.locator(".staff-rule-text")).to_have_text(words["snapshot"])

    browse.locator("[data-answer='always-server']").click()
    page.wait_for_timeout(400)
    sent = [body for ask_id, body in focus.answers if ask_id == "ask-browse"]
    check.that(sent == [{"allow": True, "always": True, "server": True}], f"{lang}: the server-wide always sent {sent}")
    expect(rules.locator(".staff-rule")).to_have_count(2, timeout=5000)
    expect(rules.locator(".staff-rule[data-rule='mcp__daedalus_browser'] .staff-rule-text")).to_have_text(words["whole"])

    rules.locator(".staff-rule[data-rule='mcp__daedalus_browser__BrowserSnapshot'] .btn").click()
    expect(rules.locator(".staff-rule")).to_have_count(1, timeout=5000)
    check.that(focus.revoked == [("st-ira", "mcp__daedalus_browser__BrowserSnapshot")], f"{lang}: revoked {focus.revoked}")
    check.that(sideways(page) <= 0, f"{lang}: the staff view scrolls sideways")

    # The Questions tab of the project offers the same answer for the same request.
    focus2, term2, pid2 = stand(lang)
    focus2.browser_permission_of_ira()
    page2 = open_page(context, focus2, term2, feed, f"{BASE}/orchestration/project/{pid2}?panel=questions&token=t&lang={lang}")
    card = page2.locator(".q-card[data-ask='b9w4rx']")
    expect(card).to_be_visible(timeout=15000)
    chips = card.locator(".q-chip")
    keys = [chips.nth(i).get_attribute("data-option") for i in range(chips.count())]
    check.that(keys == ["allow", "always", "server", "deny", "because"], f"{lang}: the list's browser card offers {keys}")
    expect(card.locator(".q-chip[data-option='server']")).to_have_text(words["server"])
    other = page2.locator(".q-card[data-ask='k7m2qd'] .q-chip")
    others = [other.nth(i).get_attribute("data-option") for i in range(other.count())]
    check.that("server" not in others and "always" in others, f"{lang}: the list's Bash card offers {others}")
    card.locator(".q-chip[data-option='server']").click()
    page2.locator(".questions-send-btn").click()
    page2.wait_for_timeout(600)
    batches = [items for _, items in focus2.batches]
    check.that(any({"ask_id": "ask-browse", "allow": True, "always": True, "server": True} in items for items in batches), f"{lang}: the list sent {batches}")
    context.close()
    feed.close()


def run() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            permissions(browser, lang, check)
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL", problem)
    if not check.problems and not unhandled:
        print("the server-wide always and the standing grants hold")
    return 1 if check.problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
