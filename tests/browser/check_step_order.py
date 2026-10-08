"""A run's steps and the words the agent writes between them stay in the order they happened.

The complaint: during a long run the agent's interim words streamed in below its steps, and the
moment they were done they jumped to near the top of the list, with every step taken after them
above the steps taken before them. Nothing of a run is written to the transcript until it ends, so
every message of the run reaches the app as a live one, and an event reads only the last 24 of them;
the steps that fell off the front of that read came back in from the stream's copy, after the newer
ones.

The host here answers the way the real one does: the session's messages after the operator's are
all live, a read with ``tail`` gets the newest ones only, and the stream says what the run did. The
run is fourteen steps, then words that end in two more, then three more steps. On a desktop and on a
phone the opened activity must read: the fourteen, the words, the two, the three — while the run
streams and once it has ended and its rows are written.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_step_order.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub  # noqa: E402

WORDS = "The screenshot caught the terminal, not the window. Bringing it forward and taking it again."
STREAM = """const originalFetch = window.fetch;
  window.fetch = (input, init) => String(input).includes('/stream')
    ? Promise.resolve(new Response(new ReadableStream({start(controller) {
        window.sendEvent = (name, payload) => controller.enqueue(new TextEncoder().encode(
          'event: ' + name + '\\ndata: ' + JSON.stringify(payload) + '\\n\\n'));
      }}), {headers: {'Content-Type': 'text/event-stream'}}))
    : originalFetch(input, init);"""


def step(name: str, n: int) -> tuple[str, dict]:
    """Alternate tools, so no two neighbours fold into one "Ran 2 commands" row and every step reads by its own detail."""
    return ("Exec", {"command": f"echo {name}"}) if n % 2 else ("Read", {"path": f"/workspace/{name}.txt"})


def detail_of(name: str, n: int) -> str:
    return f"echo {name}" if n % 2 else f"{name}.txt"


class Run:
    """The run as the host holds it: the operator's message, then every message of the run, all live."""

    def __init__(self) -> None:
        self.seq = 101
        self.messages: list[dict] = [message(101, "user", "Bring the window forward and look at it.", run_id="r1")]
        self.expected: list[str] = []

    def add(self, role: str, text: str = "", **over: object) -> None:
        self.seq += 1
        self.messages.append(message(self.seq, role, text, run_id="r1", live=True, **over))

    def tools(self, names: list[str], text: str = "") -> list[tuple[str, str, dict]]:
        calls = []
        for name in names:
            n = int(name[1:])
            tool, args = step(name, n)
            calls.append((name, tool, args))
        self.add("assistant", text, tool_calls=[{"id": name, "name": tool, "arguments": args} for name, tool, args in calls])
        for name, _, _ in calls:
            self.add("tool", "", tool_results=[{"id": name, "content": "ok", "is_error": False}])
        if text:
            self.expected.append(text)
        self.expected += [detail_of(name, int(name[1:])) for name, _, _ in calls]
        return calls


