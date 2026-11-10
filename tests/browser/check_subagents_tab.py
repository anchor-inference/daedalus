"""The Subagents tab draws each helper as a card that says everything about it at a glance.

Each card carries its name and a state chip, what a working helper is doing now with its step, its
task folded to two lines that unfold on a click, its model with the provider's mark and the effort,
its spend and its context, and the report of a finished one with whether the leader has it. Over the
cards a line counts the working, finished and failed helpers and sums their cost; the working ones come
first. A session without helpers says what will appear. Checked in both languages, light and dark, on a
desktop and on a phone's sheet, where nothing may run off the side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots  # noqa: E402
from api_stub import DEFAULT_APP  # noqa: E402
from screenshots import S1, S2, S12, S13, S14, ago, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

ASKS, PARKED, STOPPED = "8c3d4e5f6a7b", "9d4e5f6a7b8c", "ae5f6a7b8c9d"
LONG_BRIEF = (
    "Rewrite the menu page copy in the bakery's own voice: keep every price and allergen note exactly as it is, "
    "shorten each description to one sentence, and list the three items whose names you would change, with why, "
    "at the end of your report rather than changing them yourself."
)

WORDS = {
    "en": {"summary": ["3 running", "1 done", "1 failed"], "working": "working", "question": "has a question", "parked": "parked",
           "done": "done", "failed": "failed", "stopped": "stopped", "step": "step 14", "delivered": "Report delivered", "held": "report waiting",
           "empty": "No subagents yet", "effort": "high"},
    "ru": {"summary": ["работают: 3", "готово: 1", "сбоев: 1"], "working": "работает", "question": "задал вопрос", "parked": "на паузе",
           "done": "готово", "failed": "сбой", "stopped": "остановлен", "step": "шаг 14", "delivered": "Отчёт доставлен", "held": "отчёт ждёт",
           "empty": "Субагентов пока нет", "effort": "высокая"},
}

plain_subagents_of = screenshots.subagents_of


def helper(id_: str, name: str, status: str, **over: object) -> dict:
    base = {
        "session_id": id_, "name": name, "running": False, "status": status, "model": "claude/claude-opus-5-5", "provider": "claude", "effort": "xhigh",
        "kept": True, "title": "", "brief": "", "idle_start": False, "created_at": ago(minutes=40), "last_activity_at": ago(minutes=2),
        "run_started_at": None, "finished_at": None, "activity": None, "reports_held": 0, "parked": None,
        "usage": {"calls": 31, "input": 2_400_000, "output": 41_000, "cache_read": 2_100_000, "usd": 0.0, "unpriced": 0},
        "context": {"tokens": 164_000, "window": 200_000}, "report": None,
    }
    base.update(over)
    return base


def rich_subagents_of(leader: str) -> list[dict]:
    if leader != S1:
        return plain_subagents_of(leader)
    return plain_subagents_of(leader) + [
        helper(ASKS, "menu-copy", "waiting", title=LONG_BRIEF[:80], brief=LONG_BRIEF, run_started_at=ago(minutes=11), created_at=ago(minutes=12)),
        helper(PARKED, "seo-pass", "parked", parked={"reason": "budget", "at": ago(minutes=20)}, reports_held=1, provider="openai", model="openai/gpt-5.6", effort="medium",
               brief="Check every page's title and description", created_at=ago(minutes=50),
               usage={"calls": 12, "input": 310_000, "output": 9_800, "cache_read": 250_000, "usd": 1.84, "unpriced": 0}, context={"tokens": 61_000, "window": 400_000},
               report={"at": ago(minutes=20), "cause": "budget", "summary": "", "delivered": False, "delivered_at": None}),
        helper(STOPPED, "translate-ru", "idle", brief="Translate the menu into Russian", finished_at=ago(hours=3), last_activity_at=ago(hours=3), created_at=ago(hours=4),
               report={"at": ago(hours=3), "cause": "cancelled", "summary": "", "delivered": True, "delivered_at": ago(hours=3)}),
    ]


def scene() -> None:
    screenshots.subagents_of = rich_subagents_of


def look(page: Page, lang: str, scheme: str, phone: bool, problems: list[str]) -> None:
    words = WORDS[lang]
    tag = f"{lang}-{scheme}-{'phone' if phone else 'desktop'}"
    shots = os.environ.get("SHOTS")
    page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}&scheme={scheme}&panel=subagents", wait_until="networkidle")
    scope = page.locator(".panel-sheet" if phone else ".panel")
    expect(scope.locator('.panel-tab[data-tab="subagents"].on')).to_be_visible()
    page.wait_for_timeout(300)
    if shots:
        page.screenshot(path=f"{shots}/subagents-tab-{tag}.png")
    summary = scope.locator("[data-subagents-summary]")
    for word in words["summary"]:
        expect(summary).to_contain_text(word)
    expect(summary.locator(".subagents-total")).to_contain_text("$2.27")
    cards = scope.locator(".subagent-card")
    expect(cards).to_have_count(6)
    # Working ones first, the newest started first; then the finished, the latest to end first.
    order = cards.evaluate_all("cards => cards.map(c => c.dataset.subagent)")
    if order != [ASKS, PARKED, S12, S13, S14, STOPPED]:
        problems.append(f"{tag}: the cards are in the order {order}")
    phases = {S12: "working", ASKS: "question", PARKED: "parked", S13: "done", S14: "failed", STOPPED: "stopped"}
    for id_, phase in phases.items():
        expect(scope.locator(f'.subagent-card[data-subagent="{id_}"] .subagent-chip[data-phase="{phase}"]')).to_contain_text(words[phase])
    running = scope.locator(f'.subagent-card[data-subagent="{S12}"]')
    expect(running.locator("[data-subagent-now]")).to_contain_text("WebFetch")
    expect(running.locator("[data-subagent-now]")).to_contain_text(words["step"])
    expect(running.locator(".subagent-actions .subagent-stop")).to_have_count(1)
    done = scope.locator(f'.subagent-card[data-subagent="{S13}"]')
    expect(done.locator(".subagent-effort")).to_contain_text(words["effort"])
    expect(done.locator(".provider-mark")).to_have_count(1)
    expect(done.locator("[data-subagent-report]")).to_contain_text("photos.json")
    expect(done.locator(".subagent-delivery.ok")).to_contain_text(words["delivered"])
    expect(done.locator(".subagent-spend")).to_contain_text("$0.41")
    expect(done.locator(".subagent-context .subagent-bar")).to_have_count(1)
    parked = scope.locator(f'.subagent-card[data-subagent="{PARKED}"]')
    expect(parked).to_contain_text(words["held"])
    expect(parked.locator(".subagent-delivery.wait")).to_have_count(1)
    # The task folds to two lines and unfolds in place.
    brief = scope.locator(f'.subagent-card[data-subagent="{ASKS}"] .subagent-brief')
    folded = brief.evaluate("el => el.getBoundingClientRect().height")
    brief.click()
    expect(brief).to_have_attribute("aria-expanded", "true")
    unfolded = brief.evaluate("el => el.getBoundingClientRect().height")
    if unfolded <= folded:
        problems.append(f"{tag}: the task did not unfold ({folded:.0f}px -> {unfolded:.0f}px)")
    if shots:
        brief.scroll_into_view_if_needed()
        page.screenshot(path=f"{shots}/subagents-tab-unfolded-{tag}.png")
    # Nothing runs past the panel's side.
    over = scope.locator(".subagents").evaluate(
        "root => [...root.querySelectorAll('.subagent-card *')].filter(el => { const r = el.getBoundingClientRect(), b = el.closest('.subagent-card').getBoundingClientRect();"
        " return r.width > 0 && (r.right > b.right + 0.5 || r.left < b.left - 0.5); }).map(el => el.className).slice(0, 5)"
    )
    if over:
        problems.append(f"{tag}: something runs past its card: {over}")
    if phone:
        wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if wide > 0:
            problems.append(f"{tag}: the page scrolls sideways by {wide}px")
    # The head opens the conversation in the tab.
    running.locator(".subagent-card-head").click()
    expect(scope.locator(".subagent-open .subagent-head")).to_contain_text("link-check")
    # A session without helpers says what will appear here.
    page.goto(f"{BASE}/agents/{S2}?token=t&lang={lang}&scheme={scheme}&panel=subagents", wait_until="networkidle")
    expect(scope.locator(".subagents-empty")).to_contain_text(words["empty"])
    if shots:
        page.screenshot(path=f"{shots}/subagents-tab-empty-{tag}.png")


def run() -> int:
    scene()
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for scheme in ("dark", "light"):
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
