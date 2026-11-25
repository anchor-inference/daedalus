"""Settings geometry stays stable with long descriptions and ordered multiple selections."""
from __future__ import annotations

import json
import os

from api_stub import DEFAULT_APP, expect_app
from check_settings_phone import long_picks
from playwright.sync_api import sync_playwright
from screenshots import UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for width in (1440, 1128, 760, 390):
            for scheme in ("dark", "light"):
                context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
                page = context.new_page()
                page.route("**/api/**", stub)
                settings = long_picks()

                def save(route, *, settings=settings):  # type: ignore[no-untyped-def]
                    if route.request.method == "PUT":
                        settings.update(route.request.post_data_json)
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))

                page.route("**/api/settings", save)
                suffix = f"?token=t&lang=ru&scheme={scheme}"
                for section, selector in (("speech-models", ".stt-list"),):
                    page.goto(f"{BASE}/settings/{section}{suffix}")
                    page.wait_for_selector(f"{selector} > div")
                    page.wait_for_timeout(250)
                    for grid in page.locator(selector).all():
                        heights = grid.evaluate("g => [...g.children].map(c => c.getBoundingClientRect().height)")
                        if width >= 760:
                            assert max(heights) - min(heights) < 1, (width, scheme, section, heights)
                page.goto(f"{BASE}/settings/text{suffix}")
                panel = page.locator('[data-setting="appearance.font"]')
                panel.wait_for()
                code = panel.locator('[data-font-role="code"]')
                field = code.locator(".combo-input")
                field.click()
                assert page.locator(".combo-popup").evaluate("e => e.getBoundingClientRect().height <= 480")
                page.locator(".combo-item").filter(has_text="JetBrains Mono").click()
                assert field.input_value().startswith("JetBrains Mono")
                field.fill("https://example.com/not-a-font.css")
                page.locator(".combo-item").first.click()
                assert code.locator(".attn").is_visible()
                page.goto(f"{BASE}/settings/tools{suffix}")
                page.wait_for_selector(".tgroup")
                assert page.locator(".tgroup").evaluate_all("rows => rows.every(r => r.querySelector('.tgroup-use-text').getBoundingClientRect().right <= r.querySelector('.dropdown-btn').getBoundingClientRect().left - 7)")
                page.goto(f"{BASE}/settings/search{suffix}")
                page.wait_for_selector("#search-fallbacks")
                # The fallbacks are an in-place list: ticking rows must not shift its search field sideways
                # or change its width, the way a summary button that grew with its caption once did.
                fallbacks = page.locator("#search-fallbacks")
                search = fallbacks.locator(".sel-search")
                search.scroll_into_view_if_needed()
                before = search.bounding_box()
                for _ in range(3):
                    fallbacks.locator(".sel-list [role=option]").nth(0).click()
                    after = search.bounding_box()
                    assert before and after and abs(before["x"] - after["x"]) < 1 and abs(before["width"] - after["width"]) < 1, (before, after)
                context.close()
        browser.close()
    print("Settings layout: cards, font selection, usage lanes and a stable fallback list passed at four widths in both themes")
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
