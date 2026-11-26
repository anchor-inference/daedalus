"""A task on the Board screen reads as a task, in both languages, wide and on a phone.

The card a project's orchestrator wrote used to open on its title, its status and a Move to row, and
nothing else: its brief, who did it and what came of it were only on the project's own board. Checked
here: the pane (a sheet on a phone) gives the brief as one Description and one Acceptance, who it is
assigned to, the history with the member's result, and a link that opens the same task on its
project's board. A task outside every project shows none of that and still opens; a new task is made,
checked off and closed from the pane.
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

WORDS = {
    "en": {"description": "Description", "acceptance": "Acceptance", "deliverable": "Deliverable", "assigned": "Ira · team", "open": "Open on the board of Bakery", "history": "History and results",
           "title": "Title", "checklist": "Checklist (one item per line)", "actions": "Task actions"},
    "ru": {"description": "Описание", "acceptance": "Приёмка", "deliverable": "Результат", "assigned": "Ira · команда", "open": "Открыть на доске проекта Bakery", "history": "История и результаты",
           "title": "Название", "checklist": "Чек-лист (по пункту в строке)", "actions": "Действия с задачей"},
}
RESULT = "[2026-09-24 09:30] result from Ira: plan.md written, four limits and two open questions"


def project() -> dict:
    return {"id": PID, "name": "Bakery", "folders": folders("/home/operator/work/bakery"), "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}


def board() -> BoardStub:
    ira = BoardStub.assignee("st-ira", "Ira", harness="claude", color="orange")
    brief = {"objective": "Plan the delivery limits for the shop", "deliverable": "plan.md in the notes folder", "boundaries": "", "done_when": "plan.md names every limit"}
    tasks = [
        BoardStub.task("t-plan", "Delivery limits plan", status="done", assignee=ira, brief=brief, project_id=PID, origin_session_id="sess-orch", notes=RESULT),
        BoardStub.task("t-own", "Water the plants", status="todo", origin_session_id=None, acceptance="Every pot is wet."),
    ]
    return BoardStub(project(), staff=[{"id": "st-ira", "name": "Ira", "color": "orange", "harness": "claude"}], tasks=tasks)


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


def pane_of(page: Page):  # type: ignore[no-untyped-def]
    """The open task: the pane beside a wide board, the sheet on a phone."""
    return page.locator(".task-pane:not(.in-sheet), .task-sheet")


def check(page: Page, lang: str, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    stub = board()
    serve(page, stub, unhandled)
    page.goto(f"{BASE}/board/t-plan?token=t&lang={lang}")
    pane = pane_of(page)
    expect(pane).to_contain_text("Delivery limits plan")
    blocks = pane.locator(".task-block")
    # The brief is two blocks, not four headed parts: the objective and the deliverable are the
    # description, "done when" is the acceptance; the empty boundaries are left out, not a blank line.
    description = blocks.filter(has_text=words["description"])
    expect(description).to_contain_text("Plan the delivery limits for the shop")
    expect(description).to_contain_text(f"{words['deliverable']}: plan.md in the notes folder")
    expect(blocks.filter(has_text=words["acceptance"])).to_contain_text("plan.md names every limit")
    expect(pane.locator(".task-kv")).to_contain_text(words["assigned"])
    expect(pane).to_contain_text(words["history"])
    expect(pane).to_contain_text("plan.md written, four limits")
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{lang}: the page scrolls sideways by {overflow}px"

    pane.get_by_role("button", name=words["open"]).click()
    page.wait_for_url(f"**/project/{PID}/board?task=t-plan*")

    page.goto(f"{BASE}/board/t-own?token=t&lang={lang}")
    own = pane_of(page)
    expect(own.locator(".task-block").filter(has_text=words["acceptance"])).to_contain_text("Every pot is wet.")
    expect(own.get_by_role("button", name=words["open"])).to_have_count(0)

    page.goto(f"{BASE}/board?token=t&lang={lang}")
    page.get_by_role("button", name="New task" if lang == "en" else "Новая задача").first.click()
    created = page.locator(".sheet.board-new")
    created.get_by_label(words["title"]).fill("Check plants")
    created.get_by_label(words["checklist"]).fill("Each pot inspected")
    created.get_by_role("button", name="Create" if lang == "en" else "Создать").click()
    expect(created).to_have_count(0)
    assert stub.created[-1]["client_operation_id"] and stub.created[-1]["expected_collection_revision"] == 3
    assert stub.created[-1]["checklist"] == ["Each pot inspected"], stub.created[-1]
    # A desktop's card, or the row of a phone's list.
    page.locator(".task-card, .ph-task", has_text="Check plants").first.click()
    task_pane = pane_of(page)
    task_id = next(task["id"] for task in stub.tasks if task["title"] == "Check plants")
    with page.expect_response(lambda response: urlsplit(response.url).path.endswith(f"/api/board/{task_id}") and response.request.method == "PUT"):
        task_pane.get_by_role("checkbox").click()
    assert stub.updated[-1][1]["check_ids"] == ["C1"] and stub.updated[-1][1]["client_operation_id"]
    task_pane.get_by_role("button", name=words["actions"]).click()
    page.get_by_role("menuitem", name="Close as done…" if lang == "en" else "Закрыть как готовую…").click()
    dialog = page.locator(".sheet-backdrop.confirm .dialog")
    dialog.locator("textarea").fill("Checked by hand")
    with page.expect_response(lambda response: urlsplit(response.url).path.endswith(f"/api/board/{task_id}/close")):
        dialog.locator("button").last.click()
    assert stub.closed[-1] == (task_id, {**stub.closed[-1][1], "status": "done", "reason": "Checked by hand"})
    closed = next(task for task in stub.tasks if task["title"] == "Check plants")
    assert closed["status"] == "done" and closed["acceptance_state"] == "operator_approved"


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
                context = browser.new_context(viewport=viewport)
                check(context.new_page(), lang, unhandled)
                context.close()
        browser.close()
    print("check_board_card: ok")
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
