"""Drive the agent's question card in a real browser and refuse what does not behave.

The rules are unit-tested in miniapp/src/questions/ask.test.ts; this is the card as the operator
meets it above the composer, for every kind of question the AskUser schema can express (the fixtures
are ``ASK_KINDS`` in api_stub.py): one choice with descriptions and a recommended option, several
choices, a yes/no pair, words only, options with previews, and a batch of six with its chips, its
review and the hint that names what is missing. The keys (digits pick, Enter moves on), the Reply
circle doing what the card's button does, a pick surviving the session being read again, the record
an answered question leaves in the transcript, and the card at a phone's width are checked too.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_questions_ui.py

With ``SHOTS=<dir>`` every kind is also photographed at 1440×900 and 390×844 in both themes;
``SHOTS_ONLY=1`` takes the pictures and checks nothing (for photographing an older build).
Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import ASK_KINDS, DEFAULT_APP, Unhandled, ask_turn, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
SHOTS_ONLY = os.environ.get("SHOTS_ONLY") == "1"
SESSION = "sess-ask"


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-10-11T09:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


OPENING = [message(1, "user", "Put this week's digest together."), message(2, "assistant", "I have the items. A few choices are yours before I write it.")]


class Host:
    """The session's state, and what the app posted."""

    def __init__(self) -> None:
        self.status = "waiting"
        self.pending: dict | None = None
        self.messages: list[dict] = list(OPENING)
        self.posted: list[tuple[str, dict | None]] = []
        self.reads = 0
        self.twice = 0

    def ask(self, kind: str) -> None:
        self.pending = json.loads(json.dumps(ASK_KINDS[kind]))
        self.status = "waiting"
        self.messages = [*OPENING, *ask_turn(3, f"toolu_{kind}", self.pending["questions"], None)]

    def detail(self) -> dict:
        return {
            "id": SESSION, "title": "Weekly digest", "status": self.status, "error": "", "run_id": "r1" if self.status != "idle" else None, "workspace": "/workspace",
            "workspace_name": "digest", "workspace_own": True, "workspace_sessions": [], "pending": self.pending, "model": "Claude Opus 5", "provider": "claude",
            "project": {"id": "p", "name": "Digest", "folders": folders("/workspace"), "settings": {"snapshots": True}},
            "thinking": True, "reasoning_effort": "high", "mode": "", "yagni": False, "brief": "",
            "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": {"tokens": 21000, "window": 200000, "messages": 12, "summaries": 0, "operator_turns": 2},
            "messages": self.messages,
        }


HOST = Host()


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    path = req.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if req.method != "GET":
        data = json.loads(req.post_data) if req.post_data else None
        HOST.posted.append((rel, data))
        if rel == f"/api/sessions/{SESSION}/answer":
            # As the host does: a second answer to a question already answered is refused, and
            # changes nothing — counted, because the card must never send twice.
            if HOST.pending is None:
                HOST.twice += 1
                return route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "no pending question for this session"}))
            HOST.pending = None
            HOST.status = "running"
        return route.fulfill(status=200, content_type="application/json", body="{}")
    body: object
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel == f"/api/sessions/{SESSION}":
        HOST.reads += 1
        body = HOST.detail()
    elif rel.startswith(f"/api/sessions/{SESSION}/"):
        body = []
    elif rel == "/api/settings":
        body = {"model": {"preset": "opus"}, "presets": {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000}}}
    elif rel == "/api/sessions":
        body = {"sessions": [], "projects": []}
    elif rel.startswith("/api/usage/provider/"):
        body = {"provider": "claude", "today": {"calls": 4}, "subscription": None, "balance": None}
    elif fulfil_shared(route):
        return
    else:
        UNHANDLED.record(rel)
        body = []
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def open_page(context, scheme: str = "dark", lang: str = "en", wait: str = ".composer .ask") -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme={scheme}&lang={lang}")
    page.wait_for_selector(wait, timeout=15000)
    page.wait_for_timeout(300)
    return page


def answers() -> list[dict]:
    return [data for rel, data in HOST.posted if rel.endswith("/answer")]


def sent(page: Page, before: int, problems: list[str], what: str) -> dict | None:
    """The answer the press posted, waited for rather than slept on."""
    deadline = time.monotonic() + 10
    while len(answers()) == before and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    if len(answers()) == before:
        problems.append(f"{what}: nothing reached the host")
        return None
    return answers()[-1]


def go(page: Page) -> dict:
    button = page.locator(".ask .ask-go")
    return {"go": button.get_attribute("data-go"), "disabled": button.get_attribute("aria-disabled") == "true", "text": button.inner_text().strip(), "hint": page.locator(".ask .ask-hint").inner_text().strip()}


