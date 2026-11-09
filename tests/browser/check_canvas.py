"""The canvas page and board in a browser, on a desktop and on a phone.

What it holds the app to: the list is split by the chat and its project, never one global pile; an open
canvas is a focus mode; a card is made by a double click and typed in place; two cards are connected by
dragging from a side; cards move, resize, group, delete and come back with Undo, each saved as single-item
operations on the version the page holds; an agent's edit to another card is merged without a word,
while an edit to the same card raises a banner that names it and lets the operator keep theirs; the
shared view draws the board read only; and a phone gets the same board with a toolbar it can tap,
inside the window. Live pages run on another origin in a sandbox without allow-same-origin, a script
from a site nobody allowed never leaves the browser, pointing and the bar send what the preview showed,
and a shared view shows only an inert still copy. With SHOTS set, the pictures go there.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402
from canvas_stub import BLOCKED, CanvasStub, Door, card, edge, frame_node  # noqa: E402

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


def until(page: Page, predicate, what: str, seconds: float = 10) -> None:  # type: ignore[no-untyped-def]
    """Waits by the page's clock, not the thread's: the stub answers the app's requests only while
    Playwright runs, and a plain sleep would starve the very request being waited for."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for {what}")


def frames(browser, lang: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Live pages: on another origin, in a sandbox without allow-same-origin, with no app-origin fallback;
    pointing at an element and a card, the composed message as the host composes it, a page that leaves
    its folder stopped, and a shared view that shows only an inert still copy."""
    ru = lang == "ru"
    stub = CanvasStub(shots.S1)
    app_origin = "/".join(BASE.split("/")[:3])
    door = Door(stub, app_origin)
    stub.door_port = door.port
    site = stub.add("Витрина" if ru else "Shop pages", {"nodes": [frame_node("web", 0, 0), frame_node("nodoor", 1400, 0, "phone"), card("note", 0, 900, "Герой крупнее" if ru else "Hero needs work", width=360)], "edges": []}, shared=True)
    stub.no_door.add("nodoor")
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    cdn_hits: list[str] = []

    def cdn(request_route) -> None:  # type: ignore[no-untyped-def]
        cdn_hits.append(request_route.request.url)
        request_route.fulfill(status=200, content_type="text/javascript", body="document.getElementById('cdn').textContent = 'cdn loaded';")

    context.route(f"{BLOCKED}/**", cdn)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    route(page, stub)
    page.goto(f"{BASE}/canvas/{site}?session={shots.S1}&token=t&lang={lang}&scheme={SCHEME}")
    web = page.locator(".cv-node[data-node='web']")
    iframe = web.locator("iframe.cv-frame-page")
    expect(iframe).to_be_visible(timeout=15000)
    sandbox = iframe.get_attribute("sandbox") or ""
    assert "allow-scripts" in sandbox and "allow-same-origin" not in sandbox and "allow-popups" not in sandbox and "allow-top-navigation" not in sandbox, sandbox
    src = iframe.get_attribute("src") or ""
    assert src.startswith(f"http://127.0.0.1:{door.port}/f/") and not src.startswith(app_origin), src
    expect(web.locator(".cv-frame-head")).to_contain_text("index.html")
    inner = page.frame_locator(".cv-node[data-node='web'] iframe.cv-frame-page")
    expect(inner.locator("#hero")).to_have_text("Autumn collection")
    until(page, lambda: any(report.get("loaded") for report in stub.reports), "the frame's load relayed to the host")
    # The page's script from a CDN nobody allowed: refused by the browser before any request left.
    until(page, lambda: any(BLOCKED in report.get("blocked", []) for report in stub.reports), "the blocked origin relayed")
    expect(inner.locator("#cdn")).to_have_text("waiting for the CDN")
    assert not cdn_hits, cdn_hits
    # No services address: a card that says so, and no frame of any kind.
    nodoor = page.locator(".cv-node[data-node='nodoor']")
    expect(nodoor.locator(".cv-frame-notice")).to_contain_text("адрес сервисов" if ru else "services address")
    expect(nodoor.locator("iframe")).to_have_count(0)
    until(page, lambda: ("web" in [key[1] for key in stub.snapshots]), "a still copy for the shared view", 15)

    # Comment mode: point at an element in the page and at a card.
    page.get_by_role("button", name="Комментарий" if ru else "Comment").click()
    # Dispatched rather than clicked at coordinates: the frame is drawn scaled with the board, and
    # Playwright's hit point inside a scaled cross-origin frame lands off target.
    inner.locator("#hero").dispatch_event("click")
    note = page.locator(".cv-pick-note")
    expect(note).to_be_visible()
    expect(note).to_contain_text("#hero")
    note.locator("textarea").fill("Крупнее" if ru else "Make it larger")
    note.get_by_role("button", name="Добавить" if ru else "Add").click()
    chip = page.locator(".cv-chip", has_text="index.html · #hero")
    expect(chip).to_be_visible()
    page.locator(".cv-node[data-node='note']").click()
    expect(page.locator(".cv-chip", has_text="Герой крупнее" if ru else "Hero needs work")).to_be_visible()
    # Hovering the chip outlines the element in its page.
    chip.hover()
    page.wait_for_timeout(400)
    outlined = None
    for frame in page.frames:
        if frame.url.startswith(f"http://127.0.0.1:{door.port}/f/"):
            outlined = frame.evaluate("[...document.documentElement.children].some((node) => node.style && node.style.zIndex === '2147483647' && node.style.display === 'block')")
    assert outlined, "the chip's element is not outlined in its page"
    bar = page.locator(".cv-comment textarea")
    bar.fill("Пожалуйста, исправьте" if ru else "Please fix this")
    preview = page.locator(".cv-compose-preview").inner_text()
    shot(page, f"canvas-frames-{lang}-{SCHEME}")
    bar.press("Enter")
    until(page, lambda: len(stub.messages) == 1, "the bar's message")
    sent = stub.messages[0]
    assert sent["picks"][0]["selector"] == "#hero" and sent["picks"][0]["note"] == ("Крупнее" if ru else "Make it larger"), sent
    assert "On card note" in sent["text"], sent["text"]
    assert sent["composed"] == preview, (sent["composed"], preview)
    expect(page.locator(".cv-sent")).to_be_visible()
    expect(page.locator(".cv-chip")).to_have_count(0)

    # A page that goes elsewhere is stopped and its address retired.
    page.get_by_role("button", name="Работа" if ru else "Interact").click()
    inner.locator("#leave").dispatch_event("click")
    expect(web.locator(".cv-frame-notice")).to_contain_text("ушла из рамки" if ru else "left its frame", timeout=10000)
    until(page, lambda: "web" in stub.revoked, "the address retired")

    # The shared view: an inert still copy, never a live address.
    token = stub.canvases[site]["share_token"]
    page.goto(f"{BASE}/d/{token}?lang={lang}&scheme={SCHEME}")
    still = page.locator(".cv-shared .cv-node[data-node='web'] iframe")
    expect(still).to_have_attribute("sandbox", "")
    assert "src" not in (still.evaluate("(node) => node.getAttributeNames()")), "a shared still copy has no address"
    expect(page.frame_locator(".cv-shared .cv-node[data-node='web'] iframe").locator("#hero")).to_have_text("Autumn collection")
    expect(page.locator(".cv-shared .cv-node[data-node='nodoor'] .cv-frame-notice")).to_be_visible()
    for frame in page.locator("iframe").all():
        assert str(door.port) not in (frame.get_attribute("src") or ""), "a live page in a shared view"
    shot(page, f"canvas-frames-shared-{lang}-{SCHEME}")

    # A phone: the frames fit in a smaller window, the bar stays at the bottom.
    phone_page = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()
    route(phone_page, stub)
    phone_page.goto(f"{BASE}/canvas/{site}?session={shots.S1}&token=t&lang={lang}&scheme={SCHEME}")
    expect(phone_page.locator(".cv-comment textarea")).to_be_visible()
    assert phone_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the frames page scrolls sideways"
    phone_page.wait_for_timeout(1500)
    shot(phone_page, f"canvas-frames-phone-{lang}-{SCHEME}")
    phone_page.context.close()
    context.close()
    door.close()


def run() -> int:
    expect_app(BASE)
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    langs = os.environ.get("LANGS", "en,ru").split(",")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in langs:
            if os.environ.get("ONLY") != "frames":
                desktop(browser, lang, errors)
                phone(browser, lang, errors)
            frames(browser, lang, errors)
        browser.close()
    assert not errors, errors
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(run())
