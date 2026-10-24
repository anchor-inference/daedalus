"""A subscription CLI the key proxy imitates is updated with one button, from Settings and from its notification.

The reported fault: the key proxy presented Claude Code as a version the vendor had left behind, and
the only fix was a new image. What is checked:

- Settings → Models → Providers lists each imitated CLI inside the key proxy card with the version it
  presents; Claude Code, one release behind, says the newer one is out and offers Update;
- Update posts ``/api/keyproxy/clients/claude/update``, the row then says it is up to date and waits
  for its first request, names the header values the update kept, and offers Revert;
- the notification for the release carries the same Update button, which answers through
  ``/api/notifications/<id>/act`` and shows the outcome in place;
- every word is in English and in Russian, and nothing reaches past a phone's screen.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_keyproxy_clients.py

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
    "en": {"out": "2.1.310 is out", "update": "Update", "current": "up to date", "kept": "Kept as they were: X-Stainless-Package-Version", "revert": "Revert", "title": "Subscription client versions", "done": "Updated to 2.1.310", "pending": "awaiting its first request"},
    "ru": {"out": "вышла 2.1.310", "update": "Обновить", "current": "актуальна", "kept": "Оставлено как было: X-Stainless-Package-Version", "revert": "Откатить", "title": "Версии клиентов подписок", "done": "Обновлено до 2.1.310", "pending": "ждёт первого запроса"},
}
OUTSIDE = """() => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll('[data-setting="keyproxy.clients"] *')) {
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
    posted: list[str] = []

    def record(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method == "POST":
            posted.append(route.request.url.split("?", 1)[0].split("/api/", 1)[1])
        return stub(route)

    context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=phone, has_touch=phone)
    page = context.new_page()
    page.set_default_timeout(8000)
    page.route("**/api/**", stub)
    page.route("**/api/keyproxy/clients**", record)
    page.goto(f"{BASE}/settings/models?tab=providers&token=t&lang={lang}")
    section = page.locator('[data-setting="keyproxy.clients"]')
    section.scroll_into_view_if_needed()
    expect(section.locator(".section-title")).to_have_text(words["title"])
    claude = section.locator('[data-client="claude"]')
    expect(claude.locator(".mtitle")).to_contain_text("2.1.296")
    expect(claude.locator(".pill")).to_have_text(words["out"])
    expect(section.locator('[data-client="codex"] .pill')).to_have_text(words["current"])
    if section.locator('[data-client="codex"] [data-action="update"]').count():
        problems.append(f"{lang} {width}: a client that is up to date offers Update")
    if phone and (outside := page.evaluate(OUTSIDE)):
        problems.append(f"{lang} {width}: the client versions reach past the screen: {outside}")

    claude.locator('[data-action="update"]').click()
    # Up to date, and on trial until its first request answers.
    expect(claude.locator(".pill.idle")).to_have_text(words["current"])
    expect(claude.locator(".pill.waiting")).to_have_text(words["pending"])
    expect(claude.locator(".mtitle")).to_contain_text("2.1.310")
    expect(claude.locator(".clients-said")).to_contain_text(words["kept"])
    expect(claude.locator('[data-action="revert"]')).to_have_text(words["revert"])
    if posted != ["keyproxy/clients/claude/update"]:
        problems.append(f"{lang} {width}: Update posted {posted}")
    section.locator('[data-action="check"]').click()
    page.wait_for_timeout(300)
    if posted[-1:] != ["keyproxy/clients/check"]:
        problems.append(f"{lang} {width}: Check now posted {posted}")
    context.close()
    return problems


def notification(browser, lang: str) -> list[str]:  # type: ignore[no-untyped-def]
    words, problems = WORDS[lang], []
    acted: list[dict[str, object]] = []
    entry = {
        "id": 72, "at": "2026-10-24T10:00:00Z", "updated_at": "2026-10-24T10:00:00Z", "category": "system", "kind": "keyproxy_client", "level": "normal", "tone": "info",
        "title": "Claude Code in the key proxy is 2.1.296; 2.1.310 is out", "body": "The key proxy presents your Claude Code subscription as version 2.1.296.", "link": "/app/settings/models?tab=providers",
        "session_id": None, "run_id": None, "project_id": None, "staff_id": None, "terminal_id": None, "source": "keyproxy", "dedupe_key": "keyproxy:claude:2.1.310",
        "request_ref": "keyproxy:claude:2.1.310", "count": 1, "seen": False, "resolved": None, "needs_you": True, "delivered": {},
        "actions": [{"id": "update", "label": words["update"], "style": "primary", "quick": False}, {"id": "open", "label": "Open", "style": "ghost", "quick": False}],
    }

    def notices(route) -> None:  # type: ignore[no-untyped-def]
        path = route.request.url.split("?", 1)[0]
        if route.request.method == "POST" and path.endswith("/api/notifications/72/act"):
            acted.append(json.loads(route.request.post_data or "{}"))
            done = {**entry, "resolved": words["done"], "needs_you": False, "seen": True}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"resolution": words["done"], "notification": done, "conflict": False}))
        if route.request.method != "GET":
            return stub(route)
        if path.endswith("/api/notifications"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"entries": [entry], "next_before": None, "summary": {"unseen": 1, "needs_you": 1}}))
        if path.endswith("/api/notifications/summary"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"unseen": 1, "needs_you": 1}))
        return stub(route)

    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(8000)
    page.route("**/api/**", stub)
    page.route("**/api/notifications**", notices)
    page.goto(f"{BASE}/inbox?token=t&lang={lang}")
    row = page.locator('[data-notice="72"]').first
    expect(row).to_be_visible(timeout=15000)
    button = row.locator('[data-action="update"]')
    expect(button).to_have_text(words["update"])
    button.click()
    expect(row.locator(".notice-outcome")).to_have_text(words["done"])
    if acted != [{"action": "update"}]:
        problems.append(f"{lang}: the notification's Update sent {acted}")
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
            problems += notification(browser, lang)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("the key proxy's client versions update from Settings and from the notification")
    return max(1 if problems else 0, UNHANDLED.report())


if __name__ == "__main__":
    sys.exit(main())
