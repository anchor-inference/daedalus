"""A project task's dialog, opened from the board, read the way the operator reads it.

The task here is the kind the operator opens every day: a long four-part brief, four requirements,
two tasks it waits for, a CLI member assigned and a page of notes. What is checked is that the dialog
is a view to track the task in rather than a form to fight: wide on a desktop, with the brief as text
and the properties beside it; nothing folded inside something else that is folded; no heading over
nothing; no brief text cut off, whether read or edited; the rare tools behind the one overflow menu;
and Save and Cancel present only while there is something to save. On a phone it is one column with
the same order, and nothing scrolls sideways.

With ``SHOTS=<dir>`` the dialog is photographed (desktop in the dark and the light theme, and a
phone) into that directory, ``SHOTS_PREFIX`` naming the set; ``SHOTS_ONLY=1`` takes the pictures
without asserting, which is how the build before a change is photographed for comparison.
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
SHOTS = os.environ.get("SHOTS", "")
SHOTS_PREFIX = os.environ.get("SHOTS_PREFIX", "after")
SHOTS_ONLY = os.environ.get("SHOTS_ONLY") == "1"
PID = "5d1e9a7c3b20"

OBJECTIVE = (
    "Customers choosing a spring dish should see every new item with a photo, a short description, the "
    "allergens and the price, in both languages, before the spring menu goes live on the first of March. "
    "Today the catalogue still lists the winter items and half of the descriptions were written for the "
    "printed menu, so they are too long for the phone layout and repeat the dish's name."
)
DELIVERABLE = (
    "A catalogue file with thirty-two spring items, each with an English and a Russian description of at "
    "most two sentences, an allergen list taken from the kitchen's sheet, and a price that matches the "
    "till. A short note listing anything the kitchen still has to confirm."
)
BOUNDARIES = (
    "Only the catalogue folder. Do not change prices on your own: a price that differs from the till is "
    "listed in the note instead. Leave the winter items in place until the launch day."
)
DONE_WHEN = (
    "Every spring item renders on a 390 px phone without a cut line, the allergen list of each matches the "
    "kitchen's sheet, and the price check script reports no difference against the till export."
)
NOTES = "\n".join([
    "2026-10-28 operator: the kitchen's sheet is in the shared folder, use the October version.",
    "2026-10-29 Ira: two dishes have no photo yet; I used the placeholder and listed them in the note.",
    "2026-10-30 operator: the Russian descriptions may be a little longer than the English ones.",
    "2026-10-31 Ira: the price check found three differences, all on drinks; listed, not changed.",
])

LABELS = {
    "en": {"rare": ["Stop work owned by this task", "Compare two approaches", "Continue with another worker", "Continue a CLI conversation", "Workflow", "Task context"],
           "actions": "Task actions", "edit": "Edit", "save": "Save", "cancel": "Cancel", "objective": "Objective"},
    "ru": {"rare": ["Остановить связанную работу задачи", "Сравнить два подхода", "Продолжить с другим исполнителем", "Продолжить разговор CLI", "Маршрут работы", "Контекст задачи"],
           "actions": "Действия с задачей", "edit": "Изменить", "save": "Сохранить", "cancel": "Отмена", "objective": "Цель"},
}


def project() -> dict:
    listed = folders("/home/operator/work/bakery")
    listed[0]["is_git"] = True
    return {"id": PID, "name": "Bakery", "folders": listed, "created_at": "2026-09-20T00:00:00Z",
            "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}


def requirement(number: int, text: str, kind: str, source: str, *, operator: bool = False, deliveries: list[dict] | None = None) -> dict:
    return {"id": f"rq-{number}", "label": f"R{number}", "number": number, "text": text, "kind": kind, "source": source,
            "from_operator": operator, "state": "active", "replaces": None, "file_id": None, "file_name": "", "evidence": None,
            "mark": None, "created_at": "2026-10-28T09:00:00Z", "deliveries": deliveries or []}


def board() -> BoardStub:
    staff = [
        {"id": "st-ira", "name": "Ira", "color": "orange", "harness": "claude", "isolation": "shared"},
        {"id": "st-lev", "name": "Lev", "color": "teal", "harness": "daedalus"},
    ]
    ira = BoardStub.assignee("st-ira", "Ira", harness="claude", color="orange")
    delivered = [{"staff_id": "st-ira", "staff_name": "Ira", "via": "brief", "delivered_at": "2026-10-28T09:05:00Z",
                  "acknowledged_at": "2026-10-28T09:06:00Z", "opened_at": None, "cli": True}]
    requirements = [
        requirement(1, "Every description fits two lines on a 390 px phone in both languages.", "quality", "operator", operator=True, deliveries=delivered),
        requirement(2, "Allergens come from the kitchen's October sheet, never from the old printed menu.", "input", "operator", operator=True, deliveries=delivered),
        requirement(3, "Prices are never changed; a difference from the till goes into the note.", "constraint", "orchestrator"),
        requirement(4, "Only the catalogue folder is touched.", "scope", "orchestrator"),
    ]
    tasks = [
        BoardStub.task("t-catalog", "Prepare the menu catalogue for the spring launch", status="todo", priority=2, assignee=ira,
                       brief={"objective": OBJECTIVE, "deliverable": DELIVERABLE, "boundaries": BOUNDARIES, "done_when": DONE_WHEN},
                       requirements=requirements, depends_on=["t-photos", "t-prices"], notes=NOTES, folder_id=None,
                       updated_at="2026-11-01T18:20:00Z", project_id=PID),
        BoardStub.task("t-photos", "Photograph the new spring dishes", status="doing", priority=1,
                       assignee=BoardStub.assignee("st-lev", "Lev", color="teal", status="working", on_task=True)),
        BoardStub.task("t-prices", "Export the till's price list", status="todo", priority=3),
    ]
    return BoardStub(project(), staff=staff, tasks=tasks)


def serve(page: Page, stub: BoardStub, unhandled: Unhandled) -> None:
    team = TeamStub(project())

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        if request.method == "GET" and path == "/api/staff/st-ira/resume-sessions":
            sessions = [{"id": "ss-old", "started_at": "2026-10-30T10:00:00Z", "owner_name": "Ira", "task_title": "Spring menu draft", "can_resume": True, "resume_reason": ""}]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(sessions))
        if request.method == "GET" and path == "/api/board/t-catalog/comparisons":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"task_id": "t-catalog", "groups": [], "next_before": None}))
        answered = stub.answer(request.method, path, url.query, body) or team.answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if path == "/api/projects":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps([project()]))
        if request.method == "GET" and path == f"/api/projects/{PID}/next-actions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"project_id": PID, "actions": []}))
        if path == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"presets": {}, "model": {}}))
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        route.fulfill(status=200, content_type="application/json", body="[]")

    page.route("**/api/**", handle)


def shoot(page: Page, name: str) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(Path(SHOTS) / f"{SHOTS_PREFIX}-{name}.png"))


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


# Every heading in the dialog with nothing under it, and every folded block inside another folded
# block. A heading is a section's title or a heading element; "nothing" is no visible text and no
# control after the heading in its section.
STRUCTURE = """(root) => {
  const nested = root.querySelectorAll('details details').length;
  const empty = [];
  for (const head of root.querySelectorAll('h4, .section-title, .ptask-label, legend')) {
    const box = head.closest('section, fieldset, .ptask-prop, .ptask-block');
    if (!box || head.offsetParent === null) continue;
    const rest = Array.from(box.children).filter((el) => el !== head && !el.contains(head));
    const filled = rest.some((el) => el.offsetParent !== null && ((el.textContent || '').trim() || el.querySelector('button, input, select, textarea')));
    if (!filled) empty.push((head.textContent || '').trim());
  }
  return { nested, empty, folds: root.querySelectorAll('.pboard-fold.section-title').length };
}"""

# A field whose text does not fit the box it is drawn in: taller or wider than its box while the box
# hides the rest, or clamped to a number of lines.
CLIPPED = """(nodes) => nodes.filter((el) => {
  const style = getComputedStyle(el);
  const hides = (v) => v === 'hidden' || v === 'clip';
  const tall = el.scrollHeight - el.clientHeight > 1 && (el.tagName === 'TEXTAREA' || hides(style.overflowY));
  const wide = el.scrollWidth - el.clientWidth > 1 && (el.tagName === 'TEXTAREA' || hides(style.overflowX));
  const clamped = style.webkitLineClamp && style.webkitLineClamp !== 'none';
  return tall || wide || clamped || style.textOverflow === 'ellipsis';
}).map((el) => el.id || el.className)"""


def desktop(page: Page, lang: str, theme: str, unhandled: Unhandled) -> None:
    words = LABELS[lang]
    stub = board()
    serve(page, stub, unhandled)
    page.goto(f"{BASE}/project/{PID}/board?task=t-catalog&token=t&lang={lang}")
    sheet = page.locator(".sheet.pboard-sheet")
    expect(sheet).to_be_visible()
    expect(sheet).to_contain_text("spring launch")
    shoot(page, f"desktop-{theme}-{lang}")
    if SHOTS_ONLY:
        return

    # Wide, in two columns: the brief on the left and the properties beside it.
    width = sheet.evaluate("(el) => el.getBoundingClientRect().width")
    assert width >= 900, f"{lang}: the task dialog is only {width}px wide on a 1440px window"
    main, side = sheet.locator(".ptask-main"), sheet.locator(".ptask-side")
    expect(main).to_be_visible()
    expect(side).to_be_visible()
    main_box, side_box = main.bounding_box(), side.bounding_box()
    assert main_box and side_box and side_box["x"] >= main_box["x"] + main_box["width"] - 1, (main_box, side_box)

    # Where the task stands is at the top: its column, who has it and what it waits for.
    state = sheet.locator(".ptask-state")
    expect(state).to_contain_text("Ira")
    expect(state).to_contain_text("Photograph the new spring dishes")

    # Nothing folded in a fold, no heading over nothing, none of the old fold-buttons.
    structure = sheet.evaluate(STRUCTURE)
    assert structure["nested"] == 0, f"{lang}: {structure['nested']} folded blocks inside folded blocks"
    assert not structure["empty"], f"{lang}: headings with nothing under them: {structure['empty']}"
    assert structure["folds"] == 0, f"{lang}: {structure['folds']} section headings that are fold buttons"
    assert sheet.locator("details").count() == 0, f"{lang}: the task view opens on folded blocks"

    # The brief is text, in full: four fields, none of them cut.
    fields = sheet.locator(".ptask-brief-text")
    expect(fields).to_have_count(4)
    clipped = fields.evaluate_all(CLIPPED)
    assert not clipped, f"{lang}: brief text cut off: {clipped}"
    expect(sheet.locator(".ptask-brief-text").first).to_contain_text("printed menu, so they are too long")
    expect(sheet.locator("[data-requirement]")).to_have_count(4)

    # Save and Cancel only while something is to be saved.
    foot = sheet.locator(".sheet-foot")
    expect(foot).to_have_count(0)
    objective = sheet.locator(".ptask-brief[data-field='objective']")
    objective.get_by_role("button", name=words["edit"]).click()
    area = sheet.locator("#ptask-objective")
    expect(area).to_be_focused()
    clipped = area.evaluate_all(CLIPPED)
    assert not clipped, f"{lang}: the objective's editor cuts its text: {clipped}"
    expect(foot).to_have_count(0)
    area.fill(OBJECTIVE + " The photos arrive on the twentieth.")
    expect(foot.get_by_role("button", name=words["save"])).to_be_visible()
    shoot(page, f"desktop-{theme}-{lang}-editing")
    foot.get_by_role("button", name=words["cancel"]).click()
    expect(foot).to_have_count(0)
    expect(sheet).to_be_visible()
    expect(sheet.locator("#ptask-objective")).to_have_count(0)
    expect(sheet.locator(".ptask-brief-text").first).not_to_contain_text("twentieth")
    objective.get_by_role("button", name=words["edit"]).click()
    sheet.locator("#ptask-objective").fill("Spring items, complete and correct, before the first of March.")
    foot.get_by_role("button", name=words["save"]).click()
    edited_id, edited = stub.updated[-1]
    assert edited_id == "t-catalog" and edited["brief"] == {"objective": "Spring items, complete and correct, before the first of March."}, stub.updated[-1]
    assert set(edited) == {"brief", "expected_entity_revision", "client_operation_id"}, edited

    # The rare tools wait behind the overflow menu, and each opens in the dialog, not in a fold.
    page.goto(f"{BASE}/project/{PID}/board?task=t-catalog&token=t&lang={lang}")
    expect(sheet).to_be_visible()
    sheet.get_by_role("button", name=words["actions"]).click()
    menu = page.locator(".menu[role='menu']")
    for label in words["rare"]:
        expect(menu.get_by_role("menuitem", name=label, exact=True)).to_be_visible()
    shoot(page, f"desktop-{theme}-{lang}-menu")
    for label in words["rare"]:
        if not menu.is_visible():
            sheet.get_by_role("button", name=words["actions"]).click()
        menu.get_by_role("menuitem", name=label, exact=True).click()
        if label in (words["rare"][3],):
            # A CLI conversation is chosen beside Start, where it is used.
            expect(sheet.locator("#ptask-resume")).to_be_visible()
            continue
        tool = sheet.locator(".ptask-tool")
        expect(tool).to_be_visible()
        expect(tool.locator(".ptask-tool-head")).to_contain_text(label)
        nested = sheet.evaluate("(root) => root.querySelectorAll('details details').length")
        assert nested == 0, f"{lang}: {label} opens with folded blocks inside folded blocks"
        tool.locator(".ptask-tool-head button").first.click()
        expect(tool).to_have_count(0)
    fits(page, f"{lang} desktop {theme}")


def phone(page: Page, lang: str, unhandled: Unhandled) -> None:
    stub = board()
    serve(page, stub, unhandled)
    page.goto(f"{BASE}/project/{PID}/board?task=t-catalog&token=t&lang={lang}")
    sheet = page.locator(".ph-taskpage")
    expect(sheet).to_be_visible()
    shoot(page, f"phone-{lang}")
    if SHOTS_ONLY:
        return
    fits(page, f"{lang} phone")
    # One column, in the desktop's order: where it stands, the brief, then the properties.
    order = sheet.evaluate("""(root) => ['.ptask-state', '.ptask-brief', '.ptask-props'].map((s) => {
      const el = root.querySelector(s); return el ? el.getBoundingClientRect().top : null; })""")
    assert None not in order and order == sorted(order), f"{lang}: phone order is {order}"
    lefts = sheet.evaluate("(root) => ['.ptask-brief', '.ptask-props'].map((s) => Math.round(root.querySelector(s).getBoundingClientRect().left))")
    assert abs(lefts[0] - lefts[1]) <= 16, f"{lang}: the phone task page is not one column: {lefts}"
    structure = sheet.evaluate(STRUCTURE)
    assert structure["nested"] == 0 and not structure["empty"] and structure["folds"] == 0, f"{lang}: {structure}"
    clipped = sheet.locator(".ptask-brief-text").evaluate_all(CLIPPED)
    assert not clipped, f"{lang}: brief text cut off on a phone: {clipped}"
    expect(sheet.locator(".sheet-foot")).to_have_count(0)


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for theme in ("dark", "light"):
                if theme == "light" and lang == "ru" and not SHOTS:
                    continue
                context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=theme)
                desktop(context.new_page(), lang, theme, unhandled)
                context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
            phone(context.new_page(), lang, unhandled)
            context.close()
        browser.close()
    print("check_task_dialog: ok")
    return 0 if SHOTS_ONLY else unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
