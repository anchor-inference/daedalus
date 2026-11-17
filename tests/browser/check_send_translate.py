"""Sending a message in a translated session, in a real browser: it never looks frozen.

The host acknowledges the send at once and translates in the background; the app draws the operator's
words in the conversation straight away, with "Translating to EN…" under them, and clears the composer
as any send does. Here: the row is there within 300 ms of the press with the indicator, the field is
empty and usable while the translation runs, and when the host says the message settled the row becomes
the ordinary sent message with "Show what the agent got (EN)". A reload while the host still lists the
message draws it as translating from the host's list alone. A translator that failed leaves the existing
"Not translated — sent as written" mark, and a message the session refused after the acknowledgement
comes back into the field with the reason. The answer card says it is translating what was typed. In
English and Russian; screenshots into ``SHOTS`` when set.

    APP_URL=http://127.0.0.1:8371/app python3 tests/browser/check_send_translate.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from check_composer import BASE, CHROMIUM, HOST, SESSION, message
from check_composer import stub as composer_stub
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app, field_text  # noqa: E402

SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None
WRITTEN = "Почему тесты падают только в CI?"
GOT = "Why do the tests fail only in CI?"
TRANSLATING: list[dict] = []
"""What the stubbed host still holds with its translator, as ``GET /api/sessions/<id>`` lists it."""
SENT: list[dict] = []
WORDS = {
    "en": {"pending": "Translating to EN…", "agent": "Show what the agent got (EN)", "failed": "Not translated — sent as written", "refused": "Not sent"},
    "ru": {"pending": "Перевожу на EN…", "agent": "Показать, что получил агент (EN)", "failed": "Не переведено — отправлено как есть", "refused": "Не отправлено"},
}

# The session stream, held open by the page itself so the check can say what the host would.
STREAM = r"""(() => {
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
})();"""


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    path = req.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel == f"/api/sessions/{SESSION}" and req.method == "GET":
        detail = {**HOST.detail(), "translation": {"agent": "en", "operator": "ru", "active": True, "explicit": True, "default": "en"}, "translating": list(TRANSLATING)}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
    if rel == f"/api/sessions/{SESSION}/messages" and req.method == "POST":
        # The host has the words and answers before its translator does.
        data = json.loads(req.post_data or "{}")
        entry = {"id": data["client_message_id"], "text": data["text"], "from": "ru", "to": "en", "at": "2026-11-17T12:00:00+00:00", "files": []}
        TRANSLATING.append(entry)
        SENT.append(data)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"run_id": "", "translating": entry}))
    return composer_stub(route)


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / name))


def settle(page, sent_id: str, *, outcome: str = "sent", state: str = "done", error: str = "", text: str = WRITTEN) -> None:  # type: ignore[no-untyped-def]
    """The host's word that the message left the translator: in the transcript when sent, back when refused."""
    TRANSLATING[:] = [entry for entry in TRANSLATING if entry["id"] != sent_id]
    if outcome == "sent":
        mark = {"direction": "out", "state": state, "from": "ru", "to": "en", **({"alternate": GOT} if state == "done" else {"error": error})}
        seq = max(m["seq"] for m in HOST.messages) + 1
        HOST.messages = HOST.messages + [message(seq, "user", text, client_message_id=sent_id, translation=mark)]
    settled = {"id": sent_id, "text": text, "outcome": outcome, **({"state": state} if outcome == "sent" else {}), **({"error": error} if error else {})}
    page.evaluate("(p) => window.emitRunEvent('message_translating', p)", {"session_id": SESSION, "translating": TRANSLATING, "settled": settled})


def press_send(page) -> float:  # type: ignore[no-untyped-def]
    """Type the words, press send, and return how many milliseconds the translating row took to appear."""
    field = page.locator(".composer .composer-field")
    field.click()
    page.keyboard.type(WRITTEN)
    return page.evaluate("""() => new Promise((resolve) => {
      const t0 = performance.now();
      document.querySelector('.composer .roundbtn.primary').click();
      const look = () => document.querySelector('.msg.user[data-pending="translating"]') ? resolve(performance.now() - t0) : requestAnimationFrame(look);
      look();
    })""")


