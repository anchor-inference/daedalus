"""A task workflow can be created, rediscovered and approved on a phone or desktop."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app  # noqa: E402
from check_project_board import PID, board, serve  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


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
        page.locator(".pcard.doing", has_text="Checkout").first.click()
        sheet = page.locator(".sheet.pboard-sheet")
        workflow = sheet.locator(".task-workflow-section")
        expect(workflow).to_have_count(1)
        context = sheet.locator(".task-context")
        expect(context).to_have_count(1)
        context.locator("button").first.click()
        expect(context.locator("button").first).to_have_attribute("aria-expanded", "true")
        context.locator("button").first.click()
        workflow.locator("button").first.click()
        expect(workflow).to_contain_text("Workflow" if lang == "en" else "Маршрут работы")
        workflow.locator("summary").click()
        workflow.get_by_role("button", name="Remove" if lang == "en" else "Убрать").first.click()
        workflow.get_by_role("button", name="Start workflow" if lang == "en" else "Запустить маршрут").click()
        expect(workflow.get_by_role("button", name="Approve this step" if lang == "en" else "Подтвердить этот шаг")).to_be_visible()
        page.reload()
        expect(page.locator(".sheet.pboard-sheet")).to_be_visible()
        workflow = page.locator(".sheet.pboard-sheet .task-workflow-section")
        workflow.locator("button").first.click()
        if lang == "en" and width == 390:
            expect(workflow).to_contain_text("command result is unconfirmed")
            workflow.get_by_role("button", name="Try again").last.click()
            expect(workflow).not_to_contain_text("command result is unconfirmed")
            assert len(stub.workflow_runs) == 1
        expect(workflow.get_by_role("button", name="Approve this step" if lang == "en" else "Подтвердить этот шаг")).to_be_visible()
        workflow.get_by_role("button", name="Approve this step" if lang == "en" else "Подтвердить этот шаг").click()
        expect(workflow).to_contain_text("Completed" if lang == "en" else "Завершён")
        workflow.get_by_role("button", name="Check reuse" if lang == "en" else "Проверить повторное использование").click()
        expect(workflow).to_contain_text("Current receipt can be reused" if lang == "en" else "Текущую квитанцию можно использовать")
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
        workflow = page.locator(".sheet.pboard-sheet .task-workflow-section")
        workflow.locator("button").first.click()
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
