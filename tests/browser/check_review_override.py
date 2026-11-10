"""A call the auto-review blocked is offered to the operator as "Allow anyway", in the dock and on its row.

The case that asked for it: the review blocked an Exec that pushed screenshots and edited release
notes, the agent told the operator to press "Allow" on the request, and there was nothing to press: the
block was only a red tool error. Now the host holds the block as a request, and the app must show what
was blocked, the reviewer's reason and "Allow anyway", above the composer and on the expanded row, in
English and in Russian, on a desktop and at 390 px; pressing it grants exactly that key.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_review_override.py

Exit 0 when every step holds; ``SHOTS=<dir>`` keeps a picture of each language and width, and
``SCHEME=light`` takes them in the light theme.
"""
from __future__ import annotations

import os
import sys

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, posts, reached, stub
from playwright.sync_api import sync_playwright

KEY = "b10c4ed0be11"
PUSH = {"command": "git push origin docs-shots && gh release edit v0.23.0 --notes-file notes.md"}
REASON = "the operator asked to take screenshots, not to publish them or change the release notes"
BLOCK = (
    f"Blocked by the auto-review: {REASON} (rule review.publish). The action was not taken. Approval key: {KEY}. "
    "The operator sees this call on a card in this chat and in the Inbox, with the reviewer's reason and a button \"Allow anyway\"."
)
WORDS = {
    "en": {"title": "The auto-review blocked Exec", "reason": "Reviewer: ", "allow": "Allow anyway", "keep": "Keep blocked", "row": "Blocked by the auto-review"},
    "ru": {"title": "Автопроверка остановила Exec", "reason": "Проверка: ", "allow": "Разрешить всё равно", "keep": "Оставить запрет", "row": "Остановлено автопроверкой"},
}


def blocked_run() -> list[dict]:
    return [
        message(301, "user", "Take fresh screenshots of the settings page.", run_id="r1"),
        message(302, "assistant", "", run_id="r1", tool_calls=[{"id": "c_push", "name": "Exec", "arguments": PUSH}]),
        message(303, "tool", "", run_id="r1", tool_results=[{"id": "c_push", "content": BLOCK, "is_error": True}]),
        message(304, "assistant", "The auto-review blocked the push; the card below has Allow anyway.", run_id="r1"),
    ]


def scenario(browser, lang: str, width: int, height: int, phone: bool, shots: str | None) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    where = f"{lang} {'phone' if phone else 'desktop'}"
    words = WORDS[lang]
    scheme = os.environ.get("SCHEME", "dark")
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, is_mobile=phone, has_touch=phone)
    try:
        HOST.status = "idle"
        HOST.open = [KEY]
        HOST.messages = blocked_run()
        page = context.new_page()
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme={scheme}&lang={lang}")
        try:
            page.wait_for_selector('.composer .dock.approval[data-kind="blocked"]', timeout=15000)
        except Exception:  # noqa: BLE001 — said as a problem, not raised
            problems.append(f"{where}: the blocked call has no card above the composer")
            return problems
        dock = page.locator('.composer .dock.approval[data-kind="blocked"]')
        said = dock.inner_text()
        for what in (words["title"], words["reason"] + REASON[:40], words["allow"], words["keep"]):
            if what not in said:
                problems.append(f"{where}: the card does not say {what!r} ({said!r})")
        if "git push origin docs-shots" not in said:
            problems.append(f"{where}: the card does not show what was blocked ({said!r})")
        box = dock.bounding_box()
        if not box or box["x"] < 0 or box["x"] + box["width"] > width + 1 or box["y"] < 0 or box["y"] + box["height"] > height + 1:
            problems.append(f"{where}: the card is outside the screen ({box})")
        if page.evaluate("document.documentElement.scrollWidth") > width + 1:
            problems.append(f"{where}: the page scrolls sideways")

        # The row of the blocked call carries the same reason and the same button once opened.
        row = page.locator(".act.error").last
        row.scroll_into_view_if_needed()
        row.click()
        try:
            page.wait_for_selector(".toolcard [data-blocked]", timeout=5000)
            card = page.locator(".toolcard [data-blocked]").last
            text = card.inner_text()
            if words["row"] not in text or words["allow"] not in text or REASON[:40] not in text:
                problems.append(f"{where}: the blocked row does not carry the reason and the button ({text!r})")
            card_box = card.bounding_box()
            if not card_box or card_box["x"] + card_box["width"] > width + 1:
                problems.append(f"{where}: the blocked row's card runs off the screen ({card_box})")
        except Exception:  # noqa: BLE001
            problems.append(f"{where}: the opened row of the blocked call has no Allow anyway")
        if shots:
            page.screenshot(path=os.path.join(shots, f"review-override-{lang}-{'phone' if phone else 'desktop'}-{scheme}.png"))

        before = len(posts("/policy/grant"))
        dock.locator("button", has_text=words["allow"]).first.click()
        if reached(page, "/policy/grant", before, f"{where}: Allow anyway", problems) and posts("/policy/grant")[-1][2] != {"key": KEY}:
            problems.append(f"{where}: Allow anyway granted {posts('/policy/grant')[-1][2]!r}, not the blocked call's key")
    finally:
        context.close()
        HOST.open = []
    return problems


def main() -> int:
    shots = os.environ.get("SHOTS")
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            problems += scenario(browser, lang, 1280, 900, False, shots)
            problems += scenario(browser, lang, 390, 844, True, shots)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    failed = main()
    sys.exit(failed or UNHANDLED.report())
