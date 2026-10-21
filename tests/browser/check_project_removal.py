"""Remove a project from its settings sheet, on a phone and on a desktop, in both languages.

The dialog names the operator's chats, the ones that keep the project, and nothing else; once there
are none it says what goes with the project (the orchestrator, the team, the board) and that not one
file is deleted, and the removal is sent.
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

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")

PROJECT = {
    "id": "p1", "entity_revision": 1, "name": "Firm", "created_at": "2026-09-19T00:00:00Z", "system": "",
    "settings": {"snapshots": False, "system": "", "ephemeral": False, "default_env": "container"},
    "folders": [folder("/work/firm")],
    "sessions": [{"id": "s1", "title": "Notes", "running": False}],
    "machinery": {"orchestrator": True, "staff": 3, "tasks": 4, "archived_chats": 0},
}
WORDS = {
    "en": {"settings": "Settings for Firm", "remove": "Remove", "chats": "Notes works in it, so this will be refused",
           "goes": "Goes with it: the orchestrator, 3 team members, 4 tasks on the board", "files": "Not one file of its folders is deleted.",
           "removed": "Firm removed"},
    "ru": {"settings": "Настройки: Firm", "remove": "Убрать", "chats": "В нём работает Notes, поэтому запрос отклонят",
           "goes": "Вместе с ним уйдут: оркестратор, 3 сотрудника команды, 4 задачи на доске", "files": "Ни один файл в его папках не удаляется.",
           "removed": "Firm: проект убран"},
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
                    print(f"ok  {label}")
                except Exception as exc:  # noqa: BLE001 — one failing layout must not hide the others
                    failures.append(f"{label}: {exc}")
                    print(f"FAILED {label}: {exc}")
                context.close()
        browser.close()
    if failures:
        return 1
    return unhandled.report()


def scenario(page: Page, lang: str, unhandled: Unhandled, name: str) -> None:
    words = WORDS[lang]
    state = {"project": copy.deepcopy(PROJECT), "gone": False}
    deleted: list[str] = []

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        project = state["project"]
        listed = [] if state["gone"] else [project]
        if path == "/api/projects":
            return answer(route, listed)
        if path == "/api/projects/p1" and request.method == "DELETE":
            deleted.append(path)
            state["gone"] = True
            return answer(route, {"ok": True})
        if path == "/api/project-environments":
            return answer(route, {"local": "container", "available": ["container"], "host_bridge": False, "docker": True})
        if path == "/api/sessions":
            return answer(route, {"sessions": [], "projects": [{**p, "total": len(p["sessions"]), "active": 0, "loops": 0, "last_message_at": ""} for p in listed]})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    def open_settings() -> None:
        open_projects(page)
        page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{words['settings']}']").click()

    def ask_removal():  # type: ignore[no-untyped-def]
        page.locator(".sheet-foot .btn.danger").click()
        dialog = page.locator(".dialog[role='alertdialog']")
        expect(dialog).to_be_visible()
        return dialog

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")

    # A chat of the operator's keeps the project, and the dialog names it before anything is sent.
    open_settings()
    dialog = ask_removal()
    expect(dialog).to_contain_text(words["chats"])
    expect(dialog).not_to_contain_text(words["goes"])
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/remove-chat-{name}.png")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert not deleted, deleted

    # Without one, it says what goes and that the files stay, and the removal is sent.
    state["project"]["sessions"] = []
    page.reload()
    open_settings()
    dialog = ask_removal()
    expect(dialog).to_contain_text(words["goes"])
    expect(dialog).to_contain_text(words["files"])
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/remove-machinery-{name}.png")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"the page scrolls sideways: {overflow}"
    dialog.locator("button", has_text=words["remove"]).last.click()
    expect(page.locator(".toast")).to_contain_text(words["removed"])
    assert deleted == ["/api/projects/p1"], deleted


if __name__ == "__main__":
    raise SystemExit(run())
