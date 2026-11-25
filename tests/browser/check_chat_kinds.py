"""Chat kinds in the lists and in New chat, in a real browser.

The desktop sidebar's Chats label opens a menu whose Show part picks the kind — every kind, Chats,
Computer, Browser, Remote — and a chosen kind keeps only that kind in the lists (a project is listed while
it holds one), says itself on the label, and is remembered across a reload. Under every kind a computer
or Chrome chat carries its mark at the row's end, with a tooltip. New chat's mode chip offers
Chat, Agent, Computer and Browser, and Remote, which is only ever started from daedalus-cli and so always
stands greyed with where it starts: on a server (the shared stub's default) Computer and Browser stand
greyed with the reason too; in the native application with the operator's Chrome configured, the Computer tab
makes Computer the default, sending creates a session of kind "computer" and lands on it with the
Computer sheet open, and Browser creates one of kind "chrome". On a phone the Chats page has the same
tabs, and the drawer's recent chats carry the marks. In English and Russian.

The kind is what a chat was started as; what it controls now comes beside it. The stub's "Bakery site"
is an ordinary chat driving the operator's Chrome right now: it is listed under Browser as well as
under Chats, its mark is the Chrome one drawn hollow and live with the "controlling now" words, and
an ordinary chat that controls nothing stays out of Browser.

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
    "en": {"show": ["Every kind", "Chats", "Computer", "Browser", "Remote"], "tabs": ["All", "Chats", "Computer", "Browser", "Remote"], "computer": "Computer", "browser": "Browser", "consent": "Use my Chrome", "live": "Controlling your Chrome now"},
    "ru": {"show": ["Все виды", "Обычные", "Компьютер", "Браузер", "Удалённо"], "tabs": ["Все", "Обычные", "Компьютер", "Браузер", "Удалённо"], "computer": "Компьютер", "browser": "Браузер", "consent": "Использовать мой Chrome", "live": "Сейчас управляет вашим Chrome"},
}
CHROME = {"configured": True, "enabled": True, "daemon": True, "reason": "", "detail": "", "relay": {"connected": True, "chromes": [{"chrome": "c1", "connected": True, "profile_name": "Work"}]}, "native": True,
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


def show(page, lang: str, index: int) -> None:  # type: ignore[no-untyped-def]
    """Pick the kind the sidebar lists from the Show part of the Chats label's menu."""
    page.locator("nav.sidebar .sb-chats-menu").click()
    item = page.get_by_role("menuitemcheckbox").filter(has_text=WORDS[lang]["show"][index]).first
    item.click()
    page.keyboard.press("Escape")
    expect(page.get_by_role("menuitemcheckbox")).to_have_count(0)


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
            # The desktop column on a server: the Show menu, the filter, the marks, the remembered kind.
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            # The bakery's folder open: a collapsed project says "working" for the chat driving Chrome
            # instead of listing it, and its mark is what is looked at here.
            context.add_init_script(f"localStorage.setItem('daedalus.folder.{shots.P1}', '1')")
            page = context.new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)
            side.locator(".sb-chats-menu").click()
            kinds = page.get_by_role("menuitemcheckbox")
            labels = kinds.evaluate_all("els => els.map((b) => b.querySelector('.menu-row-t, .menu-label, span')?.textContent ?? b.textContent)")
            check(labels[:5] == words["show"], f"{lang}: the Show part lists {labels[:5]}")
            check(kinds.nth(0).get_attribute("aria-checked") == "true", f"{lang}: every kind is shown at first")
            page.keyboard.press("Escape")
            chrome_mark = side.locator(f"[data-session='{shots.S4}'] .sb-kind")
            check(chrome_mark.get_attribute("data-kind") == "chrome" and bool(chrome_mark.get_attribute("title")), f"{lang}: under every kind the Chrome chat carries its mark and a tooltip")
            check(side.locator(f"[data-session='{shots.S6}'] .sb-kind[data-kind]").count() == 0, f"{lang}: an ordinary chat carries no mark")
            borrowed = side.locator(f"[data-session='{shots.S1}'] .sb-kind").get_attribute("class") or ""
            check(all(c in borrowed.split() for c in ("kind-chrome", "kind-live", "kind-borrowed")), f"{lang}: the ordinary chat driving Chrome now borrows its mark, hollow and live ({borrowed})")
            ring = side.locator(f"[data-session='{shots.S1}'] .sb-kind").evaluate("el => getComputedStyle(el).boxShadow")
            check("inset" in ring, f"{lang}: the borrowed mark is drawn as a ring, not a fill ({ring})")
            live_plate = page.locator(f".start [data-session='{shots.S1}'] .start-live-plate")
            check(live_plate.count() == 1 and live_plate.get_attribute("title") == words["live"], f"{lang}: the start screen's live row says it controls Chrome now ({live_plate.get_attribute('title') if live_plate.count() else 'no row'})")
            shot(page, f"desktop-all-{lang}")

            show(page, lang, 2)
            page.wait_for_timeout(350)
            sessions = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(sessions == [shots.S2], f"{lang}: Computer keeps the one computer chat, inside its project ({sessions})")
            said = side.locator(".sb-chats-menu .sb-sec-filter").inner_text()
            check(said == words["computer"], f"{lang}: the Chats label says the kind it shows ({said!r})")
            shot(page, f"desktop-computer-{lang}")
            page.reload()
            expect(side.locator(".sb-chats-menu .sb-sec-filter")).to_have_text(words["computer"], timeout=15000)
            check(True, f"{lang}: the kind is remembered across a reload")

            show(page, lang, 3)
            page.wait_for_timeout(300)
            sessions = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(shots.S4 in sessions and shots.S1 in sessions, f"{lang}: Browser keeps the Chrome chat and the ordinary chat controlling Chrome now ({sessions})")
            check(shots.S6 not in sessions and shots.S2 not in sessions, f"{lang}: Browser leaves out chats that do not control Chrome ({sessions})")
            # On a server neither kind can start: the chip opens on Chat, the two others greyed with why.
            page.locator(".start .kind-select").click()
            menu = page.locator(".kind-menu")
            expect(menu).to_be_visible()
            disabled = menu.locator(".kind-row").evaluate_all("els => els.map((b) => [b.dataset.kind, b.disabled, !!b.title])")
            check(disabled == [["chat", False, False], ["agent", False, False], ["computer", True, True], ["chrome", True, True], ["remote", True, True]], f"{lang}: on a server Computer, Browser and Remote are greyed with the reason ({disabled})")
            check(page.locator(".start .kind-select").get_attribute("data-kind") == "chat", f"{lang}: the Browser tab cannot make Browser the default where it is not offered")
            page.wait_for_timeout(400)
            shot(page, f"desktop-mode-server-{lang}")
            page.keyboard.press("Escape")
            show(page, lang, 1)
            page.wait_for_timeout(250)
            sessions = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(shots.S2 not in sessions and shots.S4 not in sessions and shots.S6 in sessions, f"{lang}: Chats leaves the computer and Chrome chats out ({sessions})")
            check(shots.S1 in sessions, f"{lang}: the ordinary chat controlling Chrome is still under Chats ({sessions})")
            show(page, lang, 0)
            context.close()

            # The native application with Chrome: the kind shown is the default mode, and New chat creates it.
            desk = ComputerStub()
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", native_stub(desk))
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-chats-menu")).to_be_visible(timeout=15000)
            show(page, lang, 2)
            side.locator(".sb-pinned-nav [data-nav='new']").click()
            chip = page.locator(".start .kind-select")
            expect(chip).to_have_attribute("data-kind", "computer", timeout=5000)
            check(True, f"{lang}: with Computer shown, New chat starts in Computer mode")
            chip.click()
            rows = page.locator(".kind-menu .kind-row").evaluate_all("els => els.map((b) => b.disabled)")
            check(rows == [False, False, False, False, True], f"{lang}: natively, with Chrome configured, every mode but Remote is offered ({rows})")
            page.wait_for_timeout(400)
            shot(page, f"desktop-mode-native-{lang}")
            page.keyboard.press("Escape")
            del api_stub.CREATED_SESSIONS[:]
            page.locator(".start .composer-field").fill("Rename the photos in the Pictures folder")
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
            show(page, lang, 3)
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
            phone_live = page.locator(f"[data-session='{shots.S1}'] .ph-sicon.kind-borrowed").first
            check(phone_live.count() == 1 and phone_live.get_attribute("title") == words["live"], f"{lang}: the phone's row of the chat controlling Chrome says so")
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
