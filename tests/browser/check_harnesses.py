"""The Harnesses screen (M7) on a desktop and on a phone, and refuse what does not behave.

On a desktop at 1440 px: the header counts the CLIs, the updates waiting and when they were checked;
the five rows come in the table's order with the one button each needs (Claude Code current, three
updates, pi to install); a row unfolds to its agents with where each is defined and its last
self-check step by step, Grok's naming the step it failed at; Check, Update and Update all post what
the host expects; an update of a CLI staff are working on is refused on its row, naming them; a
`harness.updated` event brings the row up to date; the Host environment shows its own rows; a CLI
past its tested range without a self-check is marked, and the hiring form warns before it is hired.

On a phone at 390 px: one card per CLI with its button, nothing scrolling sideways, and the More sheet
leading here. The Russian page is checked on the same points with its own words.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_harnesses.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import CATALOG, DEFAULT_APP, HarnessesStub, TeamStub, drawer_go, expect_app  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import P1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

WORDS = {
    "en": {"title": "Harnesses", "subtitle": "5 CLIs · 3 updates · checked 2 minutes ago", "failed": "failed at ready", "passed": "passed", "refused": "Max (Bakery 2.0)",
           "unverified": "untested version", "guard": "outside the versions the adapter was tested with", "project": "the folder's own", "signin": "subscription · max"},
    "ru": {"title": "Исполнители", "subtitle": "5 CLI · 3 обновления · проверено 2 минуты назад", "failed": "не пройдена на шаге ready", "passed": "пройдена", "refused": "Max (Bakery 2.0)",
           "unverified": "непроверенная версия", "guard": "вне проверенных для адаптера", "project": "папки проекта", "signin": "subscription · max"},
}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)


def sideways(page: Page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))


def open_page(context, harnesses: HarnessesStub, feed: EventFeed | None, url: str, team: TeamStub | None = None) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path[parts.path.index("/api/"):]
        body = request.post_data_json if request.method == "POST" and request.post_data else None
        answered = harnesses.answer(request.method, path, parts.query, body) or (team.answer(request.method, path, parts.query, body) if team else None)
        if answered is not None:
            return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        return stub(route)

    page.route("**/api/**", handle)
    if feed is not None:
        page.route("**/api/events**", feed.route)
    page.goto(url)
    return page


def states(page: Page, selector: str) -> list[tuple[str, str]]:
    return page.eval_on_selector_all(selector, "els => els.map(e => [e.dataset.harness, e.dataset.state])")


def desktop(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    harnesses = HarnessesStub()
    feed = EventFeed()
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = open_page(context, harnesses, feed, f"{BASE}/harnesses?token=t&lang={lang}")
    page.wait_for_selector(".harness-table .harness-row", timeout=15000)
    expect(page.locator(".pagehead h1")).to_have_text(words["title"])
    expect(page.locator(".pagehead")).to_contain_text(words["subtitle"])
    got = states(page, ".harness-row")
    check.that(got == [["claude", "current"], ["codex", "update"], ["opencode", "update"], ["pi", "install"], ["grok", "update"]], f"{lang}: the rows are {got}")
    check.that(words["signin"] in page.locator(".harness-row[data-harness='claude'] .harness-signin").inner_text(), f"{lang}: Claude Code's sign-in says {page.locator('.harness-row[data-harness=claude] .harness-signin').inner_text()!r}")
    check.that(page.locator(".harness-node").count() == 1, f"{lang}: the pinned Node is not shown for the container")

    # A row unfolds: agents with their source, the self-check step by step.
    page.locator(".harness-row[data-harness='claude'] .harness-name").click()
    details = page.locator(".harness-row-details")
    expect(details.locator(".harness-agent")).to_have_count(4)
    check.that(words["project"] in details.inner_text(), f"{lang}: the agents do not say which are the folder's own")
    check.that(words["passed"] in details.locator(".harness-check-line").inner_text() and details.locator(".harness-step").count() == 9, f"{lang}: Claude Code's self-check reads {details.locator('.harness-check-line').inner_text()!r}")
    page.locator(".harness-row[data-harness='grok'] .harness-name").click()
    expect(page.locator(".harness-row-details .harness-check-line.failed")).to_contain_text(words["failed"])
    check.that(page.locator(".harness-row-details .harness-step.failed").count() == 1, f"{lang}: Grok's failed step is not marked")

    # Update: posted with its environment; refused while staff work on it, naming them.
    page.locator(".harness-row[data-harness='opencode'] .harness-action .btn").click()
    expect(page.locator(".harness-row[data-harness='opencode']")).to_have_attribute("data-state", "busy", timeout=5000)
    page.locator(".harness-row[data-harness='codex'] .harness-action .btn").click()
    expect(page.locator(".harness-row-refused .harness-refused")).to_contain_text(words["refused"], timeout=5000)
    page.locator(".harness-check").click()
    page.locator(".harness-update-all").click()
    page.wait_for_timeout(500)
    posted = [p for p, _ in harnesses.posted]
    check.that(posted == ["/api/harnesses/opencode/update", "/api/harnesses/codex/update", "/api/harnesses/check", "/api/harnesses/update-all"], f"{lang}: the screen posted {posted}")
    check.that(all(b == {"env": "container"} for _, b in harnesses.posted), f"{lang}: the posts carried {[b for _, b in harnesses.posted]}")

    # The update ends: the manager says so, and the row follows without a reload.
    opencode = next(r for r in harnesses.rows["container"] if r["harness"] == "opencode")
    opencode.update(operation=None, installed_version="1.18.32", update_available=False)
    check.that(feed.connected() >= 1, f"{lang}: the app did not open the event stream")
    feed.send("harness.updated", {"env": "container", "harness": "opencode", "kind": "update", "version": "1.18.32", "ok": True})
    expect(page.locator(".harness-row[data-harness='opencode']")).to_have_attribute("data-state", "current", timeout=5000)

    # The host environment has rows of its own, and no Node to install from here.
    page.locator(".harness-env button[data-env='host']").click()
    expect(page.locator(".harness-row[data-harness='claude'] .harness-version")).to_contain_text("2.1.270", timeout=5000)
    check.that(page.locator(".harness-node").count() == 0, f"{lang}: the host offers the pinned Node")
    check.that(sideways(page) <= 0, f"{lang}: the table scrolls the page sideways")
    context.close()

    # A CLI that moved past its tested range: marked on the screen, and warned about when hired.
    harnesses = HarnessesStub()
    harnesses.unverified("codex", "0.157.2")
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = open_page(context, harnesses, None, f"{BASE}/harnesses?token=t&lang={lang}")
    mark = page.locator(".harness-row[data-harness='codex'] .harness-mark")
    expect(mark).to_have_attribute("data-mark", "unverified", timeout=10000)
    expect(mark).to_have_text(words["unverified"])
    page.locator(".harness-row[data-harness='codex'] .harness-name").click()
    expect(page.locator(".harness-row-details .harness-guard")).to_contain_text(words["guard"])
    context.close()

    catalog = {**CATALOG, "claude": {**CATALOG["claude"], "version": "2.4.0", "version_guard": "unverified", "tested_versions": ["2.1.281", "2.2.0"]}}
    team = TeamStub({"id": P1, "name": "Bakery", "folders": []}, catalog=catalog)
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = open_page(context, HarnessesStub(), None, f"{BASE}/project/{P1}/team?token=t&lang={lang}", team)
    page.wait_for_selector(".pagehead-actions .iconbtn.primary", timeout=15000)
    page.locator(".pagehead-actions .iconbtn.primary").click()
    page.wait_for_selector(".staff-sheet .executor", timeout=5000)
    check.that(page.locator(".staff-guard").count() == 0, f"{lang}: the hiring form warns before a CLI is chosen")
    page.locator(".staff-sheet .executor", has_text="Claude Code").click()
    expect(page.locator(".staff-guard")).to_contain_text("2.4.0", timeout=5000)
    context.close()
    feed.close()


def phone(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    harnesses = HarnessesStub()
    context = browser.new_context(viewport=PHONE, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, harnesses, None, f"{BASE}/agents?token=t&lang={lang}")
    # Harnesses is one of the drawer's More destinations on a phone.
    drawer_go(page, "harnesses")
    page.wait_for_selector(".harness-cards .harness-card", timeout=10000)
    got = states(page, ".harness-card")
    check.that([h for h, _ in got] == ["claude", "codex", "opencode", "pi", "grok"], f"{lang} phone: the cards are {got}")
    install = page.locator(".harness-card[data-harness='pi'] .harness-card-head .btn")
    box = install.bounding_box()
    check.that(bool(box and box["height"] >= 40), f"{lang} phone: pi's Install button is {box} high")
    check.that(page.locator(".harness-table").count() == 0, f"{lang} phone: a table is drawn on a phone")
    page.locator(".harness-card[data-harness='grok'] .harness-card-more").tap()
    expect(page.locator(".harness-card[data-harness='grok'] .harness-check-line.failed")).to_contain_text(words["failed"])
    check.that(sideways(page) <= 0, f"{lang} phone: the screen scrolls sideways by {sideways(page)} px")
    context.close()


def run() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            desktop(browser, lang, check)
            phone(browser, lang, check)
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL", problem)
    if not check.problems and not unhandled:
        print("the Harnesses screen holds")
    return 1 if check.problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
