"""Run now and Delete on the schedule card in a conversation's Details panel, in a real browser.

The card ran and deleted its schedule through routes the host no longer serves; every press answered
404 and the toast read "Not Found" for a schedule that plainly existed. The host here keeps the
schedules (schedule_stub.py) plus one shaped like the schedule the operator met it on: an agent task
in a project that runs in the conversation that made it, every ten hours. This refuses anything but:

- ▷ posts ``/api/recurring/{id}/run`` with the schedule's revision, the collection's revision and a
  command identity, and no toast follows;
- 🗑 asks first, then posts ``/api/recurring/{id}/remove`` the same way, and the card goes;
- a press on a schedule that went meanwhile says "Schedule not found" in the page's language;
- a refusal with the host's reason says what failed and why ("Could not run the schedule: …");
- no toast anywhere says "Not Found".

Both languages, at a desktop's width.

    APP_URL=http://127.0.0.1:8471/app python3 tests/browser/check_details_schedule.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from schedule_stub import ScheduleStub, _next  # noqa: E402
from screenshots import S1, S4, UNHANDLED, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
SITE = "s1d991cfd1c7759a"
BUSY = "s9e0f1a2b3c4d5e6"
WORDS = {
    "en": {"site": "site check + autonomous release (every 10 h)", "busy": "nightly backup", "run": "Run now", "delete": "Delete",
           "missing": "Schedule not found", "refused": "Could not run the schedule: the previous scheduled run is still active"},
    "ru": {"site": "проверка сайта + автономный релиз (каждые 10 ч)", "busy": "ночной бэкап", "run": "Запустить", "delete": "Удалить",
           "missing": "Расписание не найдено", "refused": "Не удалось запустить расписание: the previous scheduled run is still active"},
}


def host_for(lang: str) -> ScheduleStub:
    host = ScheduleStub(lang, chat=S1, other=S4)
    words = WORDS[lang]
    shape = dict(host.schedules[0])
    shape.update(id=SITE, name=words["site"], cron="7 */10 * * *", next_run_at=_next("7 */10 * * *"), run_in="self",
                 kind="agent", target_session=S1, created_by_session=S1, project_id="ec9be94ae5bf", schedule_revision=2,
                 last_run_at=None, last_summary=None, files="[]")
    busy = dict(shape, id=BUSY, name=words["busy"], cron="0 3 * * *", next_run_at=_next("0 3 * * *"))
    host.schedules = [shape, busy]
    return host


def open_card(browser, lang: str, host: ScheduleStub) -> Page:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.set_default_timeout(10000)

    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        body = json.loads(r.request.post_data or "{}") if r.request.method in ("POST", "PATCH", "PUT") else None
        if rel == f"/api/recurring/{BUSY}/run":
            # The host refusing in its own words: the run before this one has not finished.
            host.writes.append((r.request.method, rel, body or {}))
            return respond(r, {"detail": "the previous scheduled run is still active"}, status=403)
        answered = host.answer(r.request.method, rel, body)
        if answered is not None:
            return respond(r, answered[1], status=answered[0])
        return stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={lang}")
    page.locator(".panel .details [data-prop='cron']").click()
    page.wait_for_selector(".panel .sched-row")
    return page


def row(page: Page, name: str):  # type: ignore[no-untyped-def]
    return page.locator(".panel .sched-row", has_text=name)


def toasts(page: Page) -> str:
    return " | ".join(page.locator(".toast").all_inner_texts())


def check(browser, lang: str) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    words = WORDS[lang]
    host = host_for(lang)
    page = open_card(browser, lang, host)
    card = row(page, words["site"])
    expect(card).to_contain_text("7 */10 * * *")

    card.get_by_role("button", name=words["run"]).click()
    page.wait_for_timeout(600)
    runs = [w for w in host.writes if w[1] == f"/api/recurring/{SITE}/run"]
    print(f"{lang} run:", runs)
    if len(runs) != 1 or runs[0][0] != "POST":
        problems.append(f"{lang}: Run now did not post the schedule's run command once: {host.writes}")
    elif runs[0][2].get("expected_schedule_revision") != 2 or runs[0][2].get("expected_collection_revision") != 9 or not runs[0][2].get("client_operation_id"):
        problems.append(f"{lang}: Run now did not send the revisions shown and a command identity: {runs[0][2]}")
    if toasts(page):
        problems.append(f"{lang}: Run now ended in a toast: {toasts(page)}")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/schedule-card-{lang}.png")

    # Refused by the host: the toast says what failed and the host's reason, not a status.
    row(page, words["busy"]).get_by_role("button", name=words["run"]).click()
    expect(page.locator(".toast")).to_contain_text(words["refused"])
    page.locator(".toast").click()

    card.get_by_role("button", name=words["delete"]).click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    expect(card).to_have_count(0)
    removes = [w for w in host.writes if w[1] == f"/api/recurring/{SITE}/remove"]
    print(f"{lang} remove:", removes)
    if len(removes) != 1 or removes[0][2].get("expected_schedule_revision") != 2 or not removes[0][2].get("client_operation_id"):
        problems.append(f"{lang}: Delete did not post the schedule's remove command with its revision: {removes}")

    # Gone behind the card's back: deleted elsewhere, and this card has not refreshed yet.
    host.schedules = [s for s in host.schedules if s["id"] != BUSY]
    row(page, words["busy"]).get_by_role("button", name=words["delete"]).click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    expect(page.locator(".toast")).to_contain_text(words["missing"])
    if "Not Found" in toasts(page):
        problems.append(f"{lang}: a toast still says Not Found: {toasts(page)}")
    page.context.close()
    return problems


def run() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        problems = [p for lang in ("en", "ru") for p in check(browser, lang)]
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
