"""The card under an answer whose turn changed the settings, in a real browser.

A turn of the agent changed two things: the answer language, which the operator lets it apply at once,
and the iteration limit, which waits for them. This draws that turn and refuses anything but: both
cards under the answer without opening the turn's steps; the waiting one with its reason, each change
from old to new, when it takes effect, and Apply and Reject; Apply posting to the host and the card
turning into an applied one without buttons; Reject on a fresh copy turning it rejected; the applied-
at-once card never offering a button. On a phone (390 px) the waiting card fits the width with no
sideways scroll and its buttons are thumb-sized.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8702 /tmp/app-root &
    APP_URL=http://127.0.0.1:8702/app python3 tests/browser/check_settings_card.py

SHOTS=<directory> also saves the pictures this looks at. Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LANG = os.environ.get("LANG_UI", "en")

WAITING = "sc0a1b2c3d4e"
APPLIED = "sc5f6a7b8c9d"
CHANGES = {
    WAITING: {
        "id": WAITING, "session_id": S1, "reason": "You asked for longer runs while the migration is going; 200 steps stopped the last one halfway.",
        "changes": [
            {"path": "limits.max_iterations", "old": 200, "new": 400, "apply_at": "next_step", "kind": "approval"},
            {"path": "limits.usd_per_run", "old": 5.0, "new": 8.0, "apply_at": "next_step", "kind": "approval"},
        ],
        "kind": "approval", "status": "pending", "created_at": ago(minutes=3), "expires_at": ago(minutes=-1437), "decided_at": None, "via": "", "error": "",
    },
    APPLIED: {
        "id": APPLIED, "session_id": S1, "reason": "You asked for every answer in English.",
        "changes": [{"path": "answer_language", "old": "auto", "new": "English", "apply_at": "next_step", "kind": "auto"}],
        "kind": "auto", "status": "applied", "created_at": ago(minutes=4), "expires_at": ago(minutes=-1436), "decided_at": ago(minutes=4), "via": "auto", "error": "",
    },
}


def turn_messages() -> list[dict]:
    calls = [
        {"id": "g1", "name": "SettingsGet", "arguments": {"section": "limits"}},
        {"id": "c1", "name": "SettingsChange", "arguments": {"changes": {"answer_language": "English"}, "reason": "asked"}},
        {"id": "c2", "name": "SettingsChange", "arguments": {"changes": {"limits": {"max_iterations": 400, "usd_per_run": 8}}, "reason": "asked"}},
    ]
    results = [
        {"id": "g1", "content": '{"revision": "abc", "section": "limits"}', "is_error": False},
        {"id": "c1", "content": f"Settings change: {APPLIED}\nApplied at once. It takes effect from the next model call", "is_error": False},
        {"id": "c2", "content": f"Settings change: {WAITING}\nWaiting for the operator: an Apply / Reject card is in this chat", "is_error": False},
    ]
    return [
        {"role": "user", "seq": 701, "text": "Answer in English from now on, and give the migration runs more room.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=6)},
        {"role": "assistant", "seq": 702, "text": "", "thinking": "", "tool_calls": calls, "tool_results": [], "created_at": ago(minutes=5)},
        {"role": "tool", "seq": 703, "text": "", "thinking": "", "tool_calls": [], "tool_results": results, "created_at": ago(minutes=4)},
        {"role": "assistant", "seq": 704, "text": "Answers are in English from now on. The longer runs need your Apply: the card is below.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
    ]


def make_route(changes: dict[str, dict], posted: list[str]):  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        if rel == f"/api/sessions/{S1}" and r.request.method == "GET":
            return respond(r, {**detail(S1), "messages": turn_messages()})
        if rel.startswith("/api/settings/changes/"):
            parts = rel.split("/")
            record = changes.get(parts[4])
            if record is None:
                return respond(r, {"detail": "no such settings change"}, status=404)
            if r.request.method == "POST" and len(parts) == 6:
                posted.append(f"{parts[4]}/{parts[5]}")
                if record["status"] != "pending":
                    return respond(r, record, status=409)
                record.update(status="applied" if parts[5] == "apply" else "rejected", decided_at=ago(minutes=0), via="app")
            return respond(r, record)
        return stub(r)
    return route


def open_page(context, changes: dict[str, dict], posted: list[str]) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", make_route(changes, posted))
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
    page.wait_for_selector(f".setchange[data-change='{WAITING}']", timeout=10000)
    page.wait_for_timeout(300)
    return page


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    posted: list[str] = []
    changes = copy.deepcopy(CHANGES)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, changes, posted)
    turn = page.locator(".turn").last
    if turn.locator(".activity").count():
        problems.append("the turn's steps are open; the cards must be seen with them folded")
    cards = turn.locator(".setchange")
    ids = cards.evaluate_all("(els) => els.map((el) => el.dataset.change)")
    print("cards:", ids)
    if ids != [APPLIED, WAITING]:
        problems.append(f"expected the applied card and the waiting one under the answer, in order, got {ids}")
    applied = turn.locator(f".setchange[data-change='{APPLIED}']")
    if applied.locator("button").count():
        problems.append("a change applied at once offers a button")
    waiting = turn.locator(f".setchange[data-change='{WAITING}']")
    text = waiting.inner_text()
    print("waiting:", repr(text[:200]))
    for words in ("200 steps stopped the last one halfway", "limits.max_iterations", "400", "limits.usd_per_run"):
        if words not in text:
            problems.append(f"the waiting card does not show {words!r}")
    if waiting.get_attribute("data-status") != "pending":
        problems.append("the waiting card is not drawn as pending")
    old = waiting.locator(".setchange-old").first
    if old.evaluate("(el) => getComputedStyle(el).textDecorationLine") != "line-through":
        problems.append("the old value is not struck through")
    page.evaluate(f"() => document.querySelector(\".setchange[data-change='{WAITING}']\")?.scrollIntoView({{ block: 'center' }})")
    shot(page, "settings-card-desktop")

    waiting.locator("button[data-action='apply']").click()
    page.wait_for_selector(f".setchange[data-change='{WAITING}'][data-status='applied']", timeout=5000)
    if posted != [f"{WAITING}/apply"]:
        problems.append(f"Apply did not post once to the host: {posted}")
    if waiting.locator("button").count():
        problems.append("the applied card still offers buttons")
    shot(page, "settings-card-desktop-applied")
    context.close()

    # Reject, on a fresh copy of the waiting change.
    posted.clear()
    changes = copy.deepcopy(CHANGES)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, changes, posted)
    waiting = page.locator(f".setchange[data-change='{WAITING}']")
    waiting.locator("button[data-action='reject']").click()
    page.wait_for_selector(f".setchange[data-change='{WAITING}'][data-status='rejected']", timeout=5000)
    if posted != [f"{WAITING}/reject"]:
        problems.append(f"Reject did not post once to the host: {posted}")
    shot(page, "settings-card-desktop-rejected")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, copy.deepcopy(CHANGES), [])
    waiting = page.locator(f".setchange[data-change='{WAITING}']")
    waiting.scroll_into_view_if_needed()
    box = waiting.bounding_box()
    print("phone card:", box)
    if box is None or box["x"] < 0 or box["x"] + box["width"] > 390.5:
        problems.append(f"phone: the card is outside the viewport ({box})")
    wide = page.evaluate("() => document.documentElement.scrollWidth")
    if wide > 391:
        problems.append(f"phone: the page scrolls sideways ({wide} px)")
    for action in ("apply", "reject"):
        target = waiting.locator(f"button[data-action='{action}']").bounding_box()
        if target is None or target["height"] < 32:
            problems.append(f"phone: the {action} button is too small to press ({target})")
    shot(page, "settings-card-phone")
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
