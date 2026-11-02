"""A card its own chat finished says so, and the operator can reopen it, in both languages.

A chat may take a task on its own board to done itself, with its result in a note. That is the one
way a card closes without the operator watching, so the card names the chat, when it finished and
what it said, on the Board screen and on the project's board, and offers Reopen. Checked here: the
section and its words, the link to the chat, and that Reopen sends the operator's move back to To do
and the section goes once the card is open again.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, TeamStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "9f3c2a1b7d40"
NOTE = "The menu page is live at /menu with every dish and its price."

WORDS = {
    "en": {"title": "Finished in its chat", "who": "Finished by", "reopen": "Reopen", "result": "Result"},
    "ru": {"title": "Завершена в своём чате", "who": "Завершил чат", "reopen": "Вернуть в работу", "result": "Результат"},
}


def project() -> dict:
    return {"id": PID, "name": "Bakery", "folders": folders("/home/operator/work/bakery"), "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}


def board() -> BoardStub:
    finished = {"session_id": "sess-chat", "title": "Menu planning", "note": NOTE, "at": "2026-11-02T09:30:00Z"}
    tasks = [
        BoardStub.task("t-chat", "Write the menu page", status="done", project_id=PID, origin_session_id="sess-chat",
                       session_id="sess-chat", acceptance_state="accepted", accepted_result_id="r-chat", finished_by=finished,
                       checklist=[{"id": "C1", "text": "draft", "done": True}]),
    ]
    return BoardStub(project(), staff=[], tasks=tasks)


def serve(page: Page, stub: BoardStub, unhandled: Unhandled) -> None:
    team = TeamStub(project())

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = stub.answer(request.method, path, url.query, body) or team.answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if path == "/api/projects":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps([project()]))
        if path == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", handle)


def check(page: Page, lang: str, phone: bool, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    stub = board()
    serve(page, stub, unhandled)

    # The project's board, where the card's result part carries the section.
    page.goto(f"{BASE}/project/{PID}/board?task=t-chat&token=t&lang={lang}")
    finish = page.locator(".chat-finish").first
    expect(finish).to_be_visible()
    expect(finish).to_contain_text(words["title"])
    expect(finish).to_contain_text(NOTE)

    # The Board screen, where the operator sees every chat's tasks.
    page.goto(f"{BASE}/board/t-chat?token=t&lang={lang}")
    finish = page.locator(".sheet .chat-finish")
    expect(finish).to_contain_text(words["title"])
    expect(finish).to_contain_text(words["who"])
    expect(finish.get_by_role("button", name="Menu planning")).to_be_visible()
    expect(finish).to_contain_text(NOTE)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{lang}: the page scrolls sideways by {overflow}px"
    with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/api/board/t-chat") and response.request.method == "PUT"):
        finish.get_by_role("button", name=words["reopen"]).click()
    sent = stub.updated[-1]
    assert sent[0] == "t-chat" and sent[1]["status"] == "todo" and sent[1]["expected_entity_revision"] == 1 and sent[1]["client_operation_id"], sent
    expect(page.locator(".chat-finish")).to_have_count(0)
    row = stub.tasks[0]
    assert row["status"] == "todo" and row["acceptance_state"] == "returned", (phone, row)


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
                context = browser.new_context(viewport=viewport)
                check(context.new_page(), lang, viewport["width"] < 600, unhandled)
                context.close()
        browser.close()
    print("check_chat_finish: ok")
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
