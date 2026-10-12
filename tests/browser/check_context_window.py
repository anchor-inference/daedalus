"""Does each model's settings say where its context window came from, and offer the way back to automatic?

The window used to be a bare number with a 128K default: nobody but the person who typed it could
tell whether it was true. Now the host finds it (the provider's listing, models.dev, a built-in
table, a refusal) and the settings say which. This check opens Settings → Models in both languages,
expands a model whose window was found and one whose window was typed, and asserts the line under
the field: "found automatically: the provider" with Check again, "set by hand" with Find
automatically — and that pressing them asks the host the right thing.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_context_window.py
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
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"found": "Found automatically: the provider", "manual": "Set by hand", "auto": "Find automatically", "refresh": "Check again"},
    "ru": {"found": "Определено автоматически: провайдер", "manual": "Задано вручную", "auto": "Определять автоматически", "refresh": "Проверить снова"},
}


def run() -> int:
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang, words in WORDS.items():
            for width in (1280, 390):
                settings = copy.deepcopy(SETTINGS)
                sent: list[tuple[str, str, object]] = []
                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                page.route("**/api/**", stub)

                def settings_route(route, *, settings=settings):  # type: ignore[no-untyped-def]
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))

                def preset_route(route, *, settings=settings, sent=sent):  # type: ignore[no-untyped-def]
                    request = route.request
                    path = request.url.split("/api/", 1)[1]
                    body = request.post_data_json if request.post_data else None
                    sent.append((request.method, path, body))
                    if request.method == "PUT":
                        pid = path.split("/", 1)[1]
                        if isinstance(body, dict) and body.get("context_window_source") == "auto":
                            settings["presets"][pid] = {**settings["presets"][pid], "context_window": 272000, "context_window_source": "catalog"}
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
                        return
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"window": 1000000, "source": "provider", "sources": {}}))

                page.route("**/api/settings", settings_route)
                page.route("**/api/presets/**", preset_route)
                page.goto(f"{BASE}/settings/models?token=t&lang={lang}")
                page.wait_for_selector(".mrow")
                found = page.locator(".mrow", has_text="Claude Opus 5")
                found.locator(".mmain").click()
                line = found.locator('[data-testid="window-source"]')
                line.wait_for()
                text = line.inner_text()
                if words["found"] not in text or words["refresh"] not in text:
                    failures.append(f"{lang}/{width}: a found window says {text!r}")
                if "1.0" not in (line.get_attribute("title") or "") or "models.dev" not in (line.get_attribute("title") or ""):
                    failures.append(f"{lang}/{width}: the sources are not in the hint: {line.get_attribute('title')!r}")
                line.get_by_role("button", name=words["refresh"]).click()
                page.wait_for_timeout(300)
                if not any(m == "POST" and path == "presets/claude-opus-5/context-window" for m, path, _ in sent):
                    failures.append(f"{lang}/{width}: Check again did not ask the host: {sent}")
                typed = page.locator(".mrow", has_text="GPT-5.6 Luna")
                typed.locator(".mmain").click()
                manual = typed.locator('[data-testid="window-source"]')
                manual.wait_for()
                if words["manual"] not in manual.inner_text():
                    failures.append(f"{lang}/{width}: a typed window says {manual.inner_text()!r}")
                manual.get_by_role("button", name=words["auto"]).click()
                page.wait_for_timeout(300)
                if ("PUT", "presets/gpt-5.6-luna", {"context_window_source": "auto"}) not in sent:
                    failures.append(f"{lang}/{width}: Find automatically sent {sent}")
                after = typed.locator('[data-testid="window-source"]').inner_text()
                if words["manual"] in after:
                    failures.append(f"{lang}/{width}: the window still says it was typed after going automatic: {after!r}")
                overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
                if overflow:
                    failures.append(f"{lang}/{width}: the page scrolls sideways")
                context.close()
        browser.close()
    for line in failures:
        print("FAIL:", line)
    print("ok" if not failures else f"{len(failures)} problem(s)")
    return (1 if failures else 0) or UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
