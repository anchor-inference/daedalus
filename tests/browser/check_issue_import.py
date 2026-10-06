"""Importing GitHub issues onto a project's board, in both languages, on a desktop and a phone.

The way in sits in the new-task sheet, beside making a card by hand. The import sheet opens with the
repository the project folder's remote names and reads its open issues at once; an issue already on
the board and one too long for a task cannot be ticked; a label narrows the listing under its fold;
the ticked issue is sent with its preview digest and the listing's collection revision, and the card
it becomes links back to its issue by number. Nothing scrolls sideways at 390 px.
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
PID = "9f3c2a1b7d40"
REPOSITORY = "bakery/shop"

WORDS = {
    "en": {"new": "New task", "open": "Import GitHub issues…", "title": "Import GitHub issues", "import": "Import 1",
           "linked": "Already on the board", "long": "Longer than a task allows", "label": "Only issues with a label", "show": "Show issues"},
    "ru": {"new": "Новая задача", "open": "Импортировать issues с GitHub…", "title": "Импорт issues с GitHub", "import": "Импортировать: 1",
           "linked": "Уже на доске", "long": "Длиннее, чем допускает задача", "label": "Только issues с меткой", "show": "Показать issues"},
}


def project() -> dict:
    return {"id": PID, "name": "Bakery", "folders": folders("/home/operator/work/bakery"), "created_at": "2026-09-20T00:00:00Z",
            "settings": {"snapshots": True, "system": "", "ephemeral": False}, "system": "", "sessions": []}


def board() -> BoardStub:
    linked = BoardStub.task("t-menu", "Seasonal menu page", project_id=PID,
                            issue={"repository": REPOSITORY, "number": 11, "state": "linked", "url": f"https://github.com/{REPOSITORY}/issues/11"})
    stub = BoardStub(project(), tasks=[linked])
    stub.issue_repository = REPOSITORY
    stub.github_issues = [
        {"number": 12, "title": "Checkout rejects a valid postcode", "labels": ["bug"]},
        {"number": 11, "title": "Seasonal menu page", "labels": ["feature"]},
        {"number": 10, "title": "Rewrite the whole ordering flow", "labels": ["feature"], "body_length": 5200},
    ]
    return stub


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, stub: BoardStub, unhandled: Unhandled) -> None:
    team = TeamStub(project())

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
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


def check(page: Page, lang: str, unhandled: Unhandled, where: str) -> None:
    words = WORDS[lang]
    stub = board()
    serve(page, stub, unhandled)
    page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
    existing = page.locator(".pcard", has_text="Seasonal menu page")
    expect(existing.locator(".pcard-issue")).to_have_text("#11")
    expect(existing.locator(".pcard-issue")).to_have_attribute("href", f"https://github.com/{REPOSITORY}/issues/11")

    page.get_by_role("button", name=words["new"]).first.click()
    page.locator(".pboard-sheet .pboard-import", has_text=words["open"]).click()
    sheet = page.locator(".sheet.issue-import")
    expect(sheet.locator("h3")).to_have_text(words["title"])
    expect(sheet.locator("#issues-repo")).to_have_value(REPOSITORY)
    rows = sheet.locator(".issue-row")
    expect(rows).to_have_count(3)
    expect(sheet.locator('.issue-row[data-issue="11"]')).to_contain_text(words["linked"])
    expect(sheet.locator('.issue-row[data-issue="11"] input')).to_be_disabled()
    expect(sheet.locator('.issue-row[data-issue="10"]')).to_contain_text(words["long"])
    expect(sheet.locator('.issue-row[data-issue="10"] input')).to_be_disabled()
    apply = sheet.locator(".sheet-foot .btn.primary")
    expect(apply).to_be_disabled()
    fits(page, f"{lang} {where} listing")

    # The label sits under a fold, and narrows the listing the host reads.
    sheet.locator("summary", has_text=words["label"]).click()
    sheet.locator("#issues-label").fill("bug")
    sheet.get_by_role("button", name=words["show"]).click()
    expect(rows).to_have_count(1)
    assert stub.issue_lists[-1]["label"] == "bug", stub.issue_lists

    sheet.locator('.issue-row[data-issue="12"] input').check()
    expect(apply).to_have_text(words["import"])
    apply.click()
    expect(sheet).to_have_count(0)
    sent = stub.issue_imports[-1]
    assert sent["repository"] == REPOSITORY and sent["expected_collection_revision"] == 2, sent
    assert sent["items"] == [{"issue_number": 12, "action": "import", "preview_digest": f"{12:064d}", "expected_entity_revision": None}], sent
    assert sent["client_operation_id"], sent
    card = page.locator(".pcard", has_text="Checkout rejects a valid postcode")
    expect(card.locator(".pcard-issue")).to_have_text("#12")
    fits(page, f"{lang} {where} board")


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            check(context.new_page(), lang, unhandled, "desktop")
            context.close()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            check(context.new_page(), lang, unhandled, "phone")
            context.close()
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
