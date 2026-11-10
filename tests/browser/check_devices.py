"""Pairing a terminal and Settings → Devices, in a real browser.

`daedalus-cli login` asks the server to be paired and prints a code. The app announces the request
with a notice, and the approval sheet opens from it: the computer's name, its system, the code in large
letters to compare with the terminal, when it asked and how long it has left, and Approve and Deny.
The link the terminal prints (``?pair-device=<code>``) opens the same sheet and leaves the address
clean; a code nobody waits with gets a sentence instead of buttons.

Settings → Devices lists the paired computers with their system, whether they are connected, when they
were last seen, their chats and their token's age; Rename edits the name in place, Revoke asks in the
row (the Telegram viewer refuses ``confirm()``) and only the second press revokes. The same page takes
the code a terminal printed, typed by hand, and opens the sheet for it. In English and Russian, on a
desktop and a phone.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_devices.py

``SHOTS=<dir>`` also writes the pictures, in a dark and a light theme, there. Exit status is the
number of failed checks.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_stub  # noqa: E402
import screenshots as shots  # noqa: E402
from api_stub import CliStub, expect_app  # noqa: E402

BASE = shots.BASE
SHOTS = os.environ.get("SHOTS", "")
CLI = api_stub.CLI
WORDS = {
    "en": {"review": "Review", "approve": "Approve", "deny": "Deny", "missing": "No terminal is waiting with this code", "revoke": "Revoke",
           "keep": "Keep", "rename": "Rename", "save": "Save", "go": "Continue", "connected": "Connected", "chats": "2 chats", "section": "Devices"},
    "ru": {"review": "Посмотреть", "approve": "Одобрить", "deny": "Отклонить", "missing": "Ни один терминал не ждёт с этим кодом", "revoke": "Отозвать",
           "keep": "Оставить", "rename": "Переименовать", "save": "Сохранить", "go": "Продолжить", "connected": "Подключён", "chats": "2 чата", "section": "Устройства"},
}


def fresh() -> None:
    """The shared stub as a run finds it: one laptop paired, nobody asking."""
    fresh_cli = CliStub()
    CLI.devices, CLI.requests, CLI.sent = fresh_cli.devices, [], []


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM)
        for lang in ("en", "ru"):
            words = WORDS[lang]
            for scheme in ("dark", "light"):
                main_run = scheme == "dark"
                # The notice, the sheet, Approve.
                fresh()
                CLI.requests.append(CliStub.request("WDJB-MJHT", "studio-mac", "darwin", "arm64"))
                context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
                page = context.new_page()
                page.route("**/api/**", shots.stub)
                page.goto(f"{BASE}/agents?token=t&scheme={scheme}&lang={lang}")
                notice = page.locator(".pair-notice")
                expect(notice).to_be_visible(timeout=15000)
                if main_run:
                    check("studio-mac" in notice.inner_text() and "macOS" in notice.inner_text(), f"{lang}: the notice names the computer and its system ({notice.inner_text()!r})")
                    check(page.locator(".pair-sheet").count() == 0, f"{lang}: the sheet does not open by itself")
                shot(page, f"desktop-pair-notice-{scheme}-{lang}")
                notice.get_by_role("button", name=words["review"]).click()
                sheet = page.locator(".pair-sheet")
                expect(sheet).to_be_visible(timeout=5000)
                if main_run:
                    text = sheet.inner_text()
                    check(sheet.locator("[data-pair-code]").inner_text() == "WDJB-MJHT", f"{lang}: the sheet shows the code the terminal printed")
                    check("studio-mac" in text and "macOS · ARM" in text, f"{lang}: the sheet names the computer and its system")
                    check(sheet.locator(".pair-when").inner_text().strip() != "" and page.locator(".pair-body[data-pair-state=open]").count() == 1, f"{lang}: the sheet says how long the request has left")
                    check(page.locator(".pair-notice").count() == 0, f"{lang}: the notice steps aside while the sheet is open")
                page.wait_for_timeout(300)
                shot(page, f"desktop-pair-sheet-{scheme}-{lang}")
                sheet.get_by_role("button", name=words["approve"]).click()
                expect(sheet).to_have_count(0, timeout=5000)
                if main_run:
                    check(("POST", "/api/cli/pair/WDJB-MJHT/approve", {}) in CLI.sent, f"{lang}: Approve posts the code ({CLI.sent})")
                    check(any(d["name"] == "studio-mac" for d in CLI.devices), f"{lang}: the computer is paired")
                    expect(page.locator(".pair-notice")).to_have_count(0, timeout=5000)
                    check(True, f"{lang}: the notice is gone once the request is answered")

                # Deny, from the link the terminal prints; the address is cleaned.
                CLI.requests.append(CliStub.request("KXQP-LMNR", "build-box", "linux", "amd64"))
                page.goto(f"{BASE}/agents?token=t&scheme={scheme}&lang={lang}&pair-device=kxqp-lmnr")
                expect(sheet).to_be_visible(timeout=15000)
                if main_run:
                    check("pair-device" not in page.url, f"{lang}: the link's code is taken out of the address ({page.url})")
                    # The sheet opens at once and says Loading until the server answers about the code;
                    # reading its text the moment it appeared failed whenever the machine was loaded,
                    # which read as a pairing race. It waits for the request, then checks it is this one.
                    named = sheet.locator("[data-pair-name]")
                    try:
                        expect(named).to_have_text("build-box", timeout=10000)
                        own = sheet.locator("[data-pair-code]").inner_text() == "KXQP-LMNR"
                    except AssertionError:
                        own = False
                    check(own, f"{lang}: the link opens the sheet for its own request ({sheet.inner_text()!r})")
                sheet.get_by_role("button", name=words["deny"]).click()
                expect(sheet).to_have_count(0, timeout=5000)
                if main_run:
                    check(("POST", "/api/cli/pair/KXQP-LMNR/deny", None) in CLI.sent, f"{lang}: Deny posts the code")
                    check(not any(d["name"] == "build-box" for d in CLI.devices), f"{lang}: a denied computer is not paired")

                # A code nobody waits with.
                page.goto(f"{BASE}/agents?token=t&scheme={scheme}&lang={lang}&pair-device=AAAA-BBBB")
                expect(sheet).to_be_visible(timeout=15000)
                expect(sheet.locator("[data-pair-state=missing]")).to_be_visible(timeout=5000)
                if main_run:
                    check(words["missing"] in sheet.inner_text() and sheet.locator("[data-action=approve]").count() == 0, f"{lang}: an unknown code gets a sentence and no Approve")
                page.keyboard.press("Escape")

                # Settings → Devices: the list, rename, revoke asked in the row, and the code typed in.
                page.goto(f"{BASE}/settings/devices?token=t&scheme={scheme}&lang={lang}")
                row = page.locator("[data-device=dev-laptop]")
                expect(row).to_be_visible(timeout=15000)
                if main_run:
                    text = row.inner_text()
                    check("laptop-graceful-unicorn" in text and "Linux" in text and words["connected"] in text and words["chats"] in text,
                          f"{lang}: a row has the name, the system, the link and the chats ({text!r})")
                    check(page.locator("[data-device=dev-wdjbmjht]").count() == 1, f"{lang}: the computer just approved is listed")
                    check("daedalus-cli login " in page.locator("[data-setting='devices.pair']").inner_text(), f"{lang}: the page says how to sign a terminal in")
                shot(page, f"desktop-devices-{scheme}-{lang}")
                if main_run:
                    row.get_by_role("button", name=words["rename"]).click()
                    field = row.locator(".device-rename input")
                    field.fill("work-laptop")
                    row.get_by_role("button", name=words["save"]).click()
                    expect(row.locator(".mtitle")).to_contain_text("work-laptop", timeout=5000)
                    check(("PATCH", "/api/cli/devices/dev-laptop", {"name": "work-laptop"}) in CLI.sent, f"{lang}: Rename sends the new name")
                    row.locator("[data-action=revoke]").click()
                    confirm = row.locator(".device-confirm")
                    expect(confirm).to_be_visible(timeout=3000)
                    check(not any(m == "DELETE" for m, _, _ in CLI.sent), f"{lang}: the first Revoke only asks")
                    shot(page, f"desktop-devices-revoke-{scheme}-{lang}")
                    confirm.get_by_role("button", name=words["keep"]).click()
                    expect(confirm).to_have_count(0)
                    check(not any(m == "DELETE" for m, _, _ in CLI.sent), f"{lang}: Keep revokes nothing")
                    row.locator("[data-action=revoke]").click()
                    row.locator("[data-action=revoke-confirm]").click()
                    expect(row).to_have_count(0, timeout=5000)
                    check(("DELETE", "/api/cli/devices/dev-laptop", None) in CLI.sent, f"{lang}: the second press revokes the device")
                    CLI.requests.append(CliStub.request("ZPLM-QRST", "garage-pi", "linux", "arm64"))
                    page.locator("#pair-code").fill("zplm qrst")
                    page.get_by_role("button", name=words["go"]).click()
                    expect(sheet).to_be_visible(timeout=5000)
                    # The sheet reads the requests afresh first: the code may be newer than the list in hand.
                    expect(sheet.locator("[data-pair-name]")).to_have_text("garage-pi", timeout=5000)
                    check(True, f"{lang}: the typed code opens the sheet for its request")
                    page.keyboard.press("Escape")
                    # A request still waiting is listed on the page too, for whoever closed the notice.
                    page.reload()
                    waiting = page.locator("[data-pair-request='ZPLM-QRST']")
                    expect(waiting).to_be_visible(timeout=15000)
                    check("garage-pi" in waiting.inner_text(), f"{lang}: Settings lists the request still waiting")
                    shot(page, f"desktop-devices-waiting-{scheme}-{lang}")
                context.close()

                # A phone: the notice and the sheet, and the Devices page.
                fresh()
                CLI.requests.append(CliStub.request("WDJB-MJHT", "studio-mac", "darwin", "arm64"))
                context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme=scheme)
                page = context.new_page()
                page.route("**/api/**", shots.stub)
                page.goto(f"{BASE}/agents?token=t&scheme={scheme}&lang={lang}")
                notice = page.locator(".pair-notice")
                expect(notice).to_be_visible(timeout=15000)
                if main_run:
                    box = notice.bounding_box() or {"x": -1, "width": 9999}
                    check(box["x"] >= 0 and box["x"] + box["width"] <= 390, f"{lang}: the phone's notice fits the screen ({box})")
                shot(page, f"phone-pair-notice-{scheme}-{lang}")
                notice.get_by_role("button", name=words["review"]).click()
                sheet = page.locator(".pair-sheet")
                expect(sheet).to_be_visible(timeout=5000)
                if main_run:
                    check(sheet.get_by_role("button", name=words["approve"]).is_visible() and sheet.get_by_role("button", name=words["deny"]).is_visible(), f"{lang}: the phone's sheet shows Approve and Deny")
                    check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{lang}: the phone's sheet fits the width")
                page.wait_for_timeout(400)
                shot(page, f"phone-pair-sheet-{scheme}-{lang}")
                page.keyboard.press("Escape")
                page.goto(f"{BASE}/settings/devices?token=t&scheme={scheme}&lang={lang}")
                expect(page.locator("[data-device=dev-laptop]")).to_be_visible(timeout=15000)
                if main_run:
                    check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{lang}: the phone's Devices page fits the width")
                page.wait_for_timeout(300)
                shot(page, f"phone-devices-{scheme}-{lang}")
                context.close()
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
