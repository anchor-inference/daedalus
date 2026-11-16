"""Several accounts per subscription, in Settings → Models → Providers.

What is checked:

- the card lists each subscription with its accounts in order: who each one is (e-mail and plan), whether it
  is signed in, a spent account resting with when it is back, its usage windows and its calls of the day;
- the default account and the fallover switch are offered only where there is more than one account;
- Add account posts the name, then opens the new account's sign-in and goes to its terminal;
- Remove asks first, then deletes that account, and the default account has no Remove at all;
- a plan's account takes a key, and Add stays off until one is typed;
- every word is in English and in Russian, and nothing reaches past a phone's screen.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_accounts.py

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
from screenshots import UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = {
    "en": {"title": "Subscription accounts", "resting": "resting: usage limit, back in", "on": "signed in", "off": "not signed in", "default": "default", "add": "Add account", "remove": "Remove", "fallover": "Switch account at a limit", "calls": "41 calls in 24 h", "default_title": "Default account"},
    "ru": {"title": "Аккаунты подписок", "resting": "пауза: лимит использования, вернётся через", "on": "вход выполнен", "off": "нет входа", "default": "по умолчанию", "add": "Добавить аккаунт", "remove": "Удалить", "fallover": "Переключать аккаунт при лимите", "calls": "запросов за 24 ч: 41", "default_title": "Аккаунт по умолчанию"},
}
OUTSIDE = """() => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll('[data-setting="accounts"] *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    let clipped = false;
    for (let p = el.parentElement; p; p = p.parentElement) { if (getComputedStyle(p).overflowX !== 'visible') { clipped = true; break; } }
    if (!clipped && (r.left < -0.5 || r.right > vw + 0.5)) out.push((el.className || el.tagName) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return out.slice(0, 5);
}"""


def settings(browser, lang: str, width: int) -> list[str]:  # type: ignore[no-untyped-def]
    words, problems = WORDS[lang], []
    phone = width < 600
    sent: list[tuple[str, str, dict]] = []

    def accounts(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0].split("/api/", 1)[1]
        if request.method == "GET":
            return stub(route)
        body = json.loads(request.post_data or "{}")
        sent.append((request.method, path, body))
        if request.method == "POST" and path == "subscription-accounts/claude":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"upstream": "claude", "id": "n3w4cc0t", "label": body.get("label", "")}))
        if request.method == "POST" and path.endswith("/login"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"terminal_id": "t-signin", "command": "claude auth login --claudeai"}))
        return route.fulfill(status=200, content_type="application/json", body="{}")

    context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=phone, has_touch=phone)
    page = context.new_page()
    page.set_default_timeout(8000)
    page.route("**/api/**", stub)
    page.route("**/api/subscription-accounts**", accounts)
    page.goto(f"{BASE}/settings/models?tab=providers&token=t&lang={lang}")
    card = page.locator('[data-setting="accounts"]')
    card.scroll_into_view_if_needed()
    expect(card.locator(".section-title").first).to_have_text(words["title"])

    claude = card.locator('[data-account-upstream="claude"]')
    rows = claude.locator(".account-row")
    expect(rows).to_have_count(3)
    first = rows.nth(0)
    expect(first.locator(".mmeta")).to_have_text("someone@example.org · max")
    expect(first.locator(".chip.accent")).to_have_text(words["default"])
    expect(first.locator("[data-account-status]")).to_contain_text(words["resting"])
    expect(first.locator(".account-facts")).to_contain_text(words["calls"])
    expect(first.locator("[data-account-usage]")).to_contain_text("5h")
    if first.locator('[data-action="remove"]').count():
        problems.append(f"{lang} {width}: the default account offers Remove")
    expect(rows.nth(1).locator("[data-account-status]")).to_have_text(words["on"])
    expect(rows.nth(2).locator("[data-account-status]")).to_have_text(words["off"])
    expect(rows.nth(2).locator(".account-command")).to_contain_text("CLAUDE_CONFIG_DIR=")
    expect(claude.get_by_text(words["fallover"], exact=True)).to_be_visible()
    expect(claude.get_by_text(words["default_title"], exact=True)).to_be_visible()
    # One account: nothing to choose between, so neither the default nor the switch is offered.
    codex = card.locator('[data-account-upstream="codex"]')
    if codex.get_by_text(words["fallover"], exact=True).count():
        problems.append(f"{lang} {width}: a subscription with one account offers the fallover switch")
    # A plan's account is a key: Add waits for one.
    opencode = card.locator('[data-account-upstream="opencode"]')
    expect(opencode.locator('.account-add [data-action="add"]')).to_be_disabled()
    if phone and (outside := page.evaluate(OUTSIDE)):
        problems.append(f"{lang} {width}: the accounts card reaches past the screen: {outside}")

    claude.locator(".account-add input").fill("second work")
    claude.locator('.account-add [data-action="add"]').click()
    page.wait_for_url("**/terminals/t-signin**")
    if sent[:2] != [("POST", "subscription-accounts/claude", {"label": "second work"}), ("POST", "subscription-accounts/claude/n3w4cc0t/login", {})]:
        problems.append(f"{lang} {width}: Add account sent {sent[:2]}")

    page.goto(f"{BASE}/settings/models?tab=providers&token=t&lang={lang}")
    work = page.locator('[data-account-upstream="claude"] [data-account="k3v9q2ma"]')
    work.scroll_into_view_if_needed()
    work.locator('[data-action="remove"]').click()
    dialog = page.locator(".sheet-backdrop.confirm .dialog")
    expect(dialog).to_be_visible()
    dialog.locator("button").last.click()
    page.wait_for_timeout(400)
    if ("DELETE", "subscription-accounts/claude/k3v9q2ma", {}) not in sent:
        problems.append(f"{lang} {width}: Remove sent {sent[2:]}")
    context.close()
    return problems


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (1440, 390):
                problems += settings(browser, lang, width)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("a subscription's accounts are listed, added with their sign-in and removed from Settings")
    return max(1 if problems else 0, UNHANDLED.report())


if __name__ == "__main__":
    sys.exit(main())
