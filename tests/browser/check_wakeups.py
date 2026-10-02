"""The orchestrator's wake-ups and the project's watches, in the right panel of focus mode and as a
page of their own on a phone, in both languages.

What is checked is what the operator relies on: every wake-up shows its note, when it fires and who
set it; one can be cancelled from its row; a new one is left with a note and a time, and the request
carries exactly one way of saying when — minutes from now, a moment with its offset, or a UTC cron
line for "every day". Every watch reads as "when X → Y" with its cooldown and how often it fired, one
that switched itself off says why, a watch is switched off and removed from its row, and a new one is
sent with only the fields its kind has. On a phone the page has no sideways scroll and its fields are
big enough not to make Safari zoom.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

WORDS = {
    "en": {
        "tab": "Wake-ups", "add": "Wake-up", "note": "Look at the deploy log", "set": "Set", "by_orch": "set by the orchestrator", "by_you": "left by you", "daily": "Every day", "cancel": "Cancel this wake-up",
        "watch": "Watch", "max": "When Max finishes a turn → wake the orchestrator", "budget": "fired too often within an hour", "off": "Switch this watch off", "remove": "Remove this watch",
        "kind": "A CI result", "notify": "Notify me", "add_watch": "Add", "cooldown": "at most every 10 min",
    },
    "ru": {
        "tab": "Будильники", "add": "Будильник", "note": "Посмотреть журнал выкладки", "set": "Поставить", "by_orch": "поставил оркестратор", "by_you": "оставили вы", "daily": "Каждый день", "cancel": "Отменить будильник",
        "watch": "Наблюдение", "max": "Max: ход закончен → разбудить оркестратор", "budget": "слишком часто срабатывало за час", "off": "Выключить наблюдение", "remove": "Снять наблюдение",
        "kind": "Результат CI", "notify": "Уведомить меня", "add_watch": "Добавить", "cooldown": "не чаще раза в 10 мин",
    },
}


def panel(page: Page, lang: str) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{BASE}/project/{PID}?token=t&lang={lang}&panel=wakeups")
    tab = page.locator(".panel .panel-tab[data-tab='wakeups']")
    if tab.count():
        tab.click()
    row = page.locator(".panel .wakeup-row").first
    expect(row).to_contain_text(invented["wake.note"])
    expect(row).to_contain_text(words["by_orch"])

    # A new wake-up in minutes: the request says only that.
    page.locator(".panel .wakeup-section-head button", has_text=words["add"]).click()
    sheet = page.locator(".sheet.wakeup-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("#wakeup-note").fill(words["note"])
    sheet.locator("#wakeup-minutes").fill("45")
    sheet.locator(".sheet-foot .btn.primary", has_text=words["set"]).click()
    expect(sheet).to_have_count(0)
    assert focus.woken[-1] == {"note": words["note"], "in_minutes": 45}, focus.woken
    expect(page.locator(".panel .wakeup-row", has_text=words["note"])).to_contain_text(words["by_you"])

    # Every day at a time: a five-field cron line, never the local clock sent as if it were UTC.
    page.locator(".panel .wakeup-section-head button", has_text=words["add"]).click()
    sheet.locator("#wakeup-note").fill(words["note"] + " 2")
    sheet.locator(".chip.select", has_text=words["daily"]).click()
    sheet.locator("#wakeup-daily").fill("09:30")
    sheet.locator(".sheet-foot .btn.primary").click()
    expect(sheet).to_have_count(0)
    sent = focus.woken[-1]
    assert set(sent) == {"note", "cron"} and len(sent["cron"].split()) == 5 and sent["cron"].endswith("* * *"), sent
    # Closing the form precedes the list's refetch; count only after the new row has landed.
    expect(page.locator(".panel .wakeup-row", has_text=words["note"] + " 2")).to_be_visible()

    # Cancelled from its row.
    count = page.locator(".panel .wakeup-row").count()
    page.locator(".panel .wakeup-row", has_text=invented["wake.note"]).get_by_role("button", name=words["cancel"]).click()
    expect(page.locator(".panel .wakeup-row")).to_have_count(count - 1)
    assert all(w["id"] != "wk1" for w in focus.wakeups)
    fits(page, f"{lang} wake-ups panel")

    # The watches: each a sentence with its bounds; the one that stopped itself says why.
    rows = page.locator(".panel .watch-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text(words["max"])
    expect(rows.nth(0)).to_contain_text(words["cooldown"])
    expect(rows.nth(1)).to_contain_text(words["budget"])
    rows.nth(0).get_by_role("button", name=words["off"]).click()
    expect(rows.nth(0)).to_have_class(re.compile(r"\boff\b"))
    assert focus.watched[-1] == ("update", {"id": "w1", "enabled": False}), focus.watched

    # A new watch sends only what its kind has.
    page.locator(".panel .wakeup-section-head button", has_text=words["watch"]).click()
    sheet = page.locator(".sheet.watch-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("#watch-kind").select_option(label=words["kind"])
    sheet.locator("#watch-conclusion").fill("failure")
    sheet.locator(".chip.select", has_text=words["notify"]).click()
    sheet.locator("#watch-title").fill("CI red")
    sheet.locator("#watch-cooldown").fill("30")
    sheet.locator(".sheet-foot .btn.primary", has_text=words["add_watch"]).click()
    expect(sheet).to_have_count(0)
    assert focus.watched[-1] == ("create", {"when": {"event": "ci", "provider": "github", "conclusion": "failure"}, "then": {"action": "notify", "title": "CI red", "text": "", "level": "normal"}, "cooldown_minutes": 30, "once": False, "note": ""}), focus.watched
    expect(rows).to_have_count(3)
    rows.nth(1).get_by_role("button", name=words["remove"]).click()
    expect(rows).to_have_count(2)
    assert focus.watched[-1] == ("delete", {"id": "w2"}), focus.watched
    fits(page, f"{lang} watches panel")


def phone(page: Page, lang: str) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{BASE}/project/{PID}/wakeups?token=t&lang={lang}")
    expect(page.locator(".wakeup-row").first).to_contain_text(invented["wake.note"])
    fits(page, f"{lang} phone wake-ups")
    page.locator(".wakeup-section-head button", has_text=words["add"]).click()
    sheet = page.locator(".sheet.wakeup-sheet")
    expect(sheet).to_be_visible()
    for field in ("#wakeup-note", "#wakeup-minutes"):
        size = page.evaluate(f"parseFloat(getComputedStyle(document.querySelector('{field}')).fontSize)")
        assert size >= 16, f"{lang} phone: {field} is {size}px, which makes Safari zoom"
    fits(page, f"{lang} phone wake-up sheet")
    sheet.locator(".sheet-foot .btn.ghost").click()
    expect(sheet).to_have_count(0)
    page.locator(".wakeup-section-head button", has_text=words["watch"]).click()
    sheet = page.locator(".sheet.watch-sheet")
    expect(sheet).to_be_visible()
    for field in ("#watch-kind", "#watch-staff", "#watch-cooldown", "#watch-note"):
        size = page.evaluate(f"parseFloat(getComputedStyle(document.querySelector('{field}')).fontSize)")
        assert size >= 16, f"{lang} phone: {field} is {size}px, which makes Safari zoom"
    fits(page, f"{lang} phone watch sheet")


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(device_scale_factor=1)
            panel(context.new_page(), lang)
            context.close()
            context = browser.new_context(device_scale_factor=2, is_mobile=True, has_touch=True)
            phone(context.new_page(), lang)
            context.close()
        browser.close()
    print("wake-ups: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
