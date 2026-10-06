"""Distinct task decisions and the current budget remain visible in the compact inbox."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, expect_app  # noqa: E402
from check_project_phone import serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PROJECT = "b4k3ry20f0c5"


def balance(available: str | None, state: str = "known") -> dict:
    return {"limit_usd": "2.000000", "spent_usd": None if state == "unknown_usage" else "2.000000",
            "held_usd": "0.000000", "uncertain_usd": "0.000000", "available_usd": available,
            "state": state}


def run() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (320, 390, 1440):
                context = browser.new_context(viewport={"width": width, "height": 720},
                                              is_mobile=width < 600, has_touch=width < 600)
                page = context.new_page()
                focus = FocusStub.bakery(lang)
                focus.ask_from_ira(lang)
                checkout = next(task for task in focus.board.tasks if task["id"] == "t-checkout")
                checkout["contract_revision"] = 1
                review = next(task for task in focus.board.tasks if task["id"] == "t-endpoint")
                review["acceptance_state"] = "accepted"
                review["contract_revision"] = 1
                focus.next_actions[PROJECT] = [
                    {"action_id": "same-ask", "task_id": "t-checkout", "contract_revision": 1,
                     "kind": "answer_question", "owner_kind": "operator", "context_ref": "ask-ira",
                     "enabled": True, "blockers": []},
                    {"action_id": "other-input", "task_id": "t-checkout", "contract_revision": 1,
                     "kind": "provide_input", "owner_kind": "operator", "context_ref": None,
                     "enabled": True, "blockers": []},
                    {"action_id": "assign-checkout", "task_id": "t-checkout", "contract_revision": 1,
                     "kind": "assign", "owner_kind": "operator", "context_ref": "ask-ira",
                     "enabled": True, "blockers": []},
                    {"action_id": "blocked-review", "task_id": "t-checkout", "contract_revision": 0,
                     "kind": "review", "owner_kind": "operator", "context_ref": None,
                     "enabled": False, "blockers": ["stale_contract", "result_not_verified"]},
                    {"action_id": "same-review", "task_id": "t-endpoint", "contract_revision": 1,
                     "kind": "review", "owner_kind": "operator", "context_ref": None,
                     "enabled": True, "blockers": []},
                ]
                focus.budget_views[PROJECT] = {"configured": True, "project_id": PROJECT,
                    "entity_revision": 1, "goal_revision": 1, "budget_id": "goal-budget",
                    "total": balance("0.000000"), "coordination": balance("1.000000")}
                serve(page, focus)
                page.goto(f"{BASE}/orchestration/project/{PROJECT}/attention?token=t&lang={lang}")
                cards = page.locator(".focus-attention-item")
                expect(cards).to_have_count(7)
                if width < 600:
                    expect(page.locator("nav.project-tabs button[data-tab='more'] .tab-badge")).to_have_text("7")
                else:
                    expect(page.locator("nav.project-sidebar a.focus-row[href$='/attention'] .focus-row-meta")).to_have_text("7")
                assert not page.get_by_text("same-ask").count()
                expect(cards.filter(has_text="Input needed" if lang == "en" else "Нужны данные")).to_have_count(1)
                expect(cards.filter(has_text="Assignment needed" if lang == "en" else "Нужно назначение")).to_have_count(1)
                blocked = cards.filter(has_text="task requirements changed" if lang == "en" else "условия задачи изменились")
                expect(blocked).to_have_count(1)
                expect(blocked).to_contain_text("result is not verified" if lang == "en" else "результат не проверен")
                expect(blocked).not_to_contain_text("Review needed" if lang == "en" else "Нужна проверка")
                expect(blocked.get_by_role("button", name="Open task" if lang == "en" else "Открыть задачу")).to_be_visible()
                expect(cards.filter(has_text="Result to review" if lang == "en" else "Результат на проверке")).to_have_count(1)
                budget = cards.filter(has_text="No budget remains" if lang == "en" else "Бюджет исчерпан")
                expect(budget).to_have_count(1)
                budget.get_by_role("button", name="Review budget" if lang == "en" else "Проверить бюджет").click()
                expect(page.locator(".project-budget summary")).to_be_visible()
                page.keyboard.press("Escape")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (lang, width)

                next(ask for ask in focus.asks if ask["id"] == "ask-ira")["resolved_at"] = "2026-01-01T00:00:00Z"
                page.reload()
                expect(cards).to_have_count(7)
                expect(cards.filter(has_text="Answer needed" if lang == "en" else "Нужен ответ")).to_have_count(1)

                review["status"] = "done"
                review["acceptance_state"] = "operator_approved"
                focus.next_actions[PROJECT] = [row for row in focus.next_actions[PROJECT]
                                               if row["action_id"] != "same-review"]
                page.reload()
                expect(cards.filter(has_text="Result to review" if lang == "en" else "Результат на проверке")).to_have_count(0)

                focus.budget_views[PROJECT]["total"] = balance(None, "unknown_usage")
                page.reload()
                expect(cards.filter(has_text="Available budget is unknown" if lang == "en" else "Доступный бюджет неизвестен")).to_have_count(1)
                focus.budget_read_error.add(PROJECT)
                page.reload()
                expect(page.locator(".focus-attention-warning").first).to_be_visible()
                if width < 600:
                    expect(page.locator("nav.project-tabs button[data-tab='more'] .tab-badge")).to_have_text("?")
                else:
                    expect(page.locator("nav.project-sidebar a.focus-row[href$='/attention'] .focus-row-meta")).to_have_text("?")
                    expect(page.locator("nav.project-sidebar a.focus-row[href$='/attention'] .focus-row-meta"))\
                        .to_have_attribute("title", "Decision count unconfirmed" if lang == "en"
                                           else "Число решений не подтверждено")
                expect(page.get_by_text("No decision needs you now" if lang == "en" else "Сейчас решений от вас не требуется")).to_have_count(0)
                context.close()
                print(f"attention {lang} {width}: ok")
        browser.close()


if __name__ == "__main__":
    run()
