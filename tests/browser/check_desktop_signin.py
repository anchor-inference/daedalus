"""The desktop window's login screen signs itself in through the launcher, and never loops.

Inside the desktop application the app's session can be gone — a month went by, or the one link the
start offered was spent — and the login screen used to ask for "a pairing link or code" that the
desktop application shows nowhere. The window now asks its launcher for a link (window.daedalus.signIn,
bound by the shell's preload) and goes through it. What is checked, with that bridge faked in a page
whose API answers "signed out":

- the screen asks the launcher once on its own, shows a sign-in button, and shows no code field;
- reloaded within a minute — the link came back here without signing in — it does not ask again on
  its own, so a link that fails cannot spin the window round; the button still asks;
- a failure the launcher reports is shown;
- in a plain browser the screen is unchanged: the code field is there and nothing is asked.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_desktop_signin.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

# What the shell's preload binds, recording each request and letting the page hear a failure.
BRIDGE = """
window.__asked = 0;
window.__failed = null;
window.daedalus = {
  window: true,
  shell: true,
  signIn: () => { window.__asked += 1; },
  onSignInFailed: (callback) => { window.__failed = callback; return () => { window.__failed = null; }; },
};
"""


def run() -> int:
    expect_app(BASE)
    stub.signedout = True  # type: ignore[attr-defined]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM)

            desk = browser.new_context(viewport={"width": 1180, "height": 820})
            desk.add_init_script(BRIDGE)
            page = desk.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents?lang=en")
            page.wait_for_selector(".login")
            page.wait_for_timeout(300)
            assert page.evaluate("window.__asked") == 1, "the window did not ask its launcher to sign it in"
            expect(page.locator(".login").get_by_role("button", name="Sign in", exact=True)).to_be_visible()
            assert page.locator("#pairing").count() == 0, "the desktop window still asks for a code it shows nowhere"

            page.reload()
            page.wait_for_selector(".login")
            page.wait_for_timeout(300)
            assert page.evaluate("window.__asked") == 0, "a reload within the minute asked again: a failing link would loop"
            page.locator(".login").get_by_role("button", name="Sign in", exact=True).click()
            assert page.evaluate("window.__asked") == 1, "the button did not ask the launcher"
            page.evaluate("window.__failed('the stack is not running')")
            expect(page.locator(".login-error")).to_contain_text("the stack is not running")
            expect(page.locator(".login").get_by_role("button", name="Sign in", exact=True)).to_be_enabled()
            desk.close()

            plain = browser.new_context(viewport={"width": 1180, "height": 820})
            page = plain.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents?lang=en")
            page.wait_for_selector(".login #pairing")
            assert page.locator(".login").get_by_role("button", name="Sign in", exact=True).count() == 0, "a plain browser was offered the launcher's sign-in"
            plain.close()
            browser.close()
    finally:
        stub.signedout = False  # type: ignore[attr-defined]
    print("desktop sign-in: asks once, does not loop, reports a failure; a browser is unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
