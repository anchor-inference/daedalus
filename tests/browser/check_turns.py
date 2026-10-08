"""Drive the turn rendering in a real browser and refuse what does not behave.

The turn's pure parts have unit tests (miniapp/src/turnview.test.ts); this is what only exists once
the turn is drawn over the invented installation the screenshots use: the folded line names the
work by family and opens into 28 px step rows with their durations, the file the turn sent is a card
under the answer and one click puts it in the panel's Preview tab, a step's file name
opens the same way, the loop agent's wake-up is a folded system note and not a bubble, and the
answer's row of actions copies the text.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_turns.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, S3, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def open_page(context, route: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/{route}{'&' if '?' in route else '?'}token=t&scheme=dark&lang=en")
    page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
    page.wait_for_timeout(500)
    return page


def heights(page: Page, sel: str) -> list[int]:
    return page.evaluate(f"() => [...document.querySelectorAll('{sel}')].map((el) => Math.round(el.getBoundingClientRect().height))")


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", permissions=["clipboard-read", "clipboard-write"])
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
    page = open_page(context, f"agents/{S1}")

    # Only the operator's message is a card; the answer is plain.
    if page.locator(".msg.user").count() != 1:
        problems.append(f"expected one user card, found {page.locator('.msg.user').count()}")
    if not page.locator(".answer").count():
        problems.append("the answer is not on the screen")

    # The folded line: how long, how many steps, and what kind of work.
    head = page.locator(".thinking-head").first
    line = head.inner_text()
    print("folded line:", repr(line))
    if "Worked for" not in line or "8 steps" not in line:
        problems.append(f"the folded line does not say how long and how many steps ({line!r})")
    if "Read 2 files" not in line or "and " not in line:
        problems.append(f"the folded line does not sum the work by family ({line!r})")
    if page.locator(".activity .act").count():
        problems.append("the steps are open before the line is clicked")

    head.click()
    page.wait_for_selector(".activity .act", timeout=5000)
    if "Read 2 files" in head.inner_text():
        problems.append("the family summary stays on the line once the steps are open")
    rows = heights(page, ".activity .act:not(.head)")
    print("step rows:", rows)
    if len(rows) != 7 or any(h != 28 for h in rows):
        problems.append(f"expected seven 28 px step rows, got {rows}")
    durations = page.locator(".activity .act .dur").all_inner_texts()
    print("durations:", durations)
    if not durations:
        problems.append("no step shows how long it took")
    if not page.locator(".activity .act .receipt-link").count():
        problems.append("the Verify step has no receipt link")

    # A step's file name opens the file in the panel's Preview tab.
    page.locator(".activity .act .detail.link", has_text="menu.html").first.click()
    page.wait_for_selector(".panel-tab.on[data-tab='preview']", timeout=5000)
    if "menu.html" not in page.locator(".panel-crumbs").inner_text():
        problems.append(f"the step's file did not open in Preview ({page.locator('.panel-crumbs').inner_text()!r})")

    # The file the turn sent is a card under the answer and opens in the panel; the files it only
    # wrote or edited are one line beneath (check_turn_files.py drives that line on its own).
    cards = page.locator(".artifacts .artifact")
    names = cards.locator(".artifact-name").all_inner_texts()
    print("artifacts:", names)
    if names != ["menu-check.md"]:
        problems.append(f"expected the sent report alone as a card, got {names}")
    changed = page.locator(".changed-files-head").all_inner_texts()
    if len(changed) != 1 or "Changed 2 files" not in changed[0]:
        problems.append(f"expected one line for the written and the edited file, got {changed}")
    if page.locator(".sent-files, .file-chip.sent-file").count():
        problems.append("the old sent-file chips are still drawn beside the cards")
    cards.last.locator(".artifact-main").click()
    page.wait_for_timeout(600)
    crumbs = page.locator(".panel-crumbs").inner_text()
    if not page.locator(".panel-tab.on[data-tab='preview']").count() or "menu-check.md" not in crumbs:
        problems.append(f"the artifact card did not open the report in Preview ({crumbs!r})")
    if not page.locator(".panel-body.tab-preview .preview-doc").count():
        problems.append("the panel shows no rendered document after the card was clicked")

    # The actions under the answer: copy puts the text on the clipboard; the link names the turn.
    page.locator(".answer").first.hover()
    actions = page.locator(".turn").last.locator(".msg-actions").last
    labels = actions.locator("button").evaluate_all("(els) => els.map((el) => el.getAttribute('aria-label'))")
    print("answer actions:", labels)
    if labels[:3] != ["Copy", "Copy a link to this turn", "Regenerate"] or "More actions" not in labels:
        problems.append(f"the answer's actions are not copy · link · retry · more ({labels})")
    actions.locator("button[aria-label='Copy']").click()
    page.wait_for_timeout(300)
    copied = page.evaluate("() => navigator.clipboard.readText()")
    if "The menu page is live" not in copied:
        problems.append(f"copy did not put the answer on the clipboard ({copied[:40]!r})")
    actions.locator("button[aria-label='Copy a link to this turn']").click()
    page.wait_for_timeout(300)
    link = page.evaluate("() => navigator.clipboard.readText()")
    if f"/app/agents/{S1}#m401" not in link:
        problems.append(f"the link does not name the turn ({link!r})")

    # A table in the answer scrolls inside the column; its header sticks.
    if not page.locator(".answer .tablewrap table").count():
        problems.append("the answer's table is not in a scrolling wrapper")
    sticky = page.evaluate("() => getComputedStyle(document.querySelector('.answer thead th')).position")
    if sticky != "sticky":
        problems.append(f"the table header is {sticky}, not sticky")

    # A code block in a step carries a copy glyph, not the word.
    page.locator(".activity .act", has_text="Ran command").first.click()
    page.wait_for_selector(".toolcard .codecard .copy", timeout=5000)
    copy = page.locator(".toolcard .codecard .copy").first
    if copy.inner_text().strip():
        problems.append(f"the code block's copy control is a word, not a glyph ({copy.inner_text()!r})")
    if not copy.locator("svg").count():
        problems.append("the code block's copy control has no glyph")

    # The loop agent: the scheduler's wake-up is one folded line, never a bubble.
    page = open_page(context, f"agents/{S3}")
    notes = page.locator(".sysnote")
    print("system notes:", notes.count(), notes.first.inner_text() if notes.count() else "")
    if notes.count() != 1:
        problems.append(f"expected one system note on the loop session, found {notes.count()}")
    else:
        title = notes.first.locator(".sysnote-head").inner_text()
        if "Loop run #14" not in title or "every 90m" not in title:
            problems.append(f"the note does not name the run and the cadence ({title!r})")
        if notes.first.locator(".sysnote-body").count():
            problems.append("the note is open before it is clicked")
        notes.first.locator(".sysnote-head").click()
        page.wait_for_selector(".sysnote-body", timeout=5000)
        body = notes.first.locator(".sysnote-body").inner_text()
        if "Read the support inbox" not in body or "Loop iteration" in body:
            problems.append(f"the open note does not show the instruction alone ({body[:60]!r})")
        loop_turn = page.locator(".loop-turn")
        loop_turn.locator(".run-disclosure").click()
        if loop_turn.locator(".turn-content").is_visible():
            problems.append("collapsing a completed run did not hide its result")
        loop_turn.locator(".run-disclosure").click()
        if not loop_turn.locator(".answer").is_visible():
            problems.append("expanding a completed run did not restore its answer")
    if page.locator(".msg.user").count() != 1:
        problems.append(f"the loop session should have one operator card, found {page.locator('.msg.user').count()}")
    for card in page.locator(".msg.user").all_inner_texts():
        if "Loop iteration" in card:
            problems.append("the loop's wake-up is drawn as a user bubble")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, f"agents/{S1}")
    page.locator(".thinking-head").first.click()
    page.wait_for_selector(".activity .act", timeout=5000)
    rows = heights(page, ".activity .act:not(.head)")
    if any(h != 28 for h in rows):
        problems.append(f"phone: step rows are {rows}")
    vw = 390
    for sel in (".artifacts .artifact", ".msg-actions", ".thinking-head"):
        for box in page.locator(sel).evaluate_all("(els) => els.map((el) => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; })"):
            if box[0] < -1 or box[1] > vw + 1:
                problems.append(f"phone: {sel} is outside the viewport ({box})")
    # On touch the actions are visible without a hover.
    opacity = page.evaluate("() => getComputedStyle(document.querySelector('.msg-actions')).opacity")
    if float(opacity) <= 0:
        problems.append("phone: the message actions are invisible without a hover")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += desktop(browser)
        problems += phone(browser)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
