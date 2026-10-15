"""Autopilot and the style profile in a real browser.

A turn autopilot sent is labelled on the message, and the label names what it was. The mode menu
offers the session's autopilot switch beside YAGNI with the turns it took since the operator's last
message, and switching it off goes to the host at once. Settings → Chat shows autopilot's settings
with the day's spend and verdicts, and the learned style profile with a Forget button that asks first.

    APP_URL=http://127.0.0.1:8172/app python3 tests/browser/check_autopilot.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message
from check_composer import stub as composer_stub
from playwright.sync_api import expect, sync_playwright
from screenshots import stub as app_stub

PILOT = {"on": True, "enabled": True, "eligible": True, "used": 1, "allowed": 3}
SWITCHED: list[dict] = []


def stub(route) -> None:  # type: ignore[no-untyped-def]
    path = route.request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == f"/api/sessions/{SESSION}/autopilot" and route.request.method == "POST":
        data = json.loads(route.request.post_data or "{}")
        SWITCHED.append(data)
        PILOT["on"] = bool(data.get("on"))
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(PILOT))
    if rel == f"/api/sessions/{SESSION}" and route.request.method == "GET":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({**HOST.detail(), "autopilot": PILOT}))
    return composer_stub(route)


def main() -> None:
    HOST.status = "idle"
    HOST.messages = [
        message(101, "user", "Fix the flaky upload test."),
        message(102, "assistant", "Fixed: the fixture now waits for the port."),
        message(103, "user", "Now run the whole suite and fix what fails.", origin="autopilot", autopilot={"kind": "continue"}),
        message(104, "assistant", "All 412 tests pass."),
        message(105, "user", "Write the changelog entry.", origin="autopilot", autopilot={"kind": "choice", "label": "Changelog"}),
        message(106, "assistant", "Added under Unreleased."),
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en&scheme=dark")

        # The labels on autopilot's turns; the operator's own message has none.
        labels = page.locator(".msg.user .msg-autopilot")
        expect(labels).to_have_count(2)
        expect(labels.nth(0)).to_have_text("Autopilot")
        expect(labels.nth(0)).to_have_attribute("data-autopilot", "continue")
        expect(labels.nth(1)).to_have_text("Autopilot · your choice")
        expect(labels.nth(1)).to_have_attribute("title", "You chose: Changelog")

        # The switch in the mode menu, with the turns taken, and off at once.
        page.locator(".composer .composer-mode").click()
        row = page.locator(".autopilot-row")
        expect(row).to_have_attribute("aria-checked", "true")
        expect(row).to_contain_text("1 of 3 turns used since your last message")
        row.click()
        expect(row).to_have_attribute("aria-checked", "false")
        assert SWITCHED == [{"on": False}], SWITCHED
        page.keyboard.press("Escape")

        # Settings → Chat: autopilot's settings and today's numbers, and the profile with Forget.
        settings = browser.new_page(viewport={"width": 1440, "height": 900})
        settings.route("**/api/**", app_stub)
        settings.goto(f"{BASE}/settings/chat?token=t&lang=en&scheme=dark")
        card = settings.locator("[data-section='autopilot']")
        expect(card.locator("[data-setting='autopilot.enabled']")).to_be_visible()
        expect(card.locator("[data-setting='autopilot.max_continuations'] input")).to_have_value("3")
        expect(card.locator("[data-autopilot-today]")).to_contain_text("continued 4, done 2, asked 1, held back 3")
        style = settings.locator("[data-section='user-style']")
        expect(style.locator("[data-style-profile]")).to_contain_text("Writes short, direct requests")
        style.get_by_role("button", name="Forget").click()
        settings.get_by_role("button", name="Confirm").click()
        expect(style.locator("[data-style-empty]")).to_be_visible()

        # The same page in Russian names them in Russian.
        settings.goto(f"{BASE}/settings/chat?token=t&lang=ru&scheme=dark")
        expect(settings.locator("[data-section='autopilot'] .section-title")).to_have_text("Автопилот")
        expect(settings.locator("[data-section='user-style'] .section-title")).to_have_text("Как вы пишете")
        browser.close()
    assert not UNHANDLED.report()
    print("autopilot: labels, the switch and the settings hold")


if __name__ == "__main__":
    main()
