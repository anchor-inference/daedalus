"""Drive the composer in a real browser and refuse what does not behave.

The composer's one circle has a unit test for what it means (miniapp/src/composer.test.ts); this is
the part that only exists once it is drawn against a host: a message goes out and the primary is
Send; while a run is on an empty pill is Stop and a written one queues a steer, which appears as a
card above the pill and is withdrawn with its ×; a tool call the policy refused is a dock above the
pill whose Allow once spends the key; the agent's question is a dock whose answer the circle sends
as Reply; the model list opens from inside the pill and a pick reaches the host; and while another
model stands in the selector says so and offers the way back.

The host is a stub in this file with the state a real one would hold — the session's status, the
queue, what was posted — so every step can be read back as the request the app made.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root/app && cp -r dist/* /tmp/app-root/app/
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_composer.py

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
from api_stub import DEFAULT_APP, GATES, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SESSION = "sess-1"
CONFIGURED = "claude-opus-5"
STANDBY = "deepseek-flash"

PRESETS = {
    "local.model": {"provider": "local", "model": "local-model", "label": "Local model", "thinking": False, "reasoning_effort": "", "images": True, "context_window": 128000, "max_output_tokens": 16384},
    "opus": {"provider": "claude", "model": CONFIGURED, "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000},
    "flash": {"provider": "deepseek", "model": STANDBY, "label": "DeepSeek Flash", "thinking": False, "reasoning_effort": "", "images": False, "context_window": 128000, "max_output_tokens": 16384},
}


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-09-18T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


class Host:
    """What the invented host holds, and what the app asked of it."""

    def __init__(self) -> None:
        self.status = "idle"
        self.error = ""
        self.queue: list[dict] = []
        self.posted: list[tuple[str, str, dict | None]] = []
        self.pending: dict | None = None
        self.fallback: dict | None = None
        self.thinking = True
        self.effort = "high"
        self.messages = [message(101, "user", "Check the run and tell me what the log says."), message(102, "assistant", "One slow query on the events table; the plan is below.")]
        self.steer_route = True
        self.n = 0
        self.mode = ""
        self.yagni = False
        # How many times the app has read the session: a re-read is what once wiped a picked answer.
        self.reads = 0

    def detail(self) -> dict:
        return {
            "id": SESSION, "title": "A session", "status": self.status, "error": self.error, "run_id": "r1" if self.status == "running" else None, "workspace": "/workspace",
            "workspace_name": "ws", "workspace_own": True, "workspace_sessions": [], "pending": self.pending, "model": "Claude Opus 5", "provider": "claude",
            "project": {"id": "p", "name": "Project", "folders": folders("/workspace"), "settings": {"snapshots": True}},
            "configured_model": CONFIGURED, "effective_model": STANDBY if self.fallback else CONFIGURED, "fallback": self.fallback,
            "thinking": self.thinking, "reasoning_effort": self.effort, "mode": self.mode, "yagni": self.yagni, "brief": "",
            "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": {"tokens": 42000, "window": 200000, "messages": 38, "summaries": 1, "operator_turns": 6},
            "messages": self.messages,
        }

    def refuse(self, key: str) -> None:
        self.messages = self.messages + [
            message(103, "assistant", "", tool_calls=[{"id": "c_rm", "name": "Exec", "arguments": {"command": "rm -rf build"}}]),
            message(104, "tool", "", tool_results=[{"id": "c_rm", "content": f"refused by policy: destructive command.\nApproval key: {key}", "is_error": True}]),
        ]


HOST = Host()


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    url = req.url
    path = url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    body: object
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if req.method != "GET":
        data = json.loads(req.post_data) if req.post_data else None
        HOST.posted.append((req.method, rel, data))
        if rel == f"/api/sessions/{SESSION}/messages":
            if HOST.status == "running":
                HOST.n += 1
                HOST.queue.append({"id": f"q_{HOST.n:04d}", "text": str((data or {}).get("text", "")), "queued_at": "2026-09-18T12:01:00+00:00"})
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"run_id": "r2"}))
        if rel.startswith(f"/api/sessions/{SESSION}/steer/"):
            sid = rel.rsplit("/", 1)[1]
            before = len(HOST.queue)
            HOST.queue = [q for q in HOST.queue if q["id"] != sid]
            if len(HOST.queue) == before:
                return route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "that message has already reached the agent"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"deleted": True}))
        if rel == f"/api/sessions/{SESSION}/stop":
            HOST.status = "idle"
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if rel == f"/api/sessions/{SESSION}/answer":
            HOST.pending = None
            HOST.status = "running"
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if rel == f"/api/sessions/{SESSION}/model":
            if isinstance(data, dict) and "thinking" in data:
                HOST.thinking = bool(data["thinking"])
            if isinstance(data, dict) and data.get("reasoning_effort"):
                HOST.effort = str(data["reasoning_effort"])
                HOST.thinking = bool(data.get("thinking", True))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"model": "DeepSeek Flash", "thinking": HOST.thinking, "reasoning_effort": HOST.effort}))
        if rel == f"/api/sessions/{SESSION}/mode":
            HOST.mode = str((data or {}).get("mode") or "")
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"mode": HOST.mode}))
        if rel == f"/api/sessions/{SESSION}/yagni":
            HOST.yagni = bool((data or {}).get("on"))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"yagni": HOST.yagni}))
        if rel in (f"/api/sessions/{SESSION}/retry", f"/api/sessions/{SESSION}/revert"):
            seq = int(data["seq"])
            through = max(m["seq"] for m in HOST.messages)
            before = len(HOST.messages)
            HOST.messages = [m for m in HOST.messages if m["seq"] < seq]
            dropped = before - len(HOST.messages)
            if rel.endswith("/retry"):
                HOST.messages.append(message(through + 1, "assistant", "Replacement answer"))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"seq": seq, "through": through, "dropped": dropped, "workspace_restored": False, "untouched": []}))
        return route.fulfill(status=200, content_type="application/json", body="{}")
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel == f"/api/sessions/{SESSION}/steer":
        if not HOST.steer_route:
            return route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "Not Found"}))
        body = HOST.queue
    elif rel == f"/api/sessions/{SESSION}":
        HOST.reads += 1
        body = HOST.detail()
    elif rel.startswith(f"/api/sessions/{SESSION}/"):
        body = []
    elif rel == "/api/settings":
        body = {"model": {"preset": "opus"}, "presets": PRESETS}
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


def open_page(context, phone: bool = False) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.wait_for_timeout(400)
    return page


def primary(page: Page) -> str:
    return page.locator(".composer .roundbtn.primary").get_attribute("data-action") or ""


def field(page: Page):  # type: ignore[no-untyped-def]
    return page.locator(".composer textarea")


def posts(kind: str, method: str | None = None) -> list[tuple[str, str, dict | None]]:
    return [p for p in HOST.posted if p[1].endswith(kind) and (method is None or p[0] == method)]


def reached(page: Page, kind: str, before: int, what: str, problems: list[str], method: str | None = None) -> bool:
    """Wait until the request a press makes has reached the host, rather than for a fixed time.

    The host here is shared state the check changes between steps (the session's status, its
    question). A request the app sent after a fixed wait ran out — a loaded machine is enough — was
    answered in the next step instead, and flipped that step's state under it: a stop that arrived
    after the check had set the session running again made it idle, and the reloaded page waited
    fifteen seconds for a Stop that could not come. Every press whose request changes that state is
    followed by this, so the next step starts from the state it set. Ten seconds is a missing
    request, and says so, instead of a stall somewhere later."""
    deadline = time.monotonic() + 10
    while len(posts(kind, method)) <= before:
        if time.monotonic() > deadline:
            problems.append(f"{what}: the request never reached the host")
            return False
        page.wait_for_timeout(50)
    return True


def desktop(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = open_page(context)
    # Nothing left over from an earlier run: the draft this check writes is the one it reads back.
    page.evaluate("() => localStorage.removeItem('daedalus.draft.sess-1')")

    # Idle: the circle is Send, disabled until there is something to send.
    print("idle primary:", primary(page), "disabled:", page.locator(".composer .roundbtn.primary").is_disabled())
    if primary(page) != "send" or not page.locator(".composer .roundbtn.primary").is_disabled():
        problems.append("at rest the circle is not a disabled Send")
    box = page.locator(".composer-box").bounding_box()
    print("card at rest:", box)
    if not box:
        problems.append("the composer card is missing")
    field(page).fill("a")
    page.wait_for_timeout(100)
    typed_box = page.locator(".composer-box").bounding_box()
    if box and typed_box and abs(box["y"] - typed_box["y"]) > 1:
        problems.append("typing the first character moved the idle composer")
    field(page).fill("")
    if not page.locator(".composer .model-select").count() or "Opus" not in page.locator(".composer .model-select").inner_text():
        problems.append("the model selector is not in the pill")
    if not page.locator(".composer .ctx-ring").count():
        problems.append("the context ring is not in the pill")
    ring = page.locator(".composer .ctx-ring").get_attribute("title") or ""
    if "21%" not in ring or "38" not in ring:
        problems.append(f"the ring's tooltip does not carry the numbers ({ring!r})")
    # The percentage is written beside the ring: an unlabelled partial circle at rest read as a spinner.
    if page.locator(".composer .ctx-ring .ctx-pct").inner_text().strip() != "21%":
        problems.append("the ring has no percentage beside it")
    if not page.locator(".composer .effort-select").count():
        problems.append("the effort selector is not in the pill")
    if "high" not in page.locator(".composer .effort-select").inner_text().lower():
        problems.append("the current effort is not on the chip")

    page.locator(".composer .plus").click()
    page.wait_for_selector(".plus-menu", timeout=5000)
    plus_menu = page.locator(".plus-menu").bounding_box()
    plus_btn = page.locator(".composer .plus").bounding_box()
    print("plus menu:", plus_menu)
    if not plus_menu or plus_menu["width"] > 360:
        problems.append(f"the plus menu is stretched ({plus_menu})")
    if plus_menu and plus_btn and plus_menu["y"] + plus_menu["height"] > plus_btn["y"] + 4:
        problems.append("the plus menu did not open upward")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # Typing: Shift+Enter is a new line, Enter sends, the draft is remembered while it is being written.
    field(page).click()
    field(page).type("first line")
    page.keyboard.press("Shift+Enter")
    field(page).type("second line")
    if field(page).input_value() != "first line\nsecond line":
        problems.append(f"Shift+Enter did not insert a line ({field(page).input_value()!r})")
    if primary(page) != "send" or page.locator(".composer .roundbtn.primary").is_disabled():
        problems.append("with a draft the circle is not an enabled Send")
    two = page.locator(".composer-box").bounding_box()
    if not two or two["height"] <= box["height"]:
        problems.append("the pill did not grow with a second line")
    # The draft is stored after a pause in typing; wait for it rather than for a guess at the pause.
    try:
        page.wait_for_function("() => localStorage.getItem('daedalus.draft.sess-1') === 'first line\\nsecond line'", timeout=10000)
    except Exception:  # noqa: BLE001 - read back and reported below
        pass
    stored = page.evaluate("() => localStorage.getItem('daedalus.draft.sess-1')")
    if stored != "first line\nsecond line":
        problems.append(f"the draft was not remembered ({stored!r})")
    page.reload()
    page.wait_for_selector(".composer textarea", timeout=15000)
    page.wait_for_timeout(300)
    if field(page).input_value() != "first line\nsecond line":
        problems.append(f"the draft did not come back after a reload ({field(page).input_value()!r})")
    field(page).click()
    page.keyboard.press("Enter")
    reached(page, "/messages", 0, "Enter", problems)
    sent = posts("/messages")
    print("sent:", sent)
    first_id = sent[0][2].get("client_message_id") if len(sent) == 1 else None
    if len(sent) != 1 or sent[0][2].get("text") != "first line\nsecond line" or not first_id or len(first_id) > 64:
        problems.append(f"Enter did not send the draft as a message ({sent})")
    if field(page).input_value() != "":
        problems.append("the field was not cleared after sending")
    if page.evaluate("() => localStorage.getItem('daedalus.draft.sess-1')"):
        problems.append("the stored draft was not cleared after sending")

    # A run is on: an empty pill is Stop; a written one queues a steer, which becomes a card with a ×.
    HOST.status = "running"
    page.reload()
    page.wait_for_selector(".composer .roundbtn.primary[data-action='stop']", timeout=15000)
    print("running primary:", primary(page))
    running_box = page.locator(".composer-box").bounding_box()
    field(page).click()
    field(page).type("also look at the log")
    if primary(page) != "queue":
        problems.append(f"a draft during a run is {primary(page)!r}, not queue")
    page.wait_for_timeout(100)
    queue_box = page.locator(".composer-box").bounding_box()
    if running_box and queue_box and abs(running_box["y"] - queue_box["y"]) > 1:
        problems.append("typing a steer moved the composer")
    if page.locator(".composer-foot").count():
        problems.append("a dynamic footer still changes the composer's height")
    hint = page.locator(".composer .roundbtn.primary").get_attribute("title") or ""
    if "next step" not in hint:
        problems.append(f"the queue state carries no hint ({hint!r})")
    page.locator(".composer .roundbtn.primary").click()
    page.wait_for_selector(".composer .steer", timeout=5000)
    steered = posts("/messages")[-1]
    print("steered:", steered)
    steer_id = steered[2].get("client_message_id")
    if steered[2].get("text") != "also look at the log" or steered[2].get("steer") is not True or not steer_id or len(steer_id) > 64 or steer_id == first_id:
        problems.append(f"the steer was not posted as one ({steered})")
    card = page.locator(".composer .steer")
    if card.count() != 1 or "also look at the log" not in card.inner_text():
        problems.append("the queued steer is not a card above the pill")
    if primary(page) != "stop":
        problems.append(f"after queuing, the circle is {primary(page)!r}, not stop")
    card.locator(".steer-x").click()
    reached(page, "/steer/q_0001", 0, "the steer's ×", problems, method="DELETE")
    page.wait_for_timeout(100)
    if page.locator(".composer .steer").count():
        problems.append("the card stayed after its × was pressed")
    deleted = [p for p in HOST.posted if p[0] == "DELETE"]
    print("withdrawn:", deleted)
    if not deleted or not deleted[-1][1].endswith("/steer/q_0001"):
        problems.append(f"the × did not DELETE the steer ({deleted})")

    # Ctrl+Shift+S stops the run, after the confirm.
    field(page).click()
    page.keyboard.press("Control+Shift+S")
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn.danger").click()
    reached(page, "/stop", 0, "the stop shortcut", problems)
    HOST.status = "idle"

    # A host without the route: no cards, no complaints.
    HOST.steer_route = False
    HOST.status = "running"
    page.reload()
    page.wait_for_selector(".composer .roundbtn.primary[data-action='stop']", timeout=15000)
    page.wait_for_timeout(400)
    if page.locator(".composer .steer").count():
        problems.append("a host without the steer route still draws cards")
    HOST.steer_route = True
    HOST.status = "idle"

    # A refused tool call: the dock offers it once; Allow once spends the key.
    HOST.refuse("0123456789ab")
    page.reload()
    page.wait_for_selector(".composer .dock.approval", timeout=15000)
    dock = page.locator(".composer .dock.approval")
    print("approval dock:", dock.inner_text().replace("\n", " | "))
    if "Exec" not in dock.inner_text() or "rm -rf build" not in dock.inner_text():
        problems.append("the dock does not name the refused call")
    before = len(posts("/policy/grant"))
    page.keyboard.press("y")
    reached(page, "/policy/grant", before, "Allow once", problems)
    page.wait_for_timeout(100)
    granted = posts("/policy/grant")
    print("granted:", granted)
    if not granted or granted[-1][2] != {"key": "0123456789ab"}:
        problems.append(f"the approval did not spend the key ({granted})")
    if page.locator(".composer .dock.approval").count():
        problems.append("the dock stayed after the key was spent")

    # Refusing tells the host as well, so the request is closed wherever else it is shown.
    HOST.messages = HOST.messages[:2]
    HOST.refuse("abcdef012345")
    page.reload()
    page.wait_for_selector(".composer .dock.approval", timeout=15000)
    before = len(posts("/policy/refuse"))
    page.keyboard.press("n")
    reached(page, "/policy/refuse", before, "refusing", problems)
    page.wait_for_timeout(100)
    refused = posts("/policy/refuse")
    print("refused:", refused)
    if not refused or refused[-1][2] != {"key": "abcdef012345"}:
        problems.append(f"refusing did not reach the host ({refused})")
    if page.locator(".composer .dock.approval").count():
        problems.append("the dock stayed after the call was refused")

    # The agent's question: a dock with the options; the circle reads Reply and sends the answer.
    HOST.messages = HOST.messages[:2]
    HOST.status = "waiting"
    HOST.pending = {"questions": [{"question": "Keep it to the usual 8?", "header": "Length", "options": [{"label": "Top 8", "description": "the usual"}, {"label": "All 14"}], "allow_custom": True}]}
    page.reload()
    page.wait_for_selector(".composer .dock.question", timeout=15000)
    print("waiting primary:", primary(page))
    if primary(page) != "reply":
        problems.append(f"with a question open the circle is {primary(page)!r}, not reply")
    if page.locator(".composer .dock.question .btn.option").count() != 2:
        problems.append("the question's options are not buttons in the dock")
    page.locator(".composer .dock.question .btn.option", has_text="Top 8").click()
    # The session is read again while the operator is still answering — its stream ends and is
    # re-opened, the safety-net poll runs — and each read brings the same question as a new object.
    # The pick must survive that read; it once did not, and Reply then sent nothing. Wait for one.
    reads = HOST.reads
    deadline = time.monotonic() + 30
    while HOST.reads == reads and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    if HOST.reads == reads:
        problems.append("the session was not read again while the question was open")
    page.wait_for_timeout(300)
    if page.locator(".composer .dock.question .btn.option.selected", has_text="Top 8").count() != 1:
        problems.append("reading the session again dropped the answer the operator had picked")
    before = len(posts("/answer"))
    page.locator(".composer .roundbtn.primary").click()
    reached(page, "/answer", before, "Reply", problems)
    answered = posts("/answer")
    print("answered:", answered)
    if not answered or answered[-1][2] != {"answers": [{"question": "Keep it to the usual 8?", "selected": ["Top 8"], "custom": None}]}:
        problems.append(f"Reply did not send the chosen answer ({answered})")
    HOST.status = "idle"

    # The model list: opens from the pill, names the presets with their kind, a pick reaches the host.
    page.reload()
    page.wait_for_selector(".composer .model-select", timeout=15000)
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-list .model-row", timeout=5000)
    providers = page.locator(".provider-row")
    if providers.count() != 3 or page.locator(".model-list .model-row.on").count():
        problems.append("the first step must list providers rather than every model")
    page.locator('.provider-row[data-provider="claude"]').click()
    if not page.locator(".model-list .model-row.on", has_text="Claude Opus 5").count():
        problems.append("the current model is not marked within its provider")
    if page.locator(".model-list .model-row", has_text="DeepSeek Flash").count():
        problems.append("another provider's model leaked into this group")
    page.locator(".provider-back").click()
    page.locator('.provider-row[data-provider="local"]').click()
    local = page.locator(".model-list .model-row", has_text="Local model")
    if not local.locator('[title="128,000 token context"]').count() or "128k" not in local.inner_text():
        problems.append("the local preset lost its discovered context window")
    before = len(posts("/model"))
    local.click()
    reached(page, "/model", before, "the local preset", problems)
    if posts("/model")[-1][2] != {"preset": "local.model"}:
        problems.append("the local model was not chosen as a preset")
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-list")
    before = len(posts("/model"))
    page.locator('.provider-row[data-provider="deepseek"]').click()
    page.locator(".model-list .model-row", has_text="DeepSeek Flash").click()
    reached(page, "/model", before, "the model pick", problems)
    page.wait_for_timeout(100)
    picked = posts("/model")
    print("picked:", picked)
    if not picked or picked[-1][2] != {"preset": "flash"}:
        problems.append(f"the pick did not reach the host ({picked})")
    if page.locator(".model-list").count():
        problems.append("the list stayed open after a pick")
    field(page).click()
    page.keyboard.press("Control+M")
    page.wait_for_selector(".model-list", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if page.locator(".model-list").count():
        problems.append("Escape did not close the model list")

    page.locator(".composer .effort-select").click()
    page.wait_for_selector(".effort-options", timeout=5000)
    effort_menu = page.locator(".effort-menu").bounding_box()
    chip = page.locator(".composer .effort-select").bounding_box()
    if effort_menu and chip and effort_menu["y"] + effort_menu["height"] > chip["y"] + 4:
        problems.append("the effort menu did not open upward")
    before = len(posts("/model"))
    page.locator('.effort-option').filter(has=page.locator('input[value="xhigh"]')).click()
    reached(page, "/model", before, "the effort pick", problems)
    efforted = [p for p in posts("/model") if isinstance(p[2], dict) and p[2].get("reasoning_effort")]
    print("effort:", efforted[-1] if efforted else None)
    if not efforted or efforted[-1][2] != {"thinking": True, "reasoning_effort": "xhigh"}:
        problems.append(f"the effort pick did not reach the host ({efforted})")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # The fallback state: the button is amber and names both models; the list offers the way back.
    HOST.fallback = {"from": CONFIGURED, "to": STANDBY, "reason": "rate_limit"}
    page.reload()
    page.wait_for_selector(".composer .model-select.attn", timeout=15000)
    label = page.locator(".composer .model-select").inner_text()
    print("fallback label:", label)
    if "flash" not in label or "opus" not in label:
        problems.append(f"the selector does not say which model stands in for which ({label!r})")
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".model-list .model-row.restore", timeout=5000)
    before = len(posts("/model"))
    page.locator(".model-list .model-row.restore").click()
    reached(page, "/model", before, "the way back", problems)
    restored = posts("/model")[-1]
    print("restored:", restored)
    if restored[2] != {"preset": "opus"}:
        problems.append(f"the way back did not pick the configured preset ({restored})")
    HOST.fallback = None
    context.close()
    return problems


def phone(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    HOST.status = "idle"
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
    page = open_page(context, phone=True)
    pill = page.locator(".composer-box").bounding_box()
    if not pill or pill["x"] < 0 or pill["x"] + pill["width"] > 391:
        problems.append(f"phone: the pill is outside the viewport ({pill})")
    if not page.locator(".composer .model-select").count():
        problems.append("phone: the model selector is not in the pill")
    if page.locator(".composer .effort-select").count():
        problems.append("phone: effort still occupies a separate composer control")
    fs = page.evaluate("() => getComputedStyle(document.querySelector('.composer textarea')).fontSize")
    if fs != "16px":
        problems.append(f"phone: the field is {fs}, which Safari would zoom into")
    page.locator(".composer .model-select").click()
    page.wait_for_selector(".sheet .model-list", timeout=5000)
    if pill and pill["height"] > 130:
        problems.append(f"phone: the empty composer is too tall ({pill})")
    before = len(posts("/model"))
    page.locator('.sheet .effort-options input[value="low"]').click()
    reached(page, "/model", before, "phone: the effort choice", problems)
    if HOST.effort != "low":
        problems.append("phone: the named effort choice did not reach the host")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if page.locator(".sheet .model-list").count():
        problems.append("phone: the model sheet did not close on Escape")
    context.close()
    return problems


def failure_bar(browser) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    HOST.status = "failed"
    HOST.error = "HTTP 400: failed to parse grammar"
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    context.add_init_script(r"""(() => {
      const original = window.fetch;
      window.fetch = (url, options) => {
        if (String(url).includes('/stream')) {
          return Promise.resolve(new Response(new ReadableStream({start(controller) {
            window.emitRunEvent = (event, payload) => controller.enqueue(new TextEncoder().encode(
              'event: ' + event + '\n' + 'data: ' + JSON.stringify(payload) + '\n\n'));
          }}), {headers: {'Content-Type': 'text/event-stream'}}));
        }
        return original(url, options);
      };
    })();""")
    page = open_page(context, phone=True)
    page.wait_for_selector(".runerror")
    if HOST.error not in page.locator(".runerror").inner_text():
        problems.append("the session response's provider error was not drawn")
    bounds = page.locator(".runerror").bounding_box()
    composer = page.locator(".composer").bounding_box()
    if not bounds or not composer or bounds["y"] + bounds["height"] > composer["y"] + 1:
        problems.append("the failure bar is not above the redesigned composer")
    page.evaluate("window.emitRunEvent('error', {message: 'provider refused again'})")
    page.wait_for_function("document.querySelector('.runerror')?.textContent.includes('provider refused again')")
    HOST.status = "running"
    HOST.error = ""
    page.evaluate("window.emitRunEvent('message_start', {})")
    page.wait_for_selector(".runerror", state="detached")
    page.wait_for_selector(".composer .roundbtn.primary[data-action='stop']")
    HOST.status = "idle"
    context.close()
    return problems


def layout(browser) -> list[str]:  # type: ignore[no-untyped-def]
    """Measure both rows, wrapping placeholders, growth and the visible keyboard viewport."""
    problems: list[str] = []
    original_asr = GATES["/api/asr"]
    GATES["/api/asr"] = {**original_asr, "configured": True}
    HOST.pending = None
    HOST.messages = [message(101, "user", "Check the run."), message(102, "assistant", "A line of the conversation.\n\n" * 40 + "The last message stays readable.")]
    for width in (390, 768, 1440):
        for language in ("en", "ru"):
            context = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=width < 1024, has_touch=width < 1024, reduced_motion="reduce")
            context.add_init_script("localStorage.setItem('daedalus.session.panel', '0')")
            page = context.new_page()
            page.route("**/api/**", stub)
            for state in ("idle", "running", "waiting"):
                HOST.status = state
                page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang={language}")
                page.wait_for_selector(".composer textarea")
                page.wait_for_timeout(150)
                measure = page.evaluate("""() => {
                  const one = s => document.querySelector(s);
                  const rect = el => { const r = el.getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height, bottom:r.bottom, right:r.right}; };
                  const field = one('.composer textarea'), cs = getComputedStyle(field);
                  const row = one('.composer-row');
                  const controls = [...row.querySelectorAll('button, .composer-mode')].filter(el => el.getBoundingClientRect().width > 0);
                  return {field:rect(field), card:rect(one('.composer-box')), row:rect(row),
                    line:parseFloat(cs.lineHeight), padding:parseFloat(cs.paddingTop)+parseFloat(cs.paddingBottom),
                    scroll:field.scrollHeight, client:field.clientHeight,
                    controls:controls.map(rect), scrollBox:rect(one('.chat-scroll')),
                    tab:one('.tabbar')?.getBoundingClientRect().height ? rect(one('.tabbar')) : null};
                }""")
                print("layout", width, language, state, json.dumps(measure))
                f, row, card = measure["field"], measure["row"], measure["card"]
                prefix = f"{width}/{language}/{state}"
                if width < 1024 and f["h"] > measure["line"] * 2 + measure["padding"] + 1:
                    problems.append(f"{prefix}: an empty field reserves more than two lines")
                if measure["scroll"] > measure["client"] + 1:
                    problems.append(f"{prefix}: placeholder is clipped")
                if f["bottom"] > row["y"] or abs(f["w"] - row["w"]) > 1:
                    problems.append(f"{prefix}: text does not own a full row")
                if any(abs(c["y"] + c["h"] / 2 - row["y"] - row["h"] / 2) > 1 or c["right"] > row["right"] + 1 for c in measure["controls"]):
                    problems.append(f"{prefix}: controls wrap or overflow")
                if measure["scrollBox"]["bottom"] > card["y"] + 1 or (measure["tab"] and card["bottom"] > measure["tab"]["y"]):
                    problems.append(f"{prefix}: composer overlaps messages or navigation")
                if card["x"] < 0 or card["right"] > width:
                    problems.append(f"{prefix}: card overflows")
            field(page).fill("\n".join(["A full line of text"] * 5))
            grown = field(page).bounding_box()
            if not grown or grown["height"] <= f["h"]:
                problems.append(f"{width}/{language}: field does not grow")
            field(page).fill("\n".join(["A full line of text"] * 12))
            overflow = field(page).evaluate("el => ({h:el.clientHeight, scroll:el.scrollHeight, line:parseFloat(getComputedStyle(el).lineHeight)})")
            if overflow["scroll"] <= overflow["h"] or overflow["h"] > overflow["line"] * 8 + measure["padding"] + 1:
                problems.append(f"{width}/{language}: long draft does not scroll at eight lines")
            field(page).fill("")
            scroll = page.locator(".chat-scroll")
            scroll.hover()
            page.mouse.wheel(0, -10000)
            page.wait_for_selector(".composer-jump-anchor .jump-down")
            jump = page.locator(".jump-down").bounding_box()
            card = page.locator(".composer-box").bounding_box()
            if not jump or not card or jump["y"] + jump["height"] > card["y"]:
                problems.append(f"{width}/{language}: newest-message shortcut covers the field")
            page.locator(".jump-down").click()
            page.wait_for_function("""() => {
              const el = document.querySelector('.chat-scroll');
              return el.scrollHeight - el.scrollTop - el.clientHeight < 2;
            }""")
            last = page.locator(".answer").last.bounding_box()
            viewport = scroll.bounding_box()
            if not last or not viewport or last["y"] + last["height"] > viewport["y"] + viewport["height"] + 1:
                problems.append(f"{width}/{language}: the last message cannot be scrolled above the card")
            page.evaluate("""() => {
              const pasted = new DataTransfer();
              pasted.items.add(new File(['pasted'], 'paste.txt', {type:'text/plain'}));
              document.querySelector('.composer textarea').dispatchEvent(new ClipboardEvent('paste', {clipboardData:pasted, bubbles:true, cancelable:true}));
              const dropped = new DataTransfer();
              dropped.items.add(new File(['dropped'], 'drop.txt', {type:'text/plain'}));
              document.querySelector('.chat').dispatchEvent(new DragEvent('drop', {dataTransfer:dropped, bubbles:true, cancelable:true}));
            }""")
            page.wait_for_selector(".attachment")
            if page.locator(".attachment").count() != 2:
                problems.append(f"{width}/{language}: paste or file drop lost an attachment")
            for _ in range(page.locator(".attachment-x").count()):
                page.locator(".attachment-x").first.click()
            if width == 390:
                field(page).focus()
                page.set_viewport_size({"width": width, "height": 480})
                page.wait_for_timeout(200)
                visible = page.locator(".composer-row").bounding_box()
                tab = page.locator(".tabbar").bounding_box() if page.locator(".tabbar").count() else None
                if not visible or visible["y"] < 0 or visible["y"] + visible["height"] > min(480, tab["y"] if tab else 480):
                    problems.append(f"{language}: keyboard viewport hides controls")
                print("keyboard", language, visible)
            context.close()
    GATES["/api/asr"] = original_asr
    HOST.status = "idle"
    return problems


def modes(browser) -> list[str]:  # type: ignore[no-untyped-def]
    """The mode chip: it lists the modes and the YAGNI switch, both reach the host, the chip
    shows what the host says after a reload, and on a narrow phone it stays in the row mid-run."""
    problems: list[str] = []
    HOST.pending = None
    HOST.status = "idle"
    HOST.mode, HOST.yagni = "", False
    HOST.messages = [message(101, "user", "Tidy the parser.", yagni="on"), message(102, "assistant", "Done, in three lines.")]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = open_page(context)
    chip = page.locator(".composer .composer-mode")
    if chip.inner_text().strip() != "Agent":
        problems.append(f"the mode chip reads {chip.inner_text()!r}, not Agent")
    marker = page.locator(".msg.user .msg-yagni")
    if marker.count() != 1 or "YAGNI on" not in marker.inner_text():
        problems.append("the message that told the agent carries no YAGNI mark")
    chip.click()
    page.wait_for_selector(".mode-menu")
    names = page.locator(".mode-menu [role='menuitemradio'] .truncate").all_inner_texts()
    if names != ["Agent", "Plan", "Quick", "Deep", "Careful"]:
        problems.append(f"the mode menu lists {names}")
    before = len(posts("/mode"))
    page.locator(".mode-menu [role='menuitemradio']", has_text="Plan").click()
    page.wait_for_selector(".mode-menu", state="detached")
    reached(page, "/mode", before, "picking Plan", problems)
    if not posts("/mode") or posts("/mode")[-1][2] != {"mode": "plan"}:
        problems.append(f"picking Plan posted {posts('/mode')[-1:]}")
    page.wait_for_function("document.querySelector('.composer .composer-mode')?.textContent.startsWith('Plan')")
    # The keyboard: the chip opens with Enter, the arrows walk the rows, Escape closes and the chip keeps focus.
    chip.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(".mode-menu")
    page.keyboard.press("ArrowUp")
    focused = page.evaluate("document.activeElement?.className || ''")
    if "yagni-row" not in focused:
        problems.append(f"ArrowUp from the first row lands on {focused!r}, not the switch")
    before = len(posts("/yagni"))
    page.keyboard.press("Enter")
    reached(page, "/yagni", before, "the YAGNI switch", problems)
    if not posts("/yagni") or posts("/yagni")[-1][2] != {"on": True}:
        problems.append(f"the switch posted {posts('/yagni')[-1:]}")
    if page.locator(".mode-menu").count() != 1:
        problems.append("the switch closed the menu")
    page.keyboard.press("Escape")
    page.wait_for_selector(".mode-menu", state="detached")
    page.reload()
    page.wait_for_selector(".composer .composer-mode")
    page.wait_for_timeout(300)
    if "yagni" not in (page.locator(".composer .composer-mode").get_attribute("class") or "").split():
        problems.append("after a reload the chip does not show YAGNI on")
    context.close()
    # A narrow phone mid-run: the model chip gives way, the mode chip stays inside the row.
    HOST.status = "running"
    for width in (360, 390):
        context = browser.new_context(viewport={"width": width, "height": 780}, is_mobile=True, has_touch=True, color_scheme="dark")
        context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
        page = open_page(context, phone=True)
        box = page.locator(".composer .composer-mode").bounding_box()
        row = page.locator(".composer-row").bounding_box()
        print("phone mode chip", width, box, row)
        if not box or not row or box["width"] < 24 or box["x"] + box["width"] > row["x"] + row["width"] + 1:
            problems.append(f"{width}: the mode chip is missing or leaves the row while running")
        page.locator(".composer .composer-mode").click()
        page.wait_for_selector(".sheet.mode-sheet .yagni-row")
        if page.locator(".sheet.mode-sheet .yagni-row").get_attribute("aria-checked") != "true":
            problems.append(f"{width}: the phone's sheet does not show YAGNI on")
        page.keyboard.press("Escape")
        page.wait_for_selector(".sheet.mode-sheet", state="detached")
        context.close()
    HOST.status = "idle"
    HOST.mode, HOST.yagni = "", False
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += desktop(browser)
        problems += phone(browser)
        problems += failure_bar(browser)
        problems += layout(browser)
        problems += modes(browser)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
