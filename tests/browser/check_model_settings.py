"""Does a model's card carry its price, and a session's Details the models its helpers may run on?

Settings → Models: a model whose built-in price was overridden says so beside the default it replaced and
offers Reset, which asks the host to remove the override; typing a rate and saving sends it; a negative
rate is refused in the card; a login on a plan offers the switch to per-token; a model on a prepaid
provider says the provider decides. A session's Details: the three-way choice, and "Chosen" asks the host
for the list. Both languages, a desktop and a phone, light and dark; with SHOTS set, pictures of each.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_model_settings.py
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
from screenshots import S1, SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

WORDS = {
    "en": {"overridden": "Overridden", "default": "Default", "reset": "Reset", "save": "Save", "builtin": "the built-in table", "metered": "Per token",
           "locked": "set up as a subscription", "invalid": "zero or more", "subm": "Models for subagents", "chosen": "Chosen", "price": "Price"},
    "ru": {"overridden": "Переопределено", "default": "По умолчанию", "reset": "Сбросить", "save": "Сохранить", "builtin": "встроенная таблица", "metered": "По токенам",
           "locked": "настроен как подписка", "invalid": "не меньше нуля", "subm": "Модели для субагентов", "chosen": "Выбранные", "price": "Цена"},
}


def models_page(browser, lang: str, width: int, scheme: str, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang}/{width}/{scheme}"
    settings = copy.deepcopy(SETTINGS)
    sent: list[tuple[str, str, object]] = []
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.route("**/api/settings", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(settings)))

    def price_route(route):  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("/api/", 1)[1]
        sent.append((request.method, path, request.post_data_json if request.post_data else None))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))

    page.route("**/api/presets/*/price", price_route)
    page.goto(f"{BASE}/settings/models?token=t&lang={lang}&scheme={scheme}")
    page.wait_for_selector(".mrow")
    flash = page.locator(".mrow", has_text="DeepSeek Flash")
    flash.locator(".mmain").click()
    price = flash.locator(".model-price")
    price.wait_for()
    text = price.inner_text()
    for word in (words["price"], words["overridden"], words["builtin"], words["reset"]):
        if word not in text:
            failures.append(f"{where}: the overridden price lacks {word!r}: {text!r}")
    if SHOTS:
        # The whole card on a desktop; on a phone it is taller than the screen, so the price alone.
        shot = flash if width >= 600 else price
        shot.scroll_into_view_if_needed()
        shot.screenshot(path=f"{SHOTS}/price-{lang}-{width}-{scheme}.png")
    price.get_by_role("button", name=words["reset"]).click()
    page.wait_for_timeout(200)
    if ("DELETE", "presets/deepseek-flash/price", None) not in sent:
        failures.append(f"{where}: Reset did not ask the host to remove the price: {sent}")
    rates = price.locator("input")
    rates.nth(0).fill("-1")
    price.get_by_role("button", name=words["save"]).click()
    if words["invalid"] not in price.inner_text():
        failures.append(f"{where}: a negative rate was not refused in the card")
    rates.nth(0).fill("0.4")
    price.get_by_role("button", name=words["save"]).click()
    page.wait_for_timeout(200)
    if ("PUT", "presets/deepseek-flash/price", {"input": 0.4, "output": 1, "cache_hit": 0.005, "billing": "metered"}) not in sent:
        failures.append(f"{where}: Save sent {sent[-1:]}")

    login = page.locator(".mrow", has_text="Claude Opus 5")
    login.locator(".mmain").click()
    login.locator(".model-price").wait_for()
    if words["default"] not in login.locator(".model-price").inner_text():
        failures.append(f"{where}: the login's zero price is not marked as the default")
    login.locator(".model-price").get_by_role("radio", name=words["metered"]).click()
    page.wait_for_timeout(200)
    if ("PUT", "presets/claude-opus-5/price", {"input": 0, "output": 0, "cache_hit": 0, "billing": "metered"}) not in sent:
        failures.append(f"{where}: switching the login to per-token sent {sent[-1:]}")

    luna = page.locator(".mrow", has_text="GPT-5.6 Luna")
    luna.locator(".mmain").click()
    luna.locator(".model-price").wait_for()
    if words["locked"] not in luna.locator(".model-price").inner_text() or luna.locator(".model-price [role=radiogroup]").count():
        failures.append(f"{where}: a prepaid provider's model offers a switch it cannot have")
    if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
        failures.append(f"{where}: the models page scrolls sideways")
    context.close()


def details(browser, lang: str, width: int, scheme: str, failures: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang}/{width}/{scheme} details"
    mobile = width < 600
    sent: list[object] = []
    context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=mobile, has_touch=mobile, color_scheme=scheme)
    page = context.new_page()
    page.set_default_timeout(10000)
    page.route("**/api/**", stub)

    def chosen(route):  # type: ignore[no-untyped-def]
        if route.request.method == "PUT":
            sent.append(route.request.post_data_json)
        route.fallback()

    page.route("**/api/sessions/*/subagent-models", chosen)
    page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme={scheme}&lang={lang}")
    scope = ".panel-sheet" if mobile else ".panel"
    box = page.locator(f"{scope} .dt-subagent-models")
    box.wait_for()
    if words["subm"] not in box.inner_text():
        failures.append(f"{where}: the session card has no helper models: {box.inner_text()!r}")
    box.get_by_role("radio", name=words["chosen"]).click()
    page.wait_for_timeout(200)
    if {"mode": "chosen", "presets": []} not in sent:
        failures.append(f"{where}: Chosen sent {sent}")
    if SHOTS:
        box.scroll_into_view_if_needed()
        box.screenshot(path=f"{SHOTS}/subagent-models-{lang}-{width}-{scheme}.png")
    if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
        failures.append(f"{where}: the details scroll sideways")
    context.close()


def run() -> int:
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang in WORDS:
            for width in (1280, 390):
                for scheme in ("dark", "light"):
                    models_page(browser, lang, width, scheme, failures)
                    details(browser, lang, width, scheme, failures)
        browser.close()
    for line in failures:
        print("FAIL:", line)
    print("ok" if not failures else f"{len(failures)} problem(s)")
    return (1 if failures else 0) or UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
