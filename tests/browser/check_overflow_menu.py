"""Do the actions in an overflow menu work when the menu sits inside a pressable card?

Reported on a desktop browser: clicking any item in a service card's "…" menu did nothing —
the menu jumped aside for an instant and came back. The cause is a stacking one: a card with
`:active { transform: scale(…) }` becomes the containing block of a `position: fixed` child
while the mouse button is down, so the menu moves between mousedown and mouseup and the
release lands outside it. No click event is ever produced.

A synthetic click cannot see this: Playwright's `.click()` dispatches at one point and the bug
lives between press and release. The check therefore drives the real mouse — move, down, up —
the way a hand does, and asserts the panel each item opens is on screen afterwards.

Build the app, serve it with tests/browser/serve_app.py, then run this with APP_URL pointing at it:

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_overflow_menu.py

Exit 0 when every action opens its panel.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import DEFAULT_APP, Unhandled, expect_app, fulfil_shared  # noqa: E402
from screenshots import S1 as SESSION  # noqa: E402

UNHANDLED = Unhandled()

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def service(i: int, *, shared: bool) -> dict:
    return {
        "name": f"svc-{i}",
        "command": "python3 -m http.server $PORT --bind 0.0.0.0",
        "cwd": "/srv/workspaces/x",
        "port": 8100 + i,
        "url": f"http://service-host.invalid:{8100 + i}",
        "pid": 1000 + i,
        "status": "running",
        "restart": True,
        "note": None,
        "started_at": "2026-09-13T13:00:00+00:00",
        "stopped_at": None,
        "share": {"mode": "key", "slug": "abc", "key": "k", "url": "https://example.test/s/abc", "public_base": "https://example.test"}
        if shared
        else {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://example.test"},
        "session_id": f"sess{i}",
        "session_title": f"Session {i}",
    }


SERVICES = [service(0, shared=True), service(1, shared=False)]


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    if url.rstrip("/").endswith("/logs") or "/logs?" in url:
        body = json.dumps({"text": "line one\nline two\nline three"})
    elif "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif "/api/services" in url:
        body = json.dumps(SERVICES)
    elif "unread" in url:
        body = json.dumps({"unread": 0})
    else:
        # The gates the app draws any screen at all behind — a model, the capabilities — live in one
        # table so that adding one cannot leave a harness sitting on the onboarding screen until its
        # selector times out. Anything past them is reported at the end rather than answered blind.
        rel = url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):] if "/api/" in rel else ""
        if fulfil_shared(route):
            return
        if rel:
            UNHANDLED.record(rel)
        body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def press(page: Page, selector_text: str) -> None:
    """Click a menu item with the real mouse: press and release are separate, as on a desk."""
    item = page.locator(".menu[role='menu'] button", has_text=selector_text).first
    box = item.bounding_box()
    assert box, f"no menu item {selector_text!r}"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(60)
    page.mouse.up()
    page.wait_for_timeout(500)


def open_menu(page: Page) -> None:
    page.locator(".pg-item[data-service]").first.locator("button[aria-haspopup='menu']").click()
    page.wait_for_selector(".menu[role='menu']", timeout=5000)
    page.locator(".menu[role='menu']").evaluate("async menu => { await Promise.all(menu.getAnimations().map(animation => animation.finished)); }")


def fresh(page: Page) -> None:
    """A clean screen for the next action: no leftover sheet to click through."""
    page.goto(f"{BASE}/services?token=t")
    page.wait_for_selector(".pg-item[data-service]", timeout=15000)


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/services?token=t")
        page.wait_for_selector(".pg-item[data-service]", timeout=15000)

        # The menu must not move while the button is held down: that movement is the bug.
        open_menu(page)
        before = page.locator(".menu[role='menu']").bounding_box()
        item = page.locator(".menu[role='menu'] button", has_text="Log").first.bounding_box()
        assert before and item
        page.mouse.move(item["x"] + item["width"] / 2, item["y"] + item["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(80)
        during = page.locator(".menu[role='menu']").bounding_box()
        page.mouse.up()
        page.wait_for_timeout(300)
        assert during
        drift = max(abs(during["x"] - before["x"]), abs(during["y"] - before["y"]))
        print(f"menu drift while pressed: {drift:.1f}px")
        if drift > 1:
            problems.append(f"the menu moves {drift:.0f}px while the button is held down")

        # Each action opens its own panel, from a freshly loaded screen.
        for label, selector, what in [
            ("Log", ".sheet-backdrop .sheet", "the log sheet"),
            ("Stop and remove", ".sheet-backdrop.confirm .dialog", "the confirmation"),
            ("Access", ".sheet-backdrop .sheet .access-options", "the sharing sheet"),
        ]:
            fresh(page)
            open_menu(page)
            press(page, label)
            opened = page.locator(selector).count() > 0
            print(f"{label} → {what} open: {opened}")
            if not opened:
                problems.append(f"{label} did not open {what}")

        if os.environ.get("SHOTS"):
            page.screenshot(path=str(Path(__file__).parent / "overflow-menu.png"))
        for width in (1440, 2560):
            wide = browser.new_page(viewport={"width": width, "height": 900})
            wide.route("**/api/**", shots.stub)
            wide.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en", wait_until="networkidle")
            wide.wait_for_selector(".chat-title", timeout=15000)
            problems += check_title_menu_stays_on_screen(wide)
            wide.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


def check_title_menu_stays_on_screen(page) -> list[str]:  # type: ignore[no-untyped-def]
    """The session's title sits against the sidebar. A menu hung from its right edge ran under the
    sidebar and lost its first characters, so the side it hangs from is chosen, not assumed."""
    problems: list[str] = []
    # The header also has a subagent roster now; this check belongs to the session actions menu.
    page.get_by_role("button", name="Session actions", exact=True).click()
    page.wait_for_timeout(400)
    menu = page.locator(".menu, [role=menu]").first
    box = menu.bounding_box() if menu.count() else None
    width = page.viewport_size["width"]
    if not box:
        problems.append("the title menu did not open")
    elif box["x"] < 8 or box["x"] + box["width"] > width - 8:
        problems.append(f"the title menu runs off the screen: {box['x']:.0f}..{box['x'] + box['width']:.0f} of {width}")
    page.keyboard.press("Escape")
    return problems


if __name__ == "__main__":
    expect_app(BASE)
    # A gate the app grew and this stub does not know about fails the run by name, rather than by a
    # selector that never appears somewhere further down.
    failed = run()
    sys.exit(failed or UNHANDLED.report())
