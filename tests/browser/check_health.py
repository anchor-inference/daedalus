"""Settings → Health: the integration card above the doctor's list, at 1280 and 390 px, in both languages.

GitHub, each MCP server and each model provider is one row. A failing row says what to do in one
line, in the page's language; its technical text from the host is folded away under Details and
opens on a press. The card is drawn from the quick answer first and then from the probed one, so a GitHub
token the probe rejects turns its row red without a reload. Nothing scrolls sideways.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_health.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from screenshots import BASE, CHROMIUM, UNHANDLED, stub  # noqa: E402

SHOTS = os.environ.get("SHOTS", "")

ROWS = [
    {"kind": "github", "name": "GitHub", "state": "configured", "severity": "ok", "detail": ""},
    {"kind": "mcp", "name": "calendar", "state": "connected", "severity": "ok", "detail": "4 tools"},
    {"kind": "mcp", "name": "notes", "state": "auth_required", "severity": "fail", "detail": "authorization required: the refresh token was revoked"},
    {"kind": "mcp", "name": "files", "state": "idle", "severity": "info", "detail": ""},
    {"kind": "provider", "name": "deepseek", "state": "no_key", "severity": "fail", "detail": "deepseek: no API key in the settings or the environment"},
    {"kind": "provider", "name": "router", "state": "rate_limited", "severity": "warn", "detail": "HTTP 429 at 2026-10-05T10:00:00+00:00"},
]
PROBED = [{**ROWS[0], "state": "token_rejected", "severity": "fail", "detail": "authentication_rejected"}, *ROWS[1:]]

WORDS = {
    "en": {"title": "Integrations", "rejected": "Token rejected", "signin": "Sign-in required", "signin_fix": "sign in to this server again",
           "nokey_fix": "Add the key in Settings → Models.", "idle": "Connects when a session turns it on", "details": "Details"},
    "ru": {"title": "Интеграции", "rejected": "Токен отклонён", "signin": "Нужен вход", "signin_fix": "заново войти на этот сервер",
           "nokey_fix": "Добавьте ключ в Настройки → Модели.", "idle": "Подключится, когда сессия его включит", "details": "Подробности"},
}


class Host:
    """The two integration answers: quick, then probed, the second held until the page asks for it."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        url = route.request.url
        rel = url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):]
        if rel == "/api/integrations/health":
            probed = "probe=true" in url
            self.asked.append("probe" if probed else "quick")
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"rows": PROBED if probed else ROWS}))
        return stub(route)


def check(page: Page, lang: str, width: int) -> None:
    words = WORDS[lang]
    where = f"{lang} {width}"
    host = Host()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/health?token=t&lang={lang}")
    card = page.locator(".card.integrations")
    expect(card).to_be_visible(timeout=15000)
    expect(card.locator(".section-title")).to_have_text(words["title"])
    rows = card.locator(".integration")
    expect(rows).to_have_count(len(ROWS))
    # The probed answer replaces the quick one: GitHub's token is rejected.
    github = rows.nth(0)
    expect(github).to_have_attribute("data-state", "token_rejected")
    expect(github).to_contain_text(words["rejected"])
    expect(github.locator(".integration-fix")).to_contain_text("GITHUB_TOKEN")
    assert host.asked[:2] == ["quick", "probe"], f"{where}: the card asked {host.asked}"
    assert [r.get_attribute("data-kind") for r in rows.all()] == ["github", "mcp", "mcp", "mcp", "provider", "provider"], f"{where}: rows out of order"

    notes = rows.filter(has_text="notes")
    expect(notes).to_contain_text(words["signin"])
    expect(notes.locator(".integration-fix")).to_contain_text(words["signin_fix"])
    expect(notes.locator(".health-mark.bad")).to_have_count(1)
    # The host's text waits under the fold until asked for.
    detail = notes.locator(".integration-detail")
    expect(detail.locator("code")).to_be_hidden()
    detail.locator("summary", has_text=words["details"]).click()
    expect(detail.locator("code")).to_have_text(ROWS[2]["detail"])

    # Healthy and idle rows carry no remedy, and a healthy one no fold either.
    for name in ("calendar", "files"):
        expect(rows.filter(has_text=name).locator(".integration-fix")).to_have_count(0)
    expect(rows.filter(has_text="calendar").locator(".integration-detail")).to_have_count(0)
    expect(rows.filter(has_text="files")).to_contain_text(words["idle"])
    expect(rows.filter(has_text="deepseek").locator(".integration-fix")).to_have_text(f"→ {words['nokey_fix']}")
    expect(rows.filter(has_text="router").locator(".health-mark.warn")).to_have_count(1)

    # The doctor's list is still there, under the card.
    doctor = page.locator(".card").filter(has_text="default model")
    expect(doctor).to_be_visible()
    assert card.bounding_box()["y"] < doctor.bounding_box()["y"], f"{where}: the card is not above the doctor's list"  # type: ignore[index]

    sideways = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sideways[0] <= sideways[1], f"{where}: the page scrolls sideways: {sideways}"
    wide = card.evaluate("el => [...el.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > window.innerWidth + 1).length")
    assert wide == 0, f"{where}: {wide} element(s) of the card reach past the window"
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/health-{lang}-{width}.png", full_page=True)


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in ((1280, 900), (390, 844)):
                phone = width < 1024
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone)
                page = context.new_page()
                check(page, lang, width)
                print(f"{lang} {width}: ok")
                context.close()
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
