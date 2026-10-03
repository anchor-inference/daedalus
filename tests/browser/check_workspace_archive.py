"""The collapsed project archive flow remains usable on desktop and phone in both languages."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folder, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = {
    "id": "p1", "entity_revision": 1, "name": "Bakery", "created_at": "2026-09-19T00:00:00Z", "system": "",
    "settings": {"snapshots": False, "system": "", "ephemeral": False, "default_env": "container"},
    "folders": [folder("/work/site", label="Site")], "sessions": [],
}
WORDS = {
    "en": {"projects": "Projects", "settings": "Settings for Bakery", "archive": "Move this workspace",
           "export": "Prepare private ZIP", "retry": "Try again", "download": "Download ZIP", "restore": "Restore as new project"},
    "ru": {"projects": "Проекты", "settings": "Настройки: Bakery", "archive": "Перенести рабочее пространство",
           "export": "Подготовить приватный ZIP", "retry": "Ещё раз", "download": "Скачать ZIP", "restore": "Восстановить как новый проект"},
}


def run() -> int:
    unhandled = Unhandled()
    failures = []
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height, mobile in ((320, 560, True), (390, 844, True), (1440, 900, False)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(5000)
                try:
                    scenario(page, lang, unhandled)
                    print(f"ok {lang} {width}")
                except Exception as exc:  # noqa: BLE001 — report each viewport independently
                    failures.append(f"{lang} {width}: {exc}")
                    print(f"FAILED {lang} {width}: {exc}")
                context.close()
        browser.close()
    return 1 if failures else unhandled.report()


def scenario(page: Page, lang: str, unhandled: Unhandled) -> None:
    words = WORDS[lang]
    project = copy.deepcopy(PROJECT)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("workspace.json", "{}")
    state: dict[str, object] = {"lost": True, "sent": [], "exports": {}, "applied": []}

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        method = request.method
        body = request.post_data_json if method == "POST" and path != "/api/import/archive" else None
        if path == "/api/projects" and method == "GET":
            return answer(route, [project])
        if path == "/api/sessions" and method == "GET":
            return answer(route, {"sessions": [], "projects": [{**project, "total": 0, "active": 0, "loops": 0, "last_message_at": ""}]})
        if path == "/api/project-environments" and method == "GET":
            return answer(route, {"local": "container", "available": ["container"], "host_bridge": False, "docker": True})
        if path == "/api/control/revisions" and method == "GET":
            return answer(route, {"scope": {"kind": "global", "id": "global"}, "collection_revision": 1, "entity_revision": None})
        if path == "/api/projects/p1/workspace-archive" and method == "GET":
            exports = state["exports"]
            latest = next(reversed(exports.values()), None) if isinstance(exports, dict) and exports else None
            return answer(route, {"latest": latest, "available": latest is not None})
        if path == "/api/projects/p1/workspace-archive" and method == "POST":
            state["sent"].append(body)
            exports = state["exports"]
            assert isinstance(exports, dict) and isinstance(body, dict)
            key = body["client_operation_id"]
            if key not in exports:
                assert body["expected_entity_revision"] == 1
                assert body["selected_paths"] == [{"folder_id": "f-site", "path": "notes/draft.txt"}]
                exports[key] = {"receipt_id": "receipt-export", "archive_artifact_id": "a" * 64,
                                "archive_digest": "a" * 64, "format_version": 2, "size_bytes": len(archive.getvalue()),
                                "selected_files": body["selected_paths"], "row_counts": {"projects": 1}, "private": True}
                if state["lost"]:
                    state["lost"] = False
                    return answer(route, {"detail": "response lost"}, 503)
            return answer(route, exports[key])
        if path == "/api/import/archive/" + "a" * 64 and method == "GET":
            return route.fulfill(status=200, content_type="application/vnd.daedalus.workspace+zip", body=archive.getvalue())
        if path == "/api/import/archive" and method == "POST":
            assert request.post_data_buffer == archive.getvalue()
            return answer(route, {"archive_artifact_id": "a" * 64, "private": True, "preview": {
                "valid": True, "archive_digest": "a" * 64, "format_version": 2, "row_counts": {"projects": 1},
                "selected_files": [{"folder_id": "f-site", "path": "notes/draft.txt"}], "selected_file_count": 1,
                "capability_handles": ["board.read"],
                "collision": False, "conflicts": [], "missing_secrets": ["provider credentials"],
                "reconnect_required": ["provider credentials"], "restores_runtime": False}})
        if path == "/api/import/apply" and method == "POST":
            state["applied"].append(body)
            assert isinstance(body, dict) and body["archive_artifact_id"] == "a" * 64
            assert body["expected_collection_revision"] == 1 and body["client_operation_id"]
            return answer(route, {"receipt_id": "receipt-import", "project_id": "restored", "archive_digest": "a" * 64, "runtime_state": "inactive"})
        if path == "/api/settings" and method == "GET":
            return answer(route, {"presets": {}, "model": {}})
        if path == "/api/project-directories" and method == "GET":
            return answer(route, {"roots": [], "docker": True})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    page.locator(f".project-chip:visible, .start-list-head .iconbtn[aria-label='{words['projects']}']:visible").first.click()
    page.locator(f".project-row .iconbtn[aria-label='{words['settings']}']").click()
    section = page.locator(".sheet-section", has=page.get_by_text(words["archive"])).last
    if section.get_attribute("open") is None:
        section.locator("summary").first.click()
    section.get_by_text("Include files from folders" if lang == "en" else "Добавить файлы из папок").click()
    section.locator("#archive-relative-path").fill("notes/draft.txt")
    section.get_by_role("button", name="Include file" if lang == "en" else "Добавить файл").click()
    section.get_by_role("button", name=words["export"]).click()
    expect(section.get_by_text("The last command's result is unknown." if lang == "en" else "Результат последней команды неизвестен.", exact=False)).to_be_visible()
    page.reload()
    page.locator(f".project-chip:visible, .start-list-head .iconbtn[aria-label='{words['projects']}']:visible").first.click()
    page.locator(f".project-row .iconbtn[aria-label='{words['settings']}']").click()
    section = page.locator(".sheet-section", has=page.get_by_text(words["archive"])).last
    if section.get_attribute("open") is None:
        section.locator("summary").first.click()
    expect(section).to_have_attribute("open", "")
    section.get_by_role("button", name=words["retry"]).click()
    expect(section.get_by_role("button", name=words["download"])).to_be_visible()
    sent = state["sent"]
    assert isinstance(sent, list) and len(sent) == 2 and sent[0] == sent[1]
    with page.expect_download() as download:
        section.get_by_role("button", name=words["download"]).click()
    assert download.value.suggested_filename == "Bakery.zip"
    upload_file = {"name": "workspace.zip", "mimeType": "application/zip", "buffer": archive.getvalue()}
    section.locator("#archive-import").set_input_files(upload_file)
    expect(section.get_by_role("button", name=words["restore"])).to_be_visible()
    section.get_by_role("button", name=words["restore"]).click()
    expect(section.get_by_text("Restored as a new inactive project." if lang == "en" else "Создан новый неактивный проект.", exact=False)).to_be_visible()
    expect(section.get_by_role("button", name="Open restored project" if lang == "en" else "Открыть восстановленный проект")).to_be_visible()
    assert len(state["applied"]) == 1
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, overflow


if __name__ == "__main__":
    raise SystemExit(run())
