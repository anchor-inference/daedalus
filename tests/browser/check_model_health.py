"""A broken or slow model is marked where the operator chooses it, and Check models marks the rest.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- Settings → Models and providers marks a broken preset with a red pill and the upstream's own words under its row,
  and a slow one with an amber pill whose tooltip says how slow;
- Check models sends one request (``POST /api/models/check``) and redraws the marks from its answer,
  with its foot counting the models that answered;
- Settings → Translation shows the mark on the chosen translator itself, and the fallback translator's
  picker shows the marks on its rows, so a broken choice is seen before it is made.

Pictures go to ``SHOTS`` when it is set.

    APP_URL=http://127.0.0.1:8471/app python3 tests/browser/check_model_health.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")
SHOTS = os.environ.get("SHOTS", "")

BROKEN = "gpt-5.6-luna"
SLOW = "deepseek-flash"
CHECKED_BROKEN = "qwen-local"
CODEX = "The 'gpt-5.6-luna' model is not supported when using Codex with a ChatGPT account."
GONE = "qwen/qwen3.8-27b:free is unavailable for free, the paid slug is qwen/qwen3.8-27b"


def settings() -> dict:
    view = copy.deepcopy(SETTINGS)
    view["model_health"] = {
        BROKEN: {"state": "broken", "detail": CODEX, "at": "2026-11-25T12:00:00+00:00"},
        SLOW: {"state": "slow", "detail": "", "median_ms": 13600, "calls": 12},
    }
    # The translator is the broken model, so its own picker must say so.
    view["translation"] = {**view["translation"], "preset": BROKEN, "fallback_suggested": SLOW}
    return view


def checked() -> dict:
    at = "2026-11-25T12:05:00+00:00"
    results = {pid: {"state": "ok", "detail": "", "ms": 1200, "at": at} for pid in SETTINGS["presets"]}
    results[BROKEN] = {"state": "broken", "detail": CODEX, "ms": 300, "at": at}
    results[CHECKED_BROKEN] = {"state": "broken", "detail": GONE, "ms": 250, "at": at}
    health = {pid: {"state": "ok", "detail": "", "check": result} for pid, result in results.items()}
    health[BROKEN] = {"state": "broken", "detail": CODEX, "check": results[BROKEN]}
    health[CHECKED_BROKEN] = {"state": "broken", "detail": GONE, "check": results[CHECKED_BROKEN]}
    return {"checked_at": at, "results": results, "health": health}


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / name), full_page=False)


def row(page, pid: str):  # type: ignore[no-untyped-def]
    """A preset's row in Settings → Models and providers, inside its provider's row."""
    return page.locator(f'.pmodel[data-preset="{pid}"]').first


def open_providers(page) -> None:  # type: ignore[no-untyped-def]
    """Every provider row opened: only the default model's provider is open at first, and a model is drawn inside its own."""
    for _ in range(page.locator(".prov-row").count()):
        closed = page.locator('.prov-row[aria-expanded="false"]')
        if not closed.count():
            break
        closed.first.click()


