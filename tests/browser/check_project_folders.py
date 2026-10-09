"""Manage a project's folders from its settings sheet, on a phone and on a desktop, in both languages.

A folder is added through the folder browser (with the mount line for a container folder that is not
mounted, and the host mark for a host one), locked read-only, and refused removal with the host's own sentence naming the agent in
it. The new-agent form offers the project's folders once there is more than one to choose from.
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

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")
"""Where to leave a picture of the sheet at each width and language, to look at; nothing when unset."""

ENVIRONMENTS = {"local": "container", "available": ["container", "host"], "host_bridge": True, "docker": True}
BUSY = "Writer works in /work/docs; move or remove it first"
PROJECT = {
    "id": "p1", "entity_revision": 1, "name": "Bakery", "created_at": "2026-09-19T00:00:00Z", "system": "",
    "settings": {"snapshots": False, "system": "", "ephemeral": False, "default_env": "container"},
    "folders": [folder("/work/site", is_git=True), folder("/work/docs", position=1, label="Docs")],
    "sessions": [{"id": "s1", "title": "Writer", "running": False}],
}
WORDS = {
    "en": {"projects": "Projects", "settings": "Settings for Bakery", "remove": "Remove", "readonly": "Agents read it and never write it.", "unmounted": "Not mounted in the container yet", "terminals": "every agent reaches it through the host terminal daemon", "mount": "Without the launcher this takes one line in the compose file", "host": "agents' commands will run on the machine"},
    "ru": {"projects": "Проекты", "settings": "Настройки: Bakery", "remove": "Убрать", "readonly": "Агенты читают её и никогда не пишут.", "unmounted": "Ещё не смонтирована в контейнер", "terminals": "все агенты работают с ней через демон терминала хоста", "mount": "Без лаунчера это одна строка в compose-файле", "host": "команды агентов пойдут на машине"},
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
    state = {"project": copy.deepcopy(PROJECT), "lost_lock_reply": True, "project_conflict": True}
    sent: list[tuple[str, str, object]] = []
    folder_stub = FolderStub(host="up")
    lock_receipts: dict[str, tuple[dict, dict]] = {}

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        project = state["project"]
        body = request.post_data_json if request.method in ("POST", "PATCH", "PUT", "DELETE") else None
        if path == "/api/projects":
            return answer(route, [project])
        if path == "/api/projects/p1" and request.method == "PATCH":
            sent.append(("PATCH", path, body))
            if state["project_conflict"]:
                state["project_conflict"] = False
                project["entity_revision"] += 1
                return answer(route, {"detail": "project changed", "current_revision": project["entity_revision"]}, 409)
            assert body["expected_entity_revision"] == project["entity_revision"] and body["client_operation_id"]
            project["name"] = body["name"]
            project["settings"]["snapshots"] = body["snapshots"]
            project["entity_revision"] += 1
            return answer(route, {"project_id": "p1", "change": "settings", "receipt_id": body["client_operation_id"], "entity_revision": project["entity_revision"]})
        if path == "/api/project-environments":
            return answer(route, ENVIRONMENTS)
        if path == "/api/projects/p1/folders" and request.method == "POST":
            sent.append(("POST", path, body))
            assert body["expected_entity_revision"] == project["entity_revision"] and body["client_operation_id"]
            host = body.get("env") == "host"
            added = folder(body["path"], position=len(project["folders"]), label=body.get("label", ""), env=body.get("env", "container"),
                           reach="terminals" if host else "agents", reachable=False, readonly=bool(body.get("readonly")))
            project["folders"].append(added)
            project["entity_revision"] += 1
            return answer(route, {"project_id": "p1", "change": "folders", "folder_id": added["id"], "receipt_id": body["client_operation_id"], "entity_revision": project["entity_revision"]})
        if path.startswith("/api/projects/p1/folders/"):
            fid = path.rsplit("/", 1)[-1]
            sent.append((request.method, path, body))
            if request.method == "DELETE":
                return answer(route, {"detail": BUSY}, 409)
            previous = lock_receipts.get(body["client_operation_id"])
            if previous:
                original, receipt = previous
                return answer(route, receipt if original == body else {"detail": "intent changed"}, 200 if original == body else 409)
            assert body["expected_entity_revision"] == project["entity_revision"] and body["client_operation_id"]
            for each in project["folders"]:
                if each["id"] == fid and "readonly" in body:
                    each["readonly"] = body["readonly"]
                    each["writable"] = not body["readonly"]
            project["entity_revision"] += 1
            receipt = {"project_id": "p1", "change": "folders", "folder_id": fid, "receipt_id": body["client_operation_id"], "entity_revision": project["entity_revision"]}
            lock_receipts[body["client_operation_id"]] = body, receipt
            if state["lost_lock_reply"]:
                state["lost_lock_reply"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, receipt)
        if path == "/api/sessions":
            listed = {**project, "total": 1, "active": 0, "loops": 0, "last_message_at": ""}
            return answer(route, {"sessions": [], "projects": [listed]})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if folder_stub.fulfil(route):
            return None
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    # The switcher is the chip in the sidebar on a desktop and a button over the chats on a phone.
    open_projects(page)
    # By its name: the row carries the team button as well, before the settings one.
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{words['settings']}']").click()
    rows = page.locator(".dir-row")
    expect(rows).to_have_count(2)
    docs = page.locator(".dir-row[data-folder='f-docs']")

    # Read-only: the switch sends the lock and the sentence under the folder says what it means.
    docs.locator(".dir-lock input").click()
    page.reload()
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{words['settings']}']").click()
    expect(page.get_by_text("Try again" if lang == "en" else "Повторить")).to_be_visible()
    page.get_by_text("Try again" if lang == "en" else "Повторить").click()
    expect(docs.locator(".dir-reach")).to_have_text(words["readonly"])
    locks = [body for method, path, body in sent if method == "PATCH" and path == "/api/projects/p1/folders/f-docs"]
    assert len(locks) == 2 and locks[0] == locks[1] and locks[0]["readonly"] is True
    assert locks[0]["expected_entity_revision"] == 1 and locks[0]["client_operation_id"], sent

    # Removal of a folder an agent works in: refused, and the refusal names the agent.
    docs.locator(".dir-head .iconbtn").click()
    page.locator(".dialog[role='alertdialog'] button", has_text=words["remove"]).last.click()
    expect(page.locator(".toast")).to_contain_text(BUSY)
    expect(rows).to_have_count(2)

    # A container folder in Docker that is not mounted: the browser says so and how to mount it before
    # it is added, and the folder's own sentence says it after.
    page.locator(".dir-add-open").click()
    form = page.locator(".dir-form")
    expect(form.locator(".fb")).to_be_visible()
    form.locator(".fb-crumb-edit").click()
    form.locator("input.fb-crumbs").fill("/work/assets")
    form.locator("input.fb-crumbs").press("Enter")
    expect(form.locator(".fb-warn")).to_contain_text(words["mount"])
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/add-{name}.png")
    form.locator(".fb-warn .btn").first.click()
    form.locator(".fb-warn .btn").first.click()
    page.locator("#folder-label").fill("Assets")
    page.locator(".dir-form-foot .btn.primary").click()
    expect(rows).to_have_count(3)
    assets = page.locator(".dir-row[data-folder='f-assets']")
    expect(assets.locator(".dir-reach")).to_contain_text(words["unmounted"])
    posted = [b for m, p, b in sent if m == "POST"]
    assert all(posted[-1][key] == value for key, value in {"path": "/work/assets", "label": "Assets", "env": "container", "readonly": False}.items())
    assert posted[-1]["expected_entity_revision"] == 2 and posted[-1]["client_operation_id"], posted

    # A host folder: chosen on the machine's side, marked HOST, and only terminals reach it.
    page.locator(".dir-add-open").click()
    form.locator(".fb-seg button").nth(1).click()
    form.locator(".fb-row", has_text="projects").dblclick()
    form.locator(".fb-row", has_text="stream-cuts").click()
    expect(form.locator(".fb-foot")).to_contain_text(words["host"])
    page.locator(".dir-form-foot .btn.primary").click()
    expect(rows).to_have_count(4)
    expect(page.locator(".dir-row[data-folder='f-stream-cuts'] .dir-reach")).to_contain_text(words["terminals"])
    assert [b for m, p, b in sent if m == "POST"][-1]["env"] == "host"

    if SHOTS:
        page.locator(".dir-row").last.scroll_into_view_if_needed()
        page.screenshot(path=f"{SHOTS}/folders-{name}.png")
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - window.innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, f"the sheet scrolls sideways: {overflow}"

    # A stale project revision keeps the operator's rename draft and requires a fresh read.
    page.locator("#project-rename").fill("Bakery revised")
    page.get_by_role("button", name="Save" if lang == "en" else "Сохранить", exact=True).click()
    expect(page.locator("#project-rename")).to_have_value("Bakery revised")
    page.get_by_text("Read current version" if lang == "en" else "Прочитать текущую версию").click()
    page.get_by_role("button", name="Save" if lang == "en" else "Сохранить", exact=True).click()
    expect(page.locator("#project-rename")).to_have_count(0)
    patches = [body for method, path, body in sent if method == "PATCH" and path == "/api/projects/p1"]
    assert len(patches) == 2 and patches[0]["expected_entity_revision"] == 4 and patches[1]["expected_entity_revision"] == 5
    assert patches[0]["client_operation_id"] != patches[1]["client_operation_id"] and state["project"]["name"] == "Bakery revised", patches

    # The new-agent form: the project now has several folders an agent can work in, so it offers them.
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    page.goto(f"{BASE}/agents?token=t&lang={lang}&new=1")
    page.locator(".sheet select.field").nth(1).select_option("p1")
    choice = page.locator("#newagent-folder")
    expect(choice).to_be_visible()
    # The site, the docs, the unmounted assets folder and the host folder, which a Daedalus agent now
    # works in through the host terminal daemon.
    expect(choice.locator("option")).to_have_count(4)
    # Unmounted, and the host folder while this installation has no host terminal daemon.
    expect(choice.locator("option[disabled]")).to_have_count(2)
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/newagent-{name}.png")


if __name__ == "__main__":
    raise SystemExit(run())
