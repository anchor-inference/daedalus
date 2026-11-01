"""The Usage screen's provider speeds: medians per provider and model, a window of their own, a phone layout."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")
"""Where to leave a picture of each layout; nothing is written when unset."""

WORDS = {
    "en": {"title": "Provider speed", "week": "7 days", "ttft": "620 ms", "slow": "2.3s", "tps": "87", "call": "first token 2.1s", "rate": "56 tok/s"},
    "ru": {"title": "Скорость провайдеров", "week": "7 дней", "ttft": "620 ms", "slow": "2.3 с", "tps": "87", "call": "первый токен 2.1 с", "rate": "56 ток/с"},
}


def noting(asked: list[str]):  # type: ignore[no-untyped-def]
    """The pictures' stub, noting the window of every speed request the page makes."""
    def route(r) -> None:  # type: ignore[no-untyped-def]
        if "/api/usage/speed" in r.request.url:
            asked.append(r.request.url.split("?", 1)[1])
        stub(r)
    return route


def run() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            words = WORDS[language]
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(8000)
                asked: list[str] = []
                page.route("**/api/**", noting(asked))
                page.goto(f"{BASE}/usage?token=t&lang={language}")
                table = page.locator("[data-speed]")
                expect(page.locator(".section-title", has_text=words["title"])).to_be_visible()
                expect(table.locator("[data-speed-model]")).to_have_count(3)
                quick = table.locator("[data-speed-model='deepseek/deepseek-flash']")
                expect(quick.locator("[data-speed-ttft]")).to_have_text(words["ttft"])
                expect(quick.locator("[data-speed-tps]")).to_have_text(words["tps"])
                expect(table.locator("[data-speed-model='claude/claude-opus-5'] [data-speed-ttft]")).to_have_text(words["slow"])
                # Calls too short for a rate say so with a dash rather than a zero.
                expect(table.locator("[data-speed-model='opencode/gpt-5.6-luna'] [data-speed-tps]")).to_have_text("—")
                expect(page.locator("[data-call-ttft]").first).to_have_text(words["call"])
                expect(page.locator("[data-call-tps]").first).to_have_text(words["rate"])
                assert asked and asked[0] == "hours=24", asked
                page.locator("[data-speed-windows]").get_by_role("button", name=words["week"]).click()
                expect(page.locator("[data-speed-windows] [aria-pressed='true']")).to_have_text(words["week"])
                page.wait_for_function("() => document.querySelector(\"[data-speed-model='deepseek/deepseek-flash'] .callsc\")?.textContent?.replace(/\\D/g, '') === '980'")
                assert asked[-1] == "hours=168", asked
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                assert overflow <= 0, f"{language} {width}px: the page scrolls sideways by {overflow}px"
                # Every number inside its card: the card clips what runs past it, so the page itself
                # never scrolls and a cut-off column would pass the check above.
                card = table.bounding_box()
                for cell in quick.locator("td").all():
                    edge = cell.bounding_box()
                    assert card and edge and edge["x"] + edge["width"] <= card["x"] + card["width"] + 1, f"{language} {width}px: a cell runs past the card: {cell.inner_text()!r}"
                if mobile:
                    # Each model is a card: its name over its four numbers, every number labelled.
                    box = quick.locator(".modelc").bounding_box()
                    numbers = quick.locator(".ttftc").bounding_box()
                    assert box and numbers and numbers["y"] >= box["y"] + box["height"] - 1, (box, numbers)
                if SHOTS:
                    Path(SHOTS).mkdir(parents=True, exist_ok=True)
                    table.scroll_into_view_if_needed()
                    page.screenshot(path=str(Path(SHOTS) / f"usage-speed-{language}-{width}.png"), full_page=False)
                context.close()
                print(f"{language} {width}px: the speeds per model, their window, and each call's own speed")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
