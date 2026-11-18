"""Settings → Limits → Memory for host commands: the cap in force beside the setting, a switch to a
size in GB saved as one, and the row found by the settings search in both languages."""
from __future__ import annotations

import json
import os
import sys

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import SETTINGS, UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"title": "Memory for host commands", "state": "Machine: 64.0 GB · in use: 5.0 GB · cap in force: 38.4 GB.", "gb": "GB",
           "at_cap": "the kernel kills the largest of those programs, not the server", "search": "OOM"},
    "ru": {"title": "Память для команд на хосте", "state": "На машине: 64.0 ГБ · занято: 5.0 ГБ · действующий предел: 38.4 ГБ.", "gb": "ГБ",
           "at_cap": "ядро завершает самую большую из этих программ, а не сервер", "search": "лимит памяти"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def check(page: Page, language: str, width: int) -> None:
    words = WORDS[language]
    saved: list[dict] = []

    def route(r) -> None:  # type: ignore[no-untyped-def]
        url, method = r.request.url.split("?", 1)[0], r.request.method
        if url.endswith("/api/settings") and method == "GET":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({**SETTINGS, "revision": "r1"}))
        if url.endswith("/api/settings/validate"):
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"valid": True, "stale": False, "problems": []}))
        if url.endswith("/api/settings") and method == "PUT":
            sent = json.loads(r.request.post_data or "{}")
            saved.append(sent)
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({**SETTINGS, "terminals": {**SETTINGS["terminals"], **sent["terminals"]}, "revision": "r2"}))
        stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}/settings/limits?token=t&lang={language}")
    row = page.locator("[data-setting='terminals.host_memory_cap']")
    expect(row).to_be_visible()
    expect(row).to_contain_text(words["title"])
    expect(row).to_contain_text(words["at_cap"])
    expect(row.locator(".settings-hostmem-state")).to_have_text(words["state"])
    expect(row.locator("input")).to_have_value("60")
    fits(page, "the memory cap row")
    with page.expect_response(lambda reply: reply.url.split("?", 1)[0].endswith("/api/settings") and reply.request.method == "PUT"):
        row.get_by_role("radio", name=words["gb"], exact=True).click()
    # A size starts from the share it replaces: 60% of the machine's 64 GB.
    assert saved and saved[-1]["terminals"]["host_memory_cap"] == "38G", saved
    field = row.locator("input")
    expect(field).to_have_value("38")
    field.fill("24")
    with page.expect_response(lambda reply: reply.url.split("?", 1)[0].endswith("/api/settings") and reply.request.method == "PUT"):
        field.press("Enter")
    assert saved[-1]["terminals"] == {"running_cap": 20, "host_memory_cap": "24G"}, saved[-1]
    expect(row).to_have_attribute("data-changed", "")
    row.scroll_into_view_if_needed()
    fits(page, "the memory cap row in GB")
    print(f"{language} {width}px: Settings → Limits showed the cap in force and saved a size in GB")


def found_by_search(page: Page, language: str) -> None:
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/settings?token=t&lang={language}")
    search = page.locator(".settings-search").first
    expect(search).to_be_visible()
    # A word the row's title does not hold: the index's synonyms find it.
    search.fill(WORDS[language]["search"])
    hit = page.locator('.settings-field-hit[data-field-hit="terminals.host_memory_cap"]')
    expect(hit).to_be_visible()
    hit.click()
    expect(page.locator("[data-setting='terminals.host_memory_cap']")).to_be_in_viewport()
    print(f"{language}: the settings search finds the memory cap by {WORDS[language]['search']!r}")


def run() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(8000)
                check(page, language, width)
                context.close()
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.set_default_timeout(8000)
            found_by_search(page, language)
            context.close()
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