def single(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    HOST.ask("single")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context)
    options = page.locator(".ask .ask-options[role='radiogroup'] .ask-opt")
    if options.count() != 3:
        problems.append(f"one choice with Other should draw three radio rows, not {options.count()}")
    first = options.nth(0)
    if first.locator(".ask-rec").count() != 1 or "(Recommended)" in first.locator(".ask-opt-label").inner_text():
        problems.append("the recommended option is not tagged, or still carries the marker in its label")
    if first.locator(".ask-opt-desc").count() != 1:
        problems.append("the option's description is not under its label")
    if page.locator(".ask .ask-chip").inner_text().strip().lower() != "week" or "Week ·" in page.locator(".ask .ask-text").inner_text():
        problems.append("the header is not its own chip above the question")
    if page.locator(".ask textarea").count():
        problems.append("a text field shows before Other is chosen")
    state = go(page)
    print("single, nothing chosen:", state)
    if state["go"] != "send" or not state["disabled"] or "Pick one" not in state["hint"] or state["text"] != "Send answer":
        problems.append(f"with nothing chosen the button must say Send answer, be off and say what is missing ({state})")
    # The circle is always live while the agent asks; pressed with nothing chosen it sends nothing.
    page.locator(".composer .roundbtn.primary").click()
    page.wait_for_timeout(200)
    if answers():
        problems.append("the Reply circle sent an empty answer")
    # Digits pick: 3 is Other, which opens its field and wants words.
    page.locator(".ask .ask-opt input").first.focus()
    page.keyboard.press("3")
    page.wait_for_selector(".ask .ask-other-field textarea", timeout=3000)
    state = go(page)
    if not state["disabled"] or "own answer" not in state["hint"]:
        problems.append(f"Other chosen and empty must hold the send and say so ({state})")
    page.keyboard.type("The last full week, but skip Monday")
    page.keyboard.press("2")
    if page.locator(".ask .ask-opt.on:not(.other)").count():
        problems.append("a digit typed into Other's field picked an option")
    before = len(answers())
    page.keyboard.press("Enter")
    body = sent(page, before, problems, "Enter in Other's field")
    if body and body != {"answers": [{"question": ASK_KINDS["single"]["questions"][0]["question"], "selected": [], "custom": "The last full week, but skip Monday2"}]}:
        problems.append(f"Other's words did not reach the host as the answer ({body})")
    context.close()

    # A pick survives the session being read again, and the label goes back verbatim.
    HOST.ask("single")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context)
    page.locator(".ask .ask-opt").first.click()
    reads = HOST.reads
    deadline = time.monotonic() + 30
    while HOST.reads == reads and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    page.wait_for_timeout(300)
    if page.locator(".ask .ask-opt.on").count() != 1:
        problems.append("reading the session again dropped the pick")
    if go(page)["disabled"]:
        problems.append("a picked option did not free the send")
    before = len(answers())
    page.locator(".composer .roundbtn.primary").click()
    body = sent(page, before, problems, "the Reply circle")
    if body and body["answers"][0]["selected"] != ["The one that just ended (Recommended)"]:
        problems.append(f"the recommended label did not go back verbatim ({body})")
    context.close()
    return problems


