"""A project cap keeps one exact command across a lost reply and refreshes a stale project revision."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import (  # noqa: E402
    DEFAULT_APP,
    BoardStub,
    TeamStub,
    Unhandled,
    expect_app,
    folder,
    fulfil_shared,
    open_projects,
)

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = {
    "id": "p1", "entity_revision": 1, "name": "Bakery", "created_at": "2026-09-19T00:00:00Z", "system": "",
    "settings": {"snapshots": False, "system": "", "ephemeral": False, "default_env": "container"},
    "folders": [folder("/work/site", is_git=True)], "sessions": [],
}


def scenario(page: Page, language: str, width: int, unhandled: Unhandled) -> None:
    project = copy.deepcopy(PROJECT)
    board = BoardStub(project)
    team = TeamStub(project)
    state = {"cap": None, "lost": True, "conflict": True, "goal": 1, "unknown": False}
    sent: list[dict] = []
    receipts: dict[str, dict] = {}

    def answer(route, body: object, status: int = 200) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def balance(limit: str, *, uncertain: bool = False) -> dict:
        return {"limit_usd": limit, "spent_usd": None if state["unknown"] else "0.100000", "held_usd": "0.200000",
                "uncertain_usd": "0.100000" if uncertain else "0.000000",
                "available_usd": None if state["unknown"] else f"{float(limit) - 0.3:.6f}",
                "state": "unknown_usage" if state["unknown"] else "uncertain" if uncertain else "known"}

    def budget() -> dict:
        quote = {"model": "deepseek-flash", "reserve_usd": "0.324404", "input_bound": 1_048_576,
                 "output_bound": 8192}
        if not state["cap"]:
            return {"configured": False, "project_id": "p1", "goal_revision": state["goal"],
                    "entity_revision": project["entity_revision"], "coordinator_quote": quote}
        total, coordination = state["cap"]
        return {"configured": True, "project_id": "p1", "budget_id": "budget", "goal_revision": state["goal"],
                "current_goal_revision": state["goal"], "activated_goal_revision": 1,
                "entity_revision": project["entity_revision"], "total": balance(total, uncertain=True),
                "coordination": balance(coordination), "coordinator_quote": quote}

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        body = request.post_data_json if request.method in ("POST", "PATCH", "PUT", "DELETE") else None
        if path == "/api/projects" and request.method == "GET":
            return answer(route, [project])
        if path == "/api/projects/p1/budget" and request.method == "GET":
            return answer(route, budget())
        if path == "/api/projects/p1/budget" and request.method == "PUT":
            sent.append(body)
            operation_id = body["client_operation_id"]
            if operation_id in receipts:
                return answer(route, receipts[operation_id])
            if state["conflict"] and len(receipts) == 1:
                state["conflict"] = False
                project["entity_revision"] += 1
                return answer(route, {"detail": "project changed", "current_revision": project["entity_revision"]}, 409)
            assert body["expected_entity_revision"] == project["entity_revision"]
            assert body["expected_goal_revision"] == state["goal"]
            state["cap"] = (body["limit_usd"], body["coordination_limit_usd"])
            project["entity_revision"] += 1
            receipt = {**budget(), "receipt_id": operation_id}
            receipts[operation_id] = receipt
            if state["lost"]:
                state["lost"] = False
                return answer(route, {"detail": "response lost"}, 503)
            return answer(route, receipt)
        handled = board.answer(request.method, path, url.query, body) or team.answer(request.method, path, url.query, body)
        if handled is not None:
            status, payload = handled
            return answer(route, payload, status)
        if path == "/api/sessions":
            listed = {**project, "total": 0, "active": 0, "loops": 0, "last_message_at": ""}
            return answer(route, {"sessions": [], "projects": [listed]})
        if path == "/api/project-environments":
            return answer(route, {"local": "container", "available": ["container"], "host_bridge": False, "docker": True})
        if path == "/api/control/revisions":
            return answer(route, {"scope": {"kind": "global", "id": "global"}, "collection_revision": 1,
                                  "entity_revision": None})
        if path == "/api/projects/p1/workspace-archive":
            return answer(route, {"latest": None, "available": False})
        if path == "/api/settings":
            return answer(route, {"presets": {}, "model": {}})
        if fulfil_shared(route):
            return None
        unhandled.record(path)
        answer(route, [])

    page.route("**/api/**", stub)
    page.goto(f"{BASE}/project/p1/board?token=t&lang={language}")
    expect(page.locator(".project-budget-chip")).to_have_count(0)
    page.goto(f"{BASE}/agents?token=t&lang={language}")
    settings = "Settings for Bakery" if language == "en" else "Настройки: Bakery"
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    block = page.locator(".project-budget")
    expect(block).to_be_visible()
    block.locator("summary").click()
    expect(block).to_contain_text("$0.324404")
    block.locator("input").first.fill("0.250000")
    block.locator("input").last.fill("0.150000")
    expect(block).to_contain_text("below one coordinator call" if language == "en" else "ниже резерва для одного вызова")
    block.locator("input").first.fill("1.000000")
    block.locator("input").last.fill("0.500000")
    page.locator(".sheet-head button").click()
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    block.locator("summary").click()
    expect(block.locator("input").first).to_have_value("1.000000")
    expect(block.locator("input").last).to_have_value("0.500000")
    page.reload()
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    block.locator("summary").click()
    expect(block.locator("input").first).to_have_value("1.000000")
    assert page.evaluate("localStorage.getItem('daedalus.project.budget.draft.p1')")
    block.locator("button").last.click()
    page.reload()
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    retry = "Try again" if language == "en" else "Повторить"
    page.get_by_text(retry).click()
    expect(block.locator("summary")).to_contain_text("$0.700000")
    expect(block).to_contain_text("$0.100000")
    assert sent[0] == sent[1] and len(receipts) == 1
    page.wait_for_function("localStorage.getItem('daedalus.project.budget.draft.p1') === null")
    block.locator("summary").click()
    block.locator("input").first.fill("2.000000")
    state["cap"] = ("1.500000", "0.500000")
    state["goal"] = 2
    project["entity_revision"] += 1
    page.reload()
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    block.locator("summary").click()
    expect(block.locator("input").first).to_have_value("2.000000")
    expect(block).to_contain_text("goal or saved caps changed" if language == "en" else "Цель или сохранённые пределы изменились")
    expect(block.locator("button").last).to_be_disabled()
    block.get_by_role("button", name="Use current version" if language == "en" else "Учесть текущую версию").click()
    block.locator("button").last.click()
    review = "Read current version" if language == "en" else "Прочитать текущую версию"
    page.get_by_text(review).click()
    expect(block.locator("input").first).to_have_value("2.000000")
    block.locator("button").last.click()
    expect(block.locator("summary")).to_contain_text("$1.700000")
    assert sent[2]["client_operation_id"] != sent[3]["client_operation_id"]
    assert sent[3]["expected_entity_revision"] == sent[2]["expected_entity_revision"] + 1
    state["lost"] = True
    block.locator("input").first.fill("3.000000")
    block.locator("button").last.click()
    block.locator("input").first.fill("4.000000")
    page.reload()
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    block.locator("summary").click()
    expect(block.locator("input").first).to_have_value("4.000000")
    page.get_by_text(retry).click()
    expect(block.locator("summary")).to_contain_text("$2.700000")
    expect(block.locator("input").first).to_have_value("4.000000")
    assert sent[4] == sent[5]
    assert page.evaluate("localStorage.getItem('daedalus.project.budget.draft.p1')")
    state["unknown"] = True
    page.reload()
    open_projects(page)
    page.locator(f":is(.project-row, .projects-row) .iconbtn[aria-label='{settings}']").click()
    block.locator("summary").click()
    expect(block).to_contain_text("balance is unknown" if language == "en" else "остаток неизвестен")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate("Array.from(document.querySelectorAll('.project-budget input, .project-budget button')).filter(x => x.offsetParent !== null).every(x => x.getBoundingClientRect().right <= innerWidth && x.getBoundingClientRect().left >= 0)")
    page.goto(f"{BASE}/project/p1/board?token=t&lang={language}")
    if width < 600:
        expect(page.locator(".project-budget-chip")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.evaluate("document.querySelector('.project-budget-chip').getBoundingClientRect().right <= innerWidth")


def run() -> int:
    unhandled = Unhandled()
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            for width in (320, 390, 1440):
                page = browser.new_page(viewport={"width": width, "height": 560 if width == 320 else 844},
                                        is_mobile=width < 600, has_touch=width < 600)
                scenario(page, language, width, unhandled)
                page.close()
        browser.close()
    return unhandled.report()


if __name__ == "__main__":
    raise SystemExit(run())
