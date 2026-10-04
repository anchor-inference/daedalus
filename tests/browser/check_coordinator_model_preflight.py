"""An enable refusal keeps its model controls and an actionable error in the same dialog."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from check_project_focus import GARDEN, WORDS, serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
REFUSAL = "coordinator model 'subscription' cannot run with spending limits: this model needs known prices and a documented provider input ceiling. Choose another model."


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            focus = FocusStub.bakery(lang)
            serve(page, focus)
            def team_choices(route, _request, *, focus=focus) -> None:  # type: ignore[no-untyped-def]
                _, payload = focus.answer("GET", f"/api/projects/{GARDEN}/staff", "", None)
                payload["choices"]["coordinator_default_preset"] = "fast"
                route.fulfill(content_type="application/json", body=json.dumps(payload))

            page.route(f"**/api/projects/{GARDEN}/staff*", team_choices)
            page.route(f"**/api/projects/{GARDEN}/orchestrator", lambda route: route.fulfill(
                status=400, content_type="application/json", body=json.dumps({"detail": REFUSAL})))
            page.goto(f"{BASE}/project/{GARDEN}?token=t&lang={lang}")
            page.get_by_role("button", name=WORDS[lang]["enable"], exact=True).click()
            sheet = page.locator(".enable-sheet")
            expect(sheet.locator("details")).not_to_have_attribute("open", "")
            sheet.get_by_role("button", name=WORDS[lang]["on"], exact=True).click()
            expect(sheet.get_by_role("alert")).to_contain_text("This model cannot run with spending limits" if lang == "en" else "Эта модель не может работать с лимитами расходов")
            expect(sheet.locator("p.sub.form-hint")).to_contain_text(REFUSAL[0].upper() + REFUSAL[1:])
            expect(sheet.locator("#orch-model")).to_be_visible()
            expect(sheet.locator("#orch-model")).to_have_value("")
            expect(sheet.locator("#orch-model option[value='']")).to_contain_text("DeepSeek Flash")
            assert focus.enabled == []
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            context.close()
        browser.close()
    print("coordinator model preflight: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