def kinds(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    # Several: checkboxes, digits toggle, arrows move, the count shows.
    HOST.ask("multi")
    page = open_page(context)
    if page.locator(".ask .ask-options[role='group'] input[type='checkbox']").count() != 4:
        problems.append("several choices are not four checkbox rows")
    page.locator(".ask .ask-opt input").first.focus()
    page.keyboard.press("1")
    page.keyboard.press("3")
    # A digit leaves the focus on the row it picked, and the arrow goes on from there.
    page.keyboard.press("ArrowDown")
    focused = page.evaluate("() => document.activeElement && document.activeElement.closest('.ask-opt')?.dataset.label")
    if focused != "Numbers":
        problems.append(f"ArrowDown did not move along the checkboxes ({focused!r})")
    if "2 chosen" not in page.locator(".ask .ask-kind").inner_text():
        problems.append("the number chosen is not shown")
    before = len(answers())
    page.keyboard.press("Enter")
    body = sent(page, before, problems, "Enter on several")
    if body and body["answers"][0]["selected"] != ["Releases", "Reading list"]:
        problems.append(f"several picks did not reach the host in order ({body})")
    page.close()

    # Yes/no: two equal buttons side by side, no Other, no radio marks.
    HOST.ask("yesno")
    page = open_page(context)
    pair = page.locator(".ask .ask-options[data-layout='pair'] .ask-opt")
    boxes = [pair.nth(i).bounding_box() for i in range(pair.count())]
    if len(boxes) != 2 or not all(boxes) or abs(boxes[0]["y"] - boxes[1]["y"]) > 1 or abs(boxes[0]["width"] - boxes[1]["width"]) > 2:  # type: ignore[index]
        problems.append(f"a yes/no is not two equal buttons side by side ({boxes})")
    if page.locator(".ask .ask-mark").count() or page.locator(".ask .ask-opt.other").count():
        problems.append("a yes/no draws radio marks or an Other row")
    page.close()

    # Words only: the field is the answer, shown at once.
    HOST.ask("text")
    page = open_page(context)
    if page.locator(".ask .ask-opt").count() or page.locator(".ask textarea.ask-field").count() != 1:
        problems.append("a free-text question must be a field and nothing else")
    if "Write your answer" not in go(page)["hint"]:
        problems.append("an empty free-text answer does not say what is missing")
    page.locator(".ask textarea").fill("Week 41: one release, one outage, one fix")
    before = len(answers())
    page.locator(".ask .ask-go").click()
    body = sent(page, before, problems, "Send on words only")
    if body and body["answers"][0] != {"question": ASK_KINDS["text"]["questions"][0]["question"], "selected": [], "custom": "Week 41: one release, one outage, one fix"}:
        problems.append(f"the words did not go as the answer ({body})")
    page.close()

    # Previews: beside the list on a desktop, following the choice.
    HOST.ask("preview")
    page = open_page(context)
    split = page.locator(".ask .ask-split")
    preview = page.locator(".ask .ask-preview")
    if not split.count() or "Digest · week 41" not in preview.inner_text():
        problems.append("the preview pane is missing or does not start on the first option's preview")
    else:
        lb, pb = page.locator(".ask .ask-options").bounding_box(), preview.bounding_box()
        if not lb or not pb or pb["x"] < lb["x"] + lb["width"] - 1:
            problems.append("on a desktop the preview is not beside the options")
    page.locator(".ask .ask-opt", has_text="Two columns").click()
    page.mouse.move(5, 5)
    if "Releases| Incidents" not in preview.inner_text():
        problems.append("the preview did not follow the chosen option")
    page.close()
    context.close()
    return problems


def batch(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    HOST.ask("batch")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context)
    steps = page.locator(".ask .ask-step")
    if steps.count() != 7:
        problems.append(f"six questions should have six chips and the review, not {steps.count()}")
    if page.locator(".ask .ask-count").inner_text().strip() != "1 of 6":
        problems.append("the progress does not read 1 of 6")
    if page.locator(".ask .ask-text").count() != 1:
        problems.append("more than one question is on screen at once")
    state = go(page)
    if state["go"] != "next" or not state["disabled"]:
        problems.append(f"the first of six must offer Next, off until answered ({state})")
    page.locator(".ask .ask-opt").nth(1).click()
    page.locator(".ask .ask-go").click()
    page.wait_for_selector(".ask[data-step='1']", timeout=3000)
    if page.locator(".ask .ask-step").nth(0).get_attribute("data-state") != "done":
        problems.append("the answered question's chip is not marked done")
    # Back keeps what was chosen.
    page.locator(".ask .ask-back").click()
    page.wait_for_selector(".ask[data-step='0']", timeout=3000)
    if page.locator(".ask .ask-opt.on").count() != 1:
        problems.append("going back lost the answer")
    page.keyboard.press("Enter")
    page.wait_for_selector(".ask[data-step='1']", timeout=3000)
    # Focus moved into the card with the step, so the digits work straight away.
    page.keyboard.press("1")
    page.keyboard.press("Enter")
    page.wait_for_selector(".ask[data-step='2']", timeout=3000)
    # A chip jumps anywhere: the last question offers the review.
    page.locator(".ask .ask-step").nth(5).click()
    page.wait_for_selector(".ask[data-step='5']", timeout=3000)
    page.locator(".ask .ask-opt").first.click()
    state = go(page)
    if state["go"] != "review" or state["text"] != "Review answers":
        problems.append(f"the last of six must lead to the review ({state})")
    page.locator(".ask .ask-go").click()
    page.wait_for_selector(".ask[data-step='review'] .ask-review", timeout=3000)
    rows = page.locator(".ask .ask-review-row")
    missing = page.locator(".ask .ask-review-row.missing")
    print("review:", rows.count(), "rows,", missing.count(), "missing;", go(page))
    if rows.count() != 6 or missing.count() != 3:
        problems.append(f"the review should list six answers with three missing ({rows.count()}, {missing.count()})")
    state = go(page)
    if state["go"] != "send" or not state["disabled"] or "3 questions left" not in state["hint"] or "Send" not in state["hint"] or "Subject" not in state["hint"]:
        problems.append(f"the send must be off and name what is missing ({state})")
    if "Plain and short" not in rows.nth(5).inner_text() or "Releases" not in rows.nth(1).inner_text():
        problems.append("the review does not show the chosen answers")
    # Answer the missing ones from the review's own links.
    missing.first.locator(".ask-review-edit").click()
    page.wait_for_selector(".ask[data-step='2']", timeout=3000)
    page.locator(".ask .ask-opt", has_text="Yes").click()
    page.locator(".ask .ask-go").click()
    page.wait_for_selector(".ask[data-step='3'] textarea", timeout=3000)
    page.locator(".ask textarea").fill("Week 41, in one page")
    page.keyboard.press("Enter")
    page.wait_for_selector(".ask[data-step='4']", timeout=3000)
    page.locator(".ask .ask-opt", has_text="Cards").click()
    page.locator(".ask .ask-step.review").click()
    page.wait_for_selector(".ask[data-step='review']", timeout=3000)
    state = go(page)
    if state["disabled"] or state["text"] != "Send answers":
        problems.append(f"with every answer given the review must offer Send answers ({state})")
    before = len(answers())
    page.locator(".ask .ask-go").click()
    body = sent(page, before, problems, "Send answers")
    if body:
        got = [(a["selected"], a["custom"]) for a in body["answers"]]
        want = [(["The running week"], None), (["Releases"], None), (["Yes"], None), ([], "Week 41, in one page"), (["Cards"], None), (["Plain and short"], None)]
        if got != want:
            problems.append(f"the batch did not reach the host as answered ({got})")
    context.close()

    # Two questions: no review on the way, the last one sends once both are answered.
    HOST.ask("pair")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context)
    page.locator(".ask .ask-opt").first.click()
    page.keyboard.press("Enter")
    page.wait_for_selector(".ask[data-step='1']", timeout=3000)
    page.locator(".ask .ask-opt", has_text="No").click()
    state = go(page)
    if state["go"] != "send" or state["disabled"]:
        problems.append(f"the last of two must send directly once both are answered ({state})")
    context.close()
    return problems


