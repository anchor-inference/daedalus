"""Remote chats in a real browser: the Remote tab, the marks, and a remote chat's header.

A remote chat is started by daedalus-cli in a folder on one of the operator's computers; its tools run
there and everything else stays on the server. The column's tabs gain Remote beside All, Chats,
Computer and Browser, with the same rules: the kind is the chat's origin, and a terminal attached to
any chat right now is a live mark that lists it under Remote while it lasts. Rows name the computer.

Opened, a remote chat's header names the computer, its folder and the link: connected, reconnecting,
or — when the computer dropped while a tool waited on it — the turn paused, said by a banner under the
header with Cancel turn, which asks first and then stops the run. On a phone the Chats page has the
same tabs and the chat's top bar says the same. In English and Russian.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_remote_kind.py

``SHOTS=<dir>`` also writes the pictures, in a dark and a light theme, there. Exit status is the
number of failed checks.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app, folders  # noqa: E402

BASE = shots.BASE
SHOTS = os.environ.get("SHOTS", "")
WORDS = {
    "en": {"tabs": ["All", "Chats", "Computer", "Browser", "Remote"], "live": "Connected to your terminal now", "connected": "Connected",
           "reconnecting": "Reconnecting…", "paused": "Turn paused · computer offline", "banner": "The turn is paused until studio-mac reconnects", "cancel": "Cancel turn", "composer": "Write a message; it goes to the agent when the turn goes on…",
           "guard": "asks before acting", "offline": "Offline"},
    "ru": {"tabs": ["Все", "Обычные", "Компьютер", "Браузер", "Удалённо"], "live": "Сейчас подключён к вашему терминалу", "connected": "Подключено",
           "reconnecting": "Переподключается…", "paused": "Ход приостановлен · компьютер не в сети", "banner": "Ход приостановлен, пока studio-mac не переподключится", "cancel": "Отменить ход", "composer": "Напишите сообщение; агент получит его, когда ход продолжится…",
           "guard": "спрашивает перед действием", "offline": "Не в сети"},
}

SR = "9c8b7a6f5e4d"
PR = "8d7c6b5a4f3e"
DEVICE = "studio-mac"
CWD = "/Users/you/work/bakery/site"

# A remote chat of its own scratch project, as daedalus-cli starts one; and the expense tracker, an
# ordinary chat, with a terminal attached to it right now.
shots.PROJECTS.append({"id": PR, "name": "site", "folders": folders(CWD), "created_at": shots.ago(hours=3), "settings": {"snapshots": False, "ephemeral": True},
                       "sessions": [{"id": SR, "title": "Fix the menu page", "running": False}]})
shots.FOLDER_COUNTS[PR] = {"total": 1, "active": 0, "loops": 0, "last_message_at": shots.ago(minutes=2)}
REMOTE: dict[str, object] = {"device_id": "dev-studio", "device_name": DEVICE, "cwd": CWD, "connected": True, "guard": "ask", "waiting": None}
ROW = shots.session(SR, "Fix the menu page", "Claude Opus 5", last=shots.ago(minutes=2), project=PR, kind="remote", controlling=("remote",))
ROW["remote"] = {**REMOTE, "waiting": False}
shots.SESSIONS.insert(0, ROW)
for s in shots.SESSIONS:
    if s["id"] == shots.S6:
        s["controlling"] = ["remote"]

STOPS: list[str] = []


def connection_frame(connected: bool) -> str:
    """The host's stream saying the studio's link went down or came up just now."""
    hello = {"head": 900, "oldest": 1, "server_time": datetime.now(UTC).isoformat(), "client": ""}
    event = {"seq": 901, "at": datetime.now(UTC).isoformat(), "type": "device.connection", "project_id": None, "session_id": None, "staff_id": None,
             "terminal_id": None, "payload": {"device_id": "dev-studio", "connected": connected}}
    return f"event: hello\ndata: {json.dumps(hello)}\n\nid: 901\nevent: device.connection\ndata: {json.dumps(event)}\n\n"


def set_state(state: str) -> None:
    """The computer's link as the host would report it next. Reconnecting is the app's own reading of
    a link this window saw drop a moment ago; a paused turn's run ended parked on the device, and the
    session reads ``waiting``, as the host says it, with no question pending."""
    REMOTE["connected"] = state == "connected"
    REMOTE["waiting"] = {"since": shots.ago(minutes=1), "calls": [{"tool_call_id": "call-7", "tool": "Exec"}]} if state == "paused" else None
    ROW["remote"] = {**REMOTE, "waiting": state == "paused"}
    ROW["controlling"] = ["remote"] if state == "connected" else []
    ROW["status"] = "waiting" if state == "paused" else "idle"
    shots.stub.events = connection_frame(False) if state == "reconnecting" else ""  # type: ignore[attr-defined]


def stub(route) -> None:  # type: ignore[no-untyped-def]
    request = route.request
    rel = request.url.split("?", 1)[0]
    rel = rel[rel.index("/api/"):]
    if rel == f"/api/sessions/{SR}" and request.method == "GET":
        detail = shots.detail(SR)
        detail.update({"kind": "remote", "controlling": ROW["controlling"], "remote": REMOTE,
                       "status": "waiting" if REMOTE["waiting"] else "idle", "run_id": None})
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
    if rel == f"/api/sessions/{SR}/stop" and request.method == "POST":
        STOPS.append(SR)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"stopped": True}))
    return shots.stub(route)


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
                set_state("connected")
                context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
                page = context.new_page()
                page.route("**/api/**", stub)
                page.goto(f"{BASE}/agents?token=t&scheme={scheme}&lang={lang}")
                side = page.locator("nav.sidebar")
                expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)
                tabs = side.locator(".kind-tabs [role=tab]")
                labels = tabs.evaluate_all("els => els.map((b) => b.querySelector('span').textContent)")
                if scheme == "dark":
                    check(labels == words["tabs"], f"{lang}: the column's tabs are {labels}")
                    clipped = side.evaluate("el => [...el.querySelectorAll('.kind-tab')].filter((b) => b.scrollWidth > b.clientWidth + 1).map((b) => b.textContent)")
                    check(clipped == [], f"{lang}: no tab's label is cut at the column's width ({clipped})")
                    plate = side.locator(f"[data-session='{SR}'] > .sb-plate")
                    classes = (plate.get_attribute("class") or "").split()
                    check(plate.get_attribute("data-kind") == "remote" and "kind-live" in classes and "kind-borrowed" not in classes,
                          f"{lang}: the remote chat's plate carries the Remote mark, live while its terminal is connected ({classes})")
                    check(plate.get_attribute("title") == words["live"], f"{lang}: its tooltip says a terminal is connected now ({plate.get_attribute('title')})")
                    meta = side.locator(f"[data-session='{SR}'] .sb-l2").inner_text()
                    check(DEVICE in meta, f"{lang}: the row names its computer ({meta!r})")
                    borrowed = (side.locator(f"[data-session='{shots.S6}'] > .sb-plate").get_attribute("class") or "").split()
                    check(all(c in borrowed for c in ("kind-remote", "kind-live", "kind-borrowed")), f"{lang}: an ordinary chat with a terminal attached borrows the mark, hollow ({borrowed})")
                side.locator("[data-kind-tab=remote]").click()
                page.wait_for_timeout(350)
                listed = side.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
                if scheme == "dark":
                    check(sorted(listed) == sorted([SR, shots.S6]), f"{lang}: Remote keeps the remote chat and the chat a terminal is attached to ({listed})")
                    pill = side.evaluate("el => { const p = el.querySelector('.kind-tabs-pill').getBoundingClientRect(); const b = el.querySelector('[data-kind-tab=remote]').getBoundingClientRect(); return Math.abs(p.left - b.left) < 2 && Math.abs(p.width - b.width) < 2; }")
                    check(pill, f"{lang}: the underline stands under Remote")
                shot(page, f"desktop-remote-tab-{scheme}-{lang}")

                side.locator(f"[data-session='{SR}']").click()
                head = page.locator(".chat-head .remote-head")
                expect(head).to_be_visible(timeout=10000)
                if scheme == "dark":
                    text = head.inner_text()
                    check(head.get_attribute("data-remote-state") == "connected" and DEVICE in text and "…/bakery/site" in text and words["connected"] in text,
                          f"{lang}: the header names the computer, the folder and the link ({text!r})")
                    check(words["guard"] in text, f"{lang}: the header says the terminal asks before acting")
                    check(page.locator(".remote-paused").count() == 0, f"{lang}: no banner while the computer is connected")
                shot(page, f"desktop-header-connected-{scheme}-{lang}")

                set_state("reconnecting")
                page.reload()
                expect(head).to_have_attribute("data-remote-state", "reconnecting", timeout=10000)
                if scheme == "dark":
                    check(words["reconnecting"] in head.inner_text(), f"{lang}: the header says it reconnects after a drop it saw")
                set_state("offline")
                page.reload()
                expect(head).to_have_attribute("data-remote-state", "offline", timeout=10000)
                if scheme == "dark":
                    check(words["offline"] in head.inner_text() and page.locator(".remote-paused").count() == 0, f"{lang}: a computer away with no turn waiting is offline, with no banner")

                set_state("paused")
                page.reload()
                banner = page.locator(".remote-paused")
                expect(banner).to_be_visible(timeout=10000)
                if scheme == "dark":
                    check(head.get_attribute("data-remote-state") == "paused" and words["paused"] in head.inner_text(), f"{lang}: the header says the turn is paused ({head.inner_text()!r})")
                    check(words["banner"] in banner.inner_text(), f"{lang}: the banner says which computer the turn waits for ({banner.inner_text()!r})")
                    check(page.locator(".chat-head .livebar, .chat-head .live-bar").count() == 0, f"{lang}: a paused turn draws no working bar")
                    check(page.locator(".composer .composer-field").first.is_enabled(), f"{lang}: the composer stays open while the turn is paused")
                    placeholder = page.locator(".composer .composer-field").first.get_attribute("placeholder") or ""
                    check(placeholder == words["composer"], f"{lang}: the composer says a message waits for the turn, not to reply ({placeholder!r})")
                shot(page, f"desktop-header-paused-{scheme}-{lang}")
                if scheme == "dark":
                    del STOPS[:]
                    banner.get_by_role("button", name=words["cancel"]).click()
                    dialog = page.locator("[role=alertdialog]")
                    expect(dialog).to_be_visible(timeout=5000)
                    check(STOPS == [], f"{lang}: Cancel turn asks before it stops anything")
                    shot(page, f"desktop-cancel-confirm-{scheme}-{lang}")
                    dialog.get_by_role("button", name=words["cancel"]).click()
                    page.wait_for_timeout(400)
                    check(STOPS == [SR], f"{lang}: confirmed, it stops the run ({STOPS})")
                page.evaluate("localStorage.removeItem('daedalus.chats.kind')")
                context.close()

                # A phone: the Chats page's tabs, then the paused chat's top bar and banner.
                set_state("connected")
                context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme=scheme)
                page = context.new_page()
                page.route("**/api/**", stub)
                page.goto(f"{BASE}/agents?view=chats&token=t&scheme={scheme}&lang={lang}")
                expect(page.locator(".ph-kinds").first).to_be_visible(timeout=15000)
                if scheme == "dark":
                    labels = page.locator(".ph-kinds [role=tab]").evaluate_all("els => els.map((b) => b.querySelector('span').textContent)")
                    check(labels == words["tabs"], f"{lang}: the phone's Chats page has the tabs ({labels})")
                    check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{lang}: the five tabs fit the phone's width")
                    clipped = page.evaluate("[...document.querySelectorAll('.ph-kinds .kind-tab')].filter((b) => b.scrollWidth > b.clientWidth + 1).map((b) => b.textContent)")
                    check(clipped == [], f"{lang}: no phone tab's label is cut ({clipped})")
                page.locator(".ph-kinds [data-kind-tab=remote]").click()
                page.wait_for_timeout(300)
                if scheme == "dark":
                    listed = page.locator("[data-session]").evaluate_all("els => [...new Set(els.map((e) => e.dataset.session))]")
                    check(sorted(listed) == sorted([SR, shots.S6]), f"{lang}: the phone's Remote tab keeps the same two chats ({listed})")
                    check(DEVICE in page.locator(f"[data-session='{SR}']").first.inner_text(), f"{lang}: the phone's row names its computer")
                shot(page, f"phone-remote-tab-{scheme}-{lang}")
                page.locator(".ph-kinds [data-kind-tab=all]").click()
                set_state("paused")
                page.goto(f"{BASE}/agents/{SR}?token=t&scheme={scheme}&lang={lang}")
                banner = page.locator(".remote-paused")
                expect(banner).to_be_visible(timeout=15000)
                if scheme == "dark":
                    top = page.locator(".ph-chat-top, .ph-topbar, header").first.inner_text()
                    check(words["paused"] in top, f"{lang}: the phone's top bar says the turn is paused ({top!r})")
                    check(banner.get_by_role("button", name=words["cancel"]).is_visible(), f"{lang}: the phone's banner offers Cancel turn")
                    check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{lang}: the banner fits the phone's width")
                shot(page, f"phone-header-paused-{scheme}-{lang}")
                set_state("connected")
                page.reload()
                expect(page.locator(".remote-paused")).to_have_count(0, timeout=10000)
                page.wait_for_timeout(300)
                shot(page, f"phone-header-connected-{scheme}-{lang}")
                page.evaluate("localStorage.removeItem('daedalus.chats.kind')")
                context.close()
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
