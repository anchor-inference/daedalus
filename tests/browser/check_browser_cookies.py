"""The Browser tab's menu loads a cookie file into the browser, and says what it loaded without a value.

- the toolbar's menu offers "Import cookies" ("Импортировать куки");
- choosing it opens a file picker; the file picked is sent whole to ``POST /api/browsers/{group}/cookies``
  with the page's site as the domain a bare header would need;
- the toast says the count and the sites the host answered, in English and Russian.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_cookies.py

Exit 0 when every step holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
FILE = "# Netscape HTTP Cookie File\n.shop.example\tTRUE\t/\tTRUE\t0\tsid\tFAKE-VALUE-1\n"
WORDS = {"en": ("Browser actions", "Import cookies", "Cookies loaded: 2"), "ru": ("Действия с браузером", "Импортировать куки", "Загружено кук: 2")}


def check(browser, scenes, lang: str) -> None:  # type: ignore[no-untyped-def]
    menu, item, said = WORDS[lang]
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/browser/g1?token=t&scheme=dark&lang={lang}", wait=".browser-full .bp .bv[data-state='live']")
    page.locator(f".bp-toolbar button[aria-label='{menu}']").click()
    with page.expect_file_chooser() as chosen:
        page.get_by_role("menuitem", name=item).click()
    chosen.value.set_files({"name": "shop.example_cookies.txt", "mimeType": "text/plain", "buffer": FILE.encode()})
    expect(page.get_by_text(said)).to_be_visible(timeout=5000)
    sent = bs.posted("/g1/cookies")
    assert len(sent) == 1 and sent[0]["text"] == FILE, f"{lang}: the app sent {sent!r}"
    assert sent[0].get("domain"), f"{lang}: the page's site was not sent as the header's domain: {sent!r}"
    context.close()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang)
        browser.close()
    print("browser cookie import holds")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
