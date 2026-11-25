"""Does a choice made inside a service's Access sheet register, on a desktop browser?

Reported after the overflow-menu fix, on the same screen: pressing "Local only" in the Access
sheet collapsed the sheet to its title, snapped it onto the service card, and changed nothing.
Same cause, one layer up: the sheet was declared inside the pressable card, so while the mouse
button was down the card's `:active { transform }` made it the containing block of the fixed
sheet and its overflow clipped the body. The release landed outside the radio.

The check drives the real mouse (press, wait, release) on the radio and asserts that the sheet
neither moves nor loses its body while pressed, that the PUT with the new mode is sent, and
that clicking the sheet does not fall through to the card underneath.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_access_sheet.py
"""
from __future__ import annotations

import json
import os
import sys

from api_stub import DEFAULT_APP, expect_app
from check_overflow_menu import SERVICES, open_menu, press
from check_overflow_menu import stub as base_stub
from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

puts: list[dict] = []


def stub(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    if request.method == "POST" and "/share" in request.url:
        puts.append(json.loads(request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({**SERVICES[0], "share": {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://example.test"}}))
        return
    base_stub(route)


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page: Page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/services?token=t")
        page.wait_for_selector(".pg-item[data-service]", timeout=15000)
        open_menu(page)
        press(page, "Access")
        sheet = page.locator(".sheet-backdrop .sheet")
        page.wait_for_selector(".access-options", timeout=5000)
        before = sheet.bounding_box()
        radio = page.locator(".access-option", has_text="Local only").first
        box = radio.bounding_box()
        assert before and box
        page.mouse.move(box["x"] + 20, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(80)
        during = sheet.bounding_box()
        body_visible = page.locator(".access-options").is_visible()
        page.mouse.up()
        page.wait_for_timeout(500)
        assert during
        drift = max(abs(during["x"] - before["x"]), abs(during["y"] - before["y"]))
        print(f"sheet drift while pressed: {drift:.1f}px · body visible while pressed: {body_visible}")
        if drift > 1:
            problems.append(f"the sheet moves {drift:.0f}px while the button is held down")
        if not body_visible:
            problems.append("the sheet's body disappears while the button is held down")
        print("POST /share bodies:", puts)
        if not any(b.get("mode") == "local" for b in puts):
            problems.append("choosing Local only sent no request")
        if page.locator(".sheet-backdrop .sheet").count() == 0:
            problems.append("the sheet closed by itself")
        # A click on the sheet's body must not reach the card underneath (it would open the session).
        url_before = page.url
        page.locator(".sheet .sheet-body").click(position={"x": 5, "y": 5})
        page.wait_for_timeout(300)
        if page.url != url_before:
            problems.append(f"a click inside the sheet navigated to {page.url}")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
