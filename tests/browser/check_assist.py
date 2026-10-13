"""The auto mode's chip on a turn and the guessed next message in the composer, in a real browser.

A message the auto mode ran says at what level, in a word on the message; clicking it offers the
three levels and the one picked goes to the host for the next message, re-running nothing. The
composer's mode chip names the mode and the model pill says the effort is the mode's to choose.

Once an answer settles, the app asks the host for the operator's likely next message and shows it
as the empty field's placeholder. Tab puts it in the field; nothing is sent by itself; and once it is
taken (or the operator types) it is not offered again for that answer.

    APP_URL=http://127.0.0.1:8238/app python3 tests/browser/check_assist.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message
from check_composer import stub as composer_stub
from playwright.sync_api import expect, sync_playwright

GUESS = "Now run the full test suite"
ASKED: list[str] = []


def stub(route) -> None:  # type: ignore[no-untyped-def]
    path = route.request.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == f"/api/sessions/{SESSION}/suggestion":
        ASKED.append(rel)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"text": GUESS}))
    if rel == f"/api/sessions/{SESSION}/depth" and route.request.method == "POST":
        data = json.loads(route.request.post_data or "{}")
        HOST.posted.append(("POST", rel, data))
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"depth_next": data.get("level") or ""}))
    return composer_stub(route)


def main() -> None:
    HOST.status = "idle"
    HOST.mode = "auto"
    depth = {"level": "long", "mode": "deep", "effort": "high", "source": "heuristic", "ms": 0}
    HOST.messages = [message(101, "user", "Refactor the storage layer and migrate every caller.", run_id="old", depth=depth),
                     message(102, "assistant", "Done; every caller now uses the pool.", run_id="old")]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script("""const originalFetch = window.fetch;
          window.fetch = (input, init) => String(input).includes('/stream')
            ? Promise.resolve(new Response(new ReadableStream({start(controller) {
                window.sendEvent = (name, payload) => controller.enqueue(new TextEncoder().encode(
                  'event: ' + name + '\\ndata: ' + JSON.stringify(payload) + '\\n\\n'));
              }}), {headers: {'Content-Type': 'text/event-stream'}}))
            : originalFetch(input, init);""")
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{SESSION}?token=t&lang=en&scheme=dark")
        page.wait_for_function("typeof window.sendEvent === 'function'")

        # The chip on the message, and the correction for the next one.
        chip = page.locator(".msg.user .msg-depth")
        expect(chip).to_have_text("Deep")
        expect(chip).to_have_attribute("data-depth", "long")
        chip.click()
        menu = page.locator(".depth-menu")
        expect(menu).to_be_visible()
        expect(menu.locator("[data-level]")).to_have_count(3)
        menu.locator("[data-level='quick']").click()
        expect(menu).to_have_count(0)
        assert ("POST", f"/api/sessions/{SESSION}/depth", {"level": "quick"}) in HOST.posted, HOST.posted
        expect(page.get_by_text("The next message runs as Quick")).to_be_visible()

        # The composer names the mode, and the effort as the mode's.
        expect(page.locator(".composer .composer-mode")).to_contain_text("Auto")
        expect(page.locator(".composer .model-effort")).to_have_text("auto")

        # The answer settles; the guess arrives as the empty field's placeholder.
        field = page.locator(".composer textarea")
        expect(field).not_to_have_attribute("data-suggestion", GUESS)

        def event(name: str, payload: dict) -> None:
            page.evaluate(f"window.sendEvent({json.dumps(name)}, {json.dumps({'run_id': 'r1', **payload})})")

        HOST.status = "running"
        event("message_start", {})
        event("content_block_delta", {"delta": {"type": "text_delta", "text": "Tests added."}})
        HOST.messages += [message(103, "user", "add tests", run_id="r1"), message(104, "assistant", "Tests added.", run_id="r1")]
        event("message_stop", {"stop_reason": "end_turn"})
        HOST.status = "idle"
        event("run_settled", {"status": "completed", "housekeeping": True})
        event("run_settled", {"status": "completed", "housekeeping": False})
        expect(field).to_have_attribute("data-suggestion", GUESS)
        expect(field).to_have_attribute("placeholder", GUESS)
        expect(page.locator(".composer-ghost-accept")).to_be_visible()
        assert len(ASKED) == 1, ASKED
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/assist-ghost.png")

        sent_before = len([p for p in HOST.posted if p[1].endswith("/messages")])
        field.focus()
        page.keyboard.press("Tab")
        expect(field).to_have_value(GUESS)
        expect(page.locator(".composer-ghost-accept")).to_have_count(0)
        assert len([p for p in HOST.posted if p[1].endswith("/messages")]) == sent_before, "a guess is never sent by itself"
        # Taken, it is not offered again for this answer: an emptied field shows the usual hint.
        field.fill("")
        expect(field).not_to_have_attribute("data-suggestion", GUESS)
        # Without a guess Tab is the browser's again.
        field.focus()
        page.keyboard.press("Tab")
        expect(field).not_to_be_focused()
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    main()
