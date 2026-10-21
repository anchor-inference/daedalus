"""Chat kinds in the lists and in New chat, in a real browser.

The desktop column has tabs over its lists — All, Chats, Computer, Browser — and a chosen tab keeps
only that kind (a project is listed while it holds one) and is remembered across a reload. Under All a
computer or Chrome chat carries its mark on the plate, with a tooltip. New chat's mode chip offers
Chat, Computer and Browser: on a server (the shared stub's default) Computer and Browser stand greyed
with the reason; in the native application with the operator's Chrome configured, the Computer tab
makes Computer the default, sending creates a session of kind "computer" and lands on it with the
Computer sheet open, and Browser creates one of kind "chrome". On a phone the Chats page has the same
tabs, and the drawer's recent chats carry the marks. In English and Russian.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_chat_kinds.py

``SHOTS=<dir>`` also writes the pictures of the column, the mode menu and the phone's Chats page
there. Exit status is the number of failed checks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_stub  # noqa: E402
import screenshots as shots  # noqa: E402
from api_stub import ComputerStub, expect_app  # noqa: E402

BASE = shots.BASE
SHOTS = os.environ.get("SHOTS", "")
WORDS = {
    "en": {"tabs": ["All", "Chats", "Computer", "Browser"], "computer": "Computer", "browser": "Browser", "consent": "Use my Chrome"},
    "ru": {"tabs": ["Все", "Обычные", "Компьютер", "Браузер"], "computer": "Компьютер", "browser": "Браузер", "consent": "Использовать мой Chrome"},
}
CHROME = {"configured": True, "enabled": True, "daemon": True, "reason": "", "detail": "", "relay": {"connected": True, "profile_name": "Work"}, "native": True,
          "launcher": None, "launcher_error": "", "devices": [], "server_url": "", "consent": ""}


def native_stub(desk: ComputerStub):  # type: ignore[no-untyped-def]
    """The shared pictures' host, as the native application with the operator's Chrome configured."""
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        rel = request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):]
        if rel == "/api/browser/user" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(CHROME))
        if rel.startswith("/api/computer"):
            body = json.loads(request.post_data or "{}") if request.method == "POST" else None
            answered = desk.answer(request.method, rel, body)
            if answered is not None:
                return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        return shots.stub(route)
    return handle


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
            # The desktop column on a server: the tabs, the filter, the marks, the remembered tab.
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)
            tabs = side.locator(".kind-tabs [role=tab]")
            labels = tabs.evaluate_all("els => els.map((b) => b.querySelector('span').textContent)")
            check(labels == words["tabs"], f"{lang}: the column's tabs are {labels}")
            check(tabs.nth(0).get_attribute("aria-selected") == "true", f"{lang}: All is chosen at first")
            clipped = side.evaluate("el => [...el.querySelectorAll('.kind-tab')].filter((b) => b.scrollWidth > b.clientWidth + 1).map((b) => b.textContent)")
            check(clipped == [], f"{lang}: no tab's label is cut at the column's width ({clipped})")
            chrome_plate = side.locator(f"[data-session='{shots.S4}'] .sb-plate")
            check(chrome_plate.get_attribute("data-kind") == "chrome" and bool(chrome_plate.get_attribute("title")), f"{lang}: under All the Chrome chat's plate carries its mark and a tooltip")
            check(side.locator(f"[data-session='{shots.S6}'] .sb-plate[data-kind]").count() == 0, f"{lang}: an ordinary chat carries no mark")
            shot(page, f"desktop-all-{lang}")

            tabs.nth(2).click()
            page.wait_for_timeout(350)
            sessions = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(sessions == [shots.S2], f"{lang}: Computer keeps the one computer chat, inside its project ({sessions})")
            pill = side.evaluate("el => { const p = el.querySelector('.kind-tabs-pill').getBoundingClientRect(); const b = el.querySelector('[data-kind-tab=computer]').getBoundingClientRect(); return Math.abs(p.left - b.left) < 2 && Math.abs(p.width - b.width) < 2; }")
            check(pill, f"{lang}: the pill stands under the chosen tab")
            shot(page, f"desktop-computer-{lang}")
            page.reload()
            expect(side.locator(".kind-tabs").first).to_be_visible(timeout=15000)
            check(side.locator("[data-kind-tab=computer]").get_attribute("aria-selected") == "true", f"{lang}: the tab is remembered across a reload")

            tabs.nth(3).click()
            page.wait_for_timeout(300)
            sessions = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(sessions == [shots.S4], f"{lang}: Browser keeps the Chrome chat ({sessions})")
            # On a server neither kind can start: the chip opens on Chat, the two others greyed with why.
            page.locator(".start .kind-select").click()
            menu = page.locator(".kind-menu")
            expect(menu).to_be_visible()
            disabled = menu.locator(".kind-row").evaluate_all("els => els.map((b) => [b.dataset.kind, b.disabled, !!b.title])")
            check(disabled == [["chat", False, False], ["computer", True, True], ["chrome", True, True]], f"{lang}: on a server Computer and Browser are greyed with the reason ({disabled})")
            check(page.locator(".start .kind-select").get_attribute("data-kind") == "chat", f"{lang}: the Browser tab cannot make Browser the default where it is not offered")
            page.wait_for_timeout(400)
            shot(page, f"desktop-mode-server-{lang}")
            page.keyboard.press("Escape")
            tabs.nth(1).click()
            page.wait_for_timeout(250)
            sessions = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(shots.S2 not in sessions and shots.S4 not in sessions and shots.S6 in sessions, f"{lang}: Chats leaves the computer and Chrome chats out ({sessions})")
            tabs.nth(0).click()
            context.close()

            # The native application with Chrome: the tab is the default mode, and New chat creates the kind.
            desk = ComputerStub()
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", native_stub(desk))
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".kind-tabs").first).to_be_visible(timeout=15000)
            side.locator("[data-kind-tab=computer]").click()
            side.locator(".sb-new").click()
            chip = page.locator(".start .kind-select")
            expect(chip).to_have_attribute("data-kind", "computer", timeout=5000)
            check(True, f"{lang}: with the Computer tab chosen, New chat starts in Computer mode")
            chip.click()
            rows = page.locator(".kind-menu .kind-row").evaluate_all("els => els.map((b) => b.disabled)")
            check(rows == [False, False, False], f"{lang}: natively, with Chrome configured, every mode is offered ({rows})")
            page.wait_for_timeout(400)
            shot(page, f"desktop-mode-native-{lang}")
            page.keyboard.press("Escape")
            del api_stub.CREATED_SESSIONS[:]
            page.locator(".start textarea").fill("Rename the photos in the Pictures folder")
            shot(page, f"desktop-newchat-computer-{lang}")
            page.locator(".start [data-action=send]").click()
            page.wait_for_url(f"**/agents/{shots.S6}*", timeout=10000)
            sent = [c.get("kind") for c in api_stub.CREATED_SESSIONS]
            check(sent == ["computer"], f"{lang}: Computer creates a session of kind computer ({sent})")
            try:
                page.wait_for_selector("[role=dialog]", timeout=5000)
                opened = True
            except Exception:  # noqa: BLE001
                opened = False
            check(opened, f"{lang}: the chat opens with the Computer sheet open")
            page.wait_for_timeout(400)
            shot(page, f"desktop-computer-sheet-{lang}")

            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side.locator("[data-kind-tab=chrome]").click()
            expect(page.locator(".start .kind-select")).to_have_attribute("data-kind", "chrome", timeout=5000)
            del api_stub.CREATED_SESSIONS[:]
            page.locator(".start [data-action=send]").click()
            page.get_by_role("button", name=words["consent"]).click()
            page.wait_for_url(f"**/agents/{shots.S6}*", timeout=10000)
            sent = [c.get("kind") for c in api_stub.CREATED_SESSIONS]
            check(sent == ["chrome"], f"{lang}: Browser asks for the consent and creates a session of kind chrome ({sent})")
            page.evaluate("localStorage.removeItem('daedalus.chats.kind')")
            context.close()

            # A phone: the Chats page's tabs and the drawer's marks.
            context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", native_stub(ComputerStub()))
            page.goto(f"{BASE}/agents?view=chats&token=t&scheme=dark&lang={lang}")
            expect(page.locator(".ph-kinds").first).to_be_visible(timeout=15000)
            labels = page.locator(".ph-kinds [role=tab]").evaluate_all("els => els.map((b) => b.querySelector('span').textContent)")
            check(labels == words["tabs"], f"{lang}: the phone's Chats page has the tabs ({labels})")
            wide = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            check(wide, f"{lang}: the tabs fit the phone's width")
            check(page.locator(f"[data-session='{shots.S4}'] .ph-sicon[data-kind=chrome]").count() == 1, f"{lang}: the phone's row of the Chrome chat carries its mark")
            shot(page, f"phone-chats-all-{lang}")
            page.locator(".ph-kinds [data-kind-tab=computer]").click()
            page.wait_for_timeout(300)
            sessions = page.locator("[data-session]").evaluate_all("els => [...new Set(els.map((e) => e.dataset.session))]")
            check(sessions == [shots.S2], f"{lang}: the phone's Computer tab keeps the computer chat ({sessions})")
            shot(page, f"phone-chats-computer-{lang}")
            page.locator(".ph-kinds [data-kind-tab=all]").click()
            shots.open_drawer(page)
            marks = page.locator(".ph-drawer-root.open .ph-drow .kind-mark").evaluate_all("els => els.map((e) => e.className)")
            check("kind-mark kind-chrome" in marks and "kind-mark kind-computer" in marks, f"{lang}: the drawer's recent chats carry the marks ({marks})")
            page.locator(".ph-drawer-root.open .ph-drow .kind-mark").last.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            shot(page, f"phone-drawer-{lang}")
            page.keyboard.press("Escape")
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            expect(page.locator(".ph-hero .kind-select")).to_be_visible(timeout=15000)
            page.locator(".ph-hero .kind-select").click()
            page.wait_for_timeout(500)
            shot(page, f"phone-mode-{lang}")
            context.close()
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