def run(browser, lang: str, viewport: dict, problems: list[str], **context) -> None:  # type: ignore[no-untyped-def]
    phone = viewport["width"] < 1024
    tag = f"{lang}{'-phone' if phone else ''}"
    where = f"[{lang} {viewport['width']}]"
    say = lambda text: problems.append(f"{where} {text}")  # noqa: E731
    print(where)
    page = browser.new_context(viewport=viewport, color_scheme="dark", **context).new_page()
    checks: list[str] = []

    def route(route) -> None:  # type: ignore[no-untyped-def]
        path = route.request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel == "/api/settings" and route.request.method == "GET":
            return respond(route, settings())
        if rel == "/api/models/check" and route.request.method == "POST":
            checks.append(rel)
            return respond(route, checked())
        return stub(route)

    page.route("**/api/**", route)

    # Settings → Models and providers: the marks from the calls already made.
    page.goto(f"{BASE}/settings/models?token=t&lang={lang}")
    page.wait_for_selector(".prov-row", timeout=15000)
    open_providers(page)
    broken = row(page, BROKEN)
    pill = broken.locator('.pmodel-tag[data-health="broken"]')
    if not pill.is_visible():
        say("the broken model has no broken pill")
    elif "failed" not in (pill.get_attribute("class") or ""):
        say(f"the broken pill is not drawn as a failure: {pill.get_attribute('class')}")
    detail = broken.locator(".pmodel-health")
    if not detail.is_visible() or CODEX not in detail.inner_text():
        say("the upstream's words are not under the broken model's row")
    slow = row(page, SLOW).locator('.pmodel-tag[data-health="slow"]')
    if not slow.is_visible():
        say("the slow model has no slow pill")
    elif "13.6" not in (slow.get_attribute("title") or ""):
        say(f"the slow pill does not say how slow: {slow.get_attribute('title')!r}")
    if row(page, CHECKED_BROKEN).locator("[data-health]").count():
        say("a model nothing is known about is marked")
    shot(page, f"models-{tag}.png")

    # Check models: one request, and the marks redrawn from its answer.
    button = page.locator('[data-setting="models.check"] button')
    if not button.is_visible():
        say("there is no Check models button")
    else:
        button.scroll_into_view_if_needed()
        with page.expect_response(lambda reply: "/api/models/check" in reply.url):
            button.click()
        try:
            row(page, CHECKED_BROKEN).locator('.pmodel-tag[data-health="broken"]').wait_for(timeout=5000)
        except Exception:  # noqa: BLE001
            say("the check's broken model was not marked")
        if GONE not in row(page, CHECKED_BROKEN).locator(".pmodel-health").inner_text():
            say("the check's refusal is not shown under its row")
        if not row(page, SLOW).locator('.pmodel-tag[data-health="ok"]').is_visible():
            say("a model that answered the check does not show its time")
        count = page.locator('[data-setting="models.check"] [data-checked]')
        if not count.is_visible() or "2" not in count.inner_text() or "4" not in count.inner_text():
            say("the check's foot does not count the models that answered")
        if len(checks) != 1:
            say(f"the check was sent {len(checks)} times")
        shot(page, f"models-checked-{tag}.png")

    # Settings → Translation: the chosen translator carries its mark, and the fallback's list marks its rows.
    page.goto(f"{BASE}/settings/translation?token=t&lang={lang}")
    page.wait_for_selector('[data-setting="translation.fallback_preset"]', timeout=10000)
    chosen = page.locator("#translation-preset .sel-current .sel-fact.alert.bad")
    if not chosen.is_visible():
        say("the chosen translator does not show that it is broken")
    elif chosen.get_attribute("title") != CODEX:
        say(f"the translator's mark does not carry the reason: {chosen.get_attribute('title')!r}")
    fallback = page.locator('[data-setting="translation.fallback_preset"]')
    if not fallback.locator(".sel.single").count():
        say("the fallback translator is not chosen with the in-place model picker")
    else:
        fallback.scroll_into_view_if_needed()
        fallback.locator("#translation-fallback .sel-current .btn").click()
        try:
            fallback.locator(f'.sel-row[data-id="{BROKEN}"] .sel-fact.alert.bad').wait_for(timeout=5000)
        except Exception:  # noqa: BLE001
            say("the fallback's list does not mark the broken model")
        if not fallback.locator(f'.sel-row[data-id="{SLOW}"] .sel-fact.alert.warn').is_visible():
            say("the fallback's list does not mark the slow model")
        shot(page, f"translation-{tag}.png")

    widths = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    if widths[0] > widths[1]:
        say(f"the page scrolls sideways: {widths}")
    page.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ([LANG] if LANG else ["en", "ru"]):
            run(browser, lang, {"width": 1440, "height": 900}, problems)
            run(browser, lang, {"width": 390, "height": 844}, problems, is_mobile=True, has_touch=True)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
