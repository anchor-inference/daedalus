"""The Details of the orchestrators and of a command-line member, on a desktop and on a phone.

A project's orchestrator has a Details tab after its Questions: its model, its context with the last
compaction, its spend in total and today, its tool groups and its brief, and none of what would break
it from there (a loop, the tool switches, moving or deleting the session). The main orchestrator's
Details are the same, keeping its workspace. A command-line member's column has a Details tab beside
Session: the CLI and its version, the model and effort, the status and turn, how long it has run, and
the spend its transcript reported; what the CLI does not report (Claude Code's window and limits, an
OpenCode member with no spend read yet) is said to be so. On a phone the same tabs open as sheets, and
nothing scrolls sideways. English and Russian.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_orchestration_details.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app  # noqa: E402
from screenshots import IRA_SCREEN, S1, UNHANDLED, focus_stub, respond, stub  # noqa: E402
from terminal_stub import TerminalStub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

WORDS = {
    "en": {"details": "Details", "today": "Today", "compacted": "Last compacted", "cli": "Claude Code 2.1.281", "nowindow": "context window size not reported by Claude Code",
           "nolimits": "Subscription limits: not reported by Claude Code.", "opencode": "Not reported by OpenCode yet.", "replies": "3 replies · 2 from the orchestrator · 1 from you"},
    "ru": {"details": "Сведения", "today": "Сегодня", "compacted": "Последнее сжатие", "cli": "Claude Code 2.1.281", "nowindow": "Claude Code не сообщает размер окна контекста",
           "nolimits": "Лимиты подписки: Claude Code их не сообщает.", "opencode": "OpenCode пока этого не сообщил.", "replies": "3 ответа · 2 от оркестратора · 1 от вас"},
}

REMOVED = {"danger", "loop", "tools"}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)


def sideways(page: Page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))


def sections(page: Page, scope: str) -> list[str]:
    ids = page.eval_on_selector_all(f"{scope} .details .dt-section", "els => els.map(e => e.id)")
    return [i.rsplit("-info-", 1)[-1] for i in ids]


def stand(lang: str):  # type: ignore[no-untyped-def]
    focus = FocusStub.bakery(lang)
    focus.staff_view_of_ira(lang)
    pid = focus.projects[0]["id"]
    term = TerminalStub(S1)
    term.add("tm-ira", title="claude · Ira", owner_kind="staff", owner_id="st-ira", project_id=pid, owner_label="Ira", cwd="/home/operator/work/bakery-site")
    term.emit("tm-ira", IRA_SCREEN)
    return focus, term, pid


def open_page(context, focus: FocusStub, term: TerminalStub, url: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", focus_stub(focus))
    term.install(page)
    page.add_init_script("try { localStorage.setItem('daedalus.term.renderer', 'dom'); } catch (e) {}")
    page.goto(url)
    return page


def orchestrator(page: Page, lang: str, check: Check, scope: str, where: str) -> None:
    words = WORDS[lang]
    page.wait_for_selector(f"{scope} .details .dt-section", timeout=15000)
    found = sections(page, scope)
    check.that({"session", "context", "usage", "brief", "advanced"} <= set(found), f"{lang} {where}: the orchestrator's Details have {found}")
    check.that(not REMOVED & set(found) and "workspace" not in found, f"{lang} {where}: the orchestrator's Details still offer {sorted((REMOVED | {'workspace'}) & set(found))}")
    today = page.locator(f"{scope} [data-usage-today]")
    check.that(today.count() == 1 and words["today"] in today.inner_text() and "$0.31" in today.inner_text(), f"{lang} {where}: today's spend reads {today.all_inner_texts()}")
    compacted = page.locator(f"{scope} [data-last-compaction]")
    check.that(compacted.count() == 1 and words["compacted"] in compacted.inner_text(), f"{lang} {where}: the last compaction reads {compacted.all_inner_texts()}")
    check.that(sideways(page) <= 0, f"{lang} {where}: the page scrolls sideways by {sideways(page)} px")


def staff(page: Page, lang: str, check: Check, scope: str, where: str) -> None:
    words = WORDS[lang]
    page.locator(f"{scope} .panel-tab[data-tab='details']").click()
    page.wait_for_selector(f"{scope} [data-staff-details]", timeout=5000)
    text = page.locator(f"{scope} [data-staff-details]").inner_text()
    for part in ("cli", "nowindow", "nolimits", "replies"):
        check.that(words[part] in text, f"{lang} {where}: the member's Details do not say {words[part]!r}")
    check.that("386k" in text or "386" in text, f"{lang} {where}: the context's tokens are not shown")
    check.that(page.locator(f"{scope} #staff-st-ira-info-staff-context .bar").count() == 0, f"{lang} {where}: a meter is drawn against a window Claude Code never reported")
    check.that(sideways(page) <= 0, f"{lang} {where}: the page scrolls sideways by {sideways(page)} px")


def desktop(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    focus, term, pid = stand(lang)
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = open_page(context, focus, term, f"{BASE}/project/{pid}?panel=details&token=t&lang={lang}")
    page.wait_for_selector(".chat.in-project .panel", timeout=15000)
    tabs = page.eval_on_selector_all(".panel .panel-tab", "els => els.map(e => e.dataset.tab)")
    check.that(tabs[:2] == ["questions", "details"], f"{lang}: the orchestrator's tabs are {tabs}")
    orchestrator(page, lang, check, ".panel", "desktop orchestrator")
    page.close()

    page = open_page(context, focus, term, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-aside .panel-tab[data-tab='details']", timeout=15000)
    tabs = page.eval_on_selector_all(".staff-aside .panel-tab", "els => els.map(e => e.dataset.tab)")
    check.that(tabs[:2] == ["session", "details"], f"{lang}: the member's tabs are {tabs}")
    staff(page, lang, check, ".staff-aside", "desktop member")
    # Every tab of the narrow column is reachable: the row scrolls rather than clipping the last one.
    reach = page.evaluate("(() => { const row = document.querySelector('.staff-aside .panel-tablist'); const last = row.lastElementChild; row.scrollLeft = row.scrollWidth; const a = last.getBoundingClientRect(), b = row.getBoundingClientRect(); return a.right <= b.right + 1; })()")
    check.that(bool(reach), f"{lang}: the last tab of the member's column cannot be reached")
    page.close()

    # A member whose CLI has not reported any spend yet says so, rather than zeros.
    page = open_page(context, focus, term, f"{BASE}/project/{pid}/staff/st-naya?token=t&lang={lang}")
    page.wait_for_selector(".staff-aside .panel-tab[data-tab='details']", timeout=15000)
    page.locator(".staff-aside .panel-tab[data-tab='details']").click()
    missing = page.locator(".staff-aside [data-not-reported]").all_inner_texts()
    check.that(WORDS[lang]["opencode"] in missing, f"{lang}: Naya's Details say {missing}")
    page.close()
    context.close()

    # The main orchestrator keeps its workspace and loses the same controls.
    main = MainStub(lang)

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = main.answer(request.method, url.path[url.path.index("/api/"):], body)
        if answered is not None:
            return respond(route, answered[1], status=answered[0])
        return stub(route)

    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", handle)
    page.goto(f"{BASE}/orchestration?panel=details&token=t&lang={lang}")
    page.wait_for_selector(".panel .details .dt-section", timeout=15000)
    found = sections(page, ".panel")
    check.that("usage" in found and not REMOVED & set(found), f"{lang}: the main orchestrator's Details have {found}")
    context.close()


def phone(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    focus, term, pid = stand(lang)
    context = browser.new_context(viewport=PHONE, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, focus, term, f"{BASE}/project/{pid}?panel=details&token=t&lang={lang}")
    page.wait_for_selector(".panel-sheet .details", timeout=15000)
    orchestrator(page, lang, check, ".panel-sheet", "phone orchestrator")
    page.close()

    page = open_page(context, focus, term, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-cli .ph-top", timeout=15000)
    page.locator(".staff-cli .ph-top .ph-ib").last.tap()
    expect(page.locator(".sheet .staff-panel")).to_be_visible(timeout=5000)
    staff(page, lang, check, ".sheet", "phone member")
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
        print("the orchestrators' and the members' Details hold")
    return 1 if check.problems or unhandled else 0


if __name__ == "__main__":
    sys.exit(run())