def record(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    record_messages()
    HOST.messages = [
        *HOST.messages,
        message(6, "user", "Now the reminder for Friday."),
        *ask_turn(7, "toolu_timeout", [ASK_KINDS["yesno"]["questions"][0]], [{"question": "", "selected": [], "custom": "No operator is available for this unattended run."}], source="timeout"),
        message(9, "assistant", "I went ahead without sending."),
        message(10, "user", "And the archive?"),
        *ask_turn(11, "toolu_stopped", [ASK_KINDS["text"]["questions"][0]], None),
        message(12, "assistant", "Stopped."),
    ]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, wait=".ask-record")
    records = page.locator(".ask-record")
    states = [records.nth(i).get_attribute("data-state") for i in range(records.count())]
    print("records:", states)
    if states != ["answered", "expired", "unanswered"]:
        problems.append(f"the three AskUser calls should read answered, expired, unanswered ({states})")
    rows = page.locator(".ask-record[data-state='answered'] .ask-record-row")
    if rows.count() != 6:
        problems.append(f"the answered record is not one line per question ({rows.count()})")
    else:
        first, second = rows.nth(0).inner_text(), rows.nth(1).inner_text()
        if "Week" not in first or "The one that just ended" not in first or "(Recommended)" in first or "Releases, Numbers" not in second:
            problems.append(f"the record does not read as what was chosen ({first!r}, {second!r})")
    if "Nobody answered" not in page.locator(".ask-record[data-state='expired']").inner_text():
        problems.append("the timed-out question does not say nobody answered")
    if page.locator(".composer .ask").count():
        problems.append("the card stayed with nothing pending")
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    HOST.ask("batch")
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context)
    card = page.locator(".composer .ask").bounding_box()
    if not card or card["x"] < 0 or card["x"] + card["width"] > 390 or card["width"] < 340:
        problems.append(f"the phone card is not the full width of the screen ({card})")
    heights = [page.locator(".ask .ask-opt").nth(i).bounding_box()["height"] for i in range(page.locator(".ask .ask-opt").count())]  # type: ignore[index]
    if not heights or min(heights) < 44:
        problems.append(f"a phone option is smaller than a thumb's 44 px ({heights})")
    button = page.locator(".ask .ask-go").bounding_box()
    if not button or button["height"] < 44:
        problems.append(f"the phone's primary is under 44 px ({button})")
    if page.evaluate("() => getComputedStyle(document.querySelector('.ask .ask-foot')).position") != "sticky":
        problems.append("the phone's action bar is not kept in reach")
    if page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"):
        problems.append("the phone page scrolls sideways with the card open")
    page.locator(".ask .ask-opt").first.tap()
    page.locator(".ask .ask-go").tap()
    page.wait_for_selector(".ask[data-step='1']", timeout=3000)
    # The preview goes under the list on a phone.
    page.locator(".ask .ask-step").nth(4).tap()
    page.wait_for_selector(".ask[data-step='4'] .ask-preview", timeout=3000)
    lb, pb = page.locator(".ask .ask-options").bounding_box(), page.locator(".ask .ask-preview").bounding_box()
    if not lb or not pb or pb["y"] < lb["y"] + lb["height"] - 1:
        problems.append("on a phone the preview is not under the options")
    context.close()
    return problems


