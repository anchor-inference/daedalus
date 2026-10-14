"""The companion's page: its face and name, its state, its menu, its four tabs, and what a typed message does.

The unit tests pin the API and the session; what only a browser shows is that the header draws what
`GET /api/companion` says, that each tab and each sheet of the menu reads its own endpoint and draws
something rather than an error, that a goal added on the page is listed and can be paused, that the
face can be edited, that the header fits a phone, and that a message typed into the conversation is
a text turn (`/api/companion/message`) and not an utterance. In both languages, on a wide window and
on a phone.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8231 /tmp/app-root &
    APP_URL=http://127.0.0.1:8231/app python3 tests/browser/check_companion.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_stub  # noqa: E402
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
TABS = {
    "en": ["Chat", "Goals", "Activity", "Computer"],
    "ru": ["Разговор", "Цели", "Активность", "Компьютер"],
}
MENU = {
    "en": {"edit": "Edit name and look", "pause": "Pause", "tools": "What it can do", "memory": "What it remembers"},
    "ru": {"edit": "Имя и внешность", "pause": "Пауза", "tools": "Что он умеет", "memory": "Что он помнит"},
}


def run(browser, check, lang: str, viewport: dict, where: str) -> None:  # type: ignore[no-untyped-def]
    api_stub.COMPANION.__init__(shots.S2)  # a fresh host per pass: the goals written last time are gone
    posted: list[tuple[str, str, dict]] = []

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        rel = request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/") :]
        if request.method != "GET" and rel.startswith("/api/companion"):
            try:
                body = json.loads(request.post_data or "{}")
            except ValueError:
                body = {}
            posted.append((request.method, rel, body))
        return shots.stub(route)

    context = browser.new_context(viewport=viewport, color_scheme="dark", permissions=["microphone"])
    context.add_init_script(shots.SILENT_MIC)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/companion?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".pagehead-row > .companion-avatar", timeout=15000)

    title = page.locator(".pagehead h1").inner_text()
    check("Daedalus" in title, f"{where}: the header names the companion ({title!r})")
    check(page.locator(".pagehead-row > .companion-avatar line").count() >= 1, f"{where}: and draws its face with the antenna the profile asked for")
    word = page.locator(".companion-state").inner_text()
    check(word.strip() in ("Agents working", "Работают агенты"), f"{where}: the state line says what it is doing ({word!r})")
    tabs = page.locator(".companion-tabs [role=tab]").all_inner_texts()
    check(tabs == TABS[lang], f"{where}: the four tabs, in the reader's language ({tabs})")
    # The header was a row that ran off a phone: Call cut in half, the state wrapped under the face.
    edge = page.evaluate("(() => { const r = document.querySelector('.companion-menu').getBoundingClientRect(); return r.right; })()")
    check(edge <= viewport["width"], f"{where}: the menu, the last thing on the header, is inside the window ({edge:.0f})")
    clipped = page.evaluate("[...document.querySelectorAll('.companion-tabs [role=tab]')].filter(b => b.scrollWidth > b.clientWidth + 1 || b.getBoundingClientRect().right > window.innerWidth).length")
    check(clipped == 0, f"{where}: every tab label is whole and on the screen ({clipped} clipped)")
    one = page.evaluate("(() => { const n = document.querySelector('.pagehead h1').getBoundingClientRect(); const s = document.querySelector('.companion-sub').getBoundingClientRect(); return s.top >= n.bottom - 2 && Math.abs(s.left - n.left) < 2 && s.height < 26; })()")
    check(one, f"{where}: the state is one line, straight under the name")
    check(page.locator(".voice-beta").count() == 0, f"{where}: no beta chip on the page any more")

    # The conversation opens on its transcript, and a typed message is a text turn.
    page.wait_for_selector(".voice-read", timeout=15000)
    field = page.locator(".voice-read .voice-compose input")
    check(field.count() == 1, f"{where}: the conversation opens on its transcript with a field to type in")
    field.fill("what is on today")
    field.press("Enter")
    page.wait_for_timeout(400)
    sent = [(m, p, b) for m, p, b in posted if p == "/api/companion/message"]
    check(len(sent) == 1 and sent[0][2].get("text") == "what is on today" and sent[0][2].get("source") == "app", f"{where}: a typed message goes to /api/companion/message ({sent})")
    check(not [p for m, p, b in posted if p == "/api/companion/say"], f"{where}: and not as an utterance")

    def menu(item: str) -> None:
        page.locator(".companion-menu").click()
        page.get_by_role("menuitem", name=MENU[lang][item], exact=True).click()

    # Pause, from the menu; the banner it leaves resumes.
    menu("pause")
    page.wait_for_timeout(400)
    check(("POST", "/api/companion/pause", {"paused": True}) in posted, f"{where}: Pause pauses it")
    check(page.locator(".companion-state.state-paused").count() == 1, f"{where}: and the header says so")
    check(page.locator(".companion-paused").count() == 1, f"{where}: with a line saying what pausing means")
    page.locator(".companion-paused button").click()
    page.wait_for_timeout(400)
    check(("POST", "/api/companion/pause", {"paused": False}) in posted, f"{where}: and its button resumes")

    # Goals: listed, added, paused, deleted.
    page.locator(".companion-tabs [role=tab]").nth(1).click()
    page.wait_for_url("**/companion/goals*")
    page.wait_for_selector(".companion-goal", timeout=15000)
    check(page.locator(".companion-goal").count() == 2, f"{where}: the goals tab lists the two open goals")
    check(page.locator(".companion-goal-report").count() == 1, f"{where}: with the last report of the one that has one")
    check(page.locator(".companion-sched").count() >= 1, f"{where}: and what runs on its clock under them")
    page.locator(".companion-head-row .btn").first.click()
    form = page.locator(".companion-goal-form")
    form.wait_for(timeout=5000)
    form.locator("input").first.fill("Read the news at nine")
    form.locator("button[type=submit]").click()
    page.wait_for_function("document.querySelectorAll('.companion-goal').length === 3", timeout=5000)
    check(any(p == "/api/companion/goals" and b.get("cadence") == "daily" for m, p, b in posted), f"{where}: a goal added on the page is written with the cadence it offered (every day)")
    check(page.locator(".companion-goal-form").count() == 0, f"{where}: and the sheet closes")
    first = page.locator(".companion-goal").first
    first.locator(".companion-goal-actions button").first.click()
    page.wait_for_timeout(400)
    check(any(m == "PATCH" and b.get("status") == "paused" for m, p, b in posted), f"{where}: and can be paused from its card")

    # The other tabs, and the two sheets of the menu, draw without an error.
    for index, selector in ((2, ".companion-activity li"), (3, ".companion-computer")):
        page.locator(".companion-tabs [role=tab]").nth(index).click()
        try:
            page.wait_for_selector(selector, timeout=8000)
            drawn = True
        except Exception:  # noqa: BLE001 — reported below
            drawn = False
        check(drawn, f"{where}: the {TABS['en'][index]} tab draws ({selector})")
    for item, selector in (("tools", ".companion-sheet code"), ("memory", ".companion-sheet .companion-empty, .companion-sheet .companion-note")):
        menu(item)
        try:
            page.wait_for_selector(selector, timeout=8000)
            drawn = True
        except Exception:  # noqa: BLE001 — reported below
            drawn = False
        check(drawn, f"{where}: the {item} sheet draws ({selector})")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    # The face can be edited.
    menu("edit")
    page.wait_for_selector(".companion-editor", timeout=5000)
    page.locator(".companion-editor input.field").fill("Iris")
    page.locator(".companion-swatch").nth(2).click()
    page.locator(".companion-editor .btnrow .btn:not(.ghost)").click()
    page.wait_for_timeout(500)
    saved = [b for m, p, b in posted if m == "PUT" and p == "/api/companion"]
    check(bool(saved) and saved[-1].get("name") == "Iris" and saved[-1]["avatar"]["color"] == "#2bb673", f"{where}: the name and the colour are saved ({saved[-1:]})")
    page.wait_for_function("document.querySelector('.pagehead h1').innerText.includes('Iris')", timeout=8000)
    check(True, f"{where}: and the header follows")

    # The field's microphone opens the stage and the microphone, and tells the server; the same
    # button, now Hang up, closes both. It is the only microphone on the page.
    page.locator(".companion-tabs [role=tab]").first.click()
    check(page.locator(".voice-read").count() == 1, f"{where}: closing the sheets with Escape left the conversation on its transcript")
    page.locator(".voice-compose-btn.mic").click()
    page.wait_for_selector(".voice-stage", timeout=10000)
    page.wait_for_timeout(500)
    check(("POST", "/api/companion/call", {"on": True}) in posted, f"{where}: the microphone opens a call on the server")
    check(page.locator(".voice-compose-btn.hangup").count() == 1, f"{where}: and the button turns into Hang up")
    check(page.locator(".pagehead .ic-mic, .pagehead [aria-label='Call'], .pagehead [aria-label='Позвонить']").count() == 0, f"{where}: with no second microphone in the header")
    page.locator(".voice-compose-btn.hangup").click()
    page.wait_for_timeout(400)
    check(("POST", "/api/companion/call", {"on": False}) in posted, f"{where}: hanging up closes it")

    check(not errors, f"{where}: no page errors ({errors[:2]})")
    check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"{where}: nothing runs off the side")
    context.close()


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        run(browser, check, "en", {"width": 1440, "height": 900}, "desktop en")
        run(browser, check, "ru", {"width": 1440, "height": 900}, "desktop ru")
        run(browser, check, "en", {"width": 390, "height": 844}, "phone en")
        browser.close()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
