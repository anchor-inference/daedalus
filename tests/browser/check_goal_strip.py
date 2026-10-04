"""The goal line over the orchestrator's composer, at 1280 and 390 px, in both languages.

The compact line names the operator's next decision. Pressed, it opens the work and result list:
each card's owner, acceptance and blocker; reports awaiting the orchestrator's decision; an accepted
result linked to its exact immutable report; and the promises. A changed revision or failed original
read is shown plainly. Nothing scrolls sideways.

    cd miniapp && npx vite build --outDir /tmp/app-dist
    mkdir -p /tmp/app-root && ln -s /tmp/app-dist /tmp/app-root/app
    python3 tests/browser/serve_app.py 8195 /tmp/app-root &
    APP_URL=http://127.0.0.1:8195/app python3 tests/browser/check_goal_strip.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import json
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
        "headline": "A decision is waiting for you", "action": "Open decision", "accepted": "Accepted results",
        "open_accepted": "Open exact result", "stale": "This accepted result has changed", "unavailable": "The exact result could not be confirmed",
        "goals": "Work in hand", "results": "Waiting for the orchestrator's decision", "commitments": "Promised to you",
        "waits": "waits on:", "decision": "the orchestrator's decision", "handed": "handed in",
        "lev": "Lev needs input on", "max": "Max reported done on", "reminded": "reminded",
    },
    "ru": {
        "headline": "Ждут вашего решения", "action": "Открыть решение", "accepted": "Принятые результаты",
        "open_accepted": "Открыть этот результат", "stale": "Этот принятый результат изменился", "unavailable": "Не удалось подтвердить именно этот результат",
        "goals": "В работе", "results": "Ждут решения оркестратора", "commitments": "Обещано вам",
        "waits": "ждёт:", "decision": "решения оркестратора", "handed": "сдано",
        "lev": "Lev: нужны данные", "max": "Max: сдано", "reminded": "напомнено",
    },
}


CLOSE = {"en": "Close the panel", "ru": "Закрыть панель"}


def one_row(strip, words: dict, where: str) -> None:  # type: ignore[no-untyped-def]
    """The compact summary stays legible above the composer at desk and phone widths."""
    line = strip.locator(".goal-line")
    expect(line.locator(".goal-headline")).to_have_text(words["headline"])
    expect(strip.locator(".goal-next-action")).to_have_text(words["action"])
    assert line.evaluate("el => el.scrollWidth - el.clientWidth") <= 1, f"{where}: the summary is cut off"


def check(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    phone = width < 1024
    where = f"{lang} {width}"
    focus = FocusStub.bakery(lang)
    pending_asks = focus.asks
    # With no direct question to open, the goal line still carries other project decisions.
    focus.asks = []
    serve(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = page.locator(".chat.in-project.orchestrator")
    strip = chat.locator(".goal-strip")
    expect(strip).to_be_visible(timeout=15000)

    # The compact line names the next decision; the expanded list carries the full work and result history.
    line = strip.locator(".goal-line")
    one_row(strip, words, where)
    if not phone:
        # The summary remains one line when the conversation gains the full desk width.
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
    page.wait_for_function("() => { const line = document.querySelector('.chat.in-project.orchestrator .goal-line'); const field = document.querySelector('.chat.in-project.orchestrator .composer-box'); return !!line && !!field && line.getBoundingClientRect().bottom <= field.getBoundingClientRect().top + 1; }")
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
    expect(sections).to_have_count(4)
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

    accepted = sections.nth(2)
    expect(accepted.locator(".goal-sec-head")).to_contain_text(words["accepted"])
    expect(accepted.locator(".goal-item")).to_have_count(1)
    expect(accepted).to_contain_text(invented["task.hero"])
    expect(accepted).to_contain_text("Olga")
    promised = sections.nth(3)
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

    # A row opens its own immutable report through the board, with the exact identity in the route.
    accepted.get_by_role("button", name=words["open_accepted"]).click()
    expect(page.locator(".result-original")).to_contain_text("Original worker report: image delivered")
    expect(page.locator(".result-original")).to_contain_text("Checked on desktop and phone.")
    expect(page.locator(".result-flow")).to_contain_text("hero.png")
    assert "result=res-hero" in page.url and "revision=1" in page.url and "attempt=attempt-hero" in page.url, page.url
    exact_url = page.url
    page.goto(exact_url.replace("revision=1", "revision=2"))
    expect(page.locator(".result-flow [role=alert]")).to_contain_text(words["stale"])
    expect(page.locator(".result-original")).to_have_count(0)
    def newer_result(route) -> None:  # type: ignore[no-untyped-def]
        status, payload = focus.board.answer("GET", "/api/board/t-hero/results", "", None)
        rows = json.loads(json.dumps(payload))
        rows[0]["current_result_id"] = "res-newer"
        route.fulfill(status=status, body=json.dumps(rows), content_type="application/json")
    page.route("**/api/board/t-hero/results", newer_result)
    page.goto(exact_url)
    expect(page.locator(".result-flow [role=alert]")).to_contain_text(words["stale"])
    expect(page.locator(".result-original")).to_have_count(0)
    page.unroute("**/api/board/t-hero/results", newer_result)
    page.route("**/api/board/t-hero/results/res-hero/original", lambda route: route.fulfill(status=503, body='{"detail":"unavailable"}', content_type="application/json"))
    page.goto(exact_url)
    expect(page.locator(".result-flow [role=alert]")).to_contain_text(words["unavailable"])
    expect(page.locator(".result-original")).to_have_count(0)

    # When a direct question arrives, its own actionable line replaces the generic goal alert.
    focus.asks = pending_asks
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    expect(page.locator(".chat.in-project.orchestrator .questions-line")).to_be_visible()
    expect(page.locator(".chat.in-project.orchestrator .goal-strip")).to_have_count(0)


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
