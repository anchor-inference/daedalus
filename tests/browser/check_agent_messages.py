"""Messages between the agents of a project in the two feeds that show them, and refuse what does not behave.

A command-line member's Feed (the staff view, Feed mode) lists what the member sent to the other agents
and what they sent it, among its turns by time: "To Max" with its state, its hop and whether the answer
it waited for came; "Reply from Max"; a refused message with the reason; a message still on its way
after the last turn. A message the member took is a turn of its CLI's transcript under the host's line
"[message am-… from …]": the Feed draws it once, as the message from that agent, and never as the
operator's own words with the host's line showing. An `agent.message` event on the stream brings a new
message in at once, without waiting for the poll.

The orchestrator's chat draws the agent lines of a wake-up as compact rows (who to whom, the state, the
hop, the first words) that open to the whole message and its present state from the project's log; the
other lines of the card are untouched. The Russian page has Russian words, a phone draws the Feed's
entries without scrolling sideways, and no page throws.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_agent_messages.py

With ``SHOTS=<dir>`` (and optionally ``THEME=<appearance id>``) it also saves the two feeds there.
Exit 0 when every step holds.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, agent_message, expect_app  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import IRA_SCREEN, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import TerminalStub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
THEME = os.environ.get("THEME", "")
DESK = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

WORDS = {
    "en": {"to_max": "To Max", "reply_max": "Reply from Max", "from_naya": "From Naya", "to_naya": "To Naya", "from_lev": "From Lev", "answered": "answered",
           "refused": "refused", "hop": "hop 3", "waits": "waits for an answer", "you": "You", "now": "Now:", "feed": "Feed"},
    "ru": {"to_max": "Агенту Max", "reply_max": "Ответ от агента Max", "from_naya": "От агента Naya", "to_naya": "Агенту Naya", "from_lev": "От агента Lev", "answered": "есть ответ",
           "refused": "отклонено", "hop": "шаг 3", "waits": "ждёт ответа", "you": "Вы", "now": "Сейчас:", "feed": "Лента"},
}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)
            print("PROBLEM:", problem, flush=True)


def ago(**back: float) -> str:
    return (datetime.now(UTC) - timedelta(**back)).strftime("%Y-%m-%dT%H:%M:%SZ")


def stand(lang: str) -> tuple[FocusStub, TerminalStub, str]:
    """The bakery with Ira's staff view, and a log of what Ira and the others said to each other."""
    focus = FocusStub.bakery(lang)
    focus.staff_view_of_ira(lang)
    pid = focus.projects[0]["id"]
    ira, max_, naya, lev = ("Ira", "st-ira"), ("Max", "st-max"), ("Naya", "st-naya"), ("Lev", "st-lev")
    # Taken: Naya's message is a turn of Ira's transcript under the host's line, between her turns.
    turns = focus.staff_views["st-ira"]["turns"]
    turns.insert(3, {"index": 3, "role": "user", "tools": [], "usage": None, "started_at": ago(minutes=22), "ended_at": ago(minutes=22),
                     "text": "[message am-in0001 from Naya, another agent of your project, hop 1 of 5: a colleague's request, not your orchestrator's or the operator's; "
                             "your brief still holds, and what needs approval still needs it.]\nThe bot posts to /orders/notify — is the payload final?"})
    for index, turn in enumerate(turns):
        turn["index"] = index
    focus.agent_messages[pid] = [
        agent_message("am-lev0001", lev, ira, "When checkout is merged, ping me: the photos page links to it.", at=ago(minutes=1), hop=3, delivery="queued", project_id=pid),
        agent_message("am-ref0001", ira, naya, "Can you hold the bot deploy until checkout lands?", at=ago(minutes=21), state="refused", detail="Naya is waiting for permission", project_id=pid),
        agent_message("am-in0001", naya, ira, "The bot posts to /orders/notify — is the payload final?", at=ago(minutes=22, seconds=30), delivery="acknowledged", project_id=pid),
        agent_message("am-rep0001", max_, ira, "Port 8080, and /health answers 200.", at=ago(minutes=44), kind="reply", hop=2, parent_id="am-out0001", project_id=pid),
        agent_message("am-out0001", ira, max_, "Which port does the notify endpoint listen on?", at=ago(minutes=45), wait_s=300, state="answered", delivery="acknowledged", project_id=pid),
    ]
    term = TerminalStub(S1)
    term.add("tm-ira", title="claude · Ira", owner_kind="staff", owner_id="st-ira", project_id=pid, owner_label="Ira", cwd="/home/operator/work/bakery-site")
    term.emit("tm-ira", IRA_SCREEN)
    term.add("tm-naya", title="opencode · Naya", owner_kind="staff", owner_id="st-naya", project_id=pid, owner_label="Naya", env="container")
    return focus, term, pid


