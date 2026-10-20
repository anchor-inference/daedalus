"""A file the agent attached into its answer stands where the answer put it, and opens in Preview.

AttachMedia attaches any file, not only a picture: a document in the answer is a file card in place —
its name, its size and kind, a download link — between the text before it and after it, and pressing
it opens the document in Preview, read through the presentation's own address. At 1440 px and on a
390 px phone, in English and Russian.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_attached_file.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import (  # noqa: E402
    ATTACHED_ITEM,
    ATTACHED_NAME,
    ATTACHED_PRESENTATION,
    ATTACHED_TEXT,
    DEFAULT_APP,
    expect_app,
)
from screenshots import S2, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ATTACHED = {"en": "attached", "ru": "приложен"}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def visit(browser, lang: str, phone: bool) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {'phone' if phone else 'desktop'}"
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=phone, has_touch=phone)
    page = context.new_page()
    requests: list[str] = []
    page.on("request", lambda request: requests.append(unquote(urlsplit(request.url).path + "?" + urlsplit(request.url).query)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S2}?token=t&lang={lang}")

    card = page.locator(".answer-with-media .inline-file .artifact")
    expect(card).to_be_visible(timeout=15000)
    expect(card.locator(".artifact-name")).to_have_text(ATTACHED_NAME)
    meta = card.locator(".artifact-meta").inner_text()
    assert "docx" in meta and ATTACHED[lang] in meta, f"{where}: the card says {meta!r}"
    # In place: the answer's own words come before the card, inside the same answer.
    order = page.evaluate("""() => { const answer = document.querySelector('.answer-with-media'); const kids = [...answer.children];
      return kids.map((el) => el.classList.contains('inline-file') ? 'file' : 'text'); }""")
    assert order and order[0] == "text" and "file" in order, f"{where}: the answer is laid out as {order}"
    href = card.locator(".artifact-actions a").get_attribute("href") or ""
    assert unquote(href).startswith(f"/api/sessions/{S2}/media/{ATTACHED_PRESENTATION}/download?path={ATTACHED_ITEM}/{ATTACHED_NAME}"), f"{where}: the download goes to {href}"
    # The card needs no media credential: no upgrade request before it can be used.
    assert not any(r.startswith("/api/media/access") for r in requests), f"{where}: the card asked for media access"

    card.locator(".artifact-main").click()
    expect(page.locator(".preview-doc.docx")).to_contain_text(ATTACHED_TEXT, timeout=10000)
    assert any(r.startswith(f"/api/sessions/{S2}/media/{ATTACHED_PRESENTATION}/download") for r in requests), f"{where}: Preview read {[r for r in requests if 'media' in r]}"
    fits(page, where)
    context.close()


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (False, True):
                try:
                    visit(browser, lang, phone)
                except AssertionError as error:
                    problems.append(str(error))
                except Exception as error:  # noqa: BLE001 - a selector that never appears is a failure to report, not a crash
                    problems.append(f"{lang} {'phone' if phone else 'desktop'}: {error}")
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in problems:
        print("FAIL", problem)
    if not problems and not unhandled:
        print("a file attached into an answer is a card in place that opens in Preview")
    return 1 if problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
