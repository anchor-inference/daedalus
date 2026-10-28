"""The card under an answer whose turn created schedules, in a real browser.

A turn of the agent made two schedules: a morning digest it set at once (approval off, the default),
and a reminder that waits for the operator (made while approval was on). This draws that turn and
refuses anything but: both cards under the answer without opening the turn's steps; the set one with
its time in the reader's own words, its next run and a way to Schedules, and no button to decide; the
waiting one with Accept and Decline, Accept posting the exact proposal to the host and the card turning
into the schedule it became; Decline on a fresh copy turning it declined. The Inbox lists the waiting
proposal with its own Accept and counts it on the Inbox entry of the rail. On a phone (390 px) the
waiting card fits the width with no sideways scroll and its buttons are thumb-sized.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8702 /tmp/app-root &
    APP_URL=http://127.0.0.1:8702/app python3 tests/browser/check_schedule_card.py

SHOTS=<directory> also saves the pictures this looks at. Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import INBOX_SUMMARY, S1, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
LANG = os.environ.get("LANG_UI", "en")

SET = "s0a1b2c3d4e5f6a7"
WAITING = "p1b2c3d4e5f6a7b8"
BECAME = "s9f8e7d6c5b4a3f2"
NAMES = {
    "en": ("Morning digest", "Call the bank"),
    "ru": ("Утренняя сводка", "Позвонить в банк"),
}[LANG if LANG in ("en", "ru") else "en"]


def state() -> dict:
    """The host's two records, fresh for every page: the set schedule and the waiting proposal."""
    return {
        "schedules": {
            SET: {"id": SET, "name": NAMES[0], "cron": "0 6 * * *", "run_at": None, "next_run_at": ago(hours=-14),
                  "kind": "agent", "run_in": "self", "enabled": 1, "authority_state": "current", "deleted": False, "set_by_agent": True},
        },
        "proposal": {
            "id": WAITING, "name": NAMES[1], "prompt": NAMES[1], "cron": None, "run_at": ago(hours=-20), "kind": "message",
            "run_in": "new", "next_run_at": ago(hours=-20), "file_count": 0, "files": [], "legacy_file_review_required": False,
            "source_session_id": S1, "source_project_id": None, "proposal_revision": 1, "request_digest": "digest-1",
            "status": "pending", "accepted_schedule_id": None, "legacy_schedule_id": None, "created_at": ago(minutes=4),
        },
        "revision": 5,
    }


