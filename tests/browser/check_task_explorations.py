"""A card raced by competing attempts shows each attempt's state and cost and the one kept, in both
languages and at phone and desktop widths, without widening the sheet."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, BoardStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = {"id": "p1", "name": "Bakery", "entity_revision": 1, "folders": folders("/home/operator/work/bakery"),
           "created_at": "2026-09-20T00:00:00Z", "settings": {"snapshots": False, "default_env": "container"}, "system": "", "sessions": []}
EXPLORATION = {
    "id": "x1a2b3c4d5e", "task_id": "task", "state": "chosen", "reason": "every attempt has reported", "winner_slot": 2,
    "decision_reason": "The second keeps the old layout.", "budget_usd": "2.000000", "deadline_at": "2026-10-15T12:00:00+00:00",
    "created_at": "2026-10-15T10:00:00+00:00",
    "attempts": [
        {"slot": 1, "state": "lost", "staff_name": "Ira-1a2b-1", "harness": "daedalus", "model": "", "task_id": "t-a1",
         "forked": True, "usd": 0.4234, "unpriced": False},
        {"slot": 2, "state": "won", "staff_name": "Ira-1a2b-2", "harness": "claude", "model": "opus", "task_id": "t-a2",
         "forked": False, "usd": 0.0, "unpriced": True},
    ],
}
WORDS = {
    "en": {"title": "Competing attempts", "state": "Kept attempt 2", "won": "kept", "lost": "not kept", "unpriced": "and unpriced use"},
    "ru": {"title": "Конкурирующие попытки", "state": "Оставлена попытка 2", "won": "оставлена", "lost": "не оставлена", "unpriced": "неоценённый расход"},
}


def run() -> int:
    unhandled = Unhandled()
    failures = []
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width, height, mobile in ((320, 560, True), (1440, 900, False)):
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(6000)
                try:
                    scenario(page, language, unhandled)
                    print(f"ok {language} {width}")
                except Exception as exc:  # noqa: BLE001 — retain every viewport result
                    failures.append(f"{language} {width}: {exc}")
                    print(f"FAILED {language} {width}: {exc}")
                context.close()
        browser.close()
    return 1 if failures else unhandled.report()


def scenario(page: Page, language: str, unhandled: Unhandled) -> None:
    words = WORDS[language]
    staff = [{"id": "one", "name": "Ira", "color": "orange", "harness": "daedalus", "isolation": "worktree"}]
    task = BoardStub.task("task", "Price list", project_id="p1", status="dropped",
                          brief={"objective": "Prices", "deliverable": "A list", "boundaries": "One file", "done_when": "Committed"})
    task["explorations"] = [EXPLORATION]
    board = BoardStub(PROJECT, staff=staff, tasks=[task])

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PATCH") and request.post_data else None
        if path == "/api/projects" and request.method == "GET":
            return answer(route, [PROJECT])
        if path == "/api/projects/p1/staff" and request.method == "GET":
            return answer(route, {"project": {"id": "p1", "concurrency": 2, "concurrency_cap": 10, "orchestrator": True}, "staff": staff})
        if path in ("/api/projects/p1/wakeups",) and request.method == "GET":
            return answer(route, [])
        if path == "/api/projects/p1/watches" and request.method == "GET":
            return answer(route, {"watches": [], "max": 20, "min_cooldown_minutes": 1, "providers": [],
                                  "collection_revision": 1, "project_entity_revision": 1})
        if path == "/api/sessions" and request.method == "GET":
            return answer(route, {"sessions": [], "projects": []})
        if path == "/api/settings" and request.method == "GET":
            return answer(route, {"presets": {}, "model": {}})
        known = board.answer(request.method, path, url.query, body)
        if known is not None:
            return answer(route, known[1], known[0])
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/project/p1/board?task=task&token=t&lang={language}")
    section = page.locator("section.explorations").first
    expect(section).to_contain_text(words["title"])
    expect(section).to_contain_text(words["state"])
    expect(section.locator("li.won")).to_contain_text("Ira-1a2b-2")
    expect(section.locator("li.won")).to_contain_text(words["unpriced"])
    expect(section.locator("li").first).to_contain_text("$0.42")
    expect(section.locator("li").first).to_contain_text(words["lost"])
    overflow = page.evaluate("() => { const s = document.querySelector('.sheet'); return [document.documentElement.scrollWidth - innerWidth, s ? s.scrollWidth - s.clientWidth : 0]; }")
    assert overflow[0] <= 0 and overflow[1] <= 1, overflow


if __name__ == "__main__":
    raise SystemExit(run())
