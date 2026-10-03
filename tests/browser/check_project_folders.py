"""Manage a project's folders from its settings sheet, on a phone and on a desktop, in both languages.

A folder is added (with the Docker mount warning for a container folder, and the host sentence for a
host one), locked read-only, and refused removal with the host's own sentence naming the agent in
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
from api_stub import DEFAULT_APP, Unhandled, expect_app, folder, fulfil_shared  # noqa: E402

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
    "en": {"projects": "Projects", "settings": "Settings for Bakery", "remove": "Remove", "readonly": "Agents read it and never write it.", "unmounted": "Not mounted in the container yet", "terminals": "only CLI agents and host terminals reach it", "mount": "the bot sees a folder only once it is mounted", "host": "A host folder is worked in through the host terminal"},
    "ru": {"projects": "Проекты", "settings": "Настройки: Bakery", "remove": "Убрать", "readonly": "Агенты читают её и никогда не пишут.", "unmounted": "Ещё не смонтирована в контейнер", "terminals": "её видят только CLI-агенты и терминалы хоста", "mount": "бот видит папку, только когда она смонтирована", "host": "С папкой на хосте работают через терминал хоста"},
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
        if path == "/api/project-directories":
            return answer(route, {"roots": [{"name": "work", "path": "/work", "readable": True, "writable": True, "project_id": None}], "docker": True})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    # The switcher is the chip in the sidebar on a desktop and a button over the chats on a phone.
    page.locator(f".project-chip:visible, .start-list-head .iconbtn[aria-label='{words['projects']}']:visible").first.click()
    # By its name: the row carries the team button as well, before the settings one.
    page.locator(f".project-row .iconbtn[aria-label='{words['settings']}']").click()
    rows = page.locator(".dir-row")
    expect(rows).to_have_count(2)
    docs = page.locator(".dir-row[data-folder='f-docs']")

    # Read-only: the switch sends the lock and the sentence under the folder says what it means.
    docs.locator(".dir-lock input").click()
    page.reload()
    page.locator(f".project-chip:visible, .start-list-head .iconbtn[aria-label='{words['projects']}']:visible").first.click()
    page.locator(f".project-row .iconbtn[aria-label='{words['settings']}']").click()
    expect(page.get_by_text("Retry original request" if lang == "en" else "Повторить исходный запрос")).to_be_visible()
    page.get_by_text("Retry original request" if lang == "en" else "Повторить исходный запрос").click()
    expect(docs.locator(".dir-reach")).to_have_text(words["readonly"])
    locks = [body for method, path, body in sent if method == "PATCH" and path == "/api/projects/p1/folders/f-docs"]
    assert len(locks) == 2 and locks[0] == locks[1] and locks[0]["readonly"] is True
    assert locks[0]["expected_entity_revision"] == 1 and locks[0]["client_operation_id"], sent

    # Removal of a folder an agent works in: refused, and the refusal names the agent.
    docs.locator(".dir-head .iconbtn").click()
    page.locator(".dialog[role='alertdialog'] button", has_text=words["remove"]).last.click()
    expect(page.locator(".toast")).to_contain_text(BUSY)
    expect(rows).to_have_count(2)

    # A container folder in Docker: the mount warning before it is added, the unmounted sentence after.
    page.locator(".dir-add-open").click()
    expect(page.locator("#folder-env")).to_be_visible()
    expect(page.locator(".dir-mount")).to_contain_text(words["mount"])
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/add-{name}.png")
    page.locator("#project-root").fill("/work/assets")
    page.locator("#folder-label").fill("Assets")
    page.locator(".dir-form-foot .btn.primary").click()
    expect(rows).to_have_count(3)
    assets = page.locator(".dir-row[data-folder='f-assets']")
    expect(assets.locator(".dir-reach")).to_contain_text(words["unmounted"])
    posted = [b for m, p, b in sent if m == "POST"]
    assert all(posted[-1][key] == value for key, value in {"path": "/work/assets", "label": "Assets", "env": "container", "readonly": False}.items())
    assert posted[-1]["expected_entity_revision"] == 2 and posted[-1]["client_operation_id"], posted

    # A host folder: no mount warning, the host sentence instead, and only terminals reach it.
    page.locator(".dir-add-open").click()
    page.locator("#folder-env").select_option("host")
    expect(page.locator(".dir-mount")).to_have_count(0)
    expect(page.locator(".dir-form")).to_contain_text(words["host"])
    page.locator("#project-root").fill("/srv/tools")
    page.locator(".dir-form-foot .btn.primary").click()
    expect(rows).to_have_count(4)
    expect(page.locator(".dir-row[data-folder='f-tools'] .dir-reach")).to_contain_text(words["terminals"])

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
    # The site, the docs and the unmounted assets folder; the host folder is not an agent's to work in.
    expect(choice.locator("option")).to_have_count(3)
    expect(choice.locator("option[disabled]")).to_have_count(1)
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/newagent-{name}.png")


if __name__ == "__main__":
    raise SystemExit(run())
