"""The export of one conversation over an agent link, in both languages, schemes and widths.

- a conversation's card in the Agents tab has the Export row under its thread: Zip downloads the zip under the name
  the host gave it, To notes posts the export and says it was saved to notes;
- a link's conversation in the Agent link sheet has the same row, which asks for the link's own export;
- nothing scrolls sideways, on a phone either.

    APP_URL=http://127.0.0.1:8371/app SHOTS=/tmp/shots python3 tests/browser/check_link_export.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeGuests, FakeTalks, expect_app  # noqa: E402
from check_link_files import fits, open_sheet, snap, snap_element  # noqa: E402
from playwright.sync_api import Page, Route, expect, sync_playwright  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"export": "Export", "zip": "Zip", "note": "To notes", "saved": "Saved to notes", "actions": "Session actions", "menu": "Agent link…", "read": "Read"},
    "ru": {"export": "Выгрузить", "zip": "Архив", "note": "В заметки", "saved": "Сохранено в заметки", "actions": "Действия с сессией", "menu": "Ссылка для агента…", "read": "Читать"},
}
NAME = "conversation-Пекарня-2026-11-11.zip"


def named_zip(route: Route) -> None:
    """The zip as the host answers it, with its name in ``filename*``: the stub's shared answer has no headers to give."""
    api_stub.LINK_EXPORTS.append((route.request.method, route.request.url.split("?", 1)[0]))
    route.fulfill(
        status=200, body=b"PK\x05\x06" + b"\x00" * 18, content_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=\"conversation.zip\"; filename*=utf-8''conversation-%D0%9F%D0%B5%D0%BA%D0%B0%D1%80%D0%BD%D1%8F-2026-11-11.zip"},
    )


def both_actions(page: Page, row, words: dict[str, str], path: str) -> None:  # type: ignore[no-untyped-def]
    expect(row).to_be_visible()
    expect(row.locator(".link-export-title")).to_have_text(words["export"])
    expect(row.locator("[data-link-export-zip]")).to_have_text(words["zip"])
    expect(row.locator("[data-link-export-note]")).to_have_text(words["note"])
    with page.expect_download() as download:
        row.locator("[data-link-export-zip]").click()
    assert download.value.suggested_filename == NAME, download.value.suggested_filename
    row.locator("[data-link-export-note]").click()
    expect(row.locator("[data-link-export-done]")).to_have_text(words["saved"])
    asked = [(verb, where.split("/api/", 1)[-1]) for verb, where in api_stub.LINK_EXPORTS]
    assert ("GET", f"{path}/export") in asked and ("POST", f"{path}/export/note") in asked, asked


def page_for(browser, scheme: str, width: int, height: int):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, accept_downloads=True)
    page = context.new_page()
    page.route("**/api/**", stub)
    # Registered after the stub, so it is asked first.
    page.route("**/api/**/export?*", named_zip)
    return context, page


def check_tab(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    api_stub.GUESTS = FakeGuests(exchange=True)
    api_stub.TALKS = FakeTalks(S1, exchange=True)
    api_stub.LINK_EXPORTS.clear()
    context, page = page_for(browser, scheme, width, height)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}&panel=agents", wait_until="domcontentloaded")
    card = page.locator("[data-agents-tab] [data-talk='t1']")
    row = card.locator("[data-link-export='talk']")
    row.scroll_into_view_if_needed()
    snap_element(page, "[data-agents-tab] [data-talk='t1'] [data-link-export='talk']", f"export-row-{name}")
    snap(page, f"export-tab-{name}")
    both_actions(page, row, words, "agent-talks/t1")
    fits(page, f"export tab {name}")
    context.close()


def check_link(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    api_stub.GUESTS = FakeGuests(exchange=True)
    api_stub.TALKS = FakeTalks(S1)
    api_stub.LINK_EXPORTS.clear()
    context, page = page_for(browser, scheme, width, height)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")
    open_sheet(page, words)
    page.locator("[data-guest-link='1a2b']").get_by_role("button", name=words["read"], exact=True).click()
    reader = page.locator("[data-guest-reader]")
    row = reader.locator("[data-link-export='guest']")
    expect(row).to_be_visible()
    snap(page, f"export-reader-{name}")
    both_actions(page, row, words, "guest/links/1a2b")
    fits(page, f"export reader {name}")
    context.close()


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang in WORDS:
            for scheme in ("light", "dark"):
                for width, height in ((1280, 800), (390, 844)):
                    check_tab(browser, lang, scheme, width, height)
                    check_link(browser, lang, scheme, width, height)
        browser.close()
    if UNHANDLED.report():
        raise SystemExit(1)
    print("link export: ok")


if __name__ == "__main__":
    main()
