"""A pending schedule can be reviewed and a new reminder created on a phone."""

from __future__ import annotations

import os

from api_stub import DEFAULT_APP, expect_app
from check_nav_rail import go, serve
from playwright.sync_api import expect, sync_playwright
from screenshots import UNHANDLED

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def check(lang: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        serve(page, lang)
        go(page, "/schedules/wk1", lang)
        action = "Approve this action" if lang == "en" else "Одобрить это действие"
        current = "Current" if lang == "en" else "Действует"
        expect(page.get_by_role("button", name=action)).to_be_visible()
        page.get_by_role("button", name=action).click()
        expect(page.get_by_text(current, exact=True)).to_be_visible()

        go(page, "/schedules", lang)
        new = "New schedule" if lang == "en" else "Новое расписание"
        page.get_by_role("button", name=new).first.click()
        page.locator(".sheet input.field").first.fill("Check medicine")
        page.get_by_role("radio", name="Reminder" if lang == "en" else "Напоминание").click()
        page.locator(".sheet textarea.field").fill("Take medicine")
        page.get_by_role("button", name="Create" if lang == "en" else "Создать", exact=True).click()
        expect(page.get_by_text("Check medicine")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for language in ("en", "ru"):
        check(language)
    assert UNHANDLED.report() == 0
    print("recurring schedule mobile: ok")
