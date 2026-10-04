"""The installer Free tab explains where to get a key and tests its selected model."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from check_launcher_setup import CHROMIUM, free_port, serve_launcher, settled
from launcher_shots import build
from playwright.sync_api import expect, sync_playwright


def main() -> int:
    binary = Path(os.environ["LAUNCHER"]) if os.environ.get("LAUNCHER") else build()
    work = Path(tempfile.mkdtemp(prefix="launcher-free-"))
    port = free_port()
    launcher = serve_launcher(binary, work, port)
    catalog = {"providers": [
        {"id": "kilo", "name": "Kilo Gateway", "key_url": "", "key_required": False, "models": [{"id": "free-one", "name": "Free One", "may_train": True, "tools_reported": True}]},
        {"id": "openrouter", "name": "OpenRouter", "key_url": "https://openrouter.ai/settings/keys", "key_required": True, "models": [{"id": "free-two", "name": "Free Two", "may_train": False, "tools_reported": True}]},
    ]}
    checked: list[dict] = []
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path=CHROMIUM)
            for width in (1200, 390):
                page = browser.new_page(viewport={"width": width, "height": 844}, locale="ru-RU")

                def intercept(route) -> None:
                    if route.request.url.endswith("/api/setup/free-catalog"):
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(catalog))
                    elif route.request.url.endswith("/api/setup/free-probe"):
                        checked.append(json.loads(route.request.post_data or "{}"))
                        route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                    else:
                        route.continue_()

                page.route("**/api/setup/free-*", intercept)
                page.goto(f"http://127.0.0.1:{port}/setup?motion=0")
                settled(page)
                page.click(".f-foot [data-act=next]")
                page.wait_for_function("() => document.querySelector('[data-wizard]').dataset.step === 'runs'")
                page.click(".f-foot [data-act=next]")
                page.wait_for_function("() => document.querySelector('[data-wizard]').dataset.step === 'model'")
                page.click("[data-k=kind-free]")
                expect(page.locator("[data-bind='free.provider']")).to_have_count(2)
                page.locator("label.prov", has_text="OpenRouter").click()
                expect(page.locator("a[href='https://openrouter.ai/settings/keys']")).to_contain_text("OpenRouter")
                page.locator("[data-bind='free.model']").select_option("free-two")
                page.locator("[data-bind='free.key']").fill("test-key")
                page.click("[data-act=test-free]")
                page.wait_for_function("() => window.Wizard.state.free.test === 'ok'")
                assert checked[-1] == {"provider": "openrouter", "model": "free-two", "key": "test-key"}
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.close()
            browser.close()
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)
    print("installer Free tab, key link, and probe: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