def walk(browser, lang: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    HOST.status = "idle"
    HOST.pending = None
    HOST.messages = [message(1, "user", "Посмотри логи сборки", translation={"direction": "out", "state": "done", "from": "ru", "to": "en", "alternate": "Look at the build logs"}), message(2, "assistant", "The build is green; two tests are flaky.")]
    TRANSLATING.clear()
    SENT.clear()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script(STREAM)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&lang={lang}&scheme=dark")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.wait_for_function("() => typeof window.emitRunEvent === 'function'", timeout=10000)

    # The press: the row with the indicator at once, the field empty and usable meanwhile.
    took = press_send(page)
    assert took < 300, f"the translating row took {took:.0f} ms"
    row = page.locator(".msg.user[data-pending='translating']")
    expect(row).to_contain_text(WRITTEN)
    expect(row.locator("[data-translation='out-pending']")).to_have_text(words["pending"])
    field = page.locator(".composer .composer-field")
    page.wait_for_function("() => document.querySelector('.composer .composer-field')?.textContent === ''", timeout=2000)
    assert field.get_attribute("aria-disabled") is None, "the field is usable while the translation runs"
    assert len(SENT) == 1 and SENT[0]["text"] == WRITTEN
    sent_id = SENT[0]["client_message_id"]
    page.mouse.move(5, 5)
    shot(page, f"send-translating-{lang}.png")

    # Settled: the ordinary sent message, with what the agent got behind its switch.
    settle(page, sent_id)
    expect(page.locator(".msg.user[data-pending]")).to_have_count(0)
    mine = page.locator(".msg.user", has_text=WRITTEN)
    expect(mine).to_have_count(1)
    flip = mine.locator("[data-translation='out']")
    expect(flip).to_have_text(words["agent"])
    shot(page, f"send-translated-{lang}.png")

    # A reload while the host still has a message with its translator: drawn from the host's list alone.
    TRANSLATING.append({"id": "held-1", "text": "А на macOS?", "from": "ru", "to": "en", "at": "2026-11-17T12:01:00+00:00", "files": []})
    page.reload()
    page.wait_for_function("() => typeof window.emitRunEvent === 'function'", timeout=10000)
    held = page.locator(".msg.user[data-pending='translating']", has_text="А на macOS?")
    expect(held).to_be_visible()
    expect(held.locator("[data-translation='out-pending']")).to_have_text(words["pending"])
    settle(page, "held-1", text="А на macOS?")
    expect(page.locator(".msg.user[data-pending]")).to_have_count(0)
    expect(page.locator(".msg.user", has_text="А на macOS?")).to_have_count(1)

    # The translator failed: the message went as written, with the mark that says so.
    SENT.clear()
    press_send(page)
    failed_id = SENT[0]["client_message_id"]
    settle(page, failed_id, state="failed", error="the translator did not answer within 120 s")
    expect(page.locator(".msg.user[data-pending]")).to_have_count(0)
    expect(page.locator(".msg.user", has_text=WRITTEN).last.locator("[data-translation='out-failed']")).to_have_text(words["failed"])
    shot(page, f"send-translate-failed-{lang}.png")

    # Refused after the acknowledgement: the row goes, the words come back into the field, the reason is said.
    SENT.clear()
    press_send(page)
    refused_id = SENT[0]["client_message_id"]
    expect(page.locator(".msg.user[data-pending='translating']")).to_have_count(1)
    settle(page, refused_id, outcome="refused", error="daily budget exceeded")
    expect(page.locator(".msg.user[data-pending]")).to_have_count(0)
    page.wait_for_function(f"() => document.querySelector('.composer .composer-field')?.textContent === {json.dumps(WRITTEN)}", timeout=3000)
    assert field_text(field) == WRITTEN
    expect(page.get_by_text(words["refused"], exact=False).first).to_be_visible()
    expect(page.get_by_text("daily budget exceeded", exact=False).first).to_be_visible()
    context.close()


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        try:
            for lang in ("en", "ru"):
                walk(browser, lang)
                print(f"ok {lang}")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
