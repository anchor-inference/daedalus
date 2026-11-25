"""The Board's scope, its task pane's edits, a drag and a refused move, in both languages.

The board drew every card of every chat with no way to narrow it to one project or one chat, opened a
card in a dialog over the board, and could neither set a due date nor keep a card for the operator.
Checked here: the scope control asks the host for one project's or one chat's cards and says so in the
address, so a chat's own board can link to its place here; the pane sets the priority, the due date
and the assignee with one PUT each; a card the operator keeps can be dragged into work; a move the host
refuses is said under the button that asked, not in a toast; the history of the card is listed, and a
card older than the host's history table says that its earlier history was not recorded. A column's
"+" makes the card in that column where the host allows it (not Review), and a refusal is the host's
words under the form.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, TeamStub, Unhandled, expect_app, fulfil_shared  # noqa: E402
from check_board_card import PID, project  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"scope": "Board scope", "project": "Project", "chat": "Chat", "all": "All", "priority": "Priority", "high": "High · P2", "due": "Due",
           "assignee": "Assignee", "you": "You", "doing": "Doing", "activity": "Activity", "kept": "Created in To do", "old": "Earlier history was not recorded",
           "search": "Search tasks", "late": "overdue", "yours": "Yours", "clear": "Clear the due date",
           "title": "Title", "create": "Create", "agent": "The chat's agent"},
    "ru": {"scope": "Область доски", "project": "Проект", "chat": "Чат", "all": "Все", "priority": "Приоритет", "high": "Высокий · P2", "due": "Срок",
           "assignee": "Исполнитель", "you": "Вы", "doing": "В работе", "activity": "Активность", "kept": "Создана в колонке «К работе»", "old": "Более ранняя история не записывалась",
           "search": "Поиск по задачам", "late": "просрочена", "yours": "Ждёт вас", "clear": "Убрать срок",
           "title": "Название", "create": "Создать", "agent": "Агент чата"},
}
REFUSED = "this task still has unfinished dependencies"
# The app says the host's reason as a sentence, its first letter raised.
REFUSED_SAID = re.compile(re.escape(REFUSED), re.I)
SESSIONS = [
    {"id": "sess-menu", "title": "Menu planning", "status": "running", "created_at": "2026-11-20T09:00:00Z", "last_message_at": "2026-11-24T09:00:00Z", "run_id": None, "project_id": PID, "project": "Bakery"},
    {"id": "sess-home", "title": "Home chores", "status": "idle", "created_at": "2026-11-20T09:00:00Z", "last_message_at": "2026-11-23T09:00:00Z", "run_id": None, "project_id": "", "project": ""},
]


def board() -> BoardStub:
    tasks = [
        BoardStub.task("t-menu", "Seasonal menu page", status="doing", project_id=PID, origin_session_id="sess-menu", session_id="sess-menu"),
        BoardStub.task("t-taste", "Taste the rye loaf", status="todo", project_id=PID, origin_session_id="sess-menu", depends_on=["t-menu"]),
        BoardStub.task("t-water", "Water the plants", status="todo", origin_session_id="sess-home", due_at="2026-10-01T23:59:59Z",
                       created_at="2026-09-01T09:00:00Z"),
        BoardStub.task("t-old", "Old banner", status="dropped", origin_session_id="sess-home"),
    ]
    stub = BoardStub(project(), staff=[], tasks=tasks)
    stub._own(stub.tasks[1], True)
    # A card older than the history table: its creation was rebuilt without its column.
    stub.history["t-water"] = [{"seq": 1, "type": "task.created", "at": "2026-09-01T09:00:00Z", "actor": {"kind": "system", "id": None, "name": None},
                                "from": None, "to": None, "text": "Created", "operation": None, "backfilled": True}]
    stub.refusals["t-taste"] = REFUSED
    return stub


def serve(page: Page, stub: BoardStub, unhandled: Unhandled, asked: list[dict]) -> None:
    team = TeamStub(project())

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/board" and request.method == "GET":
            asked.append({k: v[0] for k, v in parse_qs(url.query).items()})
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = stub.answer(request.method, path, url.query, body) or team.answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if path == "/api/projects":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps([project()]))
        if path == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": SESSIONS, "projects": []}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", handle)


def check(page: Page, lang: str, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    stub = board()
    asked: list[dict] = []
    serve(page, stub, unhandled, asked)
    page.goto(f"{BASE}/board?token=t&lang={lang}")
    cards = page.locator(".task-card")
    expect(cards).to_have_count(3)
    expect(page.locator(".board-filter[data-filter='done'] .n")).to_have_text("1")

    # The scope: one project's cards, then one chat's, each asked of the host and kept in the address.
    scope = page.get_by_role("radiogroup", name=words["scope"])
    scope.get_by_role("radio", name=words["project"]).click()
    page.wait_for_url(f"**/board?scope=project&project={PID}*")
    expect(cards).to_have_count(2)
    assert asked[-1].get("project") == PID, asked[-1]
    page.locator(".board-scope-pick").click()
    page.locator(".board-scope-pop").get_by_role("option", name="Home chores").click()
    page.wait_for_url("**/board?scope=chat&session=sess-home*")
    expect(cards).to_have_count(1)
    assert asked[-1].get("session") == "sess-home", asked[-1]
    # An empty column says so rather than standing blank.
    expect(page.locator(".kanban-col[data-column='doing'] .kanban-empty")).to_be_visible()
    scope.get_by_role("radio", name=words["all"]).click()
    expect(cards).to_have_count(3)

    # The search narrows to the cards whose words match.
    page.get_by_role("button", name=words["search"]).click()
    page.locator(".board-search input").fill("rye")
    expect(cards).to_have_count(1)
    page.locator(".board-search input").press("Escape")
    expect(cards).to_have_count(3)

    # A late card says so on the card and in the pane, and the pane's edits are one PUT each.
    water = page.locator(".task-card[data-task='t-water']")
    expect(water.locator(".task-card-due.late")).to_be_visible()
    water.click()
    pane = page.locator(".task-pane")
    expect(pane.locator(".task-due-btn")).to_contain_text(words["late"])
    expect(pane.locator(".task-block").filter(has_text=words["activity"])).to_contain_text(words["old"])
    pane.get_by_role("combobox", name=words["priority"]).click()
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/board/t-water") and r.request.method == "PUT"):
        page.get_by_role("option", name=words["high"]).click()
    assert stub.updated[-1][1]["priority"] == 2 and stub.updated[-1][1]["client_operation_id"], stub.updated[-1]
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/board/t-water") and r.request.method == "PUT"):
        pane.get_by_label(words["due"], exact=True).fill("2026-12-24")
    assert stub.updated[-1][1]["due_at"] == "2026-12-24", stub.updated[-1]
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/board/t-water") and r.request.method == "PUT"):
        pane.get_by_role("button", name=words["clear"]).click()
    assert stub.updated[-1][1]["due_at"] == "", stub.updated[-1]
    pane.get_by_role("combobox", name=words["assignee"]).click()
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/board/t-water") and r.request.method == "PUT"):
        page.get_by_role("option", name=words["you"], exact=True).click()
    assert stub.updated[-1][1]["assignee_operator"] is True, stub.updated[-1]
    expect(page.locator(".task-card[data-task='t-water'] .task-card-who.you")).to_be_visible()
    expect(page.locator(".task-card[data-task='t-water'] .task-card-state")).to_contain_text(words["yours"])

    # A card the operator keeps can be dragged into work; the host refuses this one, and the card says why.
    taste = page.locator(".task-card[data-task='t-taste']")
    taste.drag_to(page.locator(".kanban-col[data-column='doing']"))
    expect(taste.locator(".task-card-refusal")).to_contain_text(REFUSED_SAID)
    assert stub.updated[-1][1]["status"] == "doing", stub.updated[-1]
    # In the pane, the refusal is under the move that asked for it.
    taste.click()
    pane.get_by_role("button", name=words["doing"], exact=True).click()
    expect(pane.locator(".task-block .task-refusal[role=alert]")).to_contain_text(REFUSED_SAID)
    expect(pane.locator(".task-block").filter(has_text=words["activity"])).to_contain_text(words["kept"])
    page.keyboard.press("Escape")
    expect(pane).to_have_count(0)

    # Review is reached only through a result, so it has no "+"; Blocked takes a card from anyone.
    expect(page.locator(".kanban-col[data-column='review'] .kanban-add")).to_have_count(0)
    page.locator(".kanban-col[data-column='blocked'] .kanban-add").click()
    sheet = page.locator(".board-new")
    sheet.get_by_label(words["title"], exact=True).fill("Wait for the quote")
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/board") and r.request.method == "POST"):
        sheet.get_by_role("button", name=words["create"], exact=True).click()
    assert stub.created[-1]["status"] == "blocked", stub.created[-1]
    expect(page.locator(".kanban-col[data-column='blocked'] .task-card")).to_have_count(1)
    # Doing takes only a card the operator keeps: the sheet starts with "You", and handing it to the
    # agent is refused in the host's words under the form.
    page.locator(".kanban-col[data-column='doing'] .kanban-add").click()
    sheet.get_by_label(words["title"], exact=True).fill("Renew the domain")
    sheet.get_by_role("radio", name=words["agent"]).click()
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/board") and r.request.method == "POST" and r.status == 409):
        sheet.get_by_role("button", name=words["create"], exact=True).click()
    expect(sheet.locator(".task-refusal[role=alert]")).to_contain_text("dedicated result or launch command")
    page.keyboard.press("Escape")
    expect(sheet).to_have_count(0)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{lang}: the page scrolls sideways by {overflow}px"


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            check(context.new_page(), lang, unhandled)
            context.close()
        browser.close()
    print("check_board_pane: ok")
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
