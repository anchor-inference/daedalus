"""Two-factor sign-in in a real browser: enrolment in Settings → Security, and the login's second step.

The host's arithmetic has unit tests (tests/unit/test_two_factor.py); this is what exists only once it
is drawn. A host stand-in with a known secret answers the routes, so the check can compute the right
code itself:

- Security offers "Set up"; it shows the QR code (drawn, square, of a useful size) and the key to type;
- a wrong code is refused with the host's words and nothing is switched on;
- the right code switches it on and shows ten recovery codes once, with Copy; "I saved them" hides them;
- at phone width the enrolment fits without a sideways scroll;
- in the desktop window the card offers no enrolment and says why;
- signed out with a first factor spent, the login asks for the code: a wrong one is refused, the
  right one signs in.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_two_factor.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import stub as installation  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
RECOVERY = [f"abcd-efgh-{n:04d}" for n in range(10)]
# Not a real QR code — the check does not scan it — but the shape of one: 29 modules square.
QR = ["".join("1" if (x * y + x + y) % 3 == 0 else "0" for x in range(29)) for y in range(29)]


def code(step: int) -> str:
    """RFC 6238 with SHA-1, six digits: written out here so the check needs nothing of the host's."""
    digest = hmac.new(base64.b32decode(SECRET), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)


def accepted(typed: str) -> bool:
    now = int(time.time() // 30)
    return typed in {code(now + k) for k in (-1, 0, 1)}


def wrong() -> str:
    now = int(time.time() // 30)
    good = {code(now + k) for k in (-2, -1, 0, 1, 2)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in good)


class Host:
    def __init__(self) -> None:
        self.enabled = False
        self.signed_in = True
        self.codes_posted: list[str] = []

    def status(self) -> dict:
        return {"enabled": self.enabled, "enrolling": False, "recovery_left": 10 if self.enabled else 0, "enabled_at": "2026-10-14T09:00:00+00:00" if self.enabled else None}


HOST = Host()


def stub(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    rel = request.url.split("?", 1)[0]
    rel = rel[rel.index("/api/"):]

    def answer(body: object, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    data = json.loads(request.post_data) if request.method == "POST" and request.post_data else {}
    if rel == "/api/auth/me":
        return answer({"user_id": 1, "via": "cookie"}) if HOST.signed_in else answer({"detail": "authentication required"}, 401)
    if rel == "/api/auth/config":
        return answer({"telegram": None, "pairing": False, "second_factor": not HOST.signed_in})
    if rel == "/api/auth/totp":
        return answer(HOST.status())
    if rel == "/api/auth/totp/setup":
        return answer({"secret": SECRET, "uri": f"otpauth://totp/Daedalus:operator?secret={SECRET}&issuer=Daedalus", "qr": QR, "label": "operator"})
    if rel in ("/api/auth/totp/enable", "/api/auth/totp/verify"):
        typed = str(data.get("code", ""))
        HOST.codes_posted.append(typed)
        if not accepted(typed):
            return answer({"detail": "that code is not right; check the clock on your phone, or use a recovery code"}, 400)
        if rel.endswith("/enable"):
            HOST.enabled = True
            return answer({**HOST.status(), "recovery_codes": RECOVERY})
        HOST.signed_in = True
        return answer({"ok": True, "with": "totp", "recovery_left": 10})
    if rel == "/api/auth/logout":
        return answer({"ok": True})
    return installation(route)


def open_security(context) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/settings/security?lang=en")
    page.wait_for_selector(".totp-card[data-totp]:not([data-totp='loading'])", timeout=15000)
    return page


def enrol(page: Page, problems: list[str]) -> None:
    card = page.locator(".totp-card")
    card.get_by_role("button", name="Set up").click()
    qr = card.locator("svg.totp-qr")
    expect(qr).to_be_visible()
    box = qr.bounding_box()
    if not box or box["width"] < 150 or abs(box["width"] - box["height"]) > 1:
        problems.append(f"the QR code is not a square of a scannable size ({box})")
    if qr.locator("path").get_attribute("d", timeout=2000).count("M") < 100:
        problems.append("the QR code has no modules drawn")
    shown = card.locator(".totp-secret").inner_text().replace(" ", "")
    if shown != SECRET:
        problems.append(f"the key to type is not the secret ({shown})")

    field = card.locator("#totp-enable-code")
    field.fill(wrong())
    card.get_by_role("button", name="Turn on").click()
    expect(card.locator(".totp-error")).to_contain_text("not right")
    if HOST.enabled:
        problems.append("a wrong code switched two-factor on")
    if card.locator("svg.totp-qr").count() != 1:
        problems.append("a wrong code took the QR code away")

    field.fill(code(int(time.time() // 30)))
    card.get_by_role("button", name="Turn on").click()
    expect(card.locator(".totp-codes li")).to_have_count(10)
    if not HOST.enabled:
        problems.append("the right code did not switch two-factor on")
    if card.locator(".totp-codes").inner_text().split() != RECOVERY:
        problems.append("the recovery codes shown are not the host's")
    expect(card.get_by_role("button", name="Copy")).to_be_visible()
    card.get_by_role("button", name="I saved them").click()
    expect(card.locator(".totp-codes")).to_have_count(0)
    expect(card).to_contain_text("10 of 10 recovery codes left")
    expect(card.get_by_role("button", name="Turn off")).to_be_visible()


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)

        desk = browser.new_context(viewport={"width": 1280, "height": 860})
        page = open_security(desk)
        enrol(page, problems)
        desk.close()

        HOST.enabled = False
        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        page = open_security(phone)
        page.locator(".totp-card").get_by_role("button", name="Set up").click()
        expect(page.locator("svg.totp-qr")).to_be_visible()
        page.locator(".totp-secret").scroll_into_view_if_needed()
        wide = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if wide > 0:
            problems.append(f"at phone width the enrolment scrolls sideways by {wide}px")
        phone.close()

        window = browser.new_context(viewport={"width": 1280, "height": 860})
        window.add_init_script("window.daedalus = { window: true, shell: true };")
        page = open_security(window)
        card = page.locator(".totp-card")
        if card.get_by_role("button").count() != 0:
            problems.append("the desktop window offers to set two-factor up")
        expect(card).to_contain_text("launcher")
        window.close()

        HOST.enabled, HOST.signed_in = True, False
        plain = browser.new_context(viewport={"width": 1180, "height": 820})
        page = plain.new_page()
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents?lang=en")
        page.wait_for_selector(".login #login-code", timeout=15000)
        if page.locator("#pairing").count():
            problems.append("the second step still offers the pairing field")
        page.locator("#login-code").fill(wrong())
        page.locator(".login-code").get_by_role("button", name="Sign in").click()
        expect(page.locator(".login-error")).to_contain_text("not right")
        page.locator("#login-code").fill(code(int(time.time() // 30)))
        page.locator(".login-code").get_by_role("button", name="Sign in").click()
        page.wait_for_selector(".login", state="detached", timeout=10000)
        if not HOST.signed_in:
            problems.append("the right code did not sign the browser in")
        plain.close()
        browser.close()

    for problem in problems:
        print("FAIL", problem)
    if problems:
        return 1
    print("two-factor: QR and key shown, a wrong code refused, the right one enables; the window offers none; the login asks for the code")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