SHOT_KINDS = ["single", "multi", "yesno", "text", "preview", "batch", "batch-review", "record"]


def shots(browser, out: Path, lang: str = "en") -> None:  # type: ignore[no-untyped-def]
    """Every kind in both themes at both sizes. The review is reached by its chip (or, on a build
    without one, the plain batch is photographed again)."""
    out.mkdir(parents=True, exist_ok=True)
    for scheme in ("dark", "light"):
        for width, height, size in ((1440, 900, "desktop"), (390, 844, "phone")):
            mobile = width < 1024
            context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, is_mobile=mobile, has_touch=mobile, reduced_motion="reduce")
            context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
            for kind in SHOT_KINDS:
                if kind == "record":
                    record_messages()
                    page = open_page(context, scheme, lang, wait=".chat, .screen")
                    page.wait_for_timeout(600)
                else:
                    HOST.ask("batch" if kind == "batch-review" else kind)
                    page = open_page(context, scheme, lang, wait=".composer .ask, .composer .dock.question")
                    if kind == "batch":
                        tap = page.locator(".ask .ask-opt").first
                        if tap.count():
                            tap.click()
                            page.locator(".ask .ask-go").click()
                            page.locator(".ask .ask-opt").nth(1).click()
                    if kind == "batch-review" and page.locator(".ask .ask-step.review").count():
                        # Four of the six answered, so the review shows answers and what is missing.
                        for step, option in ((0, 1), (1, 0), (2, 0), (5, 0)):
                            page.locator(".ask .ask-step").nth(step).click()
                            page.locator(".ask .ask-opt").nth(option).click()
                        page.locator(".ask .ask-step.review").click()
                    if kind == "single" and page.locator(".ask .ask-opt").count():
                        page.locator(".ask .ask-opt").first.click()
                    if kind == "multi" and page.locator(".ask .ask-opt").count():
                        page.locator(".ask .ask-opt").nth(0).click()
                        page.locator(".ask .ask-opt").nth(3).click()
                    page.mouse.move(1, 1)
                    page.wait_for_timeout(300)
                page.screenshot(path=str(out / f"{size}-{scheme}-{kind}.png"))
                page.close()
            context.close()


def record_messages() -> None:
    questions = ASK_KINDS["batch"]["questions"]
    HOST.pending, HOST.status = None, "idle"
    HOST.messages = [
        *OPENING,
        *ask_turn(3, "toolu_done", questions, [
            {"question": questions[0]["question"], "selected": ["The one that just ended (Recommended)"], "custom": None},
            {"question": questions[1]["question"], "selected": ["Releases", "Numbers"], "custom": None},
            {"question": questions[2]["question"], "selected": ["Yes"], "custom": None},
            {"question": questions[3]["question"], "selected": [], "custom": "Week 41, in one page"},
            {"question": questions[4]["question"], "selected": ["One column"], "custom": None},
            {"question": questions[5]["question"], "selected": [], "custom": "Plain, and thank the testers"},
        ]),
        message(5, "assistant", "Done: the digest is drafted, one page, sections Releases and Numbers."),
    ]


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        if not SHOTS_ONLY:
            problems += single(browser)
            problems += kinds(browser)
            problems += batch(browser)
            problems += record(browser)
            problems += phone(browser)
            if HOST.twice:
                problems.append(f"an answer was sent again after the question was answered ({HOST.twice} times)")
        if SHOTS:
            shots(browser, Path(SHOTS), os.environ.get("LANG_UI", "en"))
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or (0 if SHOTS_ONLY else UNHANDLED.report()))
