"""The goal line over the orchestrator's composer, at 1280 and 390 px, in both languages.

What the operator relies on: one line under the conversation that names what is going on in the
project — the work in hand, the decisions the orchestrator owes, what waits for the operator, what no
member confirmed, what was promised — with only the counts that are not zero, on one row that does
not cut the last of them off. Pressed, it opens the list behind the counts: every card with its owner,
acceptance and what it waits on (the checkout, blocked on the operator's answer, first); the results
waiting for the orchestrator's decision, oldest first, each saying who reported what on which card;
and the promises. Escape closes it. Nothing scrolls sideways.

    cd miniapp && npx vite build --outDir /tmp/app-dist
    mkdir -p /tmp/app-root && ln -s /tmp/app-dist /tmp/app-root/app
    python3 tests/browser/serve_app.py 8195 /tmp/app-root &
    APP_URL=http://127.0.0.1:8195/app python3 tests/browser/check_goal_strip.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from check_project_focus import fits, serve  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
PID = "b4k3ry20f0c5"

WORDS = {
    "en": {
        "long": ["in work: 5", "decisions needed: 2", "waiting for you: 1", "not confirmed: 2", "promised: 2"],
        "short": ["5 in work", "to decide 2", "for you 1", "unconfirmed 2", "promised 2"],
        "goals": "Work in hand", "results": "Waiting for the orchestrator's decision", "commitments": "Promised to you",
        "waits": "waits on:", "decision": "the orchestrator's decision", "handed": "handed in",
        "lev": "Lev needs input on", "max": "Max reported done on", "reminded": "reminded",
    },
    "ru": {
        "long": ["в работе: 5", "нужны решения: 2", "ждут вас: 1", "не подтверждено: 2", "обещано: 2"],
        "short": ["в работе 5", "решить 2", "вам 1", "не подтверждено 2", "обещано 2"],
        "goals": "В работе", "results": "Ждут решения оркестратора", "commitments": "Обещано вам",
        "waits": "ждёт:", "decision": "решения оркестратора", "handed": "сдано",
        "lev": "Lev: нужны данные", "max": "Max: сдано", "reminded": "напомнено",
    },
}


CLOSE = {"en": "Close the panel", "ru": "Закрыть панель"}


def one_row(strip, words: dict, where: str) -> None:  # type: ignore[no-untyped-def]
    """The counts on one row, none cut off: the long words where the strip is wide, the short where not."""
    wide = strip.evaluate("el => el.getBoundingClientRect().width") > 640
    shown = [c.inner_text().strip() for c in strip.locator(".goal-count").all()]
    assert shown == words["long" if wide else "short"], f"{where}: the line says {shown}"
    cut = strip.locator(".goal-counts").evaluate("el => el.scrollWidth - el.clientWidth")
    assert cut <= 1, f"{where}: the line cuts {cut}px of its counts off"


def check(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    phone = width < 1024
    where = f"{lang} {width}"
    focus = FocusStub.bakery(lang)
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = page.locator(".chat.in-project.orchestrator")
    strip = chat.locator(".goal-strip")
    expect(strip).to_be_visible(timeout=15000)

    # The line: the five counts, in their long words where the column is wide and their short ones where
    # it is not (a phone, or a desk with the panel open beside the chat), on one row.
    counts = strip.locator(".goal-count")
    expect(counts).to_have_count(5)
    line = strip.locator(".goal-line")
    one_row(strip, words, where)
    if not phone:
        # With the panel closed the conversation has a desk's width, and the line its long words.
        page.get_by_role("button", name=CLOSE[lang], exact=True).first.click()
        expect(page.locator(".panel.shown")).to_have_count(0)
        page.wait_for_timeout(300)
        assert strip.evaluate("el => el.getBoundingClientRect().width") > 640, f"{where}: the column stayed narrow with the panel closed"
        one_row(strip, words, f"{where} without the panel")
    box = line.bounding_box()
    assert box and box["height"] <= 44, f"{where}: the goal line is {box} — it should be one row"
    # A decision owed and the operator's own question edge it in amber.
    assert "attn" in (strip.get_attribute("class") or ""), f"{where}: the line is not marked as waiting on someone"
    # It sits between the conversation and the composer, above the field.
    field = chat.locator(".composer-box").bounding_box()
    assert field and box["y"] + box["height"] <= field["y"] + 1, f"{where}: the line {box} is not above the composer {field}"
    expect(line).to_have_attribute("aria-expanded", "false")
    fits(page, f"{where} line")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/goal-line-{lang}-{width}.png")

    # The list behind it.
    line.click()
    expect(line).to_have_attribute("aria-expanded", "true")
    listing = strip.locator(".goal-list")
    expect(listing).to_be_visible()
    sections = listing.locator(".goal-sec")
    expect(sections).to_have_count(3)
    expect(sections.nth(0).locator(".goal-sec-head")).to_contain_text(words["goals"])
    goals = sections.nth(0).locator(".goal-item")
    expect(goals).to_have_count(5)
    # The cards that wait on someone come first, and the checkout, blocked on the operator, leads.
    first = goals.nth(0)
    expect(first).to_have_attribute("data-task", "t-checkout")
    assert "blocked" in (first.get_attribute("class") or ""), f"{where}: the checkout is not drawn as blocked"
    expect(first.locator(".goal-title")).to_have_text(invented["task.checkout"])
    expect(first.locator(".goal-owner")).to_contain_text("Ira")
    expect(first.locator(".goal-next")).to_contain_text(words["waits"])
    expect(first.locator(".goal-next")).to_contain_text("[q4r8tz]")
    endpoint = goals.filter(has_text=invented["task.endpoint"])
    expect(endpoint.locator(".goal-accept")).to_have_text(words["handed"])
    expect(endpoint.locator(".goal-next")).to_contain_text(words["decision"])
    # A card nobody waits on is plain.
    expect(goals.filter(has_text=invented["task.bot"]).locator(".goal-next")).to_have_count(0)

    results = sections.nth(1)
    expect(results.locator(".goal-sec-head")).to_contain_text(words["results"])
    rows = results.locator(".goal-item")
    expect(rows).to_have_count(2)
    # Oldest first: Lev asked at 09:36, Max reported at 09:51.
    expect(rows.nth(0)).to_contain_text(words["lev"])
    expect(rows.nth(0)).to_contain_text(invented["task.photos"])
    expect(rows.nth(0)).to_contain_text(invented["result.lev"])
    expect(rows.nth(0).locator(".goal-accept")).to_have_text(words["reminded"])
    expect(rows.nth(1)).to_contain_text(words["max"])
    expect(rows.nth(1)).to_contain_text("3 files, tests green")
    for row in rows.all():
        assert row.locator(".goal-age").inner_text().strip(), f"{where}: an open result has no age"

    promised = sections.nth(2)
    expect(promised.locator(".goal-sec-head")).to_contain_text(words["commitments"])
    expect(promised.locator(".goal-item")).to_have_count(2)
    expect(promised).to_contain_text(invented["commit.gallery"])
    expect(promised).to_contain_text(invented["commit.holidays"])
    expect(promised.locator(".goal-item").nth(1).locator(".goal-owner")).to_contain_text(invented["task.hours"])

    # The open list stays inside the window and inside its own height, and nothing scrolls sideways.
    lbox = listing.bounding_box()
    assert lbox and lbox["x"] >= 0 and lbox["x"] + lbox["width"] <= width + 1, f"{where}: the list is outside the window: {lbox}"
    viewport = page.viewport_size or {"height": 900}
    assert lbox["height"] <= viewport["height"] * 0.5 + 2, f"{where}: the list is {lbox['height']}px tall on a {viewport['height']}px window"
    wide = listing.evaluate("el => [...el.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > window.innerWidth + 1).length")
    assert wide == 0, f"{where}: {wide} element(s) of the list reach past the window"
    fits(page, f"{where} list")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/goal-list-{lang}-{width}.png")

    page.keyboard.press("Escape")
    expect(listing).to_have_count(0)
    expect(line).to_have_attribute("aria-expanded", "false")


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in ((1280, 900), (390, 844)):
                phone = width < 1024
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone)
                page = context.new_page()
                check(page, lang, width)
                print(f"{lang} {width}: ok")
                context.close()
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
