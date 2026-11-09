"""The canvas page and board in a browser, on a desktop and on a phone.

What it holds the app to: the list is split by the chat and its project, never one global pile; an open
canvas is a focus mode; a card is made by a double click and typed in place; two cards are connected by
dragging from a side; cards move, resize, group, delete and come back with Undo, each saved as single-item
operations on the version the page holds; an agent's edit to another card is merged without a word,
while an edit to the same card raises a banner that names it and lets the operator keep theirs; the
shared view draws the board read only; and a phone gets the same board with a toolbar it can tap,
inside the window. With SHOTS set, the pictures go there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402
from canvas_stub import CanvasStub, card, edge  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS")
SCHEME = os.environ.get("SCHEME", "dark")


def seed(stub: CanvasStub, lang: str) -> dict[str, str]:
    ru = lang == "ru"
    nodes = [
        card("ask", 0, 0, "Запрос" if ru else "Request", "4"),
        card("plan", 320, 0, "## План\nразбить на шаги" if ru else "## Plan\nsplit into steps", "5"),
        card("ok", 640, 0, "Готово?" if ru else "Ready?", "3"),
        card("ship", 960, -120, "Выпуск" if ru else "Ship", "4"),
        card("fix", 960, 120, "Исправить" if ru else "Fix", "1"),
    ]
    edges = [edge("e1", "ask", "right", "plan", "left"), edge("e2", "plan", "right", "ok", "left"), edge("e3", "ok", "right", "ship", "left", "да" if ru else "yes"), edge("e4", "ok", "right", "fix", "left", "нет" if ru else "no")]
    flow_doc = {"nodes": nodes, "edges": edges}
    first = {"nodes": nodes[:2], "edges": edges[:1]}
    flow = stub.add("Процесс выпуска" if ru else "Release flow", flow_doc, history=[({"nodes": [], "edges": []}, "agent", 60 * 26), (first, "agent", 90), (flow_doc, "operator", 20)])
    arch = stub.add("Архитектура" if ru else "Architecture", {"nodes": [{"id": "g", "type": "group", "label": "Host", "x": -40, "y": -60, "width": 620, "height": 200}, card("app", 0, 0, "App", "5"), card("api", 300, 0, "API", "6"), card("db", 700, 0, "SQLite")], "edges": [edge("a1", "app", "right", "api", "left"), edge("a2", "api", "right", "db", "left")]}, scope="project", source="agent", minutes_ago=60 * 3, shared=True)
    return {"flow": flow, "arch": arch}


def route(page: Page, stub: CanvasStub) -> None:
    def answer(request_route) -> None:  # type: ignore[no-untyped-def]
        if stub.handle(request_route):
            return
        shots.stub(request_route)

    page.route("**/api/**", answer)


def shot(page: Page, name: str) -> None:
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/{name}.png", animations="disabled")


def node(page: Page, node_id: str):  # type: ignore[no-untyped-def]
    return page.locator(f".cv-board [data-node='{node_id}']")


def ops_calls(stub: CanvasStub) -> list[dict]:
    return [call[2] for call in stub.calls if call[0] == "POST" and call[1].endswith("/ops")]


def wait_saved(page: Page) -> None:
    expect(page.locator(".cv-status.saved")).to_be_visible(timeout=10000)


def fits(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the page scrolls sideways"
    width = page.evaluate("window.innerWidth")
    for selector in (".cv-bar", ".cv-toolbar", ".cv-zoom"):
        box = page.locator(selector).first.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, f"{selector} sticks out: {box}"


def drag(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=10)
    page.mouse.up()


def centre(page: Page, selector_or_locator) -> tuple[float, float]:  # type: ignore[no-untyped-def]
    locator = page.locator(selector_or_locator) if isinstance(selector_or_locator, str) else selector_or_locator
    box = locator.bounding_box()
    assert box is not None
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def desktop(browser, lang: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    ru = lang == "ru"
    stub = CanvasStub(shots.S1)
    ids = seed(stub, lang)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE.split("/app")[0])
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    route(page, stub)

    # The list from a chat: this chat's canvases and its project's, each section with its own New.
    page.goto(f"{BASE}/canvas?session={shots.S1}&token=t&lang={lang}&scheme={SCHEME}")
    sections = page.locator(".cv-section")
    expect(sections).to_have_count(2)
    expect(sections.nth(0)).to_contain_text("Этот чат" if ru else "This chat")
    expect(sections.nth(1)).to_contain_text("Проект Website" if ru else "Project Website")
    expect(sections.nth(0).locator(".cv-card")).to_have_count(1)
    expect(sections.nth(1).locator(".cv-card")).to_have_count(1)
    expect(page.locator(".cv-card .cv-thumb img").first).to_be_visible()
    shot(page, f"canvas-list-{lang}-{SCHEME}")

    # From the navigation: grouped by where each canvas belongs.
    page.goto(f"{BASE}/canvas?token=t&lang={lang}&scheme={SCHEME}")
    expect(page.locator(".cv-section")).to_have_count(2)

    page.goto(f"{BASE}/canvas/{ids['flow']}?session={shots.S1}&token=t&lang={lang}&scheme={SCHEME}")
    board = page.locator(".cv-board")
    expect(board).to_be_visible()
    expect(page.locator(".app.focus-mode")).to_have_count(1)
    expect(node(page, "plan").get_by_role("heading")).to_have_text("План" if ru else "Plan")
    # Opened fitted: every card inside the board.
    frame = board.bounding_box()
    assert frame is not None
    for node_id in ("ask", "plan", "ok", "ship", "fix"):
        box = node(page, node_id).bounding_box()
        assert box is not None and box["x"] >= frame["x"] and box["x"] + box["width"] <= frame["x"] + frame["width"] + 1, (node_id, box, frame)
    expect(page.locator(".cv-edge")).to_have_count(4)
    expect(page.locator(".cv-edge-label")).to_have_count(2)
    fits(page)
    shot(page, f"canvas-editor-{lang}-{SCHEME}")

    # A double click on empty board makes a card and types in it; Escape keeps the words.
    empty = (frame["x"] + 200, frame["y"] + frame["height"] - 150)
    page.mouse.dblclick(*empty)
    field = page.locator(".cv-text-field")
    expect(field).to_be_focused()
    field.type("Заметка" if ru else "A note")
    page.keyboard.press("Escape")
    expect(page.locator(".cv-board .cv-text", has_text="Заметка" if ru else "A note")).to_have_count(1)
    wait_saved(page)
    made = ops_calls(stub)[-1]
    assert made["ops"][0]["op"] == "put" and made["ops"][0]["kind"] == "node" and made["base_version"] == 3, made
    new_id = made["ops"][0]["item"]["id"]

    # Connect the new card to "Request" by dragging from its top side onto the card.
    node(page, new_id).hover()
    handle = node(page, new_id).locator(".cv-side.top")
    drag(page, centre(page, handle), centre(page, node(page, "ask")))
    wait_saved(page)
    connected = ops_calls(stub)[-1]["ops"][0]
    assert connected["kind"] == "edge" and connected["item"]["fromNode"] == new_id and connected["item"]["toNode"] == "ask" and connected["item"]["fromSide"] == "top", connected
    expect(page.locator(".cv-edge")).to_have_count(5)

    # Move it, then resize it from its corner: each a single put, saved.
    before = node(page, new_id).bounding_box()
    assert before is not None
    drag(page, (before["x"] + 30, before["y"] + 30), (before["x"] + 130, before["y"] + 10))
    wait_saved(page)
    moved = ops_calls(stub)[-1]["ops"]
    assert len(moved) == 1 and moved[0]["item"]["id"] == new_id, moved
    node(page, new_id).click()
    corner = node(page, new_id).locator(".cv-handle.se")
    drag(page, centre(page, corner), (centre(page, corner)[0] + 80, centre(page, corner)[1] + 40))
    wait_saved(page)
    item = stub.canvases[ids["flow"]]["doc"]["nodes"][-1]
    assert item["id"] == new_id and item["width"] > 260, item

    # Group two cards with shift-click and the toolbar.
    node(page, "ship").click()
    node(page, "fix").click(modifiers=["Shift"])
    page.get_by_role("button", name="Сгруппировать" if ru else "Group").click()
    label = page.locator(".cv-group .cv-inline-field")
    expect(label).to_be_focused()
    label.fill("Итог" if ru else "Outcome")
    page.keyboard.press("Enter")
    wait_saved(page)
    expect(page.locator(".cv-group-label", has_text="Итог" if ru else "Outcome")).to_be_visible()
    group = next(n for n in stub.canvases[ids["flow"]]["doc"]["nodes"] if n["type"] == "group")
    assert stub.canvases[ids["flow"]]["doc"]["nodes"][0]["id"] == group["id"], "a new group goes behind the cards"

    # Delete a card with its edges, and bring it back with Undo.
    node(page, "fix").click()
    page.keyboard.press("Delete")
    expect(node(page, "fix")).to_have_count(0)
    wait_saved(page)
    assert all(e["toNode"] != "fix" for e in stub.canvases[ids["flow"]]["doc"]["edges"])
    page.keyboard.press("Control+z")
    expect(node(page, "fix")).to_have_count(1)
    wait_saved(page)
    assert any(e["toNode"] == "fix" for e in stub.canvases[ids["flow"]]["doc"]["edges"]), "Undo puts the edges back with the card"

    # The agent changes another card: the operator's next move merges, no banner.
    ask = next(n for n in stub.canvases[ids["flow"]]["doc"]["nodes"] if n["id"] == "ask")
    stub.agent_edit(ids["flow"], [{"op": "put", "kind": "node", "item": {**ask, "text": "Запрос клиента" if ru else "Customer request"}}])
    node(page, "ok").click()
    page.keyboard.press("ArrowDown")
    wait_saved(page)
    expect(node(page, "ask")).to_contain_text("Запрос клиента" if ru else "Customer request")
    expect(page.locator(".cv-banner.conflict")).to_have_count(0)

    # The agent changes the same card the operator then edits: a banner names it; Keep mine wins.
    # The agent writes while the operator is typing in that card, which is when it matters most.
    node(page, "ok").dblclick()
    expect(page.locator(".cv-text-field")).to_be_focused()
    ok = next(n for n in stub.canvases[ids["flow"]]["doc"]["nodes"] if n["id"] == "ok")
    stub.agent_edit(ids["flow"], [{"op": "put", "kind": "node", "item": {**ok, "text": "Проверено агентом" if ru else "Checked by the agent"}}])
    page.locator(".cv-text-field").fill("Моя правка" if ru else "My edit")
    page.keyboard.press("Escape")
    banner = page.locator(".cv-banner.conflict")
    expect(banner).to_be_visible(timeout=10000)
    expect(banner).to_contain_text("Моя правка" if ru else "My edit")
    assert "marked" in (node(page, "ok").get_attribute("class") or ""), "the conflicting card is outlined"
    shot(page, f"canvas-conflict-{lang}-{SCHEME}")
    banner.get_by_role("button", name="Оставить мой" if ru else "Keep mine").click()
    expect(banner).to_have_count(0)
    wait_saved(page)
    final = next(n for n in stub.canvases[ids["flow"]]["doc"]["nodes"] if n["id"] == "ok")
    assert final["text"] == ("Моя правка" if ru else "My edit"), final

    # History drawer lists who saved, and the revision preview is a read-only board.
    page.get_by_role("button", name="История" if ru else "History").click()
    drawer = page.locator(".cv-history")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".cv-who.agent").first).to_be_visible()
    drawer.locator(".cv-revision").last.click()
    expect(page.locator(".cv-preview .cv-board.read-only")).to_be_visible()
    shot(page, f"canvas-history-{lang}-{SCHEME}")
    page.locator(".cv-preview").get_by_role("button", name="К текущей" if ru else "Back to current").click()
    page.get_by_role("button", name="История" if ru else "History").click()

    # The shared view: the latest version, read only.
    token = stub.canvases[ids["arch"]]["share_token"]
    page.goto(f"{BASE}/d/{token}?lang={lang}&scheme={SCHEME}")
    expect(page.locator(".cv-shared .cv-board.read-only")).to_be_visible()
    expect(page.locator(".cv-shared .cv-node")).to_have_count(4)
    expect(page.locator(".cv-shared .cv-toolbar")).to_have_count(0)
    shot(page, f"canvas-shared-{lang}-{SCHEME}")
    context.close()


def phone(browser, lang: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    ru = lang == "ru"
    stub = CanvasStub(shots.S1)
    ids = seed(stub, lang)
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    route(page, stub)
    page.goto(f"{BASE}/canvas?session={shots.S1}&token=t&lang={lang}&scheme={SCHEME}")
    expect(page.locator(".cv-section")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the list scrolls sideways"
    shot(page, f"canvas-phone-list-{lang}-{SCHEME}")
    page.goto(f"{BASE}/canvas/{ids['flow']}?session={shots.S1}&token=t&lang={lang}&scheme={SCHEME}")
    expect(page.locator(".cv-board")).to_be_visible()
    fits(page)
    shot(page, f"canvas-phone-editor-{lang}-{SCHEME}")
    # A tap selects; the toolbar then offers what a keyboard would.
    node(page, "plan").tap()
    expect(page.get_by_role("button", name="Удалить" if ru else "Delete")).to_be_visible()
    page.get_by_role("button", name="Добавить карточку" if ru else "Add a card").tap()
    field = page.locator(".cv-text-field")
    expect(field).to_be_visible()
    field.fill("С телефона" if ru else "From the phone")
    page.locator(".cv-bar .cv-title").tap()
    expect(page.locator(".cv-board .cv-text", has_text="С телефона" if ru else "From the phone")).to_have_count(1)
    expect(page.locator(".cv-status.saved")).to_be_visible(timeout=10000)
    fits(page)
    shot(page, f"canvas-phone-card-{lang}-{SCHEME}")
    context.close()


def run() -> int:
    expect_app(BASE)
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    langs = os.environ.get("LANGS", "en,ru").split(",")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in langs:
            desktop(browser, lang, errors)
            phone(browser, lang, errors)
        browser.close()
    assert not errors, errors
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
