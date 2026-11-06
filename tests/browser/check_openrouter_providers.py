"""Does OpenRouter's provider step limit a model, say what it costs, and stay out of the way when it cannot?

Add a model on OpenRouter shows the model's providers after the model is picked. Ticking two has to show
the higher price, and saving has to send exactly those slugs: what reaches the API is the point, so the
check reads the request. The same table in a model's settings has to show a provider that has gone and
let it be unticked. With no listing the step must not appear at all and the model must still save.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8171 /tmp/app-root &
    APP_URL=http://127.0.0.1:8171/app python3 tests/browser/check_openrouter_providers.py

Pictures of each language and width go to OUT, for a person to look at. Exit 0 when every claim holds.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import SETTINGS, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
OUT = Path(os.environ.get("OUT", "/tmp/openrouter-providers"))
MODEL = "deepseek/deepseek-v4-flash"
PRESET = "openrouter.deepseek-deepseek-v4-flash"


def row(slug: str, name: str, price_in: float, price_out: float, cached: float | None, **extra: object) -> dict[str, object]:
    base = {
        "slug": slug, "name": name, "quantization": "fp8", "tier": None, "input": price_in, "output": price_out, "cache_hit": cached, "varies": False,
        "context_length": 1048576, "max_output_tokens": 393216, "tools": True, "tool_choice": "full", "reasoning": True, "caching": cached is not None, "uptime": 99.9, "zdr": True,
        "bill": {"input": price_in, "output": price_out, "cache_hit": cached},
    }
    return {**base, **extra}


LISTING = {"model": MODEL, "images": True, "unavailable": False, "rows": [
    row("relace/fp4", "Relace", 0.006, 0.5, 0.005, tool_choice="partial"),
    row("wafer/us", "Wafer", 0.038, 0.5, 0.01),
    row("deepinfra/fp8/flex", "DeepInfra", 0.078, 0.235, 0.0024, tier="flex"),
    row("alibaba", "Alibaba", 0.15, 0.6, 0.015, zdr=False, varies=True, context_length=1000000),
    row("mancer/fp8", "Mancer 2", 0.15, 0.5, None),
    row("plain", "NoTools Inc", 0.2, 0.8, 0.02, tools=False, tool_choice="none", reasoning=False),
    row("fireworks", "Fireworks", 0.3, 1.2, 0.006),
    row("fireworks/us", "Fireworks", 0.45, 1.8, 0.009),
    row("cohere", "Cohere", 0.195, 0.66, 0.006, max_output_tokens=384000),
]}
NOTHING = {"model": MODEL, "images": False, "rows": [], "unavailable": True}


def host(listing: dict, saved: list[dict], settings: dict | None = None):  # type: ignore[no-untyped-def]
    def handle(route):  # type: ignore[no-untyped-def]
        request = route.request
        rel = "/api/" + request.url.split("?", 1)[0].split("/api/", 1)[-1]
        if rel.endswith("/model-providers"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(listing))
        if rel.startswith("/api/presets/") and request.method == "PUT":
            saved.append(request.post_data_json)
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(settings or SETTINGS))
        if rel == "/api/settings" and request.method == "GET" and settings is not None:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
        return stub(route)
    return handle


def walk_to_providers(page, lang: str) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.wait_for_selector(".addmodel", timeout=15000)
    page.locator(".pickgrid .pick", has_text="OpenRouter").first.click()
    page.wait_for_selector(".modelgrid .pick", timeout=15000)
    page.locator(".modelgrid .pick", has_text="DeepSeek V4 Flash").first.click()


def ticks(page, slug: str):  # type: ignore[no-untyped-def]
    return page.locator(f".orp-row[data-slug='{slug}'] input")


def wizard(browser, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    for lang, width in (("en", 1440), ("en", 390), ("ru", 1440), ("ru", 390)):
        where = f"{lang} {width}"
        saved: list[dict] = []
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.route("**/api/**", host(LISTING, saved))
        walk_to_providers(page, lang)
        page.wait_for_selector(".orp-row", timeout=5000)
        if page.locator(".orp-row").count() != len(LISTING["rows"]):
            failures.append(f"{where}: not one row per provider")
        if ticks(page, "wafer/us").is_checked():
            failures.append(f"{where}: a provider starts ticked")
        ticks(page, "wafer/us").check()
        ticks(page, "alibaba").check()
        price = page.get_by_test_id("route-price").inner_text()
        if "$0.150" not in price or "$0.600" not in price or "$0.015" not in price:
            failures.append(f"{where}: two providers do not show the higher of each price: {price!r}")
        ticks(page, "mancer/fp8").check()
        price = page.get_by_test_id("route-price").inner_text()
        if "$0.0" in price.split("$0.150", 1)[-1].split("$0.600", 1)[-1]:
            failures.append(f"{where}: a provider with no cached price still shows a cached one: {price!r}")
        ticks(page, "mancer/fp8").uncheck()
        ticks(page, "plain").check()
        if page.locator(".orp .result-warning").count() != 1:
            failures.append(f"{where}: a provider without tools is not called out when ticked")
        ticks(page, "plain").uncheck()
        OUT.mkdir(parents=True, exist_ok=True)
        page.locator(".orp").evaluate("e => e.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / f"wizard-{lang}-{width}.png"))
        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
            failures.append(f"{where}: the page scrolls sideways")
        boxed = page.evaluate("(() => { const l = document.querySelector('.orp-list'); return l.scrollWidth <= l.clientWidth; })()")
        if not boxed:
            failures.append(f"{where}: the table scrolls sideways")
        page.locator(".addmodel-foot .btn.primary").click()
        page.wait_for_timeout(300)
        chosen = [body.get("openrouter_providers") for body in saved]
        if chosen != [["wafer/us", "alibaba"]]:
            failures.append(f"{where}: saving sent {chosen}, not the two ticked")
        context.close()


def no_listing(browser, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    saved: list[dict] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.route("**/api/**", host(NOTHING, saved))
    walk_to_providers(page, "en")
    page.wait_for_timeout(600)
    if page.locator(".orp").count() or page.locator(".step", has_text="Which provider").count():
        failures.append("an unavailable listing still shows the provider step")
    if page.locator(".step-n").all_inner_texts()[-1:] == ["4"]:
        failures.append("the steps kept a number for the step that is not there")
    page.locator(".addmodel-foot .btn.primary").click()
    page.wait_for_timeout(300)
    if [body.get("openrouter_providers") for body in saved] != [[]]:
        failures.append(f"without a listing the model did not save unrestricted: {saved}")
    context.close()


def settings_row(browser, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    settings = copy.deepcopy(SETTINGS)
    settings["providers"]["openrouter"] = {"kind": "openrouter", "base_url": "http://keyproxy:3200/openrouter", "timeout_seconds": 600}
    settings["presets"][PRESET] = {"provider": "openrouter", "model": MODEL, "label": "DeepSeek through OpenRouter", "thinking": True, "reasoning_effort": "medium", "images": True,
                                   "context_window": 1000000, "context_window_source": "provider", "max_output_tokens": 32000,
                                   "openrouter_providers": ["wafer/us", "vanished/fp8"], "openrouter_providers_gone": ["vanished/fp8"]}
    for lang, width in (("en", 1440), ("en", 390), ("ru", 1440), ("ru", 390)):
        where = f"settings {lang} {width}"
        saved: list[dict] = []
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.route("**/api/**", host(LISTING, saved, settings))
        page.goto(f"{BASE}/settings/models?token=t&lang={lang}")
        line = page.locator(".mrow", has_text="DeepSeek through OpenRouter").first
        line.wait_for(timeout=15000)
        if line.locator(".pill.failed").count() != 1:
            failures.append(f"{where}: the model's line does not say a provider is gone")
        line.locator(".mmain").click()
        if "vanished/fp8" not in line.locator(".result-warning").inner_text():
            failures.append(f"{where}: the panel does not name the provider that went")
        line.get_by_role("button", name="Choose" if lang == "en" else "Выбрать").click()
        page.wait_for_selector(".orp-row[data-slug='vanished/fp8']", timeout=5000)
        ticks(page, "vanished/fp8").uncheck()
        ticks(page, "deepinfra/fp8/flex").check()
        page.locator(".orp").evaluate("e => e.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / f"settings-{lang}-{width}.png"))
        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
            failures.append(f"{where}: the page scrolls sideways")
        line.get_by_role("button", name="Save" if lang == "en" else "Сохранить").click()
        page.wait_for_timeout(300)
        if saved != [{"openrouter_providers": ["wafer/us", "deepinfra/fp8/flex"]}]:
            failures.append(f"{where}: saving sent {saved}")
        context.close()


def run() -> int:
    failures: list[str] = []
    stub.fresh = True  # type: ignore[attr-defined]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        wizard(browser, failures)
        no_listing(browser, failures)
        stub.fresh = False  # type: ignore[attr-defined]
        settings_row(browser, failures)
        browser.close()
    for failure in failures:
        print("FAILED:", failure)
    print("ok" if not failures else f"{len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
