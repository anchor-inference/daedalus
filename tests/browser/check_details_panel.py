"""The session's Details tab keeps every fact it has, at a desktop's 1440 px and a phone's 390 px.

The tab was redrawn from a column of grey sentences and raw numbers into cards with a headline each,
and the risk of such a redraw is a datum that quietly drops out. This check feeds the richest session
the app can describe (imported from another program, a measured context with its breakdown and cache,
spend today, a subscription with three windows, a loop-less session with MCP servers and disk use)
and asserts, in both languages and at both widths, that each section is there and that each of its
facts is on the page in its human form: sizes in MB, tokens in k and M, the quota bars labelled with
their percent and reset, the original's size note readable rather than a byte count.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_details_panel.py

``SHOTS=<dir>`` also saves the panel in dark and light at both widths, the viewport and the whole tab.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, fulfil_shared  # noqa: E402
from screenshots import PROVIDER_USAGE, S1, UNHANDLED, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

NOW = datetime.now(UTC)
ORIGIN_ID = "5f0c2a9e-41d7-4b3e-9a6c-0d8e7f1b2c3a"


def iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


def rich_detail() -> dict:
    data = detail(S1)
    data["imported"] = {
        "harness": "claude", "id": ORIGIN_ID, "source_model": "claude-opus-5", "branch": "feature/menu-prices", "mode": "full",
        "live": False, "complete": True, "imported_at": iso(-timedelta(hours=5)), "refreshed_at": None, "masked": 4,
        "counts": {"user": 4100, "assistant": 6021, "sidechains": 3},
        "original": {"stored": False, "bytes": None, "name": None, "reason": "the original is 188057562 bytes, over the 67108864 kept"},
    }
    data["context"] = {
        "tokens": 491000, "window": 980000, "estimated": False, "messages": 1075, "summaries": 1, "operator_turns": 26,
        "last_compaction": {"at": iso(-timedelta(hours=3)), "reason": "auto"},
        "breakdown": {"instructions": 18200, "tools": 42600, "conversation": 398000, "attachments": 12200, "reserved_response": 20000},
        "recent_cache": {"hit_percent": 100, "read_tokens": 491000},
        "prefix_changed": ["toolset"],
    }
    data["usage"] = {"c": 787, "i": 220_900_000, "o": 405_000, "ch": 212_900_000, "usd": 41.7,
                     "c_today": 112, "i_today": 31_400_000, "o_today": 58_000, "usd_today": 6.12}
    return data


PROVIDER = {**PROVIDER_USAGE, "subscription": {"logged_in": True, "plan": "default_claude_max_20x", "windows": [
    {"name": "5h", "used_percent": 23, "resets_at": iso(timedelta(hours=3, minutes=12))},
    {"name": "weekly", "used_percent": 84, "resets_at": iso(timedelta(days=4))},
    {"name": "Fable", "used_percent": 100, "resets_at": iso(timedelta(days=2))},
]}}

MCP = {"enabled": ["github"], "servers": [
    {"name": "github", "description": "Issues and pull requests", "connected": True, "error": None, "tools": ["list_issues", "create_pr"], "state": "ready", "catalog_revision": 1, "last_error_code": None, "in_flight": 1},
    {"name": "sentry", "description": "", "connected": False, "error": None, "tools": [], "state": "disabled", "catalog_revision": 0, "last_error_code": None, "in_flight": 0},
]}


def serve(route) -> None:  # type: ignore[no-untyped-def]
    path = urlparse(route.request.url).path
    if path == f"/api/sessions/{S1}":
        return respond(route, rich_detail())
    if path == f"/api/sessions/{S1}/mcp" and route.request.method == "GET":
        return respond(route, MCP)
    if path.startswith("/api/usage/provider/"):
        return respond(route, PROVIDER)
    if "/api/disk" in path and fulfil_shared(route):
        return None
    return stub(route)


# Each section by its id, and the facts it must show, in the words the page renders them in.
FACTS = {
    "en": {
        "origin": ["Claude Code", "claude-opus-5", "feature/menu-prices", "10,121", "whole", "3", "4", "179.3 MB", "64.0 MB"],
        "session": ["Change", "Mode"],
        "context": ["50%", "491k", "980k", "Last model request", "1,075", "26", "3 hours ago", "auto", "Instructions", "Tools", "Conversation", "Attachments", "Reserved response", "398k", "100%", "tool set", "Compact history"],
        "usage": ["$41.70", "220.9M", "405k", "212.9M", "787", "$6.12", "31.4M", "58k", "112", "Tool time"],
        "workspace": ["Files"],
        "provider": ["Max 20×", "5h", "23%", "weekly", "84%", "Fable", "100%"],
        "loop": ["No loop", "Loop"],
        "mcp": ["github", "Ready", "1 active", "list_issues", "sentry", "no description"],
    },
    "ru": {
        "origin": ["Claude Code", "claude-opus-5", "feature/menu-prices", "10 121", "целиком", "3", "4", "179.3 МБ", "64.0 МБ"],
        "session": ["Сменить", "Режим"],
        "context": ["50%", "491 тыс", "980 тыс", "Последний запрос к модели", "1 075", "26", "3 часа назад", "auto", "Инструкции", "Инструменты", "Диалог", "Вложения", "Резерв ответа", "398 тыс", "100%", "набор инструментов", "Сжать историю"],
        "usage": ["$41.70", "220.9 млн", "405 тыс", "212.9 млн", "787", "$6.12", "31.4 млн", "58 тыс", "112", "Время инструментов"],
        "workspace": ["Файлы"],
        "provider": ["Max 20×", "5h", "23%", "weekly", "84%", "Fable", "100%"],
        "loop": ["Цикла нет", "Цикл"],
        "mcp": ["github", "Готов", "активных: 1", "list_issues", "sentry", "без описания"],
    },
}


def text_of(page: Page, scope: str) -> str:
    """The section's words, opened folds and hidden "More" text included: a fact behind a fold is
    still on the page, which is what the redraw promised."""
    return page.locator(scope).evaluate("el => el.textContent.replace(/\\s+/g, ' ')")


def check(page: Page, scope: str, language: str, where: str) -> None:
    for section, facts in FACTS[language].items():
        locator = page.locator(f"{scope} [id$='-info-{section}']")
        expect(locator, f"{where}: the {section} section").to_have_count(1)
        words = text_of(page, f"{scope} [id$='-info-{section}']")
        for fact in facts:
            assert fact.replace(" ", " ") in words.replace(" ", " ").replace(" ", " "), f"{where}: {section} lacks {fact!r}: {words[:600]}"
    # The raw byte counts are gone from the page, the facts they carried are not (asserted above).
    whole = text_of(page, f"{scope} .details")
    assert "188057562" not in whole and "67108864" not in whole, f"{where}: a raw byte count is on the page"
    # The context's breakdown is one segmented bar with a legend entry per part.
    expect(page.locator(f"{scope} [data-context-segments] [data-segment]"), f"{where}: segments").to_have_count(5)
    # Every quota window is a labelled bar coloured by how full it is.
    bars = page.locator(f"{scope} [data-quota]")
    expect(bars, f"{where}: quota bars").to_have_count(3)
    tones = bars.evaluate_all("els => els.map(e => e.dataset.tone)")
    assert tones == ["ok", "attn", "bad"], f"{where}: quota tones {tones}"
    # The id is a copy button, shortened but whole in its title.
    copy = page.locator(f"{scope} [data-origin-id]")
    assert copy.get_attribute("title") == ORIGIN_ID, f"{where}: the origin id's title"
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"
    panel_overflow = page.locator(f"{scope} .details").evaluate("el => el.scrollWidth - el.clientWidth")
    assert panel_overflow <= 1, f"{where}: the tab scrolls sideways by {panel_overflow}px"


def shoot(page: Page, scope: str, name: str, width: int, height: int) -> None:
    page.screenshot(path=f"{SHOTS}/{name}.png")
    # The whole tab: a viewport tall enough to hold it, so nothing is cut off by the panel's scroller.
    tall = page.locator(f"{scope} .details").evaluate("el => el.scrollHeight") + 400
    page.set_viewport_size({"width": width, "height": max(height, tall)})
    page.wait_for_timeout(300)
    page.screenshot(path=f"{SHOTS}/{name}-full.png")
    page.set_viewport_size({"width": width, "height": height})


def run() -> int:
    expect_app(BASE)
    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                for scheme in (("dark", "light") if SHOTS and language == "en" else ("dark",)):
                    where = f"{language} {width} {scheme}"
                    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile, color_scheme=scheme,
                                                  device_scale_factor=2 if SHOTS else 1)
                    # The disk section starts closed (it walks a disk); opened here so its sizes are read too.
                    context.add_init_script("try { localStorage.setItem('daedalus.session.details.disk', 'open'); } catch (e) {}")
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.route("**/api/**", serve)
                    page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme={scheme}&lang={language}")
                    scope = ".panel-sheet" if mobile else ".panel"
                    try:
                        page.wait_for_selector(f"{scope} .details [id$='-info-mcp'] [data-mcp-server], {scope} .details [id$='-info-mcp'] .card")
                        page.wait_for_selector(f"{scope} [data-disk-workspace]")
                        if SHOTS:
                            page.wait_for_timeout(500)
                            shoot(page, scope, f"details-{'phone' if mobile else 'desk'}-{scheme}-{language}", width, height)
                        check(page, scope, language, where)
                        print(f"{where}: every section and fact present, nothing scrolls sideways")
                    except AssertionError as error:
                        failures += 1
                        print(f"FAIL {where}: {error}")
                        if SHOTS:
                            page.screenshot(path=f"{SHOTS}/fail-{language}-{width}-{scheme}.png")
                    context.close()
        browser.close()
    return 1 if failures else UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