def serve(run: Run):  # type: ignore[no-untyped-def]
    def route(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        if request.method == "GET" and path.endswith(f"/api/sessions/{SESSION}"):
            query = dict(part.split("=", 1) for part in request.url.split("?", 1)[1].split("&")) if "?" in request.url else {}
            HOST.messages = run.messages
            detail = HOST.detail()
            tail = int(query.get("tail") or 600)
            detail["messages"] = run.messages[-tail:]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
        return stub(route)

    return route


def order(page) -> list[str]:  # type: ignore[no-untyped-def]
    return page.evaluate("""() => [...document.querySelectorAll('.turn:last-of-type .activity .act .detail, .turn:last-of-type .activity .note')]
      .map((el) => el.classList.contains('note') ? el.innerText.trim() : el.textContent.trim())""")


def check(browser, phone: bool) -> list[str]:  # type: ignore[no-untyped-def]
    where = "phone" if phone else "desktop"
    problems: list[str] = []
    run = Run()
    HOST.status = "running"
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, is_mobile=phone, has_touch=phone, color_scheme="dark")
    page = context.new_page()
    page.add_init_script(STREAM)
    page.route("**/api/**", serve(run))

    def event(name: str, payload: dict) -> None:
        page.evaluate(f"window.sendEvent({json.dumps(name)}, {json.dumps({'run_id': 'r1', **payload})})")

    def stream_tools(calls: list[tuple[str, str, dict]], text: str = "") -> None:
        event("message_start", {})
        if text:
            event("content_block_delta", {"delta": {"type": "text_delta", "text": text}})
        for name, tool, args in calls:
            event("tool_use_start", {"tool_call_id": name, "tool_name": tool})
            event("tool_use_stop", {"tool_call_id": name, "final_input": args})
        event("message_stop", {"stop_reason": "tool_use"})
        for name, _, _ in calls:
            event("tool_result", {"tool_call_id": name, "content": "ok"})
        page.wait_for_timeout(120)

    # The screen is open from the start of the run, so the stream carries every step of it.
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en")
    page.wait_for_function("typeof window.sendEvent === 'function'", timeout=15000)
    page.wait_for_selector(".msg.user", timeout=15000)
    for k in range(1, 15):
        stream_tools(run.tools([f"a{k}"]))
    # The words stream in below the steps…
    event("message_start", {})
    event("content_block_delta", {"delta": {"type": "text_delta", "text": WORDS}})
    page.locator(".turn:last-of-type .thinking-head").click()
    page.wait_for_selector(".answer.streaming", timeout=5000)
    streaming = order(page)
    if streaming != run.expected:
        problems.append(f"{where}: while the words stream the steps read {streaming}, not {run.expected}")
    # …and end in two commands, after which the run goes on.
    calls = run.tools(["b15", "b16"], WORDS)
    for name, tool, args in calls:
        event("tool_use_start", {"tool_call_id": name, "tool_name": tool})
        event("tool_use_stop", {"tool_call_id": name, "final_input": args})
    event("message_stop", {"stop_reason": "tool_use"})
    for name, _, _ in calls:
        event("tool_result", {"tool_call_id": name, "content": "ok"})
    page.wait_for_timeout(300)
    for k in range(17, 20):
        stream_tools(run.tools([f"c{k}"]))
    page.wait_for_timeout(600)
    after = order(page)
    if after != run.expected:
        problems.append(f"{where}: after the words the run reads {after}\n    not {run.expected}")

    # The run ends and its rows are written: the same order, read from the transcript.
    run.messages = [{**m, "live": False} for m in run.messages]
    run.add("assistant", "Done: the window is in front and the picture is attached.")
    for m in run.messages:
        m.pop("live", None)
    event("message_start", {})
    event("content_block_delta", {"delta": {"type": "text_delta", "text": "Done: the window is in front and the picture is attached."}})
    event("message_stop", {"stop_reason": "end_turn"})
    HOST.status = "idle"
    event("run_settled", {"status": "completed"})
    page.wait_for_selector(".answer:not(.streaming)", timeout=5000)
    page.wait_for_timeout(500)
    if not page.locator(".turn:last-of-type .activity").count():
        page.locator(".turn:last-of-type .thinking-head").click()
    settled = order(page)
    if settled != run.expected:
        problems.append(f"{where}: once written the run reads {settled}\n    not {run.expected}")
    if os.environ.get("SHOTS"):
        page.screenshot(path=f"{os.environ['SHOTS']}/step-order-{where}.png", full_page=False)
    context.close()
    print(f"{where}: {'ok' if not problems else 'FAILED'} ({len(run.expected)} rows)")
    return problems


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        problems += check(browser, phone=False)
        problems += check(browser, phone=True)
        browser.close()
    if UNHANDLED.report():
        problems.append("the app asked the stub for a route it does not know")
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
