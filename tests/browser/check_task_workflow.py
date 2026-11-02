"""A task workflow can be created, rediscovered and approved on a phone or desktop."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, open_task_tool  # noqa: E402
from check_project_board import PID, board, serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


WORKFLOW = {"en": "Workflow", "ru": "Маршрут работы"}


def check(lang: str, width: int) -> None:
    stub = board()
    if lang == "en" and width == 390:
        stub.workflow_unknowns = 1
    unhandled = Unhandled()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        page = browser.new_page(viewport={"width": width, "height": 560 if width == 320 else 800})
        serve(page, stub, unhandled)
        page.goto(f"{BASE}/project/{PID}/board?token=t&lang={lang}")
        page.locator(".pcard.doing, .ph-row.doing", has_text="Checkout").first.click()
        sheet = page.locator(".sheet.pboard-sheet")
        # The workflow and the context wait in the task's menu, out of the task's text until chosen.
        expect(sheet.locator(".task-workflow-section")).to_have_count(0)
        expect(sheet.locator(".task-context")).to_have_count(0)
        context = open_task_tool(page, "Task context" if lang == "en" else "Контекст задачи", lang)
        expect(context.locator(".task-context")).to_have_count(1)
        context.locator(".ptask-tool-head button").click()
        expect(sheet.locator(".ptask-tool")).to_have_count(0)
        tool = open_task_tool(page, WORKFLOW[lang], lang)
        expect(tool.locator(".ptask-tool-head")).to_contain_text(WORKFLOW[lang])
        workflow = tool.locator(".task-workflow-section")
        workflow.locator("summary").click()
        workflow.get_by_role("button", name="Remove" if lang == "en" else "Убрать").first.click()
        workflow.get_by_role("button", name="Start workflow" if lang == "en" else "Запустить маршрут").click()
        expect(workflow.get_by_role("button", name="Approve this step" if lang == "en" else "Подтвердить этот шаг")).to_be_visible()
        page.reload()
        expect(page.locator(".sheet.pboard-sheet")).to_be_visible()
        workflow = open_task_tool(page, WORKFLOW[lang], lang).locator(".task-workflow-section")
        if lang == "en" and width == 390:
            expect(workflow).to_contain_text("Couldn't confirm the change was saved")
            workflow.get_by_role("button", name="Try again").last.click()
            expect(workflow).not_to_contain_text("Couldn't confirm the change was saved")
            assert len(stub.workflow_runs) == 1
        expect(workflow.get_by_role("button", name="Approve this step" if lang == "en" else "Подтвердить этот шаг")).to_be_visible()
        workflow.get_by_role("button", name="Approve this step" if lang == "en" else "Подтвердить этот шаг").click()
        expect(workflow).to_contain_text("Completed" if lang == "en" else "Завершён")
        workflow.get_by_role("button", name="Check if still valid" if lang == "en" else "Проверить, актуален ли").click()
        expect(workflow).to_contain_text("The earlier result still holds" if lang == "en" else "Прежний результат по-прежнему подходит")
        run = next(iter(stub.workflow_runs.values()))
        run["projection_current"] = False
        run["steps"][0]["source_current"] = False
        run["steps"][0]["current_input_digest"] = "c" * 64
        workflow.get_by_role("button", name="Try again" if lang == "en" else "Ещё раз").first.click()
        expect(workflow).to_contain_text("A source changed" if lang == "en" else "Источник изменился")
        workflow.get_by_role("button", name="Start workflow" if lang == "en" else "Запустить маршрут").click()
        expect(workflow.get_by_label("Earlier runs" if lang == "en" else "Предыдущие запуски")).to_be_visible()
        page.reload()
        expect(page.locator(".sheet.pboard-sheet")).to_be_visible()
        workflow = open_task_tool(page, WORKFLOW[lang], lang).locator(".task-workflow-section")
        expect(workflow.get_by_label("Earlier runs" if lang == "en" else "Предыдущие запуски")).to_have_value("workflow-2")
        workflow.get_by_label("Earlier runs" if lang == "en" else "Предыдущие запуски").select_option("workflow-1")
        expect(workflow).to_contain_text("A newer run replaced this one" if lang == "en" else "Этот запуск заменён новым")
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        assert unhandled.report() == 0
        browser.close()


if __name__ == "__main__":
    expect_app(BASE)
    for language in ("en", "ru"):
        for viewport in (320, 390, 1440):
            check(language, viewport)
            print(f"workflow {language} {viewport}: PASS")
