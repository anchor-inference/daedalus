"""Move a project's folders between the container and the host from its settings sheet, on a phone
and on a desktop, in both languages.

The container folder is moved to the host: the proposed destination is under ~/Daedalus/projects,
choosing a folder in the host's browser keeps the folder's name under it, and the move shows its bar
and ends saying the container copy was removed. The host folder comes back into the container with
the host original kept unless the box is ticked, and the box's choice is what is sent. A daemon too
old for moves says so and keeps the button off. A folder mounted from the host offers no move at all.

And a move outlives its dialog: the plan names the link it will not carry before anything starts; once
started and the dialog closed, the folder's row says how far it is; after a reload the row still says
so and the inbox lists it under "in progress"; the button reopens the dialog on the running move, which
then ends there.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folder, fulfil_shared, open_projects  # noqa: E402
from folder_stub import FolderStub  # noqa: E402
from move_stub import HOST_HOME, MoveStub, plan  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")

ENVIRONMENTS = {"local": "container", "available": ["container", "host"], "host_bridge": True, "docker": True, "home": "/root", "workspaces_root": "/srv/workspaces"}
SITE = {**folder("/srv/workspaces/bakery", is_git=True), "managed": True}
CUTS = folder(f"{HOST_HOME}/projects/stream-cuts", position=1, env="host", reach="terminals")
MOUNTED = folder("/work/mounted", position=2)
PROJECT = {
    "id": "p1", "entity_revision": 1, "name": "Bakery", "created_at": "2026-09-19T00:00:00Z", "system": "",
    "settings": {"snapshots": False, "system": "", "ephemeral": False, "default_env": "container"},
    "folders": [SITE, CUTS, MOUNTED], "sessions": [{"id": "s1", "title": "Writer", "running": False}],
}
WORDS = {
    "en": {"settings": "Settings for Bakery", "tohost": "Move to the host…", "tocontainer": "Move into the container…", "done": "Moved",
           "removed": "was removed", "kept": "was kept as it is", "outdated": "Update the host terminal first:",
           "skipped": "1 entry will not be carried", "live": "Moving to the host… 33 %", "open": "Show the move", "now": "In progress"},
    "ru": {"settings": "Настройки: Bakery", "tohost": "Перенести на хост…", "tocontainer": "Перенести в контейнер…", "done": "Перенесено",
           "removed": "удалена", "kept": "оставлена как есть", "outdated": "Сначала обновите терминал на хосте:",
           "skipped": "1 запись не будет перенесена", "live": "Переносится на хост… 33 %", "open": "Показать перенос", "now": "Идёт сейчас"},
}


def run() -> int:
    unhandled = Unhandled()
    failures: list[str] = []
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height, mobile in ((390, 844, True), (1440, 900, False)):
                label = f"{lang} {width}px"
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                try:
                    scenario(page, lang, unhandled, f"{lang}-{width}")
                    page.close()
                    page = context.new_page()
                    survives_reload(page, lang, unhandled, f"{lang}-{width}", mobile)
                    print(f"ok  {label}")
                except Exception as exc:  # noqa: BLE001 — one failing layout must not hide the others
                    failures.append(f"{label}: {exc}")
                    print(f"FAILED {label}: {exc}")
                context.close()
        browser.close()
    if failures:
        return 1
    return unhandled.report()


def install(page: Page, project: dict, moves: MoveStub, folders: FolderStub, unhandled: Unhandled) -> None:
    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        url = urlsplit(route.request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/projects":
            return answer(route, [project])
        if path == "/api/project-environments":
            return answer(route, ENVIRONMENTS)
        if path == "/api/sessions":
            return answer(route, {"sessions": [], "projects": [{**project, "total": 1, "active": 0, "loops": 0, "last_message_at": ""}]})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if moves.fulfil(route) or folders.fulfil(route) or fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)


def open_settings(page: Page, lang: str) -> None:
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{WORDS[lang]['settings']}']").click()
    expect(page.locator(".dir-row")).to_have_count(3)


def scenario(page: Page, lang: str, unhandled: Unhandled, name: str) -> None:
    words = WORDS[lang]
    project = copy.deepcopy(PROJECT)
    moves = MoveStub({
        SITE["id"]: plan(SITE["id"], "to_host", SITE["path"], f"{HOST_HOME}/Daedalus/projects/bakery"),
        CUTS["id"]: plan(CUTS["id"], "to_container", CUTS["path"], "/srv/workspaces/stream-cuts"),
    })
    install(page, project, moves, FolderStub(host="up"), unhandled)
    open_settings(page, lang)
    # A folder mounted from the host is on the host already: no move is offered for it.
    expect(page.locator(".dir-row[data-folder='f-mounted'] [data-move-folder]")).to_have_count(0)

    # Out of the container: the proposed place, then a folder chosen on the host keeps the name.
    page.locator(f"[data-move-folder='{SITE['id']}']").click()
    sheet = page.locator(".move-folder")
    expect(sheet).to_have_attribute("data-move-plan", "to_host")
    destination = page.locator("#move-destination")
    expect(destination).to_have_value(f"{HOST_HOME}/Daedalus/projects/bakery")
    sheet.locator(".move-dest .btn").click()
    sheet.locator(".fb-row", has_text="projects").dblclick()
    expect(destination).to_have_value(f"{HOST_HOME}/projects/bakery")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/move-plan-{name}.png")
    sheet.locator(".sheet-foot .btn.primary").click()
    expect(page.locator(".move-folder progress")).to_be_visible()
    expect(page.locator(".move-folder[data-move-state='done']")).to_be_visible(timeout=15000)
    expect(page.locator(".move-folder")).to_contain_text(words["removed"])
    assert moves.started[-1] == (SITE["id"], {"destination": f"{HOST_HOME}/projects/bakery"}), moves.started
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/move-done-{name}.png")
    page.locator(".move-folder .sheet-foot .btn.primary").click()

    # Back into the container: the host original is kept unless the box asks otherwise.
    page.locator(f"[data-move-folder='{CUTS['id']}']").click()
    expect(sheet).to_have_attribute("data-move-plan", "to_container")
    expect(page.locator("#move-destination")).to_have_count(0)
    expect(sheet.locator(".toggle-row input")).not_to_be_checked()
    sheet.locator(".sheet-foot .btn.primary").click()
    expect(page.locator(".move-folder[data-move-state='done']")).to_be_visible(timeout=15000)
    expect(page.locator(".move-folder")).to_contain_text(words["kept"])
    assert moves.started[-1] == (CUTS["id"], {"remove_original": False}), moves.started
    page.locator(".move-folder .sheet-foot .btn.primary").click()

    # A host daemon too old for moves: the reason, and nothing to press.
    moves.plans[SITE["id"]] = plan(SITE["id"], "to_host", SITE["path"], "", problem="host_outdated",
                                   detail="the host terminal daemon is older than this version and cannot move folders; update it")
    page.locator(f"[data-move-folder='{SITE['id']}']").click()
    expect(page.locator("[data-move-problem='host_outdated']")).to_contain_text(words["outdated"])
    expect(sheet.locator(".sheet-foot .btn.primary")).to_be_disabled()



def survives_reload(page: Page, lang: str, unhandled: Unhandled, name: str, mobile: bool) -> None:
    words = WORDS[lang]
    project = copy.deepcopy(PROJECT)
    cache_link = [{"path": ".uv-cache/archive-v0/x/bin/python", "reason": "link outside the folder"}]
    moves = MoveStub({SITE["id"]: plan(SITE["id"], "to_host", SITE["path"], f"{HOST_HOME}/Daedalus/projects/bakery", skipped=cache_link)})
    moves.held = True
    install(page, project, moves, FolderStub(host="up"), unhandled)
    open_settings(page, lang)
    row = page.locator(f".dir-row[data-folder='{SITE['id']}']")

    # Before anything starts: what will not come along, and why.
    row.locator("[data-move-folder]").click()
    skipped = page.locator(".move-folder [data-move-skipped='1']")
    expect(skipped).to_contain_text(words["skipped"])
    skipped.locator("summary").click()
    expect(skipped).to_contain_text(".uv-cache/archive-v0/x/bin/python")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/move-skipped-{name}.png")
    page.locator(".move-folder .sheet-foot .btn.primary").click()
    expect(page.locator(".move-folder progress")).to_be_visible()
    page.locator(".move-folder .sheet-foot .btn.primary").click()  # continue in the background
    expect(page.locator(".move-folder")).to_have_count(0)
    expect(row.locator("[data-folder-move-live]")).to_contain_text(words["live"])

    # A reload in the middle: the row still says so, and the inbox lists it.
    page.reload()
    if mobile:
        page.goto(f"{BASE}/inbox?token=t&lang={lang}")
        expect(page.locator(".moves-now-row")).to_contain_text("Bakery")
        expect(page.locator(".moves-now")).to_contain_text(words["now"])
    else:
        page.locator(".bell").first.click()
        expect(page.locator(".bell-pop .moves-now-row")).to_contain_text("Bakery")
        expect(page.locator(".bell-pop .moves-now-row")).to_contain_text(words["live"])
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/move-inbox-{name}.png")
        page.keyboard.press("Escape")
    open_settings(page, lang)
    row = page.locator(f".dir-row[data-folder='{SITE['id']}']")
    expect(row.locator("[data-folder-move-live]")).to_contain_text(words["live"])
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/move-row-{name}.png")

    # The button opens the dialog on the running move, which ends there.
    expect(row.locator("[data-move-folder]")).to_contain_text(words["open"])
    row.locator("[data-move-folder]").click()
    expect(page.locator(".move-folder[data-move-state='copying']")).to_be_visible()
    moves.held = False
    expect(page.locator(".move-folder[data-move-state='done']")).to_be_visible(timeout=15000)
    page.locator(".move-folder .sheet-foot .btn.primary").click()
    expect(row.locator("[data-folder-move-live]")).to_have_count(0)
    assert len(moves.started) == 1, moves.started


if __name__ == "__main__":
    sys.exit(run())
