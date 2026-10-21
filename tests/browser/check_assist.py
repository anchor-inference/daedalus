"""The guessed next message in the composer and meeting notes from audio, in a real browser.

Once an answer settles, the app asks the host for the operator's likely next message and shows it
as the empty field's placeholder. Tab puts it in the field; nothing is sent by itself; and once it is
taken (or the operator types) it is not offered again for that answer.

An audio file attached to the composer is offered as meeting notes: the upload, the progress, then
the notes with their action items, which go on the calendar on the chosen day with one click.

    APP_URL=http://127.0.0.1:8238/app python3 tests/browser/check_assist.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os

from api_stub import meeting_job
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
    if rel.startswith(f"/api/sessions/{SESSION}/meeting"):
        method = route.request.method
        if method == "POST" and rel.endswith("/meeting"):
            HOST.posted.append(("POST", rel, None))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(meeting_job(SESSION, "transcribing")))
        if method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(meeting_job(SESSION, "done")))
        data = json.loads(route.request.post_data or "{}")
        HOST.posted.append((method, rel, data))
        answer = {"event_id": "evt-meeting", "day": data.get("day", "")} if rel.endswith("/calendar") else {"run_id": "r-meeting"}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
    return composer_stub(route)


def main() -> None:
    HOST.status = "idle"
    HOST.messages = [message(101, "user", "Refactor the storage layer and migrate every caller.", run_id="old"),
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

        expect(page.locator(".msg.user").first).to_contain_text("Refactor the storage layer")

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

        # An attached recording is offered as meeting notes, and its action items go on the calendar.
        page.locator(".composer input[type=file]").first.set_input_files(files=[{"name": "standup.m4a", "mimeType": "audio/mp4", "buffer": b"\0" * 2048}])
        offer = page.locator(".meeting-offer")
        expect(offer).to_be_visible()
        expect(offer).to_contain_text("standup.m4a")
        offer.get_by_role("button", name="Make notes").click()
        expect(offer).to_have_count(0)
        assert any(p[1] == f"/api/sessions/{SESSION}/meeting" for p in HOST.posted), HOST.posted
        notes = page.locator(".meeting-done")
        expect(notes).to_be_visible(timeout=10000)
        expect(notes).to_contain_text("Weekly standup")
        expect(notes.locator(".meeting-list li")).to_have_count(3)
        notes.locator("input[type=date]").fill("2026-10-20")
        notes.locator("[data-action=calendar]").click()
        expect(notes).to_contain_text("On the calendar")
        assert ("POST", f"/api/sessions/{SESSION}/meeting/mtg1/calendar", {"day": "2026-10-20"}) in HOST.posted, HOST.posted
        if os.environ.get("SHOTS"):
            page.screenshot(path=f"{os.environ['SHOTS']}/assist-meeting.png")
        # The recording did not stay in the pill as an attachment.
        expect(page.locator(".composer .attachments")).to_have_count(0)
        browser.close()
    assert not UNHANDLED.report()


if __name__ == "__main__":
    main()
