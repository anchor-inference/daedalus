"""A link to one setting still ends on its row when the section draws around the row late.

A section's cards read their own answers, and the ones above the row arrive after the page has been
scrolled to the row, pushing it down the page; a reader on a slow device or a slow host landed near
the row and not on it. Whether that happens does not depend on the machine's speed but on the order
of two things, so the check fixes the order and does not time anything: the answer that draws the
block above the row is held back by the page (`holding.py`) until the row has been scrolled to and
has blinked and well more than the time the app once allowed for such a thing has gone by, and only
then let through. What is asked, for each case at 1440 × 900 and at 390 × 844:

- the block does draw above the row, and the row's place in the section is further down because of it;
- the row ends inside the box that scrolls it, and stays there;
- a reader who has scrolled before the block comes is left where they are: the row is not pulled back
  from under their hand.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_settings_late.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import holding  # noqa: E402
from api_stub import expect_app  # noqa: E402
from browser_stub import render_scenes  # noqa: E402
from check_settings_tabs import BASE, CHROMIUM, SCENES, open_page  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from waiting import until  # noqa: E402

# Longer than the second or two of grace a fixed timer once gave the row: the answer is let through
# this long after the row has blinked, so a hold that only lasts that long fails here whatever the
# machine does.
AFTER_REVEAL_MS = 4000
BOUND_MS = 60_000
# The block's arrival is what puts the row back, in the frame it lands in; this is the limit of a failure.
PUT_BACK_MS = 10_000


@dataclass(frozen=True)
class Case:
    """An address, the row it brings the page to, the answers the page is kept from and what they draw above the row."""

    address: str
    target: str
    held: list[str]
    late: str


def row_case(section: str, row: str, held: list[str], late: str) -> Case:
    return Case(f"/settings/{section}#{row}", f'[data-setting="{row}"]', held, late)


CASES = [
    # The card for the user's own Chrome, above the profiles.
    row_case("browser", "browser.profiles", [r"/api/browser/user($|\?)"], ".bs-chrome"),
    # The matrix of events and the muted projects, above the test button, once the preferences and the devices answer.
    row_case("notifications", "notifications.test", [r"/api/notifications/preferences", r"/api/push/subscriptions"], '[data-setting="notifications.matrix"]'),
    # The tool groups, above the output limit.
    row_case("tools", "tools.exec.max_output_chars", [r"/api/tool-groups"], ".tgroup"),
    # The environments and the running browsers, above the browser cap's limits.
    row_case("environments", "browser.idle_close_minutes", [r"/api/browsers($|\?)", r"/api/browsers/running", r"/api/workloads/load"], ".bs-env"),
]

BOX = """(selector) => {
    const row = document.querySelector(selector);
    if (!row) return null;
    let pane = null;
    for (let node = row.parentElement; node && node !== document.body; node = node.parentElement) {
        const overflow = getComputedStyle(node).overflowY;
        if ((overflow === 'auto' || overflow === 'scroll') && node.scrollHeight > node.clientHeight) { pane = node; break; }
    }
    const box = row.getBoundingClientRect(), edge = pane ? pane.getBoundingClientRect() : null;
    const top = Math.max(0, edge ? edge.top : 0), bottom = Math.min(innerHeight, edge ? edge.bottom : innerHeight);
    const scrolled = pane ? pane.scrollTop : scrollY;
    return { inside: box.top >= top - 1 && box.bottom <= bottom + 1, place: Math.round(box.top + scrolled), scrolled: Math.round(scrolled), top: Math.round(box.top), bottom: Math.round(box.bottom), pane: [Math.round(top), Math.round(bottom)] };
}"""


def run(browser, width: int, case: Case, reader: bool, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"[{width}] {case.address}{' (the reader scrolls first)' if reader else ''}"
    page = open_page(browser, width, "en", held=case.held)
    page.goto(f"{BASE}{case.address.replace('#', '?token=t&lang=en#') if '#' in case.address else case.address + '?token=t&lang=en'}")
    if not holding.wait_held(page, BOUND_MS):
        problems.append(f"{where}: the page never asked for the answer that is held ({case.held})")
        page.context.close()
        return
    try:
        page.wait_for_function("() => window.__flashed.size > 0", timeout=BOUND_MS)
    except Exception:  # noqa: BLE001
        problems.append(f"{where}: the row did not blink")
        page.context.close()
        return
    page.wait_for_timeout(AFTER_REVEAL_MS)
    if page.locator(case.late).count():
        problems.append(f"{where}: {case.late} is drawn before its answer was let through")
    if reader:
        scroll_by_hand(page, case.target)
    before = page.evaluate(BOX, case.target)
    holding.release(page)
    try:
        page.wait_for_selector(case.late, state="attached", timeout=BOUND_MS)
    except Exception:  # noqa: BLE001
        problems.append(f"{where}: {case.late} was not drawn after the answer was let through")
        page.context.close()
        return
    # Two frames: what the block's arrival sets going has run, and nothing more is waited for after.
    page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
    if reader:
        settled(page)
        after = page.evaluate(BOX, case.target)
        moved = abs(after["scrolled"] - before["scrolled"])
        print(f"{where}: scrolled at {before['scrolled']} before the block, {after['scrolled']} after; row {before['top']}..{before['bottom']} -> {after['top']}..{after['bottom']}")
        if moved > 1:
            problems.append(f"{where}: the page was moved by {moved} px under a reader who had scrolled")
    else:
        got_in = until(page, lambda: page.evaluate(BOX, case.target)["inside"], PUT_BACK_MS)
        after = page.evaluate(BOX, case.target)
        pushed = after["place"] - before["place"]
        print(f"{where}: the target sits {pushed} px further down the section after the block; now {after['top']}..{after['bottom']} in {after['pane']}")
        if pushed < 100:
            problems.append(f"{where}: the late block moved the target by {pushed} px, which proves nothing")
        if not got_in:
            problems.append(f"{where}: the target is not in view after the block came: {after}")
        else:
            settled(page)
            again = page.evaluate(BOX, case.target)
            if not again["inside"]:
                problems.append(f"{where}: the target was in view and then left it: {again}")
    page.context.close()


def scroll_by_hand(page: Page, target: str) -> None:
    """A wheel turn over the page, which is what a reader's hand does and nothing the app does itself."""
    box = page.evaluate(BOX, target)
    page.mouse.move(150, (box["pane"][0] + box["pane"][1]) / 2)
    page.mouse.wheel(0, 24)
    until(page, lambda: page.evaluate(BOX, target)["scrolled"] != box["scrolled"], 5000)


def settled(page: Page) -> None:
    """The page given a third of a second more, so that a hold that went on correcting, or one that
    undid itself, shows in the read that follows."""
    page.evaluate("() => new Promise((done) => setTimeout(done, 300))")


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        SCENES.append(render_scenes(browser))
        for width in (1440, 390):
            for case in CASES:
                run(browser, width, case, False, problems)
            run(browser, width, CASES[0], True, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
