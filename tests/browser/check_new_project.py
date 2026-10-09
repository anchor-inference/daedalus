"""The new-project dialog and its folder browser, state by state, in both languages.

A new folder named after the project; an existing folder chosen in the container and on the machine;
chat folders folded away and refused; the machine not answering, its daemon too old, and a container
folder that is not mounted; an empty name, a folder another project owns, and a server that does not
answer, with the draft kept and one project made on "Try again"; the extras; Ctrl+Enter; a native
install without the container switch; orchestration mode's own dialog; and a phone.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402
from folder_stub import HOST_HOME, WORKSPACES, FolderStub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"existing": "Existing folder", "new": "New folder", "create": "Create project", "host": "Host", "container": "Container",
           "taken": "already the project", "open": "Open the project", "retry": "Try again", "kept": "your draft is kept",
           "name_missing": "Name the project", "chats": "Chat folders", "chat_hint": "Keep as a project", "down": "The machine is not answering",
           "check": "Check again", "type": "Type a path", "old": "The host daemon needs updating", "missing": "There is no such folder in the container",
           "onhost": "exists on the machine", "howto": "Show the line", "use": "Use it", "more": "More", "snapshots": "File snapshots",
           "readonly": "Read-only", "rename": "Rename the folder", "orch": "New orchestration project", "orch_create": "Create project and first task",
           "chosen": "Chosen", "writable": "writable", "hosttag": "HOST", "service": "Service"},
    "ru": {"existing": "Существующая папка", "new": "Новая папка", "create": "Создать проект", "host": "Хост", "container": "Контейнер",
           "taken": "уже проект", "open": "Открыть проект", "retry": "Ещё раз", "kept": "черновик сохранён",
           "name_missing": "Назовите проект", "chats": "Папки чатов", "chat_hint": "Оставить как проект", "down": "Машина не отвечает",
           "check": "Проверить снова", "type": "Ввести путь", "old": "Демон на хосте нужно обновить", "missing": "В контейнере такой папки нет",
           "onhost": "есть на машине", "howto": "Показать строку", "use": "Использовать", "more": "Дополнительно", "snapshots": "Снимки файлов",
           "readonly": "Только чтение", "rename": "Переименовать папку", "orch": "Новый проект оркестрации", "orch_create": "Создать проект и первую задачу",
           "chosen": "Выбрано", "writable": "можно писать", "hosttag": "ХОСТ", "service": "Служебные"},
}


class Harness:
    """One page over the shared stub, the folder stub, and a project store that records what is posted."""

    def __init__(self, page: Page, folders_stub: FolderStub, unhandled: Unhandled) -> None:
        self.page = page
        self.folders = folders_stub
        self.unhandled = unhandled
        self.projects: list[dict] = []
        self.posted: list[dict] = []
        self.starts: list[dict] = []
        self.fail_next = 0
        page.route("**/api/**", self.route)

    def answer(self, route, value, status=200):  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        if self.folders.fulfil(route):
            return
        if path == "/api/projects" and request.method == "POST":
            body = request.post_data_json
            self.posted.append(body)
            if self.fail_next:
                self.fail_next -= 1
                return self.answer(route, {"detail": "server unavailable"}, 503)
            folder_path = body["folders"][0]["path"] if body.get("folders") else f"{WORKSPACES}/{body['folder_name']}"
            project = {"id": f"p{len(self.projects) + 1}", "name": body["name"], "entity_revision": 1, "created_at": "2026-10-09T00:00:00Z",
                       "folders": folders(folder_path), "settings": {"snapshots": body["snapshots"], "ephemeral": False}, "system": "", "sessions": []}
            self.projects.append(project)
            return self.answer(route, project)
        if path == "/api/projects" and request.method == "GET":
            return self.answer(route, self.projects)
        if path == "/api/settings":
            return self.answer(route, {"presets": {}, "model": {}})
        if path == "/api/control/revisions":
            return self.answer(route, {"collection_revision": 1})
        if path == "/api/project-start" and request.method == "POST":
            body = request.post_data_json
            self.starts.append(body)
            self.projects.append({"id": "po", "name": body["name"], "entity_revision": 1, "created_at": "2026-10-09T00:00:00Z",
                                  "folders": folders(f"{WORKSPACES}/{body.get('folder_name', 'x')}"), "settings": {"snapshots": True}, "system": "", "sessions": []})
            return self.answer(route, {"project_id": "po", "task_id": "t1", "receipt_id": "r1", "entity_revision": 2})
        if fulfil_shared(route):
            return
        if path.startswith("/api/projects/"):
            return self.answer(route, [])
        self.unhandled.record(path)
        self.answer(route, [])

    def open(self, language: str, kind: str = "agents", screen: str = "agents") -> None:
        self.page.goto(f"{BASE}/{screen}?token=t&lang={language}")
        self.page.wait_for_selector(".app")
        self.page.evaluate(f"window.dispatchEvent(new CustomEvent('daedalus:new-project', {{ detail: '{kind}' }}))")
        expect(self.page.locator(".sheet.np-sheet")).to_be_visible()

    def existing(self, words: dict[str, str]) -> None:
        self.page.locator(".np-card", has_text=words["existing"]).click()
        expect(self.page.locator(".fb")).to_be_visible()


def fits(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))


def new_folder(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    h = Harness(page, FolderStub(host="up"), unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    expect(sheet.locator(".np-card", has_text=words["new"])).to_have_attribute("aria-checked", "true")
    page.locator("#project-name").fill("Умный дом")
    expect(sheet.locator("[data-new-folder]")).to_have_attribute("data-new-folder", "umnyj-dom")
    expect(sheet.locator(".np-newpath")).to_contain_text(f"{WORKSPACES}/umnyj-dom")
    # A proposed name that is taken moves on; an edited one is the operator's.
    page.locator("#project-name").fill("Smart home")
    expect(sheet.locator("[data-new-folder]")).to_have_attribute("data-new-folder", "smart-home-2")
    sheet.get_by_role("button", name=words["rename"]).click()
    sheet.locator(".np-slug-input").fill("home-lab")
    sheet.locator(".np-slug-input").press("Enter")
    expect(sheet.locator("[data-new-folder]")).to_have_attribute("data-new-folder", "home-lab")
    sheet.locator(".np-extras-h").click()
    sheet.locator(".np-toggle", has_text=words["snapshots"]).click()
    page.locator("#project-name").press("Control+Enter")
    expect(sheet).to_have_count(0)
    assert h.posted == [{"name": "Smart home", "snapshots": False, "folder_name": "home-lab"}], h.posted
    assert fits(page)
    browser.close()
    assert unhandled.report() == 0


def existing_folders(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    stub = FolderStub(host="up")
    h = Harness(page, stub, unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("ESP32 firmware")
    h.existing(words)
    sheet.locator(".fb-place.fav", has_text="projects").click()
    expect(sheet.locator(".fb-crumbs")).to_contain_text("projects")
    row = sheet.locator(".fb-row", has_text="esp32-door")
    expect(row).to_contain_text("Git")
    row.click()
    foot = sheet.locator(".fb-foot")
    expect(foot).to_contain_text(words["chosen"])
    expect(foot).to_contain_text("/srv/projects/esp32-door")
    expect(foot).to_contain_text(words["writable"])
    expect(sheet.locator(".fb-row", has_text="notes-archive").locator(".fb-badge.warn")).to_be_visible()
    # A folder another project owns cannot be chosen, and says which project.
    sheet.locator(".fb-row", has_text="landing-cafe").click()
    expect(foot).to_contain_text(words["taken"])
    expect(foot.get_by_role("button", name=words["open"])).to_be_visible()
    expect(sheet.get_by_role("button", name=words["create"])).to_be_disabled()
    # Into a folder by a double click, back, and a new folder inside it.
    sheet.locator(".fb-row", has_text="scripts").dblclick()
    expect(sheet.locator(".fb-crumb.last")).to_have_text("scripts")
    sheet.get_by_role("button", name="Back" if language == "en" else "Назад").click()
    expect(sheet.locator(".fb-crumb.last")).to_have_text("projects")
    sheet.locator(".fb-mk").click()
    sheet.locator(".fb-mk-input").fill("garden")
    sheet.locator(".fb-mk-input").press("Enter")
    expect(foot).to_contain_text("/srv/projects/garden")
    assert stub.made == [{"env": "container", "path": "/srv/projects/garden"}]
    # The folder in view is a favourite already: the pin takes it off the places and puts it back first.
    expect(sheet.locator(".fb-pin")).to_have_attribute("aria-pressed", "true")
    sheet.locator(".fb-pin").click()
    expect(sheet.locator(".fb-place.fav", has_text="projects")).to_have_count(0)
    sheet.locator(".fb-pin").click()
    expect(sheet.locator(".fb-place.fav").first).to_have_text("projects")
    sheet.locator(".np-extras-h").click()
    sheet.locator(".np-toggle", has_text=words["readonly"]).click()
    sheet.locator("#np-label").fill("garden")
    sheet.get_by_role("button", name=words["create"]).click()
    expect(sheet).to_have_count(0)
    assert h.posted[-1] == {"name": "ESP32 firmware", "snapshots": False, "folders": [{"path": "/srv/projects/garden", "label": "garden", "readonly": True}]}, h.posted
    browser.close()
    assert unhandled.report() == 0


def chat_folders(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    Harness(page, FolderStub(host="up"), unhandled).open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator(".np-card", has_text=words["existing"]).click()
    sheet.locator(".fb-place.root").nth(1).click()
    group = sheet.locator(".fb-group", has_text=words["chats"])
    expect(group).to_contain_text("3")
    expect(sheet.locator(".fb-row.sub")).to_have_count(0)
    group.click()
    chat = sheet.locator(".fb-row", has_text="Why the docker build fails")
    expect(chat).to_contain_text("62d62b5f668d")
    chat.click()
    expect(sheet.locator(".fb-foot")).to_contain_text(words["chat_hint"])
    expect(sheet.get_by_role("button", name=words["create"])).to_be_disabled()
    expect(sheet.locator(".fb-group", has_text=words["service"])).to_contain_text("2")
    browser.close()
    assert unhandled.report() == 0


def host_side(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    stub = FolderStub(host="up")
    h = Harness(page, stub, unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("Thesis")
    h.existing(words)
    sheet.locator(".fb-seg").get_by_role("radio", name=words["host"]).click()
    expect(sheet.locator(".fb-crumb").first).to_have_text("~")
    sheet.locator(".fb-row", has_text="projects").dblclick()
    sheet.locator(".fb-row", has_text="thesis-latex").click()
    foot = sheet.locator(".fb-foot")
    expect(foot).to_contain_text(words["hosttag"])
    expect(foot).to_contain_text(f"{HOST_HOME}/projects/thesis-latex")
    sheet.get_by_role("button", name=words["create"]).click()
    expect(sheet).to_have_count(0)
    assert h.posted[-1]["folders"] == [{"path": f"{HOST_HOME}/projects/thesis-latex", "env": "host"}], h.posted
    browser.close()
    assert unhandled.report() == 0


def host_trouble(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    stub = FolderStub(host="down")
    h = Harness(page, stub, unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("Thesis")
    h.existing(words)
    sheet.locator(".fb-seg").get_by_role("radio", name=words["host"]).click()
    panel = sheet.locator(".fb-empty")
    expect(panel).to_contain_text(words["down"])
    expect(panel).to_contain_text("systemctl --user start daedalus-ptyd")
    expect(panel).to_contain_text("bash deploy/host-terminal.sh install")
    expect(sheet.locator(".fb-down")).to_be_visible()
    expect(sheet.locator(".fb-place.fav", has_text="Documents")).to_be_visible()
    expect(sheet.get_by_role("button", name=words["create"])).to_be_disabled()
    # Checking again reaches a daemon that came back, but one too old for browsing.
    stub.host = "outdated"
    panel.get_by_role("button", name=words["check"]).click()
    expect(panel).to_contain_text(words["old"])
    panel.get_by_role("button", name=words["type"]).click()
    sheet.locator("input.fb-crumbs").fill(f"{HOST_HOME}/projects/thesis-latex")
    sheet.locator("input.fb-crumbs").press("Enter")
    expect(sheet.locator(".fb-foot")).to_contain_text(f"{HOST_HOME}/projects/thesis-latex")
    expect(sheet.get_by_role("button", name=words["create"])).to_be_enabled()
    browser.close()
    assert unhandled.report() == 0


def unmounted(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    h = Harness(page, FolderStub(host="up"), unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("Photos")
    h.existing(words)
    sheet.locator(".fb-crumb-edit").click()
    sheet.locator("input.fb-crumbs").fill("/mnt/nas/photos")
    sheet.locator("input.fb-crumbs").press("Enter")
    expect(sheet.locator(".fb-empty")).to_contain_text(words["missing"])
    expect(sheet.locator(".fb-empty")).to_contain_text(words["onhost"])
    warn = sheet.locator(".fb-warn")
    expect(warn).to_be_visible()
    expect(sheet.get_by_role("button", name=words["create"])).to_be_disabled()
    # Without the launcher the mount is one compose line, shown on request.
    warn.get_by_role("button", name=words["howto"]).click()
    expect(warn).to_contain_text("- /mnt/nas/photos:/mnt/nas/photos")
    warn.get_by_role("button", name=words["use"]).click()
    sheet.get_by_role("button", name=words["create"]).click()
    expect(sheet).to_have_count(0)
    assert h.posted[-1]["folders"] == [{"path": "/mnt/nas/photos"}], h.posted
    browser.close()
    assert unhandled.report() == 0


def errors(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    h = Harness(page, FolderStub(host="up"), unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    sheet.get_by_role("button", name=words["create"]).click()
    expect(sheet.locator(".np-error")).to_contain_text(words["name_missing"])
    expect(page.locator("#project-name")).to_have_attribute("aria-invalid", "true")
    expect(sheet.get_by_role("button", name=words["create"])).to_be_disabled()
    page.locator("#project-name").fill("Bakery")
    h.fail_next = 1
    sheet.get_by_role("button", name=words["create"]).click()
    failed = sheet.locator(".np-failed")
    expect(failed).to_contain_text(words["kept"])
    # The draft survives a reload: the dialog opens again on what was typed.
    page.reload()
    page.wait_for_selector(".app")
    page.evaluate("window.dispatchEvent(new CustomEvent('daedalus:new-project', { detail: 'agents' }))")
    expect(page.locator("#project-name")).to_have_value("Bakery")
    h.fail_next = 1
    page.locator(".sheet.np-sheet").get_by_role("button", name=words["create"]).click()
    failed = page.locator(".sheet.np-sheet .np-failed")
    failed.get_by_role("button", name=words["retry"]).click()
    expect(page.locator(".sheet.np-sheet")).to_have_count(0)
    assert len(h.projects) == 1 and h.posted[-1] == h.posted[-2], "a retry sends the same request and makes one project"
    browser.close()
    assert unhandled.report() == 0


def native(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    h = Harness(page, FolderStub(native=True), unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("Native")
    expect(sheet.locator(".np-newpath")).to_contain_text("~/.daedalus/workspaces/native")
    h.existing(words)
    expect(sheet.locator(".fb-seg")).to_have_count(0)
    expect(sheet.locator(".fb-crumb").first).to_have_text("~")
    browser.close()
    assert unhandled.report() == 0


def orchestration(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    unhandled = Unhandled()
    h = Harness(page, FolderStub(host="up"), unhandled)
    page.goto(f"{BASE}/orchestration?token=t&lang={language}")
    page.get_by_role("button", name=words["orch"]).first.click()
    sheet = page.locator(".sheet.np-sheet.np-orch")
    expect(sheet).to_be_visible()
    page.locator("#project-name").fill("DevHub")
    expect(sheet.locator("[data-new-folder]")).to_have_attribute("data-new-folder", "devhub")
    page.locator("#project-start-goal").fill("One board for three repositories")
    page.locator("#project-start-constraints").fill("Review before merge")
    page.locator("#project-start-task").fill("List the repositories")
    page.locator("#project-start-checks").fill("All three are listed")
    sheet.get_by_role("radio", name="Assign a member later" if language == "en" else "Позже назначу участника").click()
    page.locator("#project-start-checks").press("Control+Enter")
    page.wait_for_url("**/project/po/board?task=t1*")
    start = h.starts[0]
    assert start["folder_name"] == "devhub" and "folder" not in start and start["owner_intent"] == "later", start
    # The plain dialog leads here rather than holding the goal fields itself.
    h.open(language)
    plain = page.locator(".sheet.np-sheet")
    expect(plain.locator("#project-start-goal")).to_have_count(0)
    plain.locator(".np-hint").click()
    expect(page.locator(".sheet.np-orch")).to_be_visible()
    browser.close()
    assert unhandled.report() == 0


def phone(playwright, language: str) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[language]
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    unhandled = Unhandled()
    h = Harness(page, FolderStub(host="up"), unhandled)
    h.open(language)
    sheet = page.locator(".sheet.np-sheet")
    page.locator("#project-name").fill("Phone project")
    assert fits(page), "the new folder overflows a phone"
    h.existing(words)
    sheet.locator(".fb-place.fav", has_text="projects").click()
    sheet.locator(".fb-row", has_text="esp32-door").click()
    assert fits(page), "the browser overflows a phone"
    box = sheet.locator(".fb-row", has_text="esp32-door").bounding_box()
    assert box and box["height"] >= 44, box
    create = sheet.get_by_role("button", name=words["create"])
    create.scroll_into_view_if_needed()
    create.click()
    expect(sheet).to_have_count(0)
    context.close()
    browser.close()
    assert unhandled.report() == 0


if __name__ == "__main__":
    expect_app(BASE)
    with sync_playwright() as pw:
        for lang in ("en", "ru"):
            for check in (new_folder, existing_folders, chat_folders, host_side, host_trouble, unmounted, errors, native, orchestration, phone):
                check(pw, lang)
                print(f"new project {check.__name__} {lang}: PASS")
