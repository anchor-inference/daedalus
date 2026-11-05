"""Does a turn say when the operator wrote and when the agent answered, and only then?

The operator's message carries the time it was sent, in the row under the bubble. The agent's final
answer carries the time it was given, after the icons of its copy and retry row, with the whole date
and how long the turn took in its tooltip. The texts the agent wrote between its steps get no time,
and neither does a turn that broke off without answering: a time there would say it answered.

The check loads a session in headless Chromium with the API stubbed, at a desktop width in English
and at a phone width in Russian, and asserts which rows carry a time, what it says, and that on the
phone the time is visible and inside the screen without a hover.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_message_times.py

Exit 0 when every time is where it belongs and nowhere else.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SESSION = "sess-times"

QUESTION = "How is the build going?"
INTERIM = "Let me look at the build log first."
FINAL = "The build is green and the tests pass."
CUT_QUESTION = "And the deploy?"
CUT_INTERIM = "Checking the deploy now."


def message(seq: int, role: str, at: datetime, **over) -> dict:  # type: ignore[no-untyped-def]
    base = {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "",
            "seq": seq, "compaction": None, "archived": None, "headline": "", "text": "", "thinking": "",
            "tool_calls": [], "tool_results": [], "created_at": at.isoformat()}
    base.update(over)
    return base


def detail() -> dict:
    # Live times, so the ages read the same whatever day the check runs: written eight minutes ago,
    # answered two minutes and fourteen seconds later.
    asked = datetime.now(UTC) - timedelta(minutes=8)
    answered = asked + timedelta(minutes=2, seconds=14)
    cut = asked + timedelta(minutes=4)
    call = {"id": "c1", "name": "Exec", "arguments": {"command": "make build"}}
    call2 = {"id": "c2", "name": "Exec", "arguments": {"command": "make deploy"}}
    messages = [
        message(101, "user", asked, text=QUESTION, run_id="r1"),
        message(102, "assistant", asked + timedelta(seconds=5), text=INTERIM, tool_calls=[call], run_id="r1"),
        message(103, "tool", asked + timedelta(seconds=40), tool_results=[{"id": "c1", "content": "ok", "is_error": False}], run_id="r1"),
        message(104, "assistant", answered, text=FINAL, run_id="r1"),
        # A turn the operator stopped: a text between steps, a step, and the run's closing line.
        message(105, "user", cut, text=CUT_QUESTION, run_id="r2"),
        message(106, "assistant", cut + timedelta(seconds=3), text=CUT_INTERIM, tool_calls=[call2], run_id="r2"),
        message(107, "tool", cut + timedelta(seconds=9), tool_results=[{"id": "c2", "content": "stopped", "is_error": True}], run_id="r2"),
        message(108, "system", cut + timedelta(seconds=10), text="The run was stopped", run_id="r2",
                outcome={"status": "cancelled", "cause": "cancelled"}),
    ]
    return {
        "id": SESSION, "title": "Build", "status": "idle", "run_id": None, "workspace": "/workspace",
        "workspace_name": "ws", "workspace_own": True, "workspace_sessions": [],
        "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
        "pending": None, "model": "some-model", "brief": "", "tools_off": [], "loop": None, "services": [],
        "subagents": [], "usage": {}, "messages": messages,
        "context": {"tokens": 10, "window": 100000, "messages": len(messages), "summaries": 0, "operator_turns": 2},
    }


def stub(route) -> None:  # type: ignore[no-untyped-def]
    url = route.request.url
    if url.split("?", 1)[0].endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if "auth/me" in url:
        body = json.dumps({"user_id": 1, "via": "token"})
    elif f"/api/sessions/{SESSION}" in url and "/events" not in url:
        body = json.dumps(detail())
    elif "unread" in url:
        body = json.dumps({"unread": 0})
    elif url.rstrip("/").endswith("/api/sessions"):
        body = json.dumps({"sessions": [], "projects": []})
    else:
        if fulfil_shared(route):
            return
        rel = url.split("?", 1)[0]
        if "/api/" in rel:
            UNHANDLED.record(rel[rel.index("/api/"):])
        body = "[]"
    route.fulfill(status=200, content_type="application/json", body=body)


def times(page: Page) -> list[dict]:
    """Every time in the transcript, with what it stands beside."""
    return page.evaluate(
        """() => [...document.querySelectorAll('.timeline .msg-time')].map(el => {
             const row = el.closest('.msg-actions');
             const turn = el.closest('.turn');
             const r = el.getBoundingClientRect();
             let above = row.previousElementSibling;
             while (above && !above.classList.contains('answer')) above = above.previousElementSibling;
             return {
               text: el.textContent, title: el.getAttribute('title') || '', datetime: el.getAttribute('datetime') || '',
               user: !!el.closest('.msg-wrap'), last: row.lastElementChild === el, first: row.firstElementChild === el,
               turn: turn ? turn.innerText.slice(0, 200) : '', answer: above ? above.innerText : '',
               opacity: getComputedStyle(row).opacity, left: r.left, right: r.right, width: r.width,
             };
           })"""
    )


def check(page: Page, width: int, lang: str) -> list[str]:
    name = f"{width}px {lang}"
    problems: list[str] = []
    page.goto(f"{BASE}/session/{SESSION}?token=t&lang={lang}")
    page.locator(".timeline .msg-time").first.wait_for(timeout=15000)
    page.wait_for_timeout(300)
    found = times(page)
    users = [f for f in found if f["user"]]
    answers = [f for f in found if not f["user"]]
    if len(users) != 2:
        problems.append(f"{name}: {len(users)} operator messages carry a time, expected 2: {users}")
    if len(answers) != 1:
        problems.append(f"{name}: {len(answers)} answers carry a time, expected only the final one: {answers}")
    age = {"en": r"^[0-9]+m$", "ru": r"^[0-9]+ м$"}[lang]
    for f in users:
        if not re.match(age, f["text"]):
            problems.append(f"{name}: an operator message's time reads {f['text']!r}")
        if not f["first"]:
            problems.append(f"{name}: an operator message's time is not at the start of its row")
        if str(datetime.now().year) not in f["title"]:
            problems.append(f"{name}: an operator message's tooltip is {f['title']!r}")
    if answers:
        final = answers[0]
        if FINAL not in final["answer"]:
            problems.append(f"{name}: the time stands under {final['answer']!r}, not the final answer")
        if not final["last"]:
            problems.append(f"{name}: the answer's time is not after the icons")
        if not re.match(age, final["text"]):
            problems.append(f"{name}: the answer's time reads {final['text']!r}")
        took = {"en": "answered in 2m 14s", "ru": "ответ за 2 мин 14 с"}[lang]
        if took not in final["title"]:
            problems.append(f"{name}: the answer's tooltip {final['title']!r} does not say {took!r}")
        if not final["datetime"]:
            problems.append(f"{name}: the answer's time has no machine-readable datetime")
    for f in found:
        if INTERIM in f["answer"] or CUT_INTERIM in f["answer"]:
            problems.append(f"{name}: an intermediate text carries a time ({f['text']!r})")
    # The stopped turn keeps its copy row under its last text, with no time in it.
    cut_rows = page.locator(".turn", has_text=CUT_QUESTION).locator(".turn-content > .msg-actions")
    if cut_rows.count() and cut_rows.last.locator(".msg-time").count():
        problems.append(f"{name}: the stopped turn's row carries a time")
    if width < 600:
        # No hover on a phone: the time is there to read, inside the screen.
        for f in found:
            if float(f["opacity"]) < 0.5:
                problems.append(f"{name}: a time is faded to {f['opacity']} on the phone")
            if f["left"] < 0 or f["right"] > width + 1 or f["width"] <= 0:
                problems.append(f"{name}: a time is outside the screen ({f['left']}..{f['right']})")
    return problems


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        for width, height, lang, touch in ((1280, 860, "en", False), (390, 844, "ru", True)):
            context = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch, is_mobile=touch, locale=lang)
            page = context.new_page()
            page.route("**/api/**", stub)
            problems += check(page, width, lang)
            context.close()
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: the operator's messages and the final answer carry their times, nothing else does")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
