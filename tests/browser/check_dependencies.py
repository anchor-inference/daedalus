"""Dependency review survives reload, requires consent, and cancellation never installs."""
from __future__ import annotations

import copy
import json
import os

from api_stub import GATES
from check_composer import BASE, CHROMIUM, UNHANDLED, stub
from playwright.sync_api import expect, sync_playwright


def run() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for width in (390, 1440):
            for language in ("en", "ru"):
                view = copy.deepcopy(GATES["/api/dependencies"])
                requests = []

                def route_api(route, *, view=view, requests=requests):
                    path = route.request.url.split("?", 1)[0].split("/api/", 1)[1]
                    if not path.startswith("dependencies"):
                        return stub(route)
                    if route.request.method == "POST":
                        requests.append(path)
                        if path.endswith("/request"):
                            data = route.request.post_data_json
                            assert data == {"request": "Add Pillow", "preset": "default"}
                            view["proposal"] = {"id": "request", "state": "ready", "request": data["request"], "explanation": "Pillow adds image processing.", "proposal": {"recipe": {"python": ["Pillow"], "system": []}, "patch": "--- deploy/dependencies/local.json\n+++ deploy/dependencies/local.json\n+ Pillow"}}
                        elif path.endswith("/cancel"):
                            view["proposal"]["state"] = "cancelled"
                        elif path.endswith("/approve"):
                            view["proposal"]["state"] = "accepted"
                            view["job"] = {"id": "request", "state": "installing", "stage": "building", "started_at": 1, "updated_at": 1, "progress": [], "error": ""}
                        else:
                            raise AssertionError(path)
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(view))

                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                page.route("**/api/**", route_api)
                page.goto(f"{BASE}/settings/dependencies?lang={language}")
                if directory := os.environ.get("DEPENDENCY_SCREENSHOTS"):
                    page.screenshot(path=f"{directory}/dependency-idle-{width}-{language}.png")
                page.locator("#dependency-request").fill("Add Pillow")
                prepare = "Prepare patch" if language == "en" else "Подготовить патч"
                cancel = "Cancel" if language == "en" else "Отмена"
                install = "Accept and install" if language == "en" else "Принять и установить"
                page.get_by_role("button", name=prepare, exact=True).click()
                # The review sheet: Settings is itself a dialog now, so "the dialog" has to name which.
                dialog = page.locator(".sheet[role='dialog']")
                expect(dialog).to_be_visible()
                expect(dialog.locator(".deps-patch")).to_contain_text("Pillow")
                expect(dialog.get_by_role("button", name=install)).to_be_disabled()
                dialog.get_by_role("button", name=cancel, exact=True).click()
                expect(dialog).to_have_count(0)
                assert not any(path.endswith("/approve") for path in requests)
                page.get_by_role("button", name=prepare, exact=True).click()
                # Reopening a ready proposal after reload must require fresh consent.
                page.reload()
                expect(dialog).to_be_visible()
                expect(dialog.get_by_role("checkbox")).not_to_be_checked()
                dialog.get_by_role("checkbox").check()
                expect(dialog.get_by_role("button", name=install)).to_be_enabled()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if directory := os.environ.get("DEPENDENCY_SCREENSHOTS"):
                    page.wait_for_timeout(300)
                    page.screenshot(path=f"{directory}/dependency-review-{width}-{language}.png")
                dialog.get_by_role("button", name=install).click()
                expect(dialog).to_have_count(0)
                page.reload()
                expect(page.locator(".deps-job")).to_be_visible()
                expect(page.locator("#dependency-request")).to_have_count(0)
                view["job"]["state"] = "failed"
                view["job"]["error"] = "The old image is still running."
                expect(page.locator(".deps-job")).to_contain_text("old image", timeout=8000)
                expect(page.locator("#dependency-request")).to_be_enabled()
                expect(page.locator("#dependency-request")).to_have_value("")
                # The accepted receipt survives another reload, but must not masquerade as a draft.
                page.reload()
                expect(page.locator("#dependency-request")).to_have_value("")
                assert sum(path.endswith("/approve") for path in requests) == 1
                context.close()
                print(f"{width} {language}: preview, consent, cancellation, reload, installation and failure passed")
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    run()