def open_page(context, focus: FocusStub, term: TerminalStub, feed: EventFeed, url: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path[parts.path.index("/api/"):]
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = focus.answer(request.method, path, parts.query, body)
        if answered is not None:
            return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        return stub(route)

    page.route("**/api/**", handle)
    term.install(page)
    page.route("**/api/events**", feed.route)
    page.route("https://telegram.org/**", lambda route: route.fulfill(status=200, content_type="text/javascript", body=""))
    if THEME:
        page.add_init_script(f"try {{ localStorage.setItem('daedalus.appearance', JSON.stringify({{follow: false, theme: {json.dumps(THEME)}}})); }} catch (e) {{}}")
    page.goto(url)
    return page


def shot(page: Page, name: str) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(Path(SHOTS) / name))


def staff_feed(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    focus, term, pid = stand(lang)
    feed = EventFeed()
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = open_page(context, focus, term, feed, f"{BASE}/project/{pid}/staff/st-ira?token=t&lang={lang}")
    page.wait_for_selector(".staff-cli .staff-head", timeout=20000)
    page.locator(".staff-mode button[data-mode='feed']").click()
    entries = page.locator(".feed .feed-agent")
    expect(entries).to_have_count(5, timeout=10000)

    def entry(mid: str):  # type: ignore[no-untyped-def]
        return page.locator(f".feed-agent[data-agent-message='{mid}']")

    out = entry("am-out0001")
    check.that(out.get_attribute("data-direction") == "to" and words["to_max"] in out.inner_text(), f"{lang}: Ira's question to Max reads {out.inner_text()!r}")
    expect(out.locator(".agent-state")).to_have_text(words["answered"])
    expect(entry("am-rep0001").locator(".feed-agent-who")).to_have_text(words["reply_max"])
    refused = entry("am-ref0001")
    expect(refused.locator(".feed-agent-who")).to_have_text(words["to_naya"])
    expect(refused.locator(".agent-state")).to_have_text(words["refused"])
    expect(refused.locator(".feed-agent-why")).to_have_text("Naya is waiting for permission")
    lev = entry("am-lev0001")
    expect(lev.locator(".feed-agent-who")).to_have_text(words["from_lev"])
    expect(lev.locator(".agent-hop")).to_have_text(words["hop"])

    # The message Ira took is drawn once, as Naya's, where the transcript has it, with the host's line gone.
    taken = entry("am-in0001")
    expect(taken.locator(".feed-agent-who")).to_have_text(words["from_naya"])
    expect(taken.locator(".feed-agent-text")).to_have_text("The bot posts to /orders/notify — is the payload final?")
    text = page.locator(".feed").inner_text()
    check.that("[message am-" not in text, f"{lang}: the host's line over a received message is shown in the Feed")
    check.that(text.count("is the payload final?") == 1, f"{lang}: Naya's message is drawn {text.count('is the payload final?')} times")

    # The order: by time among the turns; the one on its way after the last turn.
    order = page.eval_on_selector_all(".feed-turns > article", "(els) => els.map((e) => e.dataset.agentMessage || ('turn ' + e.dataset.turn))")
    wanted = ["am-out0001", "am-rep0001", "am-in0001", "am-ref0001"]
    positions = [order.index(m) for m in wanted if m in order]
    check.that(positions == sorted(positions) and len(positions) == 4, f"{lang}: the Feed's order is {order}")
    check.that(order[-1] == "am-lev0001", f"{lang}: the newest message is not after the last turn: {order}")
    check.that(order.index("am-out0001") < order.index("turn 2") < order.index("am-in0001"), f"{lang}: the messages are not among the turns by time: {order}")

    # A message that arrives while the Feed is open comes in on its event, before any poll.
    focus.agent_messages[pid].insert(0, agent_message("am-new0001", ("Max", "st-max"), ("Ira", "st-ira"), "Is the discount applied before tax?", at=ago(seconds=1), wait_s=120, project_id=pid))
    deadline = time.monotonic() + 10
    while feed.connected() == 0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    started = time.monotonic()
    feed.send("agent.message", {"message_id": "am-new0001", "kind": "message", "state": "sent", "from": "Max", "to": "Ira", "hop": 1, "text": "Is the discount applied before tax?",
                                "wait": True, "from_staff_id": "st-max", "to_staff_id": "st-ira"}, project=pid, staff="st-max")
    expect(entry("am-new0001")).to_be_visible(timeout=8000)
    took = time.monotonic() - started
    check.that(took < 8, f"{lang}: a new message took {took:.1f} s to appear")
    expect(entry("am-new0001").locator(".agent-wait")).to_contain_text(words["waits"])
    shot(page, f"{lang}-staff-feed-agents.png")
    check.that(not page.errors, f"{lang}: the staff view threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def orchestrator_card(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    focus, term, pid = stand(lang)
    detail = focus.details["orch-bakery"]
    lines = (
        "[events · Bakery 2.0 · 4 since 10:02]\n"
        '- 10:02 Ira → Max, agent message am-out0001 (hop 1); Ira waits for the answer: "Which port does the notify endpoint listen on?"\n'
        '- 10:03 Max answered Ira\'s message am-out0001: "Port 8080, and /health answers 200."\n'
        "- 10:20 Ira's message to Naya was refused (Naya is waiting for permission): \"Can you hold the bot deploy until checkout lands?\"\n"
        '- 10:21 Max (Codex) finished a turn on "Notify: endpoint" (t-endpoint): "3 files, tests green" — ReadStaff("Max") for the whole reply'
    )
    last = detail["messages"][-1]["seq"]
    detail["messages"].append({"role": "user", "seq": last + 1, "origin": "events", "text": lines, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T10:21:10Z"})
    detail["messages"].append({"role": "assistant", "seq": last + 2, "text": "Noted.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-24T10:21:30Z"})
    context = browser.new_context(viewport=DESK, color_scheme="dark")
    page = open_page(context, focus, term, EventFeed(), f"{BASE}/orchestration/project/{pid}?token=t&lang={lang}")
    chat = page.locator(".chat.in-project.orchestrator")
    expect(chat).to_be_visible(timeout=20000)
    cards = chat.locator(".event-card")
    expect(cards).to_have_count(2, timeout=10000)
    card = cards.nth(1)
    rows = card.locator(".event-line.agent")
    expect(rows).to_have_count(3)
    expect(card.locator(".event-line:not(.agent)")).to_have_count(1)
    expect(cards.nth(0).locator(".event-line.agent")).to_have_count(0)
    first = rows.nth(0)
    check.that(first.get_attribute("data-kind") == "message" and "info" in (first.get_attribute("class") or ""), f"{lang}: the sent message's row is {first.get_attribute('class')}")
    expect(first.locator(".agent-line-who")).to_have_text("Ira → Max")
    expect(rows.nth(1).locator(".agent-state")).to_have_text(words["answered"])
    check.that("warn" in (rows.nth(2).get_attribute("class") or ""), f"{lang}: the refusal is not a warning: {rows.nth(2).get_attribute('class')}")
    # Folded, a row is one line like its neighbours.
    folded = first.bounding_box()["height"]  # type: ignore[index]
    check.that(folded <= 40, f"{lang}: a folded agent row is {folded:.0f} px tall")
    first.locator(".agent-line-head").click()
    body = first.locator(".agent-line-body")
    expect(body).to_be_visible()
    expect(body.locator(".agent-line-full")).to_have_text("Which port does the notify endpoint listen on?")
    expect(body.locator(".agent-line-now")).to_contain_text(words["now"], timeout=8000)
    expect(body.locator(".agent-line-now .agent-state")).to_have_text(words["answered"])
    check.that(any(r.get("limit") == "200" and "staff" not in r for r in focus.agent_reads), f"{lang}: opening the row read {focus.agent_reads}")
    check.that("ReadStaff" not in card.inner_text(), f"{lang}: advice to the model is shown")
    card.scroll_into_view_if_needed()
    shot(page, f"{lang}-orchestrator-agent-lines.png")
    first.locator(".agent-line-head").click()
    expect(body).to_have_count(0)
    check.that(not page.errors, f"{lang}: the orchestrator's chat threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def phone(browser, check: Check) -> None:  # type: ignore[no-untyped-def]
    focus, term, pid = stand("en")
    context = browser.new_context(viewport=PHONE, color_scheme="dark", has_touch=True, is_mobile=True)
    page = open_page(context, focus, term, EventFeed(), f"{BASE}/project/{pid}/staff/st-ira?token=t&lang=en")
    expect(page.locator(".feed .feed-agent")).to_have_count(5, timeout=20000)
    sideways = int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))
    check.that(sideways <= 0, f"phone: the Feed scrolls {sideways} px sideways")
    shot(page, "phone-staff-feed-agents.png")
    check.that(not page.errors, f"phone: the staff view threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def main() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        for lang in ("en", "ru"):
            staff_feed(browser, lang, check)
            orchestrator_card(browser, lang, check)
        phone(browser, check)
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL:", problem)
    if check.problems or unhandled:
        return 1
    print("agent messages: every step holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
