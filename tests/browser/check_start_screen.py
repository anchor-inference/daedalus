"""The desktop's start screen: the question, the composer, "Continue" and the one white circle.
English and Russian at 1440 px, and a phone that must not grow any of it.

What is checked:

- "Continue" lists the chats to pick up in the order of what needs the operator: the one waiting
  first, then the ones at work, then the rest by recency, three of them with the rest behind "Show
  all"; the card is the whole link and says its state in words under the title;
- where the chat starts is a chip in the composer's own row: it names "No project" at first, opens
  every project with a search, and a project picked there is the one the new chat is created in; the
  Container/Host choice, which only a chat of its own has, leaves the row while a project is picked;
- the projects page's "New chat in …" arrives with that project already picked (``?in=<id>``);
- the circle is Send, greyed while the field is empty;
- "Full access", chosen in the row, is switched on for the chat once it exists;
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
WORDS = {"en": {"resume": "Continue", "all": "Show all 9", "none": "No project", "waiting": "Waiting for your answer", "working": "Working", "full": "Full access"},
         "ru": {"resume": "Продолжить", "all": "Показать все 9", "none": "Без проекта", "waiting": "Ждёт вашего ответа", "working": "Работает", "full": "Полный доступ"}}


def serve(page: Page, posted: list[object], bypassed: list[tuple[str, object]]) -> None:
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
        if request.method == "POST" and path.endswith("/bypass"):
            bypassed.append((path, request.post_data_json))
        if request.method == "POST" and path.startswith(f"/api/sessions/{shots.S1}/"):
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "r1"}))
        return shots.stub(r)

    page.route("**/api/**", route)


def desktop(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    posted: list[object] = []
    bypassed: list[tuple[str, object]] = []
    serve(page, posted, bypassed)
    page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".resume-card", timeout=15000)
    where = f"{lang} 1440"

    # Continue: waiting first, then the two at work, then by recency; three cards, the rest behind Show all.
    resume = page.locator(".start-resume")
    check(words["resume"] in resume.locator(".start-resume-head").inner_text(), f"{where}: the section is called {words['resume']!r}")
    cards = page.locator(".resume-card")
    order = [cards.nth(i).get_attribute("data-session") for i in range(cards.count())]
    check(len(order) == 3, f"{where}: three cards at most, not {len(order)}")
    check(order[:1] == [shots.S4], f"{where}: the chat waiting for the operator comes first ({order})")
    states = [cards.nth(i).get_attribute("data-state") for i in range(cards.count())]
    check(states == ["waiting", "working", "working"], f"{where}: then the two at work ({states})")
    check(words["waiting"] in cards.first.inner_text(), f"{where}: the first card says {words['waiting']!r} ({cards.first.inner_text()!r})")
    check(words["working"] in cards.nth(1).inner_text(), f"{where}: the next says {words['working']!r}")
    heights = {round(cards.nth(i).bounding_box()["height"]) for i in range(cards.count())}
    check(heights == {56}, f"{where}: cards are 56 px ({heights})")
    more = page.locator(".start-resume-more")
    check(more.inner_text().strip() == words["all"], f"{where}: the rest are behind {words['all']!r} ({more.inner_text()!r})")
    more.click()
    check(page.locator(f".resume-card[data-session='{shots.S3}']").count() == 1, f"{where}: Show all brings the loop in")
    meta = page.locator(f".resume-card[data-session='{shots.S10}']").inner_text()
    check("Companion" in meta, f"{where}: a chat's title names its project ({meta!r})")
    cards.first.click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    check(shots.S4 in page.url, f"{where}: a card opens its chat")
    page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".resume-card", timeout=15000)

    # The circle is Send, and nothing in an ordinary composer opens Voice mode: that is a beta with a
    # chat of its own, and the button that led there from every empty field is gone.
    composer = page.locator(".start-composer")
    check(composer.locator("[data-action='voice'], .roundbtn.voice").count() == 0, f"{where}: an empty field offers no voice conversation")
    send = composer.locator(".roundbtn[data-action='send']")
    check(send.count() == 1 and send.is_disabled(), f"{where}: an empty field's Send waits for words")
    composer.locator(".composer-field").fill("Bake the menu")
    circle = send.bounding_box()
    check(send.is_visible() and send.is_enabled(), f"{where}: text makes Send live")
    check(bool(circle) and abs(circle["width"] - circle["height"]) < 1, f"{where}: the primary is a circle ({circle})")
    box = composer.locator(".composer-box").bounding_box()
    check(bool(box) and round(box["width"]) == 760, f"{where}: the composer is the conversation's 760 px column ({box and box['width']})")

    # Where the chat starts: a chip in the row, "No project" first, with the run-on choice.
    chip = composer.locator(".composer-row .composer-project")
    check(chip.count() == 1 and words["none"] in chip.inner_text(), f"{where}: the chip in the composer's row says {words['none']!r}")
    check(page.locator(".start-where").count() == 0, f"{where}: no row of chips under the composer")
    check(composer.locator(".runon-select").count() == 1, f"{where}: a chat of its own offers Container or Host")
    chip.click()
    search = page.locator(".project-menu input[type='search']")
    check(search.count() == 1 and search.evaluate("el => el === document.activeElement"), f"{where}: the chooser opens on its search")
    rows = page.locator(".project-menu .project-pick-row")
    check(rows.first.get_attribute("data-project") is None and rows.count() >= 4, f"{where}: No project first, then every project ({rows.count()})")
    search.fill("supp")
    check(rows.count() == 1, f"{where}: the search narrows the list ({rows.count()})")
    pick = rows.first.get_attribute("data-project")
    search.press("Enter")
    check(page.locator(".project-menu").count() == 0, f"{where}: Enter picks the first match and closes the chooser")
    check(chip.get_attribute("data-project") == pick, f"{where}: the chip names the project picked")
    chip.click()
    page.locator(f".project-menu .project-pick-row[data-project='{shots.P1}']").click()
    check(chip.get_attribute("data-project") == shots.P1, f"{where}: a project is picked by a click")
    check(composer.locator(".runon-select").count() == 0, f"{where}: and the project's folder decides where it runs")

    # Full access is the row's own chip; it is switched on once the chat exists.
    composer.locator(".composer-access").click()
    page.locator(".access-menu [data-toggle='bypass']").click()
    check(words["full"] in composer.locator(".composer-access").inner_text(), f"{where}: the access chip says {words['full']!r}")
    page.keyboard.press("Escape")
    composer.locator(".roundbtn[data-action='send']").click()
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    body = posted[0] if posted else {}
    check(isinstance(body, dict) and body.get("project_id") == shots.P1, f"{where}: the chat is created in the picked project ({body})")
    check(any(path.endswith("/bypass") and sent == {"on": True} for path, sent in bypassed), f"{where}: full access is switched on for the new chat ({bypassed})")

    # The projects page's New chat arrives with its project picked.
    page.goto(f"{BASE}/agents?in={shots.P2}&token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".start-composer .composer-project", timeout=15000)
    check(page.locator(".composer-project").get_attribute("data-project") == shots.P2, f"{where}: ?in= picks that project")
    context.close()


def phone(browser, check) -> None:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", shots.stub)
    page.goto(f"{BASE}/agents?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".ph-home .start-composer", timeout=15000)
    page.wait_for_timeout(600)
    check(page.locator(".start-resume, .composer-project, .start-greeting").count() == 0, "390: the phone's home has none of the desktop's parts")
    check(page.locator(".ph-live .list-row, .ph-live > *").count() >= 1, "390: and keeps its own live rows")
    page.locator(".ph-where").click()
    sheet = page.locator(".sheet .project-pick")
    check(sheet.count() == 1, "390: the phone's home chooses a project in a sheet")
    sheet.locator(f".project-pick-row[data-project='{shots.P1}']").click()
    check("Bakery" in page.locator(".ph-where").inner_text(), "390: the project picked names itself on the home")
    # Nothing on a phone leads to the projects page (its drawer opens the projects sheet), but a window
    # narrowed with the page open keeps it, so it has to fit.
    page.goto(f"{BASE}/agents?view=projects&token=t&scheme=dark&lang=en")
    page.wait_for_selector(".projects-row", timeout=15000)
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
