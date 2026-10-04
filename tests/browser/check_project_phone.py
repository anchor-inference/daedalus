"""A project on a phone, at 320, 390 and 400 px with a touch screen, in both languages.

The project's three tabs take the place of the app's own; contextual tools stay behind the header.
The header names
the project, says how its work goes and leads back to orchestration's list of projects, where the app's
own tabs return.
The request that has waited longest for the operator is a banner answered with one tap — an option, or
words of the operator's own — and the next one takes its place until none is left. The team lists its
members with what each is on and opens a member's conversation, which gives the whole height to it and
goes back to the team. The board is the list under chips, and a task in review is accepted (merged, for a
staff branch) with one tap. The terminals are rows that open the phone's terminal. The orchestrator's composer ends above the
tabs. Every answer is a touch row high, and nothing scrolls sideways.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

WORDS = {
    "en": {"tabs": ["Orchestrator", "Needs decision", "History"], "needs": "Needs you", "ira": "Ira asks:", "orchestrator": "The orchestrator asks:", "write": "Answer…", "head": "3 working · 1 in review",
           "own": "Your own answer…", "send": "Answer", "working": "working", "review": "Review", "merge": "Merge", "board": "Board · Bakery 2.0"},
    "ru": {"tabs": ["Оркестратор", "Требуют решения", "История"], "needs": "Нужны вы", "ira": "Ira спрашивает:", "orchestrator": "Оркестратор спрашивает:", "write": "Ответить…", "head": "3 работают · 1 на проверке",
           "own": "Свой ответ…", "send": "Ответить", "working": "работает", "review": "Проверка", "merge": "Слить", "board": "Доска · Bakery 2.0"},
}


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, focus: FocusStub) -> None:
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = focus.answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return installation(route)

    page.route("**/api/**", handle)


def tall_enough(page: Page, selector: str, where: str) -> None:
    for box in [b.bounding_box() for b in page.locator(selector).all()]:
        assert box and box["height"] >= 43.5, f"{where}: {selector} is {box and box['height']}px high, not a touch row"


def tabs(page: Page, words: dict, active: str | None) -> None:
    bar = page.locator("nav.project-tabs")
    expect(bar).to_be_visible()
    expect(page.locator("nav.tabbar:not(.project-tabs)")).to_have_count(0)
    # While the bar's code is still loading, App.tsx holds its place with an empty bar of the same
    # class, so the tabs are waited for rather than read at once from that placeholder.
    expect(bar.locator("a")).to_have_count(len(words["tabs"]))
    labels = [x.strip() for x in bar.locator("a").all_inner_texts()]
    assert [lbl.split("\n")[-1] for lbl in labels] == words["tabs"], labels
    current = bar.locator("a.active")
    if active is None:
        expect(current).to_have_count(0)
    else:
        expect(current).to_have_attribute("data-tab", active)


def run_one(page: Page, lang: str, width: int) -> None:
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    focus.ask_from_ira(lang)
    serve(page, focus)
    where = f"{lang} {width}"

    # The team: the header, the tabs, the banner with Ira's question (the oldest), the members.
    page.goto(f"{BASE}/project/{PID}/team?token=t&lang={lang}")
    expect(page.locator(".pagehead h1")).to_have_text("Bakery 2.0")
    expect(page.locator(".pagehead .sub")).to_have_text(words["head"])
    tabs(page, words, None)
    banner = page.locator(".needs-banner")
    expect(banner).to_have_attribute("data-ask", "q9w2e1")
    expect(banner).to_contain_text(words["needs"])
    expect(banner).to_contain_text(words["ira"])
    expect(page.locator("nav.project-tabs a[data-tab='attention'] .tab-badge")).to_have_text("2")
    tall_enough(page, ".needs-banner .ask-answers-row .btn", where)
    items = page.locator(".phone-staff-item")
    expect(items).to_have_count(6)
    expect(items.filter(has_text="Olga")).to_contain_text(invented["olga.role"])
    tall_enough(page, ".phone-staff-row", where)
    fits(page, f"{where} team")

    # The board leaves out the request the banner above it already shows: the same question twice, in
    # two different sets of controls, took the top half of the screen. The orchestrator's stays listed.
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    page.wait_for_url(f"**/project/{PID}/board**")
    expect(page.locator(".needs-banner")).to_have_attribute("data-ask", "q9w2e1")
    needs = page.locator(".pboard-list .pcard.need")
    expect(needs).to_have_count(1)
    expect(needs.locator(".pcard-short")).to_have_text("q4r8tz")
    page.goto(f"{BASE}/project/{PID}/team?token=t&lang={lang}")
    page.wait_for_url(f"**/project/{PID}/team**")
    expect(banner).to_have_attribute("data-ask", "q9w2e1")

    # One tap answers Ira; the orchestrator's own question takes the banner, and is answered in words.
    banner.locator(".ask-answers-row .btn", has_text=invented["ask.before"]).tap()
    expect(banner).to_have_attribute("data-ask", "q4r8tz")
    assert focus.answers[-1] == ("ask-ira", {"selected": [invented["ask.before"]]}), focus.answers
    expect(banner).to_contain_text(words["orchestrator"])
    banner.locator(".ask-answers-row .btn", has_text=words["write"]).tap()
    field = banner.locator(".ask-answers-own .field")
    expect(field).to_be_focused()
    size = field.evaluate("(e) => parseFloat(getComputedStyle(e).fontSize)")
    assert size >= 16, f"{where}: the answer field is {size}px, and Safari would zoom into it"
    field.fill("after, like last spring")
    banner.locator(".ask-answers-own .btn.primary").tap()
    expect(page.locator(".needs-banner")).to_have_count(0)
    assert focus.answers[-1] == ("ask-spring", {"text": "after, like last spring"}), focus.answers
    expect(page.locator("nav.project-tabs a[data-tab='attention'] .tab-badge")).to_have_count(0)

    # A member with a conversation opens it without the tabs, and its back returns to the team.
    items.filter(has_text="Lev").locator(".phone-staff-row").tap()
    page.wait_for_url(f"**/project/{PID}/s/sess-lev**")
    expect(page.locator(".chat.in-project")).to_be_visible()
    expect(page.locator("nav.project-tabs")).to_have_count(0)
    fits(page, f"{where} member")
    page.locator(".chat-head .iconbtn").first.tap()
    page.wait_for_url(f"**/project/{PID}/team**")

    # The board remains a contextual project tool, under chips.
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    page.wait_for_url(f"**/project/{PID}/board**")
    tabs(page, words, None)
    expect(page.locator(".pagehead h1")).to_have_text(words["board"])
    expect(page.locator(".pboard-cols")).to_have_count(0)
    page.locator(".pboard-chips .chip", has_text=words["review"]).tap()
    # A branch result cannot be accepted without the exact current receipt and verdict.
    page.locator(".pboard-list .pcard").tap()
    expect(page.locator(".sheet.pboard-sheet .result-flow button", has_text="Accept" if lang == "en" else "Принять")).to_be_disabled()
    assert not focus.board.accepted
    fits(page, f"{where} board")

    # The terminals: rows that lead to the phone's terminal.
    page.goto(f"{BASE}/project/{PID}/terminals?token=t&lang={lang}")
    page.wait_for_url(f"**/project/{PID}/terminals**")
    tabs(page, words, None)
    rows = page.locator(".phone-term")
    expect(rows).to_have_count(3)
    expect(rows.first).to_have_attribute("href", "/app/terminals/tm-ira")
    tall_enough(page, ".phone-term", where)
    fits(page, f"{where} terminals")

    # The orchestrator: its chat, its composer above the tabs.
    page.locator("nav.project-tabs a[data-tab='orchestrator']").tap()
    page.wait_for_selector(".chat.in-project .event-card", timeout=10000)
    tabs(page, words, "orchestrator")
    composer = page.locator(".composer").bounding_box()
    bar = page.locator("nav.project-tabs").bounding_box()
    assert composer and bar and composer["y"] + composer["height"] <= bar["y"] + 0.5, (composer, bar)
    fits(page, f"{where} orchestrator")

    # A page behind the header's menu keeps the tabs with none lit.
    page.goto(f"{BASE}/project/{PID}/journal?token=t&lang={lang}")
    page.wait_for_selector(".journal-entry", timeout=10000)
    tabs(page, words, "journal")

    # The header's back leaves the project for orchestration's list, and the app's own tabs come back.
    page.goto(f"{BASE}/project/{PID}/team?token=t&lang={lang}")
    page.locator(".pagehead a.iconbtn[href]").first.tap()
    page.wait_for_url("**/app/orchestration/projects**")
    expect(page.locator("nav.project-tabs")).to_have_count(0)
    expect(page.locator("nav.tabbar")).to_be_visible()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (320, 390, 400):
                context = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
                run_one(context.new_page(), lang, width)
                context.close()
        browser.close()
    print("project on the phone: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
