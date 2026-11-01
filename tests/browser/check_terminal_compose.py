"""The Terminals workspace's compose line and message flash on a desktop, and refuse what does not behave.

A running staff member's pane has one line between its terminal and its status line; a plain shell, a
finished member's terminal and a phone have none. What is written there goes to the member as a staff
message (`POST /api/staff/<id>/messages`, "now" where the CLI takes it), never into the terminal's
socket, so it cannot answer the permission a member waits on; that member's line says the message goes
in once the request is answered. A slash at the start lists the CLI's own commands (Claude Code's
here), the arrows move through them and Tab completes; a multi-line paste becomes a chip that the
message carries whole; text selected in the terminal is quoted by Reply. Another agent's message to a
member lights its tab and pane header in the message state for a moment and lets go; a refused one, or
one to a member waiting on a request, changes nothing. The line is one dense row, the Russian page has
Russian words, and no page throws.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_terminal_compose.py

With ``SHOTS=<dir>`` (and optionally ``THEME=<appearance id>``) it also saves the workspace there.
Exit 0 when every step holds.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, terminal_agent  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import P1, S1, UNHANDLED, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
THEME = os.environ.get("THEME", "")
SHELL, IRA, NAYA, DONE = "c1shell00000", "c2ira0000000", "c3naya000000", "c4done000000"

WORDS = {
    "en": {"placeholder": "Write to Ira…", "held": "it goes in once the request is answered", "pasted": "pasted · 3 lines", "reply": "Reply", "compact": "compress the conversation"},
    "ru": {"placeholder": "Написать сотруднику Ira…", "held": "уйдёт, когда запрос будет решён", "pasted": "вставка · 3 строки", "reply": "Ответить", "compact": "сжать разговор"},
}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)
            print("PROBLEM:", problem, flush=True)


def fleet() -> TerminalStub:
    """A shell, Ira's Claude Code at work, Naya's OpenCode waiting for permission, and a member who finished."""
    term = TerminalStub(S1)
    term.add(SHELL, title="bash · bakery", project_id=P1)
    term.add(IRA, title="claude · Ira", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=P1, agent=terminal_agent("claude"))
    term.add(NAYA, title="opencode · Naya", owner_kind="staff", owner_id="st-naya", owner_label="Naya", project_id=P1, agent=terminal_agent("opencode", state="waiting"),
             activity={"status": "permission", "label": "bash: npm install grammy", "level": "warn", "action": {"kind": "answer", "label": "Answer", "path": "/app/inbox"}})
    term.add(DONE, title="codex · Max", owner_kind="staff", owner_id="st-max", owner_label="Max", project_id=P1, status="exited", exit_code=0, agent=terminal_agent("codex"))
    term.emit(SHELL, "".join(f"shell line {n}\r\n" for n in range(10)))
    term.emit(IRA, "".join(f"ira line {n}\r\n" for n in range(6)) + "FAIL src/cart.test.ts > applies SPRING10\r\n")
    term.emit(NAYA, "Allow bash: npm install grammy? (y/n)\r\n")
    return term


def open_page(context, term: TerminalStub, feed: EventFeed, sent: list[tuple[str, dict]], lang: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        parts = path.split("/")
        # The members' own routes, as api_staff.py answers them: the session with its capabilities,
        # and the message, recorded with the body as sent.
        if len(parts) == 5 and parts[2] == "staff" and parts[4] == "session" and request.method == "GET":
            steer = {"st-ira": "tui_queue", "st-naya": "degrade_to_queue"}.get(parts[3], "native")
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"staff": {"id": parts[3]}, "session": None, "capabilities": {"harness": "claude", "label": "Claude Code", "steer": steer}}))
        if len(parts) == 5 and parts[2] == "staff" and parts[4] == "messages" and request.method == "POST":
            sent.append((parts[3], request.post_data_json))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"state": "queued", "message_id": f"m{len(sent)}", "mode": "now", "degraded_to": ""}))
        if len(parts) == 5 and parts[2] == "staff" and parts[4] == "messages":
            return route.fulfill(status=200, content_type="application/json", body="[]")
        return stub(route)

    page.route("**/api/**", handle)
    term.install(page)
    page.route("**/api/events**", feed.route)
    if THEME:
        page.add_init_script(f"try {{ localStorage.setItem('daedalus.appearance', JSON.stringify({{follow: false, theme: {json.dumps(THEME)}}})); }} catch (e) {{}}")
    page.goto(f"{BASE}/terminals/{IRA}?layout=4&panes={SHELL},{IRA},{NAYA},{DONE}&token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".tp-ws", timeout=20000)
    return page


def paste(page: Page, selector: str, text: str) -> None:
    """A paste into the field as the browser delivers one, with the text on its clipboard data."""
    page.eval_on_selector(selector, "(el, text) => { const data = new DataTransfer(); data.setData('text/plain', text); el.focus(); el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true })); }", text)


def shot(page: Page, name: str) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(Path(SHOTS) / name))


def desktop(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    term = fleet()
    feed = EventFeed()
    sent: list[tuple[str, dict]] = []
    context = browser.new_context(viewport={"width": 1600, "height": 960}, color_scheme="dark")
    context.add_init_script(DEBUG)
    page = open_page(context, term, feed, sent, lang)
    for tid in (SHELL, IRA, NAYA):
        wait_live(page, tid)
    page.wait_for_timeout(400)

    def pane(tid: str) -> str:
        return f".tp-pane[data-pane='{tid}']"

    # Which panes have a line, and where it sits.
    for tid, wanted in ((SHELL, 0), (IRA, 1), (NAYA, 1), (DONE, 0)):
        got = page.locator(f"{pane(tid)} .tp-compose").count()
        check.that(got == wanted, f"{lang}: pane {tid} has {got} compose lines, not {wanted}")
    order = page.eval_on_selector(pane(IRA), "(p) => [...p.children].map((c) => c.className.split(' ')[0])")
    check.that(order[-3:] == ["tp-pane-body", "tp-pane-compose", "tp-pane-foot"], f"{lang}: the pane's parts are in the order {order}")
    line = page.locator(f"{pane(IRA)} .tp-compose")
    height = line.bounding_box()["height"]  # type: ignore[index]
    check.that(31 <= height <= 33, f"{lang}: the compose line is {height:.0f} px tall, not a dense row")
    field = f"{pane(IRA)} .tp-compose-field"
    expect(page.locator(field)).to_have_attribute("placeholder", words["placeholder"])
    # The member waiting on a request: the line says the message waits for the answer.
    held = page.locator(f"{pane(NAYA)} .tp-compose")
    check.that(held.get_attribute("data-held") == "1" and words["held"] in (held.locator(".tp-compose-field").get_attribute("placeholder") or ""),
               f"{lang}: Naya's line does not say it waits for the request: {held.locator('.tp-compose-field').get_attribute('placeholder')!r}")

    # Slash hints: Claude Code's commands that begin with what is typed; arrows and Tab complete.
    page.locator(field).click()
    page.keyboard.type("/co")
    rows = page.locator(f"{pane(IRA)} .tp-slash-row")
    expect(rows).to_have_count(3)
    check.that([rows.nth(i).get_attribute("data-command") for i in range(3)] == ["compact", "context", "cost"], f"{lang}: the hints are {[rows.nth(i).get_attribute('data-command') for i in range(3)]}")
    expect(rows.nth(0).locator(".tp-slash-what")).to_contain_text(words["compact"])
    page.keyboard.press("ArrowDown")
    expect(rows.nth(1)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Tab")
    expect(page.locator(field)).to_have_value("/context ")
    expect(rows).to_have_count(0)
    page.locator(field).fill("")
    page.keyboard.type("/compact")
    expect(rows).to_have_count(0)
    page.keyboard.press("Enter")
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    check.that(sent[:1] == [("st-ira", {"text": "/compact", "when": "now"})], f"{lang}: the command went as {sent[:1]}")
    expect(page.locator(field)).to_have_value("")
    # Nothing written in the line reached the terminal's socket.
    typed = term.inputs(IRA)
    check.that(b"compact" not in typed and b"/co" not in typed, f"{lang}: the line typed into the terminal: {typed!r}")

    # A multi-line paste folds into a chip, and the message carries it whole.
    log = "npm test\nFAIL src/cart.test.ts\n  expected 9.00, got 10.00"
    paste(page, field, log)
    chip = page.locator(f"{pane(IRA)} .tp-chip.paste")
    expect(chip).to_have_count(1)
    expect(chip.locator(".tp-chip-text")).to_have_text(words["pasted"])
    expect(page.locator(field)).to_have_value("")
    page.locator(field).type("see the log")
    page.keyboard.press("Enter")
    deadline = time.monotonic() + 5
    while len(sent) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    check.that(len(sent) >= 2 and sent[1][1]["text"] == f"see the log\n\n{log}", f"{lang}: the paste went as {sent[1:2]}")
    expect(chip).to_have_count(0)
    # A short one-line paste stays in the field.
    paste(page, field, "SPRING10")
    page.wait_for_timeout(100)
    check.that(page.locator(f"{pane(IRA)} .tp-chip").count() == 0, f"{lang}: a short paste was folded")
    page.locator(field).fill("")

    # Reply to a selection: drag across the failing line, Reply, and the quote leads the message.
    screen = page.locator(f"{pane(IRA)} .xterm-screen")
    b = screen.bounding_box()
    assert b is not None
    rows_text = page.evaluate(f"() => window.__terminals?.text('{IRA}') ?? []")
    fail_row = next((i for i, r in enumerate(rows_text) if r.startswith("FAIL src/cart")), 6)
    cell_h = b["height"] / max(1, page.evaluate(f"() => window.__terminals?.size('{IRA}')?.rows ?? 24"))
    y = b["y"] + cell_h * (fail_row + 0.5)
    page.mouse.move(b["x"] + 2, y)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] * 0.6, y, steps=8)
    page.mouse.up()
    reply = page.locator(f"{pane(IRA)} .tp-compose-reply")
    expect(reply).to_be_visible(timeout=3000)
    expect(reply).to_contain_text(words["reply"])
    reply.click()
    quote = page.locator(f"{pane(IRA)} .tp-chip.quote")
    expect(quote).to_have_count(1)
    check.that("FAIL src/cart" in (quote.get_attribute("title") or ""), f"{lang}: the quote is {quote.get_attribute('title')!r}")
    page.locator(field).type("why does this fail?")
    shot(page, f"{lang}-terminals-compose.png")
    page.keyboard.press("Enter")
    deadline = time.monotonic() + 5
    while len(sent) < 3 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    body = sent[2][1]["text"] if len(sent) >= 3 else ""
    check.that(body.startswith("> FAIL src/cart") and body.endswith("\n\nwhy does this fail?"), f"{lang}: the reply went as {body!r}")

    # Naya's CLI cannot take a message into a running turn: her line sends after the turn, as the staff view does.
    naya = f"{pane(NAYA)} .tp-compose-field"
    page.locator(naya).click()
    page.keyboard.type("hold the deploy")
    page.keyboard.press("Enter")
    deadline = time.monotonic() + 5
    while len(sent) < 4 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    check.that(sent[3:4] == [("st-naya", {"text": "hold the deploy", "when": "after_turn"})], f"{lang}: Naya's message went as {sent[3:4]}")

    # Another agent's message lights Ira's tab and pane header, then lets go.
    deadline = time.monotonic() + 10
    while feed.connected() == 0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    tab = page.locator(".tp-tab[data-slot='1']")
    before = tab.get_attribute("data-state")
    feed.send("agent.message", {"message_id": "am-1", "kind": "message", "state": "sent", "from": "Max", "to": "Ira", "hop": 1, "text": "port?", "from_staff_id": "st-max", "to_staff_id": "st-ira"}, project=P1, staff="st-max")
    expect(tab).to_have_attribute("data-state", "message", timeout=4000)
    expect(page.locator(pane(IRA))).to_have_attribute("data-state", "message")
    expect(tab.locator(".tp-tab-state")).to_have_attribute("data-state", "message")
    dot = page.eval_on_selector(f"{pane(IRA)} .tp-pane-head .tp-dot", "(e) => getComputedStyle(e).backgroundColor")
    info = page.evaluate("() => { const probe = document.createElement('span'); probe.style.color = 'var(--info)'; document.body.append(probe); const c = getComputedStyle(probe).color; probe.remove(); return c; }")
    check.that(dot == info, f"{lang}: the message dot is {dot}, not the theme's information colour {info}")
    shot(page, f"{lang}-terminals-message-flash.png")
    expect(tab).to_have_attribute("data-state", before or "idle", timeout=6000)
    # A refusal, and a message to the member waiting on a request: nothing lights.
    feed.send("agent.message", {"message_id": "am-2", "kind": "message", "state": "refused", "from": "Max", "to": "Ira", "hop": 1, "text": "x", "to_staff_id": "st-ira"}, project=P1)
    feed.send("agent.message", {"message_id": "am-3", "kind": "message", "state": "sent", "from": "Max", "to": "Naya", "hop": 1, "text": "x", "to_staff_id": "st-naya"}, project=P1)
    page.wait_for_timeout(700)
    check.that(page.locator(".tp-tab[data-state='message']").count() == 0, f"{lang}: a refused message or one to a waiting member lit a tab")
    check.that(page.locator(f"{pane(NAYA)}").get_attribute("data-state") == "waiting", f"{lang}: Naya's pane stopped saying waiting")

    sideways = int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))
    check.that(sideways <= 0, f"{lang}: the workspace scrolls {sideways} px sideways")
    check.that(not page.errors, f"{lang}: the workspace threw: {page.errors}")  # type: ignore[attr-defined]
    context.close()


def phone(browser, check: Check) -> None:  # type: ignore[no-untyped-def]
    term = fleet()
    sent: list[tuple[str, dict]] = []
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", has_touch=True, is_mobile=True)
    context.add_init_script(DEBUG)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/api/**", stub)
    term.install(page)
    page.goto(f"{BASE}/terminals/{IRA}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".term-phone", timeout=20000)
    page.wait_for_timeout(500)
    check.that(page.locator(".tp-compose").count() == 0, "phone: the desktop's compose line is drawn")
    check.that(not errors and not sent, f"phone: the page threw {errors}")
    context.close()


def main() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        for lang in ("en", "ru"):
            desktop(browser, lang, check)
        phone(browser, check)
        browser.close()
    unhandled = UNHANDLED.report()
    for problem in check.problems:
        print("FAIL:", problem)
    if check.problems or unhandled:
        return 1
    print("terminal compose: every step holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
