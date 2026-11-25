"""A chat's own board lives in its right panel: the tasks it and its helpers created, by where they stand.

Every chat has a Board tab. It is drawn beside the tools the operator opened while the chat has an open
task (as Questions is while questions wait: it never takes the active tab and never opens a closed
panel), says how many cards are finished ("1/5"), and lists the chat's cards stacked: doing, to do,
review, blocked, the finished folded. Checked here, in both languages:

* the list is what the host answers for this chat, and only that; the Overview's tasks block and its
  Board row lead to the tab, and the tab leads to the full board narrowed to this chat;
* a card's "…" moves it where the host allows and closes it; a move the host refuses is written on
  the card that was moved, not in a toast;
* a card opens inside the panel as a pushed view with a way back, and the address says which;
* a new task is created on the chat's project board with the chat as its origin; a refusal is written
  under the field;
* a task event of this chat reloads the list, one of another chat does not;
* a helper's chat shows its leader's board and says so; a chat with no tasks says what the tab is for;
* on a phone the same tab is in the panel's sheet and nothing runs sideways.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_chat_board.py
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
import screenshots  # noqa: E402
from api_stub import DEFAULT_APP, BoardStub, expect_app  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from screenshots import P1, S1, S2, S6, S12  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

REFUSED = "this task still has unfinished dependencies"
WORDS = {
    "en": {"tab": "Board", "title": "This chat's tasks", "full": "Open on the full board", "add": "Add a task for the agent", "doing": "Doing", "todo": "To do",
           "review": "Review", "blocked": "Blocked", "finished": "Finished", "to_blocked": "Move to Blocked", "to_doing": "Move to Doing", "close": "Close as done…",
           "closing": "Close as done", "back": "Back to this chat's tasks", "helper": "Tasks of the leading chat “Bakery site”", "empty": "No tasks yet",
           "count": "1 of 6"},
    "ru": {"tab": "Доска", "title": "Задачи этого чата", "full": "Открыть на полной доске", "add": "Добавить задачу агенту", "doing": "В работе", "todo": "К работе",
           "review": "На проверке", "blocked": "Заблокировано", "finished": "Завершённые", "to_blocked": "Перенести в «Заблокировано»", "to_doing": "Перенести в «В работе»",
           "close": "Закрыть как готовую…", "closing": "Закрыть как готовую", "back": "Назад к задачам этого чата", "helper": "Задачи ведущего чата «Bakery site»",
           "empty": "Задач пока нет", "count": "1 из 6"},
}


def board() -> BoardStub:
    mine = {"origin_session_id": S1, "session_id": S1, "project_id": P1}
    tasks = [
        BoardStub.task("t-order", "Order form posts to the sheet", status="doing", checklist=[{"text": "form", "done": True}, {"text": "page", "done": False}], **mine),
        BoardStub.task("t-hours", "Opening hours from the calendar", status="todo", **mine),
        BoardStub.task("t-rye", "Taste the new rye loaf", status="todo", depends_on=["t-order"], **mine),
        BoardStub.task("t-own", "Call the courier", status="todo", **mine),
        BoardStub.task("t-review", "Show an error when an order fails", status="review", **mine),
        BoardStub.task("t-done", "Read the order form", status="done", **mine),
        BoardStub.task("t-other", "Photos for the gallery", status="doing", origin_session_id=S2, session_id=S2, project_id=P1),
    ]
    stub = BoardStub({"id": P1, "name": "Bakery site"}, staff=[], tasks=tasks)
    stub._own(stub.tasks[2], True)
    stub._own(stub.tasks[3], True)
    stub.refusals["t-rye"] = REFUSED
    stub.subagent_of[S12] = S1
    stub.session_projects[S1] = P1
    return stub


class Host:
    """What the page asked of the board, and the stream the check writes events into."""

    def __init__(self, stub: BoardStub, feed: EventFeed) -> None:
        self.stub = stub
        self.feed = feed
        self.listings: list[dict[str, str]] = []
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[tuple[str, dict]] = []

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):]
        board_path = path.startswith(("/api/board", "/api/control/revisions")) or path == f"/api/projects/{P1}/board"
        if not board_path:
            return screenshots.stub(route)
        body = request.post_data_json if request.method in ("POST", "PUT") and request.post_data else None
        if path == "/api/board" and request.method == "GET":
            self.listings.append({k: v[0] for k, v in parse_qs(url.query).items()})
        if request.method == "POST" and path.endswith("/board"):
            self.posts.append((path, body or {}))
        if request.method == "PUT":
            self.puts.append((path, body or {}))
        answered = self.stub.answer(request.method, path, url.query, body)
        if answered is None:
            return screenshots.stub(route)
        status, payload = answered
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def listed(self) -> int:
        return len(self.listings)


def open_chat(page: Page, lang: str, chat: str = S1, panel: str = "overview", extra: str = "") -> None:
    page.goto(f"{BASE}/agents/{chat}?token=t&lang={lang}&panel={panel}{extra}", wait_until="domcontentloaded")
    page.wait_for_selector(".panel .panel-tabs, .panel-sheet .panel-tabs")


def card(scope, id_: str):  # type: ignore[no-untyped-def]
    return scope.locator(f".cb .task-card[data-task='{id_}']")


def more(scope, id_: str):  # type: ignore[no-untyped-def]
    wrap = scope.locator(".task-card-wrap", has=scope.page.locator(f".task-card[data-task='{id_}']"))
    wrap.hover()
    return wrap.locator(".task-card-more")


def pick(page: Page, name: str) -> None:
    page.locator(".menu [role=menuitem]", has_text=name).first.click()


def desktop(page: Page, host: Host, lang: str) -> None:
    words = WORDS[lang]
    scope = page.locator(".panel")
    open_chat(page, lang)
    # The tab is drawn beside the Overview without being opened, with its figure, and the panel stays on the Overview.
    tab = scope.locator(".panel-tab[data-tab='board']")
    expect(tab).to_be_visible()
    expect(tab).to_contain_text("1/6")
    expect(scope.locator(".panel-tab[data-tab='overview'].on")).to_have_count(1)
    # The Overview says where the chat is on its board, and both ways in lead to the tab.
    row = scope.locator("[data-row='board']")
    expect(row).to_contain_text("1/6")
    expect(scope.locator(".ov-board [data-count='doing']")).to_have_count(1)
    scope.locator(".ov-board-head button").click()
    expect(scope.locator(".panel-tab[data-tab='board'].on")).to_have_count(1)
    assert "/board?" not in page.url.split("/app", 1)[-1].split("panel=")[0], page.url

    # The list: this chat's cards by where they stand, nothing of another chat's, the finished folded.
    expect(scope.locator(".cb-title")).to_contain_text(words["title"])
    expect(scope.locator(".cb-count")).to_have_text(words["count"])
    expect(scope.locator(".cb .task-card")).to_have_count(5)
    expect(scope.locator("[data-task='t-other']")).to_have_count(0)
    heads = scope.locator(".cb .kanban-col .kanban-name").all_text_contents()
    assert heads == [words["doing"], words["todo"], words["review"], words["finished"]], heads
    assert host.listings[-1].get("session") == S1 and host.listings[-1].get("include_done") == "1", host.listings[-1]
    expect(scope.locator(".cb-bar")).to_have_attribute("aria-label", re.compile("17"))
    full = scope.get_by_role("link", name=words["full"])
    expect(full).to_have_attribute("href", re.compile(rf"/board\?scope=chat&session={S1}"))
    expect(scope.locator(".cb-add input")).to_have_attribute("placeholder", words["add"])

    # A move: the card the operator keeps for themselves goes to Blocked with one edit, and sits there.
    more(scope, "t-own").click()
    pick(page, words["to_blocked"])
    expect(scope.locator(".kanban-col[data-column='blocked'] [data-task='t-own']")).to_have_count(1)
    assert host.puts[-1][0] == "/api/board/t-own" and host.puts[-1][1]["status"] == "blocked", host.puts[-1]
    assert host.puts[-1][1]["expected_entity_revision"] == 1, host.puts[-1]

    # A refusal: the host will not start a card whose dependency is open; it says so on the card, not in a toast.
    more(scope, "t-rye").click()
    pick(page, words["to_doing"])
    refusal = card(scope, "t-rye").locator(".task-card-refusal")
    expect(refusal).to_be_visible()
    expect(refusal).to_contain_text(re.compile(REFUSED, re.I))
    expect(page.locator(".toast")).to_have_count(0)
    expect(scope.locator(".kanban-col[data-column='todo'] [data-task='t-rye']")).to_have_count(1)

    # A close: the confirmation names the card, and the card leaves the open columns for the finished ones.
    more(scope, "t-hours").click()
    pick(page, words["close"])
    page.get_by_role("alertdialog").get_by_role("button", name=words["closing"]).click()
    expect(card(scope, "t-hours")).to_have_count(0)
    assert [task_id for task_id, _ in host.stub.closed] == ["t-hours"] and host.stub.closed[0][1]["status"] == "done", host.stub.closed
    expect(scope.locator(".cb-count")).to_have_text(words["count"].replace("1", "2", 1))

    # A card opens inside the panel with a way back; the address says which card.
    card(scope, "t-order").click()
    pane = scope.locator(".task-pane.in-panel")
    expect(pane).to_be_visible()
    expect(pane.locator(".task-pane-title")).to_have_text("Order form posts to the sheet")
    expect(scope.locator(".panel-tab[data-tab='board'].on")).to_have_count(1)
    page.wait_for_url(re.compile(r"panel=board.*task=t-order|task=t-order.*panel=board"))
    box = pane.bounding_box()
    panel_box = scope.bounding_box()
    assert box and panel_box and abs(box["width"] - panel_box["width"]) <= 2, (box, panel_box)
    expect(scope.locator(".kanban-col")).to_have_count(0)
    pane.get_by_role("button", name=words["back"]).click()
    expect(scope.locator(".cb .task-card")).not_to_have_count(0)
    assert "task=" not in page.url, page.url
    card(scope, "t-order").click()
    page.keyboard.press("Escape")
    expect(scope.locator(".task-pane.in-panel")).to_have_count(0)
    expect(scope.locator(".panel-tab[data-tab='board'].on")).to_have_count(1)

    # A task added: it is made on the chat's project board, the chat is its origin, and it lands in To do.
    field = scope.locator(".cb-add input")
    field.fill("Check the price list against the menu")
    field.press("Enter")
    expect(scope.locator(".kanban-col[data-column='todo'] .task-card", has_text="Check the price list against the menu")).to_have_count(1)
    path, body = host.posts[-1]
    assert path == f"/api/projects/{P1}/board" and body["session_id"] == S1 and body["title"] == "Check the price list against the menu", host.posts[-1]
    assert body["expected_collection_revision"] >= 1 and body["client_operation_id"], body
    expect(field).to_have_value("")
    # A chat the host files on another board is refused: the words stay where they were typed.
    host.stub.session_projects[S1] = "another-project"
    field.fill("Another task")
    field.press("Enter")
    alert = scope.locator(".cb-add-wrap [role=alert]")
    expect(alert).to_contain_text(re.compile("another board", re.I))
    expect(field).to_have_value("Another task")
    host.stub.session_projects[S1] = P1
    field.fill("")

    # Live: an event of this chat reloads the list, one of another chat does not.
    before = host.listed()
    host.stub.tasks.append(BoardStub.task("t-live", "Written by the agent", status="todo", origin_session_id=S1, session_id=S1, project_id=P1))
    host.feed.send("task.created", {"task_id": "t-other"}, session=S2)
    page.wait_for_timeout(900)
    assert host.listed() == before, "another chat's task event reloaded the list"
    expect(scope.locator("[data-task='t-live']")).to_have_count(0)
    host.feed.send("task.created", {"task_id": "t-live"}, session=S1)
    expect(scope.locator("[data-task='t-live']")).to_have_count(1)

    # The full board, narrowed to this chat.
    full.click()
    page.wait_for_url(re.compile(rf"/board\?.*scope=chat.*session={S1}|/board\?.*session={S1}.*scope=chat"))


def helper_and_empty(page: Page, host: Host, lang: str) -> None:
    words = WORDS[lang]
    scope = page.locator(".panel")
    # A helper works on its leader's board: the host answers its id with the leader's cards, and the heading says whose.
    open_chat(page, lang, S12, "board")
    expect(scope.locator(".cb-title")).to_contain_text(words["helper"])
    expect(scope.locator(".cb-sub")).to_be_visible()
    expect(scope.locator(".cb .task-card")).not_to_have_count(0)
    assert host.listings[-1].get("session") == S12, host.listings[-1]
    # A chat with no tasks says what the tab is for, and the field is still there.
    open_chat(page, lang, S6, "board")
    expect(scope.locator("[data-empty]")).to_contain_text(words["empty"])
    expect(scope.locator(".cb-add input")).to_be_visible()
    expect(scope.locator(".panel-tab[data-tab='board'].on")).to_have_count(1)
    # With nothing open the tab is not drawn beside the tools until it is opened: the Overview first, no Board row.
    open_chat(page, lang, S6, "overview")
    expect(scope.locator(".panel-tab[data-tab='board']")).to_have_count(0)
    expect(scope.locator("[data-row='board']")).to_have_count(0)
    # It is still in the "+" menu.
    scope.locator(".panel-add").click()
    expect(page.locator(".panel-tools [data-tool='board']")).to_have_count(1)


def phone(page: Page, host: Host, lang: str) -> None:
    words = WORDS[lang]
    sheet = page.locator(".panel-sheet")
    open_chat(page, lang, S1, "board")
    expect(sheet.locator(".cb .task-card").first).to_be_visible()
    expect(sheet.locator(".cb-add input")).to_be_visible()
    # A touch screen has no hover: the card's "…" is there to press, and it moves the card.
    button = sheet.locator(".task-card-more").first
    expect(button).to_be_visible()
    assert float(button.evaluate("el => getComputedStyle(el).opacity")) == 1.0
    sheet.locator(".task-card-wrap", has=page.locator(".task-card[data-task='t-own']")).locator(".task-card-more").click()
    pick(page, words["to_blocked"])
    expect(sheet.locator(".kanban-col[data-column='blocked'] [data-task='t-own']")).to_have_count(1)
    sheet.locator(".cb .task-card[data-task='t-order']").click()
    expect(sheet.locator(".task-pane.in-panel")).to_be_visible()
    sheet.get_by_role("button", name=words["back"]).click()
    expect(sheet.locator(".cb .task-card").first).to_be_visible()
    wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert wide <= 0, f"the page scrolls sideways by {wide}px"
    over = sheet.locator(".cb").evaluate(
        "root => { const b = root.getBoundingClientRect(); return [...root.querySelectorAll('*')].filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && (r.right > b.right + 0.5 || r.left < b.left - 0.5); }).map(el => el.className).slice(0, 5); }"
    )
    assert not over, f"something runs past the panel's side: {over}"


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for name, scene, viewport, touch in (("desktop", desktop, {"width": 1440, "height": 900}, False), ("helper and empty", helper_and_empty, {"width": 1440, "height": 900}, False), ("phone", phone, {"width": 390, "height": 844}, True)):
                feed = EventFeed()
                host = Host(board(), feed)
                page = browser.new_page(viewport=viewport, is_mobile=touch, has_touch=touch)
                page.set_default_timeout(8000)
                page.route("**/api/**", host.route)
                page.route("**/api/events**", feed.route)
                try:
                    scene(page, host, lang)
                except Exception as exc:  # noqa: BLE001 — every scene is looked at; the failures are listed together
                    problems.append(f"{lang} {name}: {str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__}")
                page.close()
                feed.close()
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(run())
