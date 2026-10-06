"""A provider refusal is explicit, and a lost hold acknowledgement reuses its command."""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime, timedelta

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import expect, sync_playwright
from screenshots import SETTINGS, UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def main() -> int:
    expect_app(BASE)
    UNHANDLED.paths.clear()
    reset = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    settings = copy.deepcopy(SETTINGS)
    settings["providers"] = {"vendor": {"kind": "openai_compat", "base_url": "https://api.openai.com/v1",
                                        "api_key_set": True}}
    observation = {"id": "observed", "project_id": "project", "session_id": "session",
                   "model": "model", "status": 429, "failure_class": "rate", "reset_at": reset,
                   "retry_after_at": None, "hold_id": None, "hold_state": None,
                   "hold_available": True}
    seen: list[dict] = []
    lost = True

    def host(route):  # type: ignore[no-untyped-def]
        nonlocal lost
        request = route.request
        rel = request.url.split("?", 1)[0].split("/api/", 1)[-1]
        rel = "/api/" + rel
        if rel == "/api/settings" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
        if rel == "/api/providers/vendor/limits" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"provider_id": "vendor", "observations": [observation]}))
        if rel == "/api/projects/project/budget" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"project_id": "project", "entity_revision": 1,
                                                  "goal_revision": 1, "configured": False}))
        if rel == "/api/providers/vendor/holds" and request.method == "POST":
            body = request.post_data_json
            seen.append(body)
            if lost:
                lost = False
                return route.fulfill(status=503, content_type="application/json",
                                     body=json.dumps({"detail": "reply lost"}))
            observation.update(hold_id="held", hold_state="held", hold_available=False)
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"hold_id": "held", "receipt_id": "receipt",
                                                  "entity_revision": 2, "state": "held"}))
        return stub(route)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page = context.new_page()
            page.route("**/api/**", host)
            page.goto(f"{BASE}/settings/models?token=t&lang={lang}")
            provider = page.locator(".mrow", has_text="vendor").first
            provider.locator(".mmain").click()
            limits = provider.locator(".provider-limit")
            expect(limits).to_contain_text("rate limit" if lang == "en" else "ограничении частоты")
            expect(limits).to_contain_text("provider-declared" if lang == "en" else "заявленный сброс")
            limits.get_by_role("button", name="Hold this session" if lang == "en" else "Удержать эту сессию").click()
            expect(limits).to_contain_text("Decision unconfirmed" if lang == "en" else "Решение не подтверждено")
            limits.get_by_role("button", name="Try again" if lang == "en" else "Повторить").click()
            expect(limits).to_contain_text("held" if lang == "en" else "удержана")
            assert len(seen) >= 2 and seen[-1] == seen[-2], seen
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), lang
            context.close()
            observation.update(hold_id=None, hold_state=None, hold_available=True)
            lost = True
            seen.clear()
        browser.close()
    assert not UNHANDLED.paths, UNHANDLED.paths
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
