"""A quick agent waiting for the operator says so under the leader's QuickAgent call.

The helper of a running QuickAgent call handed the browser over (a CAPTCHA) and waits inside its call.
Its block under the call, the helper's own steps, ends in a row in the operator's language that says
the quick agent waits for them, what to do, how long it still waits, and offers Open the browser (the
conversation's Browser tab) and Decline (which tells the helper no). A wait for the private sign-in
offers Open the form; a wait for the move into the operator's Chrome sends them to the request card
below the conversation and opens nothing. Checked at a desktop's width and a phone's, in English and
Russian, with no sideways scroll.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_quick_wait.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LEADER = "sess-lead"
HELPER = "sess-quick"
GROUP = "s-sess-lead"
WAITS = {
    "captcha": {"kind": "captcha", "what": "solve the CAPTCHA on shop.test", "group_id": GROUP},
    "login": {"kind": "login", "what": "sign in to shop.test: the order", "group_id": GROUP},
    "chrome": {"kind": "chrome", "what": "work in your Chrome «Work»: the bank is signed in there", "group_id": ""},
}
WORDS = {
    "en": {"title": "The quick agent waits for you in the browser", "login": "The quick agent waits for you to sign in", "chrome": "The quick agent asks to work in your Chrome",
           "open": "Open the browser", "form": "Open the form", "decline": "Decline", "left": "It waits", "below": "Answer the request below"},
    "ru": {"title": "Быстрый агент ждёт вас в браузере", "login": "Быстрый агент ждёт, пока вы войдёте", "chrome": "Быстрый агент просит работать в вашем Chrome",
           "open": "Открыть браузер", "form": "Открыть форму", "decline": "Отказаться", "left": "Ждёт ещё", "below": "Ответьте на запрос"},
}


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-11-24T09:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


def detail(session: str, title: str, messages: list[dict]) -> dict:
    return {
        "id": session, "title": title, "status": "running", "error": "", "run_id": f"run-{session}", "workspace": "/workspace",
        "workspace_name": "shop", "workspace_own": True, "workspace_sessions": [], "pending": None, "model": "Claude Opus 5", "provider": "claude",
        "project": {"id": "p", "name": "Shop", "folders": folders("/workspace"), "settings": {"snapshots": True}},
        "thinking": True, "reasoning_effort": "high", "yagni": False, "brief": "",
        "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": {"tokens": 21000, "window": 200000, "messages": len(messages), "summaries": 0, "operator_turns": 1},
        "messages": messages,
    }


LEADER_MESSAGES = [message(1, "user", "Pay the invoice on shop.test.")]
"""The leader's history: its request. The QuickAgent call in progress comes over the stream."""
QUICK_ARGS = {"task": "Pay the invoice on shop.test and report the receipt number.", "model": "local.fast"}
HELPER_MESSAGES = [
    message(1, "user", "[task from your leader session sess-lead via QuickAgent]\n\nPay the invoice on shop.test and report the receipt number.", origin="subagent-task:sess-lead"),
    message(2, "assistant", "", tool_calls=[{"id": "h_open", "name": "BrowserOpen", "arguments": {"url": "https://shop.test/"}}]),
    message(3, "tool", "", tool_results=[{"id": "h_open", "content": "Tab t1 is on https://shop.test/ (HTTP 200).", "is_error": False}]),
    message(4, "assistant", "", tool_calls=[{"id": "h_hand", "name": "BrowserHandoff", "arguments": {"reason": "captcha", "what": "solve the CAPTCHA on shop.test"}}]),
]


class Stub:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.declined: list[str] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        req = route.request
        path = req.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel.endswith("/stream"):
            return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
        if req.method == "POST" and rel == f"/api/browsers/{GROUP}/decline":
            self.declined.append(rel)
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        body: object
        if req.method != "GET":
            body = {}
        elif rel == "/api/auth/me":
            body = {"user_id": 1, "via": "token"}
        elif rel == f"/api/sessions/{LEADER}":
            body = detail(LEADER, "Invoices", LEADER_MESSAGES)
        elif rel == f"/api/sessions/{HELPER}":
            body = detail(HELPER, "[quick] Pay the invoice", HELPER_MESSAGES)
        elif rel == f"/api/sessions/{LEADER}/quick-agents":
            until = (datetime.now(UTC) + timedelta(minutes=7, seconds=30)).isoformat()
            body = {"quick_agents": [{"session_id": HELPER, "call": "c_quick", "created_at": "2026-11-24T09:00:02+00:00", "running": True, "waiting": {**WAITS[self.kind], "until": until}}]}
        elif rel.startswith((f"/api/sessions/{LEADER}/", f"/api/sessions/{HELPER}/")) and not rel.endswith(("/quick-models", "/tool-groups")):
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


