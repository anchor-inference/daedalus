"""A project's board on a desktop and a phone, in both languages.

What is checked is what the operator relies on: on a wide window the columns stand side by side —
Needs you, In progress, Review, Queue, and Done folded; on a phone they are chips over one list, and a
chip narrows the list to its column. A request waiting on the operator can be answered from its card;
a task in review requires a current result and verdict before acceptance; a new task carries its four-part brief, its assignee and
what it waits for; an edit sends only what changed. And the page fits: nothing scrolls sideways at 390 px.
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
    "en": {
        "title": "Board · Bakery", "needs": "Needs you", "doing": "In progress", "review": "Review", "queue": "Queue", "done": "Done",
        "answer": "Answer", "accept": "Accept", "merge": "Merge", "new": "New task", "create": "Create", "save": "Save", "working": "working",
        "after": "after “Checkout”", "accepted": "is done", "queued": "Starting the task", "missing": "Missing", "started": "Starting the task",
    },
    "ru": {
        "title": "Доска · Bakery", "needs": "Нужны вы", "doing": "В работе", "review": "Проверка", "queue": "Очередь", "done": "Готово",
        "answer": "Ответить", "accept": "Принять", "merge": "Слить", "new": "Новая задача", "create": "Создать", "save": "Сохранить", "working": "работает",
        "after": "после «Checkout»", "accepted": "готово", "queued": "Запускаем задачу", "missing": "Не заполнено", "started": "Запускаем задачу",
    },
}


def project() -> dict:
    listed = folders("/home/operator/work/bakery")
    listed[0]["is_git"] = True
    return {"id": PID, "name": "Bakery", "folders": listed, "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}


def board() -> BoardStub:
    staff = [
        {"id": "st-ira", "name": "Ira", "color": "orange", "harness": "claude"},
        {"id": "st-max", "name": "Max", "color": "blue", "harness": "codex"},
        {"id": "st-lev", "name": "Lev", "color": "teal", "harness": "daedalus"},
    ]
    ira = BoardStub.assignee("st-ira", "Ira", harness="claude", color="orange", status="working", on_task=True)
    tasks = [
        BoardStub.task("t-checkout", "Checkout", status="doing", priority=1, assignee=ira),
        BoardStub.task("t-endpoint", "Notify endpoint", status="review", assignee=BoardStub.assignee("st-max", "Max", harness="codex"), branch="agent/max/endpoint"),
        BoardStub.task("t-photos", "Menu photo captions", status="blocked", depends_on=["t-checkout"], assignee=BoardStub.assignee("st-lev", "Lev", color="teal")),
        BoardStub.task("t-old", "Old price list", status="done"),
    ]
    needs = [{
        "id": "ask-1", "short_id": "q7k2m9", "origin": "staff", "kind": "question", "text": "SPRING10: before delivery or after?", "suggestion": "",
        "created_at": "2026-09-24T09:50:00Z", "task_id": "t-checkout", "task_title": "Checkout", "staff": staff[0], "session_id": "sess-ira",
    }]
    return BoardStub(project(), staff=staff, tasks=tasks, needs_you=needs)


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, stub: BoardStub, unhandled: Unhandled, listed: dict | None = None) -> None:
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
            return route.fulfill(status=200, content_type="application/json", body=json.dumps([listed or project()]))
        if request.method == "GET" and path == f"/api/projects/{PID}/next-actions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"project_id": PID, "actions": []}))
        if path == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if path.startswith("/api/sessions/"):
            # The answer button opens the staff member's session; what that screen shows is not this check's.
            return route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "no such session"}))
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", handle)


def desktop(page: Page, lang: str, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    stub = board()
    serve(page, stub, unhandled)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    expect(page.get_by_role("heading", name=words["title"])).to_be_visible()
    cols = page.locator(".pboard-cols")
    expect(cols).to_be_visible()
    expect(page.locator(".pboard-chips")).to_have_count(0)
    titles = cols.locator(".pboard-col > .section-title")
    expect(titles).to_have_count(5)
    for i, key in enumerate(("needs", "doing", "review", "queue", "done")):
        expect(titles.nth(i)).to_contain_text(words[key])

    # The columns stand side by side, left to right.
    boxes = [cols.locator(f".pboard-col.{c}").bounding_box() for c in ("needs", "doing", "review", "queue", "done")]
    assert all(b is not None for b in boxes)
    xs = [b["x"] for b in boxes if b]
    assert xs == sorted(xs) and len(set(round(x) for x in xs)) == 5, xs

    need = cols.locator(".pboard-col.needs .pcard.need")
    expect(need).to_contain_text("SPRING10")
    expect(need).to_contain_text("q7k2m9")
    doing = cols.locator(".pboard-col.doing .pcard", has_text="Checkout")
    expect(doing.locator(".harness-badge")).to_have_text("CC")
    expect(doing).to_contain_text(words["working"])
    expect(cols.locator(".pboard-col.queue .pcard", has_text="Menu photo captions")).to_contain_text(words["after"])
    # Finished work is folded until asked for.
    expect(cols.locator(".pboard-col.done .pcard")).to_have_count(0)
    cols.locator(".pboard-col.done .pboard-fold").click()
    expect(cols.locator(".pboard-col.done .pcard", has_text="Old price list")).to_be_visible()

    # A branch is inspectable, while a missing exact result never becomes an Accept shortcut.
    review = cols.locator(".pboard-col.review .pcard", has_text="Notify endpoint")
    review.click()
    sheet = page.locator(".sheet.pboard-sheet")
    expect(sheet.locator(".review-panel")).to_be_visible()
    expect(sheet.locator(".result-flow button", has_text="Accept" if lang == "en" else "Принять")).to_be_disabled()
    assert not stub.accepted
    page.keyboard.press("Escape")

    cols.locator(".pboard-col.doing .pcard", has_text="Checkout").click()
    sheet = page.locator(".sheet.pboard-sheet")
    sheet.get_by_role("button", name="Stop this task" if lang == "en" else "Остановить задачу").click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    expect(page.locator(".toast")).to_contain_text("Stop requested" if lang == "en" else "Остановка запрошена")
    assert stub.stops and stub.stops[-1][0] == "t-checkout" and stub.stops[-1][1]["client_operation_id"], stub.stops
    expect(cols.locator(".pboard-col.doing .pcard", has_text="Checkout")).to_be_visible()
    owned = sheet.locator("details.sheet-section", has_text="Stop work owned by this task" if lang == "en" else "Остановить связанную работу задачи")
    owned.locator("summary").first.click()
    expect(owned).to_contain_text("Owned run" if lang == "en" else "Связанный запуск")
    owned.locator("textarea").fill("Operator requested a clean stop")
    owned.get_by_role("button", name="Request stop" if lang == "en" else "Запросить остановку").click()
    page.locator(".sheet-backdrop.confirm .dialog button").last.click()
    expect(owned).to_contain_text("Stopping…" if lang == "en" else "Останавливается…")
    assert stub.cancellations[-1][0] == "task:t-checkout"
    assert stub.cancellations[-1][1]["preview_fingerprint"] == "a" * 64
    page.keyboard.press("Escape")

    # A new task with its brief, an assignee and what it waits for.
    page.get_by_role("button", name=words["new"]).first.click()
    sheet = page.locator(".sheet.pboard-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("#ptask-title").fill("Delivery zones")
    sheet.locator("#ptask-assignee").select_option("st-lev")
    expect(sheet.locator(".sub.attn")).to_contain_text(words["missing"])
    sheet.locator("#ptask-objective").fill("Customers see whether we deliver to them")
    sheet.locator("#ptask-deliverable").fill("A zones page and a check at checkout")
    sheet.locator("#ptask-boundaries").fill("Only the site folder")
    sheet.locator("#ptask-done_when").fill("An address outside the zones is refused politely")
    sheet.locator("details > summary").filter(has_text="Work folder" if lang == "en" else "Рабочая папка").click()
    sheet.locator("#ptask-folder").select_option(project()["folders"][0]["id"])
    expect(sheet.locator(".sub.attn")).to_have_count(0)
    sheet.locator(".pboard-deps").get_by_role("button", name="Checkout").click()
    sheet.get_by_role("radio", name="P2", exact=True).click()
    sheet.locator(".sheet-foot").get_by_role("button", name=words["create"]).click()
    expect(sheet).to_have_count(0)
    made = stub.created[-1]
    assert {key: value for key, value in made.items() if key not in ("expected_collection_revision", "client_operation_id")} == {
        "title": "Delivery zones",
        "brief": {"objective": "Customers see whether we deliver to them", "deliverable": "A zones page and a check at checkout", "boundaries": "Only the site folder", "done_when": "An address outside the zones is refused politely"},
        "assignee_staff_id": "st-lev",
        "folder_id": project()["folders"][0]["id"],
        "depends_on": ["t-checkout"],
        "priority": 2,
    }, made
    assert made["expected_collection_revision"] == 5 and made["client_operation_id"], made
    assert stub.launched[-1][1]["staff_id"] == "st-lev" and stub.launched[-1][1]["resume_from"] is None
    expect(page.locator(".toast")).to_contain_text(words["queued"])
    expect(cols.locator(".pboard-col.queue .pcard", has_text="Delivery zones")).to_be_visible()

    # The delivery receipt can fail definitively or remain unknown; neither completes the task.
    launched_id = stub.launched[-1][0]
    effect_id = f"launch-{launched_id}"
    states = (
        ({"state": "failed", "error": "Provider authentication is required"}, "Couldn't start" if lang == "en" else "Не удалось запустить"),
        ({"state": "unknown", "error": "Delivery connection lost"}, "Couldn't confirm it started" if lang == "en" else "Запуск не подтверждён"),
        ({"state": "pending", "wait_reason": "machine", "wait_detail": "Host unavailable"}, "Waiting to start" if lang == "en" else "Ждёт запуска"),
        ({"state": "claimed"}, "Starting…" if lang == "en" else "Запускается…"),
        ({"state": "completed"}, "Started" if lang == "en" else "Запущено"),
    )
    for observed, label in states:
        stub.launch_effects[effect_id] = observed
        cols.locator(".pcard", has_text="Delivery zones").click()
        warning = sheet.locator(".result-warning[role='status']")
        expect(warning).to_contain_text(label)
        if observed.get("error") or observed.get("wait_detail"):
            expect(warning).to_contain_text(observed.get("error") or observed["wait_detail"])
        if observed["state"] == "failed":
            expect(warning).not_to_contain_text("Couldn't confirm" if lang == "en" else "не подтверждён")
        fits(page, f"{lang} delivery {observed['state']}")
        page.keyboard.press("Escape")
        expect(sheet).to_have_count(0)
    stub.launch_effects.pop(effect_id)

    # Open a task: the address names it, and an edit sends only what changed.
    cols.locator(".pcard", has_text="Menu photo captions").click()
    expect(sheet).to_be_visible()
    assert "task=t-photos" in page.url, page.url
    sheet.locator("#ptask-assignee").select_option("st-max")
    sheet.locator(".sheet-foot").get_by_role("button", name=words["save"]).click()
    expect(sheet).to_have_count(0)
    edited_id, edited = stub.updated[-1]
    assert edited_id == "t-photos" and edited["assignee_staff_id"] == "st-max" and edited["expected_entity_revision"] == 1 and edited["client_operation_id"], stub.updated[-1]
    assert stub.launched[-1][0] == "t-photos" and stub.launched[-1][1]["staff_id"] == "st-max"
    expect(page.locator(".toast")).to_contain_text(words["started"])

    # The task command is durable even if the distinct launch command is refused. A later Start
    # uses the saved card's current revision rather than creating a duplicate task.
    stub.launch_failures = 1
    page.get_by_role("button", name=words["new"]).first.click()
    sheet.locator("#ptask-title").fill("Retry launch")
    sheet.locator("#ptask-assignee").select_option("st-lev")
    sheet.locator(".sheet-foot").get_by_role("button", name=words["create"]).click()
    expect(sheet).to_have_count(0)
    assert sum(task["title"] == "Retry launch" for task in stub.tasks) == 1
    expect(page.locator(".toast")).to_contain_text("Task saved, but couldn't confirm it started" if lang == "en" else "Задача сохранена, но запуск не подтверждён")
    cols.locator(".pcard", has_text="Retry launch").click()
    sheet.get_by_role("button", name="Start task" if lang == "en" else "Запустить задачу", exact=True).click()
    expect(page.locator(".toast")).to_contain_text(words["queued"])
    assert stub.launched[-1][0] == next(task["id"] for task in stub.tasks if task["title"] == "Retry launch")
    page.keyboard.press("Escape")

    stub.launch_unknowns = 1
    page.get_by_role("button", name=words["new"]).first.click()
    sheet.locator("#ptask-title").fill("Unknown launch")
    sheet.locator("#ptask-assignee").select_option("st-lev")
    sheet.locator(".sheet-foot").get_by_role("button", name=words["create"]).click()
    expect(sheet).to_have_count(0)
    unknown_id, unknown_body = stub.unknown_launches[-1]
    cols.locator(".pcard", has_text="Unknown launch").click()
    sheet.get_by_role("button", name="Try starting again" if lang == "en" else "Запустить ещё раз").click()
    assert stub.launched[-1][0] == unknown_id
    assert stub.launched[-1][1]["client_operation_id"] == unknown_body["client_operation_id"]
    assert stub.launched[-1][1]["expected_entity_revision"] == unknown_body["expected_entity_revision"]
    expect(sheet.locator(".result-warning", has_text="Waiting to start" if lang == "en" else "Ждёт запуска")).to_be_visible()
    expect(sheet.get_by_role("button", name="Start task" if lang == "en" else "Запустить задачу", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    fits(page, f"{lang} desktop")

    # The request is answered where it was asked: the staff member's session, inside the project.
    need.get_by_role("button", name=words["answer"]).click()
    page.wait_for_url(f"**/app/orchestration/project/{PID}/s/sess-ira**")


def phone(page: Page, lang: str, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    stub = board()
    serve(page, stub, unhandled)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    expect(page.get_by_role("heading", name=words["title"])).to_be_visible()
    expect(page.locator(".pboard-cols")).to_have_count(0)
    chips = page.locator(".pboard-chips .chip")
    expect(chips).to_have_count(5)
    expect(chips.nth(0)).to_contain_text(f"{words['needs']} · 1")
    expect(chips.nth(4)).to_contain_text(f"{words['done']} · 1")
    sections = page.locator(".pboard-list > .pboard-section")
    # Without a chip: every open column that has something, finished work folded.
    expect(sections).to_have_count(4)
    fits(page, f"{lang} phone list")

    chips.nth(0).click()
    expect(chips.nth(0)).to_have_attribute("aria-pressed", "true")
    expect(sections).to_have_count(1)
    expect(sections.first.locator(".pcard.need")).to_contain_text("SPRING10")
    chips.nth(0).click()
    expect(sections).to_have_count(4)

    page.locator(".pboard-chips .chip", has_text=words["review"]).click()
    expect(sections).to_have_count(1)
    card = sections.first.locator(".pcard", has_text="Notify endpoint")
    card.click()
    sheet = page.locator(".sheet.pboard-sheet")
    expect(sheet.locator(".result-flow button", has_text="Accept" if lang == "en" else "Принять")).to_be_disabled()
    assert not stub.accepted
    page.keyboard.press("Escape")

    page.locator(".pboard-chips .chip", has_text=words["done"]).click()
    expect(page.locator(".pboard-list .pcard", has_text="Old price list")).to_be_visible()
    expect(page.locator(".pboard-list .pcard", has_text="Notify endpoint")).to_have_count(0)

    # The task sheet on a phone: every brief field reachable, nothing sideways.
    page.locator(".pboard-list .pcard", has_text="Old price list").click()
    sheet = page.locator(".sheet.pboard-sheet")
    expect(sheet).to_be_visible()
    for field in ("objective", "deliverable", "boundaries", "done_when"):
        expect(sheet.locator(f"#ptask-{field}")).to_be_attached()
    fits(page, f"{lang} phone sheet")


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            desktop(context.new_page(), lang, unhandled)
            context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            phone(context.new_page(), lang, unhandled)
            context.close()
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
