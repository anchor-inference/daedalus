"""A request the agent makes again after an Allow is offered again, outside the folded steps.

The case that asked for it: the agent pressed "Next" on a form in the operator's Chrome, the policy
asked, the operator allowed it from the dock, and the agent's later click on the same button was
refused under the same key, since a key names the action. The screen that had answered the first
request kept its key as answered and offered nothing; the agent's answer said "press Allow", and the
only buttons were inside the folded "Worked for 1m 42s". The dock must come back for the new call,
on a desktop and on a phone, the block of steps that holds the refusal must open and say it waits,
and an answer given anywhere else must take both away.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_approval_again.py

Exit 0 when every step holds; ``SHOTS=<dir>`` keeps a picture of each width.
"""
from __future__ import annotations

import os
import sys
import time

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, posts, reached, stub
from event_feed import EventFeed
from playwright.sync_api import sync_playwright

KEY = "a1b2c3d4e5f6"
CLICK = {"tab": "t1", "action": "click", "ref": "e87", "element": "Next"}
REFUSAL = f"[in the operator's own Chrome «Work»]\nneeds the operator's approval: this would submit a form on example.org (rule browser.sensitive). Approval key: {KEY}."


def refused(run: str, call: str, seq: int) -> list[dict]:
    """One run of the agent's: a step, then the click the policy holds, then its words to the operator."""
    return [
        message(seq, "user", "Fill in the form and send it.", run_id=run),
        message(seq + 1, "assistant", "", run_id=run, tool_calls=[{"id": f"x{call}", "name": "Exec", "arguments": {"command": "ls forms"}}]),
        message(seq + 2, "tool", "", run_id=run, tool_results=[{"id": f"x{call}", "content": "form.pdf", "is_error": False}]),
        message(seq + 3, "assistant", "", run_id=run, tool_calls=[{"id": call, "name": "BrowserAct", "arguments": CLICK}]),
        message(seq + 4, "tool", "", run_id=run, tool_results=[{"id": call, "content": REFUSAL, "is_error": True}]),
        message(seq + 5, "assistant", "The form is filled in. Sending it needs your approval: press Allow.", run_id=run),
    ]


def scenario(browser, width: int, height: int, phone: bool, shots: str | None) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    where = "phone" if phone else "desktop"
    feed = EventFeed()
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", is_mobile=phone, has_touch=phone)
    try:
        HOST.status = "idle"
        HOST.open = [KEY]
        HOST.messages = refused("r1", "c1", 301)
        page = context.new_page()
        page.route("**/api/**", stub)
        page.route("**/api/events**", feed.route)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme=dark&lang=en")
        page.wait_for_selector(".composer .dock.approval", timeout=15000)
        deadline = time.monotonic() + 10
        while not feed.connected() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        before = len(posts("/policy/grant"))
        page.locator(".composer .dock.approval button", has_text="Allow once").first.click()
        reached(page, "/policy/grant", before, f"{where}: Allow once", problems)
        page.wait_for_selector(".composer .dock.approval", state="detached", timeout=5000)
        if page.locator("[data-awaiting]").count():
            problems.append(f"{where}: the steps still say they wait after the request was answered")

        # The agent's next run clicks the same button; the host refuses it under the same key and
        # announces a new request. No reload: this is the screen that answered the first one.
        HOST.messages = HOST.messages + refused("r2", "c2", 401)[1:]
        HOST.open = [KEY]
        feed.send("permission.pending", {"request_id": KEY, "request_ref": f"policy:{SESSION}:{KEY}", "tool": "BrowserAct"}, session=SESSION)
        try:
            page.wait_for_selector(".composer .dock.approval", timeout=8000)
        except Exception:  # noqa: BLE001 — said as a problem, not raised
            problems.append(f"{where}: the same action refused again after an Allow is not offered in the dock")
        dock = page.locator(".composer .dock.approval")
        if dock.count():
            box = dock.bounding_box()
            if not box or box["y"] < 0 or box["y"] + box["height"] > height + 1 or box["x"] < 0 or box["x"] + box["width"] > width + 1:
                problems.append(f"{where}: the dock is outside the screen ({box})")
            if "BrowserAct" not in dock.inner_text():
                problems.append(f"{where}: the dock does not name the refused call ({dock.inner_text()!r})")
        # The block of steps holding the refusal is open and says it waits, without a click.
        badge = page.locator(".thinking-head.awaiting [data-awaiting]")
        if badge.count() != 1:
            problems.append(f"{where}: the steps holding the request are not marked as waiting ({badge.count()})")
        elif page.locator(".thinking-head.awaiting").get_attribute("aria-expanded") != "true":
            problems.append(f"{where}: the steps holding the request are folded")
        if shots:
            page.screenshot(path=os.path.join(shots, f"approval-again-{where}.png"))

        # Allowed in Telegram: the dock and the mark go, and the steps fold back.
        HOST.answered(KEY)
        feed.send("permission.resolved", {"request_id": KEY, "request_ref": f"policy:{SESSION}:{KEY}", "decision": "allow", "via": "telegram"}, session=SESSION)
        try:
            page.wait_for_selector(".composer .dock.approval", state="detached", timeout=5000)
        except Exception:  # noqa: BLE001
            problems.append(f"{where}: the dock stayed after Telegram answered the request")
        if page.locator("[data-awaiting]").count():
            problems.append(f"{where}: the waiting mark stayed after Telegram answered the request")
    finally:
        context.close()
        feed.close()
        HOST.open = []
    return problems


def main() -> int:
    shots = os.environ.get("SHOTS")
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += scenario(browser, 1280, 900, False, shots)
        problems += scenario(browser, 390, 844, True, shots)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    failed = main()
    sys.exit(failed or UNHANDLED.report())