# The leader's stream, held open: the QuickAgent call is the step in progress only while the stream names it.
STREAM = """const originalFetch = window.fetch;
  window.fetch = (input, init) => String(input).includes('/sessions/sess-lead/stream')
    ? Promise.resolve(new Response(new ReadableStream({start(controller) {
        window.sendEvent = (name, payload) => controller.enqueue(new TextEncoder().encode(
          'event: ' + name + '\\ndata: ' + JSON.stringify(payload) + '\\n\\n'));
      }}), {headers: {'Content-Type': 'text/event-stream'}}))
    : originalFetch(input, init);"""


def event(page: Page, name: str, payload: dict) -> None:
    page.evaluate(f"window.sendEvent({json.dumps(name)}, {json.dumps({'run_id': f'run-{LEADER}', **payload})})")


def open_page(context, stub: Stub, lang: str) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.add_init_script(STREAM)
    page.route("**/api/**", stub.route)
    page.goto(f"{BASE}/agents/{LEADER}?token=t&scheme=dark&lang={lang}")
    page.wait_for_function("typeof window.sendEvent === 'function'", timeout=15000)
    page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
    event(page, "message_start", {})
    event(page, "tool_use_start", {"tool_call_id": "c_quick", "tool_name": "QuickAgent"})
    event(page, "tool_use_stop", {"tool_call_id": "c_quick", "final_input": QUICK_ARGS})
    page.wait_for_selector(".thinking-head", timeout=15000)
    heads = page.locator(".thinking-head[aria-expanded='false']")
    while heads.count():
        heads.first.click()
        page.wait_for_timeout(50)
    page.wait_for_selector(".quick-wait", timeout=15000)
    page.wait_for_timeout(300)
    return page


def check(browser, width: int, height: int, lang: str, kind: str) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    words = WORDS[lang]
    where = f"{width}px {lang} {kind}"
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
    stub = Stub(kind)
    try:
        page = open_page(context, stub, lang)
    except Exception as exc:  # noqa: BLE001 — a row that never appears is the finding
        context.close()
        return [f"{where}: no waiting row under the QuickAgent call ({exc})"]
    row = page.locator(".quick-agent .quick-wait")
    if row.count() != 1:
        problems.append(f"{where}: the waiting row should be inside the quick agent's block, once ({row.count()})")
    text = row.first.inner_text()
    print(where, "row:", " | ".join(text.split("\n")))
    title = words["chrome"] if kind == "chrome" else words["login"] if kind == "login" else words["title"]
    if title not in text:
        problems.append(f"{where}: the row does not say the quick agent waits ({text!r})")
    if WAITS[kind]["what"] not in text:
        problems.append(f"{where}: the row does not say what to do ({text!r})")
    left = words["below"] if kind == "chrome" else words["left"]
    if left not in text or not any(unit in text for unit in ("7m", "7 мин")):
        problems.append(f"{where}: the row does not say how long it still waits ({text!r})")
    buttons = [row.locator("button").nth(i).inner_text().strip() for i in range(row.locator("button").count())]
    expected = [] if kind == "chrome" else [words["form"]] if kind == "login" else [words["open"], words["decline"]]
    if buttons != expected:
        problems.append(f"{where}: the row's buttons should be {expected} ({buttons})")
    box = row.first.bounding_box()
    if box is None or box["x"] < 0 or box["x"] + box["width"] > width:
        problems.append(f"{where}: the row runs off the screen ({box})")
    if kind == "captcha":
        row.locator("button", has_text=words["decline"]).click()
        page.wait_for_timeout(400)
        if stub.declined != [f"/api/browsers/{GROUP}/decline"]:
            problems.append(f"{where}: Decline did not tell the host ({stub.declined})")
        row.locator("button", has_text=words["open"]).click()
        page.wait_for_timeout(600)
        if "panel=browser" not in page.url and not page.locator("[data-tab='browser'][aria-selected='true'], .panel-tab.active[data-id='browser'], .bp").count():
            problems.append(f"{where}: Open the browser did not open the Browser tab ({page.url})")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    if overflow > 0:
        problems.append(f"{where}: the page scrolls sideways by {overflow}px")
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for kind in ("captcha", "login", "chrome"):
            problems += check(browser, 1440, 900, "en", kind)
        problems += check(browser, 1440, 900, "ru", "captcha")
        problems += check(browser, 390, 844, "en", "captcha")
        problems += check(browser, 390, 844, "ru", "chrome")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
