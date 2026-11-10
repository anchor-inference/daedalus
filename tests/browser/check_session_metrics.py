"""Context describes retained history; usage and tool timings describe the whole session."""
from __future__ import annotations

import os
from urllib.parse import urlparse

from check_panel import BASE, CHROMIUM
from playwright.sync_api import sync_playwright
from screenshots import S1, UNHANDLED, detail, respond, stub


def metrics_stub(route) -> None:  # type: ignore[no-untyped-def]
    if urlparse(route.request.url).path == f"/api/sessions/{S1}":
        data = detail(S1)
        data["context"].update(estimated=True, operator_turns=0)
        respond(route, data)
    else:
        stub(route)


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width in (360, 390, 1440):
                context = browser.new_context(viewport={"width": width, "height": 844}, color_scheme="dark")
                page = context.new_page()
                page.route("**/api/**", metrics_stub)
                page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={language}")
                # The inspector opens one section at a time: the context's page, then the usage's.
                page.locator("[data-prop='context']").click()
                panel = page.locator(".details")
                assert ("Estimated after history changes" if language == "en" else "Оценка после изменения истории") in panel.inner_text()
                page.locator(".dt-back").click()
                page.locator("[data-prop='usage']").click()
                page.wait_for_selector(".tool-timing")
                assert ("Totals across all runs" if language == "en" else "Суммарно по всем запускам") in panel.inner_text()
                assert not page.locator(".tool-timing").evaluate("el => el.open")
                summary = page.locator(".tool-timing summary")
                # The sheet's entry translation can round a 44px box to 43.999999px.
                assert summary.evaluate("el => parseFloat(getComputedStyle(el).height)") >= 44
                summary.click()
                assert page.locator(".tool-timing .dt-row").count() == 2
                assert ("9 calls" if language == "en" else "вызовов: 9") in panel.inner_text()
                assert panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                if os.environ.get("SHOTS"):
                    page.screenshot(path=f"{os.environ['SHOTS']}/metrics-{language}-{width}.png")
                print(f"{language} {width}: context scope, usage scope, tool disclosure and width OK")
                context.close()
        browser.close()


if __name__ == "__main__":
    run()
    raise SystemExit(UNHANDLED.report())
