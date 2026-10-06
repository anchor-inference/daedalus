"""The Free tab shows live provider groups, key links, and requires an agent probe."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def main() -> int:
    expect_app(BASE)
    catalog = {"providers": [
        {"id": "kilo", "name": "Kilo Gateway", "base_url": "https://api.kilo.ai/api/gateway", "key_url": "", "key_required": False, "fresh": True,
         "models": [{"id": "free-one", "name": "Free One", "provider": "kilo", "mechanism": "zero_price", "tools_reported": True, "agent_ready": False, "may_train": True}]},
        {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "key_url": "https://openrouter.ai/settings/keys", "key_required": True, "fresh": True,
         "models": [{"id": "free-two", "name": "Free Two", "provider": "openrouter", "mechanism": "zero_price", "tools_reported": True, "agent_ready": False, "may_train": False}]},
    ], "updated_at": None, "stale": False}
    probed: list[dict] = []
    in_settings = False

    def answer(route) -> None:
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/api/providers/free-catalog"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(catalog))
        elif path.endswith("/api/onboarding"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"has_model": in_settings, "presets": int(in_settings), "default_preset": "p" if in_settings else "", "providers": [{"id": "kilo", "ready": True}, {"id": "openrouter", "ready": False}], "needs": [] if in_settings else ["model"], "message": ""}))
        elif path.endswith("/api/providers/free-catalog/probe"):
            probed.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body='{"agent_ready":false}')
        else:
            stub(route)

    stub.fresh = True  # type: ignore[attr-defined]
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", answer)
        page.goto(f"{BASE}/agents?token=t&lang=ru")
        expect(page.locator(".addmodel")).to_be_visible()
        page.get_by_role("tab", name="Бесплатные").click()
        expect(page.locator(".free-catalog .pickgrid .pick")).to_have_count(2)
        expect(page.locator(".free-catalog .mrow", has_text="Free One")).to_be_visible()
        page.locator(".free-catalog .pick", has_text="OpenRouter").click()
        expect(page.locator(".free-catalog a[href='https://openrouter.ai/settings/keys']")).to_contain_text("OpenRouter")
        if output := os.environ.get("OUT"):
            page.screenshot(path=output, full_page=True)
        expect(page.locator(".free-catalog .mrow", has_text="Free Two").get_by_role("button")).to_be_disabled()
        page.locator(".free-catalog .pick", has_text="Kilo Gateway").click()
        page.locator(".free-catalog .mrow", has_text="Free One").get_by_role("button").click()
        expect(page.get_by_text("Модель не прошла проверку", exact=False)).to_be_visible()
        in_settings = True
        page.goto(f"{BASE}/settings/models?token=t&tab=free&lang=ru")
        expect(page.get_by_role("tab", name="Бесплатные")).to_have_attribute("aria-selected", "true")
        page.locator(".free-catalog .pick", has_text="OpenRouter").click()
        expect(page.locator(".free-catalog a[href='https://openrouter.ai/settings/keys']")).to_contain_text("OpenRouter")
        browser.close()
    assert probed == [{"provider": "kilo", "model": "free-one"}], probed
    print("Free provider hierarchy, key link, and agent probe: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
