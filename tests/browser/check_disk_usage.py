"""A workspace's disk use in the session's Details tab and in a project's settings, and the clean-up."""
from __future__ import annotations

import os
import sys

from api_stub import DEFAULT_APP, expect_app, fulfil_shared
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import S1, UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"settings": "Settings for Bakery site", "cleanup": "Clean up…", "total": "23.0 GB", "close": "Close", "freed": "Freed 18.0 GB", "tracked": "Include files tracked by git"},
    "ru": {"settings": "Настройки: Bakery site", "cleanup": "Очистить…", "total": "23.0 ГБ", "close": "Закрыть", "freed": "Освобождено 18.0 ГБ", "tracked": "Включая файлы, отслеживаемые git"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def usage(page: Page, scope: str, words: dict[str, str], requests: list[str]) -> None:
    """The size against the limit, the largest folders, then a clean-up of the throwaway ones."""
    panel = page.locator(scope)
    expect(panel.locator("[data-disk-workspace]")).to_be_visible()
    expect(panel.locator("[data-disk-total]")).to_have_text(words["total"])
    expect(panel.locator(".bar.attn")).to_have_count(1)
    scratch = panel.locator("[data-disk-entry='_scratch']")
    expect(scratch).to_be_visible()
    expect(panel.locator("[data-disk-entry='src']")).to_be_visible()
    scratch.locator(".disk-name").click()
    expect(panel.locator("[data-disk-child]")).to_have_count(2)
    fits(page, "usage")
    panel.get_by_role("button", name=words["cleanup"]).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("[data-disk-pick='_scratch'] input")).to_be_checked()
    expect(dialog.locator("[data-disk-pick='.uv-cache'] input")).to_be_checked()
    expect(dialog.locator("[data-disk-pick='src'] input")).not_to_be_checked()
    expect(dialog.locator("[data-disk-pick='src'] input")).to_be_disabled()
    expect(dialog.locator("[data-disk-pick='.checkpoints'] input")).to_be_disabled()
    dialog.get_by_label(words["tracked"]).check()
    expect(dialog.locator("[data-disk-pick='src'] input")).to_be_enabled()
    dialog.get_by_label(words["tracked"]).uncheck()
    dialog.locator("[data-disk-confirm]").click()
    expect(dialog.locator("[data-disk-freed]")).to_have_text(words["freed"])
    dialog.get_by_role("button", name=words["close"], exact=True).last.click()
    assert any(r.startswith("POST /api/disk/cleanup") for r in requests), requests


def serve(route) -> None:  # type: ignore[no-untyped-def]
    """The disk routes first: the pictures' stub answers every write with a bare ok, which would hide the clean-up's result."""
    if "/api/disk" in route.request.url and fulfil_shared(route):
        return
    stub(route)


def recorder(requests: list[str]):  # type: ignore[no-untyped-def]
    """A route that notes every call, so the check can tell what the page asked and when."""
    def route(r) -> None:  # type: ignore[no-untyped-def]
        requests.append(f"{r.request.method} {r.request.url.split('?', 1)[0][r.request.url.index('/api/'):]}")
        serve(r)
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
                requests: list[str] = []

                page.route("**/api/**", recorder(requests))
                # The Details tab asks nothing about the disk until its section is opened.
                page.goto(f"{BASE}/agents/{S1}?token=t&lang={language}&panel=details")
                section = page.locator("[id$='-info-disk']")
                expect(section).to_be_visible()
                assert not any("/api/disk" in r for r in requests), requests
                section.locator("summary").click()
                usage(page, "[id$='-info-disk']", words, requests)
                context.close()
                print(f"{language} {width}px: session details showed the size, the folders and the clean-up")
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.set_default_timeout(8000)
            requests = []

            page.route("**/api/**", recorder(requests))
            page.goto(f"{BASE}/agents?token=t&lang={language}")
            page.locator(".project-chip").click()
            page.locator(".projects-row", has_text="Bakery site").get_by_role("button", name=words["settings"]).click()
            fold = page.locator("details.project-disk")
            assert not any("/api/disk/project" in r for r in requests), requests
            fold.locator("summary").click()
            usage(page, "details.project-disk", words, requests)
            context.close()
            print(f"{language} project settings showed the size, the folders and the clean-up")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
