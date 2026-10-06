"""The diagrams page and editor in a browser, on a desktop and on a phone.

What it holds the app to: an open diagram is a focus mode (the app's own column goes, and comes back
after Back; a phone loses its tab bar), the editor has one bar and no panel beside the canvas, the bar
fits a phone without spilling, history is a drawer (a sheet on a phone) that restores by posting a
new revision, sharing and export are a popover and a menu, the agent's edit reaches an open canvas
without a save of its own, a stale save becomes a clear choice, the canvas has a theme of its own (light
unless switched in its menu, whatever the app's) and the app's language. With SHOTS set, the pictures
go there.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402
from diagram_stub import DiagramStub  # noqa: E402

from daedalus.stores.diagram_scene import layout, shape  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS")
SCHEME = os.environ.get("SCHEME", "dark")
CANVAS = ".diagram-canvas .excalidraw canvas"


def scene(elements: list[dict]) -> dict:
    return {"elements": elements, "appState": {"viewBackgroundColor": "#ffffff"}, "files": {}}


def seed(stub: DiagramStub, lang: str) -> dict[str, str]:
    ru = lang == "ru"
    nodes = [
        {"id": "ask", "label": "Запрос" if ru else "Request", "shape": "ellipse", "color": "green"},
        {"id": "plan", "label": "План" if ru else "Plan the work", "color": "blue"},
        {"id": "ok", "label": "Готово?" if ru else "Ready?", "shape": "diamond", "color": "yellow"},
        {"id": "ship", "label": "Выпуск" if ru else "Ship", "color": "green"},
        {"id": "fix", "label": "Исправить" if ru else "Fix", "color": "red"},
    ]
    edges = [{"from": "ask", "to": "plan"}, {"from": "plan", "to": "ok"}, {"from": "ok", "to": "ship", "label": "да" if ru else "yes"}, {"from": "ok", "to": "fix", "label": "нет" if ru else "no"}]
    first = scene(layout(nodes[:2], edges[:1]))
    second = scene(layout(nodes, edges))
    third = scene(layout(nodes, edges) + [shape("text", "note", 0, -80, 360, 30, text="Черновик процесса" if ru else "Release process draft")])
    flow = stub.add("Процесс выпуска" if ru else "Release flow", third, history=[(scene([]), "agent", 60 * 26), (first, "agent", 60 * 25), (second, "agent", 90), (third, "user", 20)])
    arch = stub.add("Архитектура" if ru else "Architecture", scene(layout([{"id": "app", "label": "App", "color": "blue"}, {"id": "api", "label": "API", "color": "violet"}, {"id": "db", "label": "SQLite", "shape": "ellipse", "color": "grey"}], [{"from": "app", "to": "api"}, {"from": "api", "to": "db"}], "down")), source="agent", minutes_ago=60 * 3, shared=True)
    stub.add("Заметки" if ru else "Notes", scene([shape("rectangle", "box", 0, 0, 200, 100, color="yellow", text="Идеи" if ru else "Ideas")]), minutes_ago=60 * 24 * 4)
    return {"flow": flow, "arch": arch}


def route(page: Page, stub: DiagramStub) -> None:
    def answer(request_route) -> None:  # type: ignore[no-untyped-def]
        if stub.handle(request_route):
            return
        shots.stub(request_route)

    page.route("**/api/**", answer)


def wait_canvas(page: Page) -> None:
    expect(page.locator(CANVAS).first).to_be_visible(timeout=30000)


def shot(page: Page, name: str) -> None:
    if SHOTS:
        # Finished, not caught mid-entrance: a sheet or a menu photographed in its first frame is see-through.
        page.screenshot(path=f"{SHOTS}/{name}.png", animations="disabled")


def fits(page: Page, selector: str) -> None:
    """Every child of the bar inside the window, and nothing scrolled sideways."""
    width = page.evaluate("window.innerWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the page scrolls sideways"
    overflow = page.evaluate(f"(() => {{ const bar = document.querySelector('{selector}'); return bar.scrollWidth - bar.clientWidth; }})()")
    assert overflow <= 0, f"the bar overflows by {overflow}px"
    for box in page.locator(f"{selector} > *").all():
        rect = box.bounding_box()
        if rect is None:
            continue
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width + 0.5, f"a bar control sticks out: {rect}"


def outline(element: dict) -> tuple[float, float, float, float]:
    points = element.get("points") or [[0, 0], [element.get("width", 0), element.get("height", 0)]]
    xs = [element["x"] + point[0] for point in points]
    ys = [element["y"] + point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def on_screen(page: Page, box_selector: str, elements: list[dict]) -> None:
    """Every top-level shape of the drawing inside the canvas once it has opened: the open fits the
    whole drawing, zooming out as far as it must, on a phone as on a desktop. The canvas writes its
    zoom and scroll onto its box; a scene point lands on screen at (point + scroll) * zoom."""
    shapes = [element for element in elements if not element.get("containerId") and not element.get("isDeleted")]
    assert shapes, "nothing to look for"
    outside: list[str] = []
    for _ in range(60):
        view = page.locator(box_selector).first.get_attribute("data-view")
        frame = page.locator(box_selector).first.bounding_box()
        outside = ["no view yet"]
        if view and frame:
            state = json.loads(view)
            zoom, sx, sy = state["zoom"], state["scrollX"], state["scrollY"]
            outside = []
            for element in shapes:
                left, top, right, bottom = outline(element)
                x1, y1 = frame["x"] + (left + sx) * zoom, frame["y"] + (top + sy) * zoom
                x2, y2 = frame["x"] + (right + sx) * zoom, frame["y"] + (bottom + sy) * zoom
                if x1 < frame["x"] - 0.5 or y1 < frame["y"] - 0.5 or x2 > frame["x"] + frame["width"] + 0.5 or y2 > frame["y"] + frame["height"] + 0.5:
                    outside.append(f"{element['id']} at {x1:.0f},{y1:.0f}-{x2:.0f},{y2:.0f} in {frame}")
            if not outside and 0.1 <= zoom <= 1:
                return
        page.wait_for_timeout(100)
    raise AssertionError(f"shapes off the canvas after it opened: {outside}")


def desktop(browser, lang: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    stub = DiagramStub()
    ids = seed(stub, lang)
    ru = lang == "ru"
    context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE.split("/app")[0])
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    route(page, stub)
    page.goto(f"{BASE}/diagrams?token=t&lang={lang}&scheme={SCHEME}")
    expect(page.locator(".diagram-card")).to_have_count(3)
    expect(page.locator(".diagram-card .diagram-thumb img").first).to_be_visible()
    assert page.locator(".diagram-card .diagram-thumb.dark").count() == 0, "the thumbnails took the app's dark theme"
    expect(page.locator(".desktop-context")).to_be_visible()
    shot(page, f"desktop-list-{lang}-{SCHEME}")
    page.locator(".diagram-search input").fill("архит" if ru else "archi")
    expect(page.locator(".diagram-card")).to_have_count(1)
    page.locator(".diagram-search input").fill("")
    page.get_by_role("radio", name="По имени" if ru else "Name").click()
    first = page.locator(".diagram-card-title").first.inner_text()
    assert first == ("Архитектура" if ru else "Architecture"), first
    page.get_by_role("radio", name="Недавние" if ru else "Recent").click()

    page.locator(".diagram-card-title", has_text="Процесс выпуска" if ru else "Release flow").click()
    wait_canvas(page)
    # Focus mode: the rail stays, the app's column and its resize handle go, and nothing else sits left of the canvas.
    expect(page.locator(".app.focus-mode")).to_have_count(1)
    expect(page.locator(".desktop-column.folded")).to_have_count(1)
    expect(page.locator(".desktop-context .sidebar")).to_have_count(0)
    expect(page.locator(".rail")).to_be_visible()
    expect(page.locator(".diagram-side")).to_have_count(0)
    expect(page.locator(".diagram-history")).to_have_count(0)
    on_screen(page, ".diagram-canvas", stub.diagrams[ids["flow"]]["scene"]["elements"])
    canvas_box = page.locator(".diagram-canvas").bounding_box()
    assert canvas_box is not None and canvas_box["x"] <= 60 and canvas_box["width"] >= 1440 - 60, canvas_box
    expect(page.locator(".diagram-status")).to_have_class("diagram-status saved")
    # The canvas is light by default whatever the app's scheme: a drawing is a document of its own.
    assert "theme--dark" not in (page.locator(".diagram-canvas .excalidraw").first.get_attribute("class") or ""), "the canvas took the app's dark theme"
    if ru:
        expect(page.locator('.diagram-canvas [title*="Прямоугольник"]').first).to_be_attached()
    fits(page, ".diagram-bar")
    shot(page, f"desktop-editor-{lang}-{SCHEME}")

    # A stroke saves itself on the version the page holds.
    box = page.locator(".diagram-canvas").bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] - 300, box["y"] + box["height"] - 200)
    page.keyboard.press("r")
    page.mouse.move(box["x"] + box["width"] - 360, box["y"] + box["height"] - 260)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 240, box["y"] + box["height"] - 180, steps=8)
    page.mouse.up()
    page.keyboard.press("Escape")
    expect(page.locator(".diagram-status.saved")).to_be_visible(timeout=15000)
    puts = [call for call in stub.calls if call[0] == "PUT"]
    assert puts and puts[-1][2]["version"] == 4, puts
    assert stub.diagrams[ids["flow"]]["version"] == 5

    # The agent edits the open diagram: the head poll brings the scene in without a save of ours.
    elements = list(stub.diagrams[ids["flow"]]["scene"]["elements"]) + [shape("rectangle", "agent-box", 0, 400, 180, 80, color="violet", text="Агент" if ru else "Agent")]
    agent_version = stub.agent_edit(ids["flow"], scene(elements))
    page.wait_for_function(f"() => document.querySelector('.diagram-status')?.getAttribute('title')?.includes('{agent_version}')", timeout=15000)
    page.wait_for_timeout(1500)
    assert stub.diagrams[ids["flow"]]["version"] == agent_version, "taking in the agent's edit must not save it back"

    # History: a drawer on the right, grouped by day, saying who saved.
    page.get_by_role("button", name="История" if ru else "History").click()
    drawer = page.locator(".diagram-history")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".diagram-history-day h4").first).to_have_text("Сегодня" if ru else "Today")
    expect(drawer.locator(".diagram-who.agent").first).to_be_visible()
    expect(drawer.locator(".diagram-who.user").first).to_be_visible()
    expect(drawer).not_to_contain_text("agent-box")
    drawer_box = drawer.bounding_box()
    assert drawer_box is not None and drawer_box["x"] + drawer_box["width"] >= 1439, drawer_box
    expect(drawer.locator(".diagram-revision .diagram-thumb img").first).to_be_visible()
    drawer.locator(".diagram-revision").nth(3).click()
    expect(page.locator(".diagram-preview .diagram-banner")).to_be_visible()
    expect(page.locator(".diagram-preview .excalidraw")).to_be_visible(timeout=20000)
    on_screen(page, ".diagram-preview-canvas", stub.revisions[ids["flow"]][3]["scene"]["elements"])
    shot(page, f"desktop-history-{lang}-{SCHEME}")
    before = stub.diagrams[ids["flow"]]["version"]
    page.get_by_role("button", name="Восстановить эту версию" if ru else "Restore this version").click()
    expect(page.locator(".diagram-preview")).to_have_count(0)
    restores = [call for call in stub.calls if call[1].endswith("/restore")]
    assert restores and restores[-1][2]["version"] == before, restores
    assert stub.diagrams[ids["flow"]]["version"] == before + 1 and stub.revisions[ids["flow"]][0]["kind"] == "restore"
    expect(drawer.locator(".diagram-revision").first).to_contain_text("Восстановлена" if ru else "Restored")
    drawer.get_by_role("button", name="Закрыть" if ru else "Close").click()
    expect(drawer).to_have_count(0)

    # Share: a popover that makes, copies and turns off the link.
    page.get_by_role("button", name="Поделиться" if ru else "Share").click()
    popover = page.locator(".diagram-share-pop")
    expect(popover).to_be_visible()
    popover.get_by_role("button", name="Создать ссылку" if ru else "Create link").click()
    expect(popover.locator("input")).to_have_value(f"{BASE.split('/app')[0]}/app/d/{stub.diagrams[ids['flow']]['share_token']}")
    shot(page, f"desktop-share-{lang}-{SCHEME}")
    popover.get_by_role("button", name="Отключить" if ru else "Turn off").click()
    expect(popover.locator("input")).to_have_count(0)
    assert stub.diagrams[ids["flow"]]["share_token"] == ""
    page.keyboard.press("Escape")
    expect(popover).to_have_count(0)

    # Export: a real menu, each item a file.
    for item, suffix in (("Файл Excalidraw" if ru else "Excalidraw file", ".excalidraw"), ("Изображение SVG" if ru else "SVG image", ".svg"), ("Изображение PNG" if ru else "PNG image", ".png")):
        page.get_by_role("button", name="Экспорт" if ru else "Export").click()
        expect(page.get_by_role("menu")).to_be_visible()
        with page.expect_download() as saved:
            page.get_by_role("menuitem", name=item).click()
        assert saved.value.suggested_filename.endswith(suffix), saved.value.suggested_filename
        assert saved.value.path().stat().st_size > 100

    # A save on top of the agent's newer version: the strokes stay, and the choice is offered.
    stub.agent_edit(ids["flow"], scene(list(stub.diagrams[ids["flow"]]["scene"]["elements"])), title="Agent title")
    page.get_by_role("textbox", name="Название схемы" if ru else "Diagram title").fill("Mine")
    expect(page.locator(".diagram-banner.conflict")).to_be_visible(timeout=15000)
    expect(page.get_by_role("textbox", name="Название схемы" if ru else "Diagram title")).to_have_value("Mine")
    page.get_by_role("button", name="Загрузить новую" if ru else "Load latest").click()
    expect(page.get_by_role("textbox", name="Название схемы" if ru else "Diagram title")).to_have_value("Agent title")
    expect(page.locator(".diagram-status.saved")).to_be_visible()
    stub.agent_edit(ids["flow"], scene(list(stub.diagrams[ids["flow"]]["scene"]["elements"])), title="Agent again")
    page.get_by_role("textbox", name="Название схемы" if ru else "Diagram title").fill("Mine after all")
    expect(page.locator(".diagram-banner.conflict")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Оставить мою" if ru else "Keep mine").click()
    expect(page.locator(".diagram-status.saved")).to_be_visible(timeout=15000)
    assert stub.diagrams[ids["flow"]]["title"] == "Mine after all"

    # The app's scheme does not reach the canvas; the canvas's own menu switches it, and the choice
    # stays with this device (a reload keeps it, the thumbnails follow it).
    flipped = "light" if SCHEME == "dark" else "dark"
    page.evaluate(f"document.documentElement.dataset.scheme = '{flipped}'")
    page.wait_for_timeout(300)
    assert not page.evaluate("document.querySelector('.diagram-canvas .excalidraw').classList.contains('theme--dark')"), "the app's scheme changed the canvas"
    page.evaluate(f"document.documentElement.dataset.scheme = '{SCHEME}'")
    page.locator(".diagram-canvas .main-menu-trigger").click()
    page.locator(".diagram-canvas [data-testid='toggle-dark-mode']").click()
    page.wait_for_function("() => document.querySelector('.diagram-canvas .excalidraw').classList.contains('theme--dark')")
    assert page.evaluate("localStorage.getItem('daedalus.diagrams.theme')") == "dark"
    page.reload()
    wait_canvas(page)
    page.wait_for_function("() => document.querySelector('.diagram-canvas .excalidraw').classList.contains('theme--dark')")
    page.locator(".diagram-canvas .main-menu-trigger").click()
    page.locator(".diagram-canvas [data-testid='toggle-dark-mode']").click()
    page.wait_for_function("() => !document.querySelector('.diagram-canvas .excalidraw').classList.contains('theme--dark')")
    assert page.evaluate("localStorage.getItem('daedalus.diagrams.theme')") == "light"

    # Back leaves focus mode: the column is there again.
    page.get_by_role("button", name="Назад" if ru else "Back").first.click()
    expect(page.locator(".diagram-card")).to_have_count(3)
    expect(page.locator(".app.focus-mode")).to_have_count(0)
    expect(page.locator(".desktop-context .sidebar")).to_be_visible()

    # The empty state offers templates; a template becomes a diagram with bound arrows.
    for item in list(stub.diagrams):
        del stub.diagrams[item]
    page.reload()
    expect(page.locator(".diagram-empty")).to_be_visible()
    expect(page.locator(".diagram-empty .diagram-template")).to_have_count(4)
    shot(page, f"desktop-empty-{lang}-{SCHEME}")
    page.locator(".diagram-empty .diagram-template").nth(1).click()
    wait_canvas(page)
    made = next(iter(stub.diagrams.values()))
    arrows = [element for element in made["scene"]["elements"] if element["type"] == "arrow"]
    shapes = {element["id"]: element for element in made["scene"]["elements"]}
    assert arrows and all(arrow["startBinding"]["elementId"] in shapes and arrow["endBinding"]["elementId"] in shapes for arrow in arrows)
    shot(page, f"desktop-template-{lang}-{SCHEME}")

    # The shared page: the title, the line about where it was made, the reader's theme.
    stub.diagrams.clear()
    shared = seed(stub, lang)["arch"]
    page.goto(f"{BASE}/d/{stub.diagrams[shared]['share_token']}?lang={lang}&scheme={SCHEME}")
    expect(page.locator(".diagram-shared header")).to_contain_text("Архитектура" if ru else "Architecture")
    expect(page.locator(".diagram-shared-made")).to_be_visible()
    expect(page.locator(".diagram-shared .excalidraw")).to_be_visible(timeout=20000)
    assert "theme--dark" not in (page.locator(".diagram-shared .excalidraw").get_attribute("class") or ""), "the shared canvas took the app's dark theme"
    on_screen(page, ".diagram-shared .diagram-canvas", stub.diagrams[shared]["scene"]["elements"])
    shot(page, f"desktop-shared-{lang}-{SCHEME}")
    context.close()


def phone(browser, lang: str, errors: list[str]) -> None:  # type: ignore[no-untyped-def]
    stub = DiagramStub()
    ids = seed(stub, lang)
    ru = lang == "ru"
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    route(page, stub)
    page.goto(f"{BASE}/diagrams?token=t&lang={lang}&scheme={SCHEME}")
    expect(page.locator(".diagram-card")).to_have_count(3)
    expect(page.locator(".tabbar")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(page.locator(".diagram-card .diagram-thumb img").first).to_be_visible()
    shot(page, f"phone-list-{lang}-{SCHEME}")
    opened = page.locator(".diagram-card-title").first.inner_text()
    page.locator(".diagram-card-open").first.click()
    wait_canvas(page)
    on_screen(page, ".diagram-canvas", next(item for item in stub.diagrams.values() if item["title"] == opened)["scene"]["elements"])
    expect(page.locator(".tabbar")).to_have_count(0)
    expect(page.locator(".diagram-side")).to_have_count(0)
    expect(page.locator(".diagram-history, .diagram-history-sheet")).to_have_count(0)
    bar = page.locator(".diagram-bar").bounding_box()
    assert bar is not None and bar["y"] <= 1, bar
    fits(page, ".diagram-bar")
    canvas = page.locator(".diagram-canvas").bounding_box()
    assert canvas is not None and canvas["y"] >= bar["y"] + bar["height"] - 1 and canvas["y"] + canvas["height"] >= 840, canvas
    shot(page, f"phone-editor-{lang}-{SCHEME}")
    for width in (360,):
        page.set_viewport_size({"width": width, "height": 780})
        fits(page, ".diagram-bar")
        page.set_viewport_size({"width": 390, "height": 844})

    page.get_by_role("button", name="История" if ru else "History").click()
    sheet = page.locator(".diagram-history-sheet")
    expect(sheet).to_be_visible()
    sheet_box = sheet.bounding_box()
    assert sheet_box is not None and sheet_box["y"] + sheet_box["height"] >= 843 and sheet_box["width"] >= 389, sheet_box
    expect(sheet.locator(".diagram-revision").first).to_be_visible()
    shot(page, f"phone-history-{lang}-{SCHEME}")
    sheet.locator(".diagram-revision").nth(2).click()
    expect(page.locator(".diagram-preview .diagram-banner")).to_be_visible()
    expect(page.locator(".diagram-preview .excalidraw")).to_be_visible(timeout=20000)
    on_screen(page, ".diagram-preview-canvas", stub.revisions[ids["flow"]][2]["scene"]["elements"])
    shot(page, f"phone-preview-{lang}-{SCHEME}")
    expect(page.locator(".diagram-history-sheet")).to_have_count(0)
    page.get_by_role("button", name="К текущей" if ru else "Back to current").click()
    expect(page.locator(".diagram-preview")).to_have_count(0)
    expect(page.locator(".diagram-history-sheet")).to_be_visible()
    page.locator(".diagram-history-sheet").get_by_role("button", name="Закрыть" if ru else "Close").click()
    expect(page.locator(".diagram-history-sheet")).to_have_count(0)

    page.get_by_role("button", name="Другие действия" if ru else "More actions").click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("menuitem")).to_have_count(5)
    shot(page, f"phone-menu-{lang}-{SCHEME}")
    menu.get_by_role("menuitem", name="Поделиться" if ru else "Share").click()
    expect(page.get_by_role("dialog")).to_contain_text("Создать ссылку" if ru else "Create link")
    page.get_by_role("dialog").get_by_role("button", name="Закрыть" if ru else "Close").click()

    page.get_by_role("button", name="Назад" if ru else "Back").first.click()
    expect(page.locator(".diagram-card")).to_have_count(3)
    expect(page.locator(".tabbar")).to_be_visible()
    assert ids
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
