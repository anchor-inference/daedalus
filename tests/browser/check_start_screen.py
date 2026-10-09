"""The desktop's start screen: "Active now", "Start in …" and the one white circle. English and
Russian at 1440 px, and a phone that must not grow any of it.

What is checked:

- "Active now" lists the live chats in the phone home's order — the one waiting for the operator,
  then the ones at work, then the loops — three of them, with the rest behind "Show all"; only the
  waiting one carries Reply, and a row opens its chat;
- "Start in …" offers a chat of its own (picked at first) and the projects, the hidden ones behind
  "N more"; a project picked there is the one the new chat is created in, and the Container/Host
  choice, which only a chat of its own has, leaves the composer while a project is picked;
- the projects page's "New chat in …" arrives with that project already picked (``?in=<id>``);
- the circle is the voice conversation while the field is empty and Send once it holds text;
- a phone draws none of it: its home is unchanged; the projects page, kept when a window narrows,
  fits without scrolling sideways.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_start_screen.py
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
WORDS = {"en": {"live": "Active now", "reply": "Reply", "all": "Show all 4", "chat": "a chat of its own"},
         "ru": {"live": "Сейчас активны", "reply": "Ответить", "all": "Показать все 4", "chat": "отдельном чате"}}


def serve(page: Page, posted: list[object]) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if request.method == "GET" and path == "/api/projects":
            # The shared fixture leaves Expenses and Weekly digest as single chats' scratch projects,
            # which the chips leave out; here both have been kept, so there are more than three
            # projects for the More chip to hold and ?in= has a kept project to pick.
            projects = copy.deepcopy(shots.PROJECTS)
            for project in projects:
                if project["id"] in (shots.P2, shots.P4):
                    project["settings"]["ephemeral"] = False
            return shots.respond(r, projects)
        if request.method == "POST" and path == "/api/sessions":
            posted.append(request.post_data_json)
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": shots.S1}))
        if request.method == "POST" and path.startswith(f"/api/sessions/{shots.S1}/"):
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "r1"}))
        return shots.stub(r)

    page.route("**/api/**", route)


def desktop(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    posted: list[object] = []
    serve(page, posted)
    page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".start-live-row", timeout=15000)
    where = f"{lang} 1440"

    # Active now: waiting first, then working, three rows; the loop is the fourth, behind Show all.
    live = page.locator(".start-live")
    check(words["live"] in live.locator(".start-live-head").inner_text(), f"{where}: the section is called {words['live']!r}")
    rows = page.locator(".start-live-row")
    order = [rows.nth(i).get_attribute("data-session") for i in range(rows.count())]
    check(len(order) == 3, f"{where}: three live rows at most, not {len(order)}")
    check(order[:1] == [shots.S4], f"{where}: the chat waiting for the operator comes first ({order})")
    check(shots.S3 not in order, f"{where}: the loop waits behind the chats at work")
    check(page.locator(".start-live-row.running").count() == 2, f"{where}: the two chats at work follow")
    check(page.locator(".start-live-reply").count() == 1 and rows.first.locator(".start-live-reply").count() == 1, f"{where}: only the waiting chat has Reply")
    reply = rows.first.locator(".start-live-reply")
    check(words["reply"] in reply.inner_text(), f"{where}: and it says {words['reply']!r}")
    check("primary" not in (reply.get_attribute("class") or ""), f"{where}: Reply is neutral, the white primary stays the composer's")
    more = page.locator(".start-live-more")
    check(more.inner_text().strip() == words["all"], f"{where}: the rest are behind {words['all']!r} ({more.inner_text()!r})")
    more.click()
    check(page.locator(f".start-live-row[data-session='{shots.S3}']").count() == 1, f"{where}: Show all brings the loop in")
    meta = page.locator(f".start-live-row[data-session='{shots.S10}'] .start-live-meta").inner_text()
    check("Voice" in meta, f"{where}: a chat's meta names its project ({meta!r})")

    # The circle is Send, and nothing in an ordinary composer opens Voice mode: that is a beta with a
    # chat of its own, and the button that led there from every empty field is gone.
    composer = page.locator(".start-composer")
    check(composer.locator("[data-action='voice'], .roundbtn.voice").count() == 0, f"{where}: an empty field offers no voice conversation")
    send = composer.locator(".roundbtn[data-action='send']")
    check(send.count() == 1 and send.is_disabled(), f"{where}: an empty field's Send waits for words")
    composer.locator("textarea").fill("Bake the menu")
    circle = send.bounding_box()
    check(send.is_visible() and send.is_enabled(), f"{where}: text makes Send live")
    check(bool(circle) and abs(circle["width"] - circle["height"]) < 1, f"{where}: the primary is a circle ({circle})")

    # Start in: a chat of its own first, with the run-on choice; a project drops the choice.
    chips = page.locator(".start-where-chip")
    check(chips.first.get_attribute("aria-pressed") == "true" and words["chat"] in chips.first.inner_text(), f"{where}: a chat of its own is picked at first")
    check(composer.locator(".runon-select").count() == 1, f"{where}: a chat of its own offers Container or Host")
    hidden = page.locator(".start-where-chip.more")
    check(hidden.count() == 1, f"{where}: the projects past three are behind a More chip")
    hidden.click()
    menu = page.locator(".start-where-menu [role='menuitem']")
    check(menu.count() >= 1, f"{where}: More lists the rest")
    pick = menu.first.get_attribute("data-project")
    menu.first.click()
    check(page.locator(f".start-where-chip[data-project='{pick}']").get_attribute("aria-pressed") == "true", f"{where}: a project picked from More stays in the row, picked")
    bakery = page.locator(f".start-where-chip[data-project='{shots.P1}']")
    bakery.click()
    check(bakery.get_attribute("aria-pressed") == "true", f"{where}: a project chip is picked by a click")
    check(composer.locator(".runon-select").count() == 0, f"{where}: and the project's folder decides where it runs")
    composer.locator(".roundbtn[data-action='send']").click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    body = posted[0] if posted else {}
    check(isinstance(body, dict) and body.get("project_id") == shots.P1, f"{where}: the chat is created in the picked project ({body})")

    # The projects page's New chat arrives with its project picked.
    page.goto(f"{BASE}/agents?in={shots.P2}&token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".start-where", timeout=15000)
    check(page.locator(f".start-where-chip[data-project='{shots.P2}']").get_attribute("aria-pressed") == "true", f"{where}: ?in= picks that project")
    context.close()


def phone(browser, check) -> None:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", shots.stub)
    page.goto(f"{BASE}/agents?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".ph-home .start-composer", timeout=15000)
    page.wait_for_timeout(600)
    check(page.locator(".start-where, .start-live, .start-mark").count() == 0, "390: the phone's home has none of the desktop's parts")
    check(page.locator(".ph-live .ph-row, .ph-live > *").count() >= 1, "390: and keeps its own live rows")
    # Nothing on a phone leads to the projects page (its drawer opens the projects sheet), but a window
    # narrowed with the page open keeps it, so it has to fit.
    page.goto(f"{BASE}/agents?view=projects&token=t&scheme=dark&lang=en")
    page.wait_for_selector(".projects-row:not(.head)", timeout=15000)
    check(page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "390: the projects page, kept in a narrow window, does not scroll sideways")
    context.close()


def main() -> int:
    expect_app(BASE)
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        for lang in ("en", "ru"):
            desktop(browser, lang, check)
        phone(browser, check)
        browser.close()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
