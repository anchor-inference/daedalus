"""The Schedules screen and the schedules dialog over a chat, in a real browser.

The host keeps a realistic set (schedule_stub.py): long markdown instructions, five cron shapes, a
proposal waiting for the operator, a failed last run, a paused schedule and a finished one-off. This
refuses anything but:

- the screen lists them grouped (needs approval, active, paused, finished), each with its time in the
  reader's own words, never a raw cron line, with the failed run marked failed;
- the open schedule folds its long instruction, opens it on Show all, and lists its recent runs;
- the waiting proposal's Approve sits in its row and posts the exact proposal with the ten-year
  horizon the host names when approval is off; Pause posts the revision shown;
- the "Schedules" link on a chat's schedule card opens a wide centred dialog (not a narrow sheet) on
  that chat's schedules with the clicked one open, "All" widens it, Escape closes it;
- on a phone (390 px) the screen shows the list, a row opens the schedule with a way back, the dialog
  is a full-screen sheet, and nothing scrolls sideways.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8487 /tmp/app-root &
    APP_URL=http://127.0.0.1:8487/app python3 tests/browser/check_schedules.py

LANG_UI=ru checks the Russian page. SCHEME=light draws the light theme. SHOTS=<directory> also saves
the pictures this looks at. Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from schedule_stub import ScheduleStub  # noqa: E402
from screenshots import S1, S4, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LANG = os.environ.get("LANG_UI", "en") if os.environ.get("LANG_UI", "en") in ("en", "ru") else "en"
SCHEME = os.environ.get("SCHEME", "dark")
MINE = "s1a2b3c4d5e6f701"
RESET = "p7081223344556a7"
RAW_CRONS = ("5 * * * *", "10 2 * * *", "50 5,11,17,23 * * *", "0 4 * * 1", "0 */6 * * *")
WORDS = {
    "en": {"approve": "Approve schedule", "pause": "Pause", "all": "All", "more": "Show all", "groups": ["Needs approval", "Active", "Paused", "Done"], "hourly": "Every hour at :"},
    "ru": {"approve": "Одобрить расписание", "pause": "Пауза", "all": "Все", "more": "Показать целиком", "groups": ["Ждут одобрения", "Активные", "На паузе", "Завершённые"], "hourly": "Каждый час в :"},
}[LANG]


def turn_messages() -> list[dict]:
    calls = [{"id": "c1", "name": "ScheduleCreate", "arguments": {"name": "check", "prompt": "check", "cron": "5 * * * *", "run_in": "self"}}]
    results = [{"id": "c1", "content": f"Schedule: {MINE}\n'check' (agent in this session) is set; next run soon (UTC).", "is_error": False}]
    return [
        {"role": "user", "seq": 901, "text": "Check the server every hour.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=6)},
        {"role": "assistant", "seq": 902, "text": "", "thinking": "", "tool_calls": calls, "tool_results": [], "created_at": ago(minutes=5)},
        {"role": "tool", "seq": 903, "text": "", "thinking": "", "tool_calls": [], "tool_results": results, "created_at": ago(minutes=4)},
        {"role": "assistant", "seq": 904, "text": "Set: an hourly check of the server.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
    ]


def open_page(context, host: ScheduleStub, path: str, wait: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()

    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        if rel == f"/api/sessions/{S1}" and r.request.method == "GET":
            return respond(r, {**detail(S1), "messages": turn_messages()})
        body = json.loads(r.request.post_data or "{}") if r.request.method in ("POST", "PATCH", "PUT") else None
        answered = host.answer(r.request.method, rel, body)
        if answered is not None:
            return respond(r, answered[1], status=answered[0])
        return stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}{path}?token=t&scheme={SCHEME}&lang={LANG}")
    page.wait_for_selector(wait, timeout=15000)
    page.wait_for_timeout(400)
    return page


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def sideways(page: Page) -> int:
    return int(page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"))


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    host = ScheduleStub(LANG, chat=S1, other=S4)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=SCHEME)
    page = open_page(context, host, "/schedules", ".schedbrowser .schedrow")
    groups = page.locator(".schedlist-title").evaluate_all("(els) => els.map((el) => el.firstChild.textContent.trim())")
    print("groups:", groups)
    if groups != WORDS["groups"]:
        problems.append(f"the groups are not needs approval, active, paused, done in that order: {groups}")
    listed = page.locator(".schedlist").inner_text()
    raw = [cron for cron in RAW_CRONS if cron in listed]
    if raw:
        problems.append(f"the list shows raw cron lines instead of the reader's time: {raw}")
    if WORDS["hourly"] not in listed:
        problems.append("the hourly schedule is not said as 'every hour at :MM'")
    if not page.locator(".schedrow .schedrow-last.failed").count():
        problems.append("the failed last run is not marked failed in its row")
    # The first row opens by itself on a wide screen: the waiting proposal.
    opened = page.locator(".scheddetail").get_attribute("data-schedule")
    if opened != RESET:
        problems.append(f"the pane did not open the first row (the waiting proposal), it opened {opened}")
    shot(page, "schedules-desktop")

    page.locator(f".schedrow[data-schedule='{MINE}'] .schedrow-open").click()
    page.wait_for_selector(f".scheddetail[data-schedule='{MINE}']")
    if not page.url.rstrip("/").endswith(f"/schedules/{MINE}"):
        problems.append(f"the open schedule is not in the address: {page.url}")
    prompt = page.locator(".scheddetail-prompt")
    folded = prompt.bounding_box()
    if "folded" not in (prompt.get_attribute("class") or "") or not folded or folded["height"] > 160:
        problems.append(f"the long instruction is not folded ({folded})")
    page.get_by_role("button", name=WORDS["more"]).click()
    opened_box = prompt.bounding_box()
    if not opened_box or not folded or opened_box["height"] <= folded["height"] or opened_box["height"] > 460:
        problems.append(f"Show all does not open the instruction to a bounded height ({opened_box})")
    rows = page.locator(".schedhistory-row").count()
    if rows < 3:
        problems.append(f"the recent runs are not listed ({rows})")
    shot(page, "schedules-detail")

    page.get_by_role("button", name=WORDS["pause"], exact=True).click()
    page.wait_for_timeout(400)
    patches = [w for w in host.writes if w[0] == "PATCH"]
    if not patches or patches[0][2].get("enabled") is not False or patches[0][2].get("expected_collection_revision") != 9:
        problems.append(f"Pause did not post the revision shown: {patches}")

    page.locator(f".schedrow[data-schedule='{RESET}'] button", has_text=WORDS["approve"]).click()
    page.wait_for_timeout(500)
    accepts = [w for w in host.writes if w[1].endswith("/accept")]
    if not accepts:
        problems.append("Approve in the proposal's row did not post")
    else:
        expires = datetime.fromisoformat(accepts[0][2]["expires_at"].replace("Z", "+00:00"))
        if (expires - datetime.now(UTC)).days < 3000 or accepts[0][2].get("request_digest") != "reset-digest":
            problems.append(f"the Accept did not send the exact proposal with the standing horizon: {accepts[0][2]}")
    if sideways(page) > 0:
        problems.append("the desktop screen scrolls sideways")
    context.close()

    # The chat card's link: the dialog, on this chat's schedules, with that schedule open.
    host = ScheduleStub(LANG, chat=S1, other=S4)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=SCHEME)
    page = open_page(context, host, f"/agents/{S1}", f".schedcard[data-schedule='{MINE}']")
    page.locator(f".schedcard[data-schedule='{MINE}'] button[data-action='open']").click()
    page.wait_for_selector(".schedules-dialog .scheddetail", timeout=10000)
    page.wait_for_timeout(300)
    box = page.locator(".schedules-dialog").bounding_box()
    print("dialog:", box)
    if not box or box["width"] < 1000 or box["height"] < 640 or abs(box["x"] + box["width"] / 2 - 720) > 2:
        problems.append(f"the dialog is not a wide centred window ({box})")
    pane = page.locator(".schedules-dialog .schedbrowser-detail").bounding_box()
    if not pane or pane["width"] < 600:
        problems.append(f"the dialog's schedule pane is squeezed beside the list ({pane})")
    keys = page.locator(".schedules-dialog .schedrow").evaluate_all("(els) => els.map((el) => el.dataset.schedule)")
    print("this chat:", keys)
    if sorted(keys) != sorted([RESET, MINE, "s3c4d5e6f7081223", "s6f7081223344556"]):
        problems.append(f"the dialog does not open on this chat's schedules: {keys}")
    if page.locator(".schedules-dialog .scheddetail").get_attribute("data-schedule") != MINE:
        problems.append("the dialog does not open the schedule the card named")
    shot(page, "schedules-dialog")
    page.locator(".schedules-dialog .schedbrowser-scope").get_by_role("radio", name=WORDS["all"]).click()
    every = page.locator(".schedules-dialog .schedrow").count()
    if every != 7:
        problems.append(f"All does not list every schedule ({every})")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if page.locator(".schedules-dialog").count():
        problems.append("Escape does not close the dialog")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    host = ScheduleStub(LANG, chat=S1, other=S4)
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=SCHEME, is_mobile=True, has_touch=True)
    page = open_page(context, host, "/schedules", ".schedbrowser .schedrow")
    if page.locator(".scheddetail").count():
        problems.append("phone: the screen opens a schedule instead of the list")
    if sideways(page) > 0:
        problems.append("phone: the list scrolls sideways")
    shot(page, "schedules-phone")
    page.locator(f".schedrow[data-schedule='{MINE}'] .schedrow-open").click()
    page.wait_for_selector(f".scheddetail[data-schedule='{MINE}']")
    if page.locator(".schedbrowser-list").count():
        problems.append("phone: the list stays beside the open schedule")
    if not page.locator(".scheddetail-back").count():
        problems.append("phone: the open schedule has no way back")
    if sideways(page) > 0:
        problems.append("phone: the open schedule scrolls sideways")
    shot(page, "schedules-phone-detail")
    page.locator(".scheddetail-back").click()
    page.wait_for_selector(".schedbrowser-list")
    context.close()

    host = ScheduleStub(LANG, chat=S1, other=S4)
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=SCHEME, is_mobile=True, has_touch=True)
    page = open_page(context, host, f"/agents/{S1}", f".schedcard[data-schedule='{MINE}']")
    page.locator(f".schedcard[data-schedule='{MINE}'] button[data-action='open']").click()
    page.wait_for_selector(".schedules-dialog .scheddetail", timeout=10000)
    page.wait_for_timeout(300)
    box = page.locator(".schedules-dialog").bounding_box()
    if not box or box["width"] < 389 or box["height"] < 840:
        problems.append(f"phone: the dialog is not a full-screen sheet ({box})")
    if sideways(page) > 0:
        problems.append("phone: the dialog scrolls sideways")
    shot(page, "schedules-phone-dialog")
    context.close()
    return problems


def run() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        problems = desktop(browser) + phone(browser)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
