"""Are a message's actions reachable, on a phone as well as on a desk?

The turn's actions used to sit in an overflow menu beside the message bubble. On a phone that
row is pushed past the right edge: the menu was simply not there. They are now a row of icon
buttons under the message — copy, and (for an operator's turn) fork and revert.

The check loads a session in headless Chromium with the API stubbed and asserts, at a phone
width and a desktop width, that every action button is inside the viewport, that the answer
has a copy button, and that pressing it puts the answer on the clipboard.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_message_actions.py

In the orchestrator's chat a line of the events card, a steps card and a reply of the orchestrator's
can be answered: "Reply to this" puts a quote over the composer, which can be taken back before
sending, and the message goes out with ``reply_to`` naming what it answers; the sent message shows
the quote. Under each of the operator's messages stands what became of it: the requirement it made
and whether the member confirmed it, a promise and whether it was kept, that it was read, and how it
arrived when that was during a turn or as one ended.

Exit 0 when the actions are reachable, copy works, and a reply carries what it answers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402
from check_project_focus import serve as serve_focus  # noqa: E402
from screenshots import UNHANDLED as INSTALLATION_UNHANDLED  # noqa: E402

UNHANDLED = Unhandled()

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

ANSWER = "The answer the operator wants to copy, with a detail worth keeping."
SESSION = "sess-1"

DETAIL = {
    "id": SESSION,
    "title": "A session",
    "status": "idle",
    "run_id": None,
    "workspace": "/workspace",
    "workspace_name": "ws",
    "workspace_own": True,
    "workspace_sessions": [],
    "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
    "pending": None,
    "model": "some-model",
    "mode": "",
    "brief": "",
    "tools_off": [],
    "loop": None,
    "services": [],
    "subagents": [],
    "usage": {},
    "context": {"tokens": 10, "window": 100000, "messages": 2, "summaries": 0, "operator_turns": 1},
    "messages": [
        {
            "role": "user",
            "summary": False,
            "internal": False,
            "origin": "operator",
            "seq": 101,
            "compaction": None,
            "archived": None,
            "headline": "",
            "text": "A question from the operator",
            "thinking": "",
            "tool_calls": [],
            "tool_results": [],
            "created_at": "2026-09-13T12:00:00+00:00",
        },
        {
            "role": "assistant",
            "summary": False,
            "internal": False,
            "origin": "",
            "seq": 102,
            "compaction": None,
            "archived": None,
            "headline": "",
            "text": ANSWER,
            "thinking": "",
            "tool_calls": [],
            "tool_results": [],
            "created_at": "2026-09-13T12:00:05+00:00",
        },
    ],
}


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    if url.split("?", 1)[0].endswith("/stream"):
        # The session's own event stream. An empty JSON body puts the reader in a retry loop for the
        # whole run; one hello frame and nothing after it is a session that is simply quiet.
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif f"/api/sessions/{SESSION}" in url and "/events" not in url and "/stream" not in url:
        body = json.dumps(DETAIL)
    elif "unread" in url:
        body = json.dumps({"unread": 0})
    elif url.rstrip("/").endswith("/api/sessions"):
        body = json.dumps({"sessions": [], "projects": []})
    else:
        # As in every other harness here: the shared gates first, then a report of what was missed.
        rel = url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):] if "/api/" in rel else ""
        if fulfil_shared(route):
            return
        if rel:
            UNHANDLED.record(rel)
        body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def visible_actions(page: Page) -> list[dict]:
    return page.evaluate(
        """() => [...document.querySelectorAll('.msg-actions')].map(row => {
             const r = row.getBoundingClientRect();
             return { left: r.left, right: r.right, width: r.width,
                      buttons: [...row.querySelectorAll('button')].map(b => b.getAttribute('aria-label')) };
           })"""
    )


PID = "b4k3ry20f0c5"
QUESTION = "Is this the endpoint Naya waits for?"


def orchestrator(page: Page, name: str, width: int) -> list[str]:
    """Answering a line of the orchestrator's events card, and the receipts under the operator's messages."""
    problems: list[str] = []
    words = FOCUS_WORDS["en"]
    focus = FocusStub.bakery("en")
    serve_focus(page, focus)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang=en")
    chat = page.locator(".chat.in-project.orchestrator")
    line = chat.locator(".event-card .event-line").first
    line.wait_for(timeout=15000)
    line.scroll_into_view_if_needed()
    line.hover()
    reply = line.locator(".event-reply")
    page.wait_for_timeout(250)
    box = reply.bounding_box()
    if not box or not reply.is_visible():
        return [f"{name}: the events line has no visible reply button"]
    if box["x"] + box["width"] > width + 1 or box["x"] < 0:
        problems.append(f"{name}: the events line's reply button is outside the viewport ({box})")
    if reply.get_attribute("aria-label") != "Reply to this":
        problems.append(f"{name}: the events line's reply button is called {reply.get_attribute('aria-label')!r}")

    # The quote waits over the composer, and can be taken back before anything is sent.
    reply.click()
    chip = chat.locator(".reply-chip")
    chip.wait_for(timeout=5000)
    if "09:51 Max" not in chip.inner_text():
        problems.append(f"{name}: the quote over the composer reads {chip.inner_text()!r}")
    chip.locator(".reply-chip-remove").click()
    page.wait_for_timeout(200)
    if chip.count():
        problems.append(f"{name}: the quote stayed after it was removed")

    # A reply of the orchestrator's can be answered too: a button on a desk, a menu entry on a phone.
    answer_row = chat.locator(".turn", has_text=words["orch.hours"]).locator(".msg-actions").last
    answer_row.scroll_into_view_if_needed()
    answer_row.hover()
    if name == "phone":
        answer_row.get_by_role("button", name="More actions").click()
        labels = page.locator('[role="menuitem"]').all_text_contents()
        page.keyboard.press("Escape")
    else:
        labels = [b.get_attribute("aria-label") for b in answer_row.locator("button").all()]
    if not any(label and "Reply to this" in label for label in labels):
        problems.append(f"{name}: the orchestrator's reply has no 'Reply to this' ({labels})")

    # Answer the events line for real: the POST carries what it answers, and the sent message shows it.
    line.scroll_into_view_if_needed()
    line.hover()
    reply.click()
    chip.wait_for(timeout=5000)
    chat.locator(".composer textarea").fill(QUESTION)
    chat.locator(".composer .roundbtn.primary").click()
    for _ in range(50):
        if focus.posted:
            break
        page.wait_for_timeout(100)
    if not focus.posted:
        return [*problems, f"{name}: sending posted nothing"]
    sid, body = focus.posted[0]
    quoted = body.get("reply_to") or {}
    print(f"{name}: posted to {sid}: {body}")
    if sid != "orch-bakery" or body.get("text") != QUESTION:
        problems.append(f"{name}: the message went out as {sid} {body}")
    if quoted.get("seq") != 14 or not str(quoted.get("excerpt", "")).startswith("09:51 Max"):
        problems.append(f"{name}: the message does not say it answers the events line: {quoted}")
    page.wait_for_timeout(600)
    if chip.count():
        problems.append(f"{name}: the quote is still over the composer after the message went out")
    sent = chat.locator(".msg-wrap", has_text=QUESTION)
    sent.last.wait_for(timeout=5000)
    if "09:51 Max" not in (sent.last.locator(".msg-quote").first.inner_text() if sent.last.locator(".msg-quote").count() else ""):
        problems.append(f"{name}: the sent message does not show what it answers")

    # What became of the operator's earlier messages.
    def fate(text: str) -> str:
        found = chat.locator(".msg-wrap", has_text=text).locator(".msg-fate")
        try:
            found.first.wait_for(timeout=5000)
        except Exception:  # noqa: BLE001 — reported below as a missing receipt
            return ""
        return " ".join(found.first.inner_text().split())

    confirmed = fate(words["op.photos"])
    for part in ("R1 on Menu photo captions", "delivered to Lev → confirmed", "commitment: " + words["commit.gallery"], "open"):
        if part not in confirmed:
            problems.append(f"{name}: the receipt of the confirmed correction lacks {part!r}: {confirmed!r}")
    pending = fate(words["op.hours"])
    for part in ("arrived as a turn ended, read in the next", "R2 on Opening hours", "delivered to Olga → not confirmed yet"):
        if part not in pending:
            problems.append(f"{name}: the receipt of the correction sent to Olga lacks {part!r}: {pending!r}")
    steered = fate(words["op.steps"])
    if "arrived during a turn" not in steered or "read by the orchestrator" not in steered:
        problems.append(f"{name}: the message placed into a turn says {steered!r}")
    if chat.locator(".msg-wrap", has_text=words["op.steps"]).locator(".msg-quote").count() != 1:
        problems.append(f"{name}: the message that answered Naya's steps does not show its quote")
    if "read by the orchestrator" not in fate(words["op.ask"]):
        problems.append(f"{name}: the first message, answered since, does not say it was read")
    rows = chat.locator(".msg-fate").evaluate_all("rows => rows.map(r => r.getBoundingClientRect().right)")
    if any(right > width + 1 for right in rows):
        problems.append(f"{name}: a receipt reaches past the window ({rows})")
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for name, width, height in [("phone", 390, 844), ("desktop", 1440, 900)]:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                is_mobile=name == "phone",
                has_touch=name == "phone",
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            page.route("**/api/**", stub)
            page.goto(f"{BASE}/agents/{SESSION}?token=t")
            page.wait_for_selector(".msg.user", timeout=15000)
            page.wait_for_timeout(400)
            # Hover brings the row in on a desktop; on a phone it is there already.
            page.locator(".turn").first.hover()
            page.wait_for_timeout(200)
            rows = visible_actions(page)
            print(f"{name}: {len(rows)} action row(s)")
            if len(rows) < 2:
                problems.append(f"{name}: expected actions under both the message and the answer, found {len(rows)}")
            for row in rows:
                print(f"   buttons={row['buttons']} left={row['left']:.0f} right={row['right']:.0f}")
                if row["right"] > width + 1 or row["left"] < -1:
                    problems.append(f"{name}: an action row is outside the viewport (left {row['left']:.0f}, right {row['right']:.0f}, width {width})")
                if row["width"] < 1:
                    problems.append(f"{name}: an action row has no size")
            labels = [b for row in rows for b in row["buttons"]]
            if "Copy" not in labels:
                problems.append(f"{name}: no copy button ({labels})")
            if name == "phone":
                page.locator(".msg-actions").first.get_by_role("button", name="More actions").click()
                labels += page.locator('[role="menuitem"]').all_text_contents()
                page.keyboard.press("Escape")
            if not any(b and "Fork" in b for b in labels):
                problems.append(f"{name}: the operator's turn has no fork action ({labels})")
            if not any(b and "Revert" in b for b in labels):
                problems.append(f"{name}: the operator's turn has no revert action ({labels})")

            # The answer's copy button puts the answer on the clipboard.
            copy_button = page.locator(".msg-actions").last.locator("button[aria-label='Copy']")
            if copy_button.count() == 0:
                problems.append(f"{name}: the answer has no copy button")
            else:
                copy_button.click()
                page.wait_for_timeout(400)
                clip = page.evaluate("() => navigator.clipboard.readText()")
                print(f"{name}: clipboard = {clip[:40]!r}")
                if ANSWER not in (clip or ""):
                    problems.append(f"{name}: copying the answer did not reach the clipboard ({clip!r})")
            if os.environ.get("SHOTS"):
                page.screenshot(path=str(Path(__file__).parent / f"message-actions-{name}.png"))
            context.close()

            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=name == "phone", has_touch=name == "phone")
            problems += orchestrator(context.new_page(), name, width)
            context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    # A gate the app grew and this stub does not know about fails the run by name, rather than by a
    # selector that never appears somewhere further down.
    failed = run()
    sys.exit(failed or UNHANDLED.report() or INSTALLATION_UNHANDLED.report())