def turn_messages() -> list[dict]:
    calls = [
        {"id": "c1", "name": "ScheduleCreate", "arguments": {"name": NAMES[0], "prompt": "digest", "cron": "0 6 * * *", "run_in": "self"}},
        {"id": "c2", "name": "ScheduleCreate", "arguments": {"name": NAMES[1], "prompt": NAMES[1], "run_at": "2026-10-29T09:00:00Z", "kind": "message"}},
    ]
    results = [
        {"id": "c1", "content": f"Schedule: {SET}\n'{NAMES[0]}' (agent in this session) is set; next run 2026-10-29T06:00:00+00:00 (UTC).", "is_error": False},
        {"id": "c2", "content": f"Schedule proposal: {WAITING}\n'{NAMES[1]}' (message) waits for the operator's Accept.", "is_error": False},
    ]
    return [
        {"role": "user", "seq": 801, "text": "Send me a digest every morning, and remind me to call the bank tomorrow.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=6)},
        {"role": "assistant", "seq": 802, "text": "", "thinking": "", "tool_calls": calls, "tool_results": [], "created_at": ago(minutes=5)},
        {"role": "tool", "seq": 803, "text": "", "thinking": "", "tool_calls": [], "tool_results": results, "created_at": ago(minutes=4)},
        {"role": "assistant", "seq": 804, "text": "The digest is set. The bank reminder waits for your Accept below.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
    ]


def card(host: dict, item_id: str) -> dict | None:
    proposal = host["proposal"]
    if item_id == WAITING:
        became = host["schedules"].get(proposal["accepted_schedule_id"] or "")
        return {"schedule": became, "proposal": proposal, "collection_revision": host["revision"]}
    found = host["schedules"].get(item_id)
    return {"schedule": found, "proposal": None, "collection_revision": None} if found else None


def make_route(host: dict, posted: list[tuple[str, dict]]):  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        if rel == f"/api/sessions/{S1}" and r.request.method == "GET":
            return respond(r, {**detail(S1), "messages": turn_messages()})
        if rel.startswith("/api/recurring/card/"):
            found = card(host, rel.rsplit("/", 1)[1])
            return respond(r, found) if found else respond(r, {"detail": "no such schedule or occurrence"}, status=404)
        if rel == "/api/recurring/proposals":
            return respond(r, {"entries": [host["proposal"]], "collection_revisions": {"global:global": host["revision"]}})
        if rel.startswith(f"/api/recurring/proposals/{WAITING}/") and r.request.method == "POST":
            action = rel.rsplit("/", 1)[1]
            body = json.loads(r.request.post_data or "{}")
            posted.append((action, body))
            proposal = host["proposal"]
            if (proposal["status"] != "pending" or body.get("request_digest") != proposal["request_digest"]
                    or body.get("expected_collection_revision") != host["revision"]):
                return respond(r, {"detail": {"reason": "the proposal changed"}}, status=409)
            host["revision"] += 1
            if action == "accept":
                proposal.update(status="accepted", accepted_schedule_id=BECAME, proposal_revision=2)
                host["schedules"][BECAME] = {"id": BECAME, "name": NAMES[1], "cron": None, "run_at": proposal["run_at"],
                                             "next_run_at": proposal["next_run_at"], "kind": "message", "run_in": "new",
                                             "enabled": 1, "authority_state": "current", "deleted": False, "set_by_agent": False}
                return respond(r, {"proposal_id": WAITING, "id": BECAME})
            proposal.update(status="withdrawn", proposal_revision=2)
            return respond(r, {"proposal_id": WAITING, "status": "withdrawn"})
        return stub(r)
    return route


def open_chat(context, host: dict, posted: list) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", make_route(host, posted))
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={LANG}")
    page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
    page.wait_for_selector(f".schedcard[data-schedule='{WAITING}']", timeout=10000)
    page.wait_for_timeout(300)
    return page


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    posted: list[tuple[str, dict]] = []
    host = state()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_chat(context, host, posted)
    turn = page.locator(".turn").last
    if turn.locator(".activity").count():
        problems.append("the turn's steps are open; the cards must be seen with them folded")
    ids = turn.locator(".schedcard").evaluate_all("(els) => els.map((el) => el.dataset.schedule)")
    print("cards:", ids)
    if ids != [SET, WAITING]:
        problems.append(f"expected the set card and the waiting one under the answer, in order, got {ids}")
    done = turn.locator(f".schedcard[data-schedule='{SET}']")
    text = done.inner_text()
    print("set:", repr(text[:200]))
    if done.get_attribute("data-status") != "set":
        problems.append("the schedule set at once is not drawn as set")
    if NAMES[0] not in text or "0 6 * * *" in text:
        problems.append("the set card does not name the schedule, or shows the raw UTC cron instead of the reader's time")
    if done.locator("button[data-action='accept'], button[data-action='decline']").count():
        problems.append("a schedule set at once offers Accept or Decline")
    if not done.locator("button[data-action='open']").count():
        problems.append("the set card has no way to Schedules")
    rail = page.locator(".rail-item[data-rail='inbox'] .rail-badge")
    counted = rail.first.get_attribute("data-count") if rail.count() else None
    if counted != str(INBOX_SUMMARY["unseen"] + 1):
        problems.append(f"the waiting schedule is not counted on the Inbox entry of the rail beside the unread entries ({counted})")
    waiting = turn.locator(f".schedcard[data-schedule='{WAITING}']")
    if waiting.get_attribute("data-status") != "pending":
        problems.append("the waiting card is not drawn as pending")
    page.evaluate(f"() => document.querySelector(\".schedcard[data-schedule='{WAITING}']\")?.scrollIntoView({{ block: 'center' }})")
    shot(page, "schedule-card-desktop")

    waiting.locator("button[data-action='accept']").click()
    page.wait_for_selector(f".schedcard[data-schedule='{WAITING}'][data-status='set']", timeout=5000)
    if [action for action, _ in posted] != ["accept"]:
        problems.append(f"Accept did not post once to the host: {posted}")
    elif posted[0][1].get("request_digest") != "digest-1" or not posted[0][1].get("expires_at"):
        problems.append(f"Accept did not send the exact proposal with an expiry: {posted[0][1]}")
    if waiting.locator("button[data-action='accept']").count():
        problems.append("the accepted card still offers Accept")
    shot(page, "schedule-card-desktop-accepted")
    context.close()

    # Decline, on a fresh copy of the waiting proposal.
    posted.clear()
    host = state()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_chat(context, host, posted)
    page.locator(f".schedcard[data-schedule='{WAITING}'] button[data-action='decline']").click()
    page.wait_for_selector(f".schedcard[data-schedule='{WAITING}'][data-status='declined']", timeout=5000)
    if [action for action, _ in posted] != ["withdraw"]:
        problems.append(f"Decline did not post once to the withdraw route: {posted}")
    context.close()

    # The Inbox lists the waiting proposal with its own buttons.
    host = state()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    page.route("**/api/**", make_route(host, []))
    page.goto(f"{BASE}/inbox?token=t&scheme=dark&lang={LANG}")
    try:
        page.wait_for_selector(f".inbox-schedules .schedcard[data-schedule='{WAITING}'] button[data-action='accept']", timeout=10000)
    except Exception:  # noqa: BLE001 — said as a problem below, with a picture
        problems.append("the Inbox does not list the waiting schedule with Accept")
    shot(page, "schedule-card-inbox")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_chat(context, state(), [])
    waiting = page.locator(f".schedcard[data-schedule='{WAITING}']")
    waiting.scroll_into_view_if_needed()
    box = waiting.bounding_box()
    print("phone card:", box)
    if box is None or box["x"] < 0 or box["x"] + box["width"] > 390.5:
        problems.append(f"phone: the card is outside the viewport ({box})")
    wide = page.evaluate("() => document.documentElement.scrollWidth")
    if wide > 391:
        problems.append(f"phone: the page scrolls sideways ({wide} px)")
    for action in ("accept", "decline"):
        target = waiting.locator(f"button[data-action='{action}']").bounding_box()
        if target is None or target["height"] < 32:
            problems.append(f"phone: the {action} button is too small to press ({target})")
    shot(page, "schedule-card-phone")
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
