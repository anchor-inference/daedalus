"""A helper the daily cap parked stays in sight: in the leader's chat and in its Subagents tab.

The leader's chat carries the host's line about it with the way out under it, unfolded, and the
helper's row reads "parked by the daily cap" with a report waiting rather than failed or gone. Checked
in both languages, on a desktop and a phone, light and dark; on the phone nothing scrolls sideways.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots  # noqa: E402
from api_stub import DEFAULT_APP  # noqa: E402
from screenshots import S1, S14, ago, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"title": "Subagent price-audit is parked by the daily cap", "way": "Paid runs resume at 00:00 UTC", "row": "parked by the daily cap", "held": "report waiting"},
    "ru": {"title": "Субагент price-audit на паузе: дневной лимит", "way": "Платные запуски продолжатся в 00:00 UTC", "row": "на паузе: дневной лимит", "held": "отчёт ждёт"},
}

NOTICE = {
    "role": "user", "seq": 999, "origin": "subagent:price-audit", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=1),
    "text": "Subagent 'price-audit' is parked by the daily spending cap. It goes on by itself once the daily cap reopens at 00:00 UTC, and its report follows.",
    "subagent_notice": {"state": "parked", "cause": "budget", "name": "price-audit", "session_id": S14, "detail": "", "budget": True, "held": True},
}

plain_subagents_of = screenshots.subagents_of


def parked_subagents_of(leader: str) -> list[dict]:
    out = plain_subagents_of(leader)
    for child in out:
        if child["session_id"] == S14:
            child.update(status="parked", parked={"reason": "budget", "at": ago(minutes=1)}, reports_held=1)
    return out


def scene() -> None:
    if NOTICE not in screenshots.MESSAGES:
        screenshots.MESSAGES.append(NOTICE)
    screenshots.subagents_of = parked_subagents_of


def look(page: Page, lang: str, scheme: str, phone: bool, problems: list[str]) -> None:
    words = WORDS[lang]
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&scheme={scheme}", wait_until="networkidle")
    note = page.locator(".sysnote.subagent.notice").last
    expect(note).to_be_visible()
    expect(note.locator(".sysnote-head")).to_contain_text(words["title"])
    expect(note.locator(".sysnote-way")).to_contain_text(words["way"])
    note.scroll_into_view_if_needed()
    shots = os.environ.get("SHOTS")
    tag = f"{lang}-{scheme}-{'phone' if phone else 'desktop'}"
    if shots:
        page.screenshot(path=f"{shots}/subagent-parked-chat-{tag}.png")
    if phone:
        wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if wide > 0:
            problems.append(f"{tag}: the chat scrolls sideways by {wide}px")
        return
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&scheme={scheme}&panel=subagents", wait_until="networkidle")
    row = page.locator(f'.panel .subagent-card[data-subagent="{S14}"]')
    expect(row).to_contain_text(words["row"])
    expect(row).to_contain_text(words["held"])
    expect(row.locator(".sub-dot.parked")).to_have_count(1)
    if shots:
        page.screenshot(path=f"{shots}/subagent-parked-panel-{tag}.png")


def run() -> int:
    scene()
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for scheme in ("light", "dark"):
                for phone in (False, True):
                    page = browser.new_page(viewport={"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}, is_mobile=phone, has_touch=phone)
                    page.route("**/api/**", stub)
                    try:
                        look(page, lang, scheme, phone, problems)
                    except Exception as exc:  # noqa: BLE001 — every scene is looked at; the failures are listed together
                        problems.append(f"{lang}-{scheme}-{'phone' if phone else 'desktop'}: {exc}")
                    page.close()
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(run())
