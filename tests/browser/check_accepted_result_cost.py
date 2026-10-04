"""Accepted result cost keeps its scope and unknown state visible at phone and desktop widths."""

from __future__ import annotations

import os

from api_stub import BoardStub, Unhandled, expect_app
from check_project_board import BASE, PID, fits, project, serve
from playwright.sync_api import sync_playwright

CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LABEL = {"en": "Worker inference through acceptance", "ru": "Инференс исполнителей до приёмки"}
UNKNOWN = {"en": "unknown", "ru": "неизвестно"}


def check() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        try:
            for language in ("en", "ru"):
                for width in (320, 390, 1440):
                    page = browser.new_page(viewport={"width": width, "height": 820})
                    unhandled = Unhandled()
                    tasks = [
                        BoardStub.task("priced", "Priced result", status="done", acceptance_state="operator_approved",
                                       accepted_result_cost_microusd=12000, accepted_result_cost_unknown_reasons=[]),
                        BoardStub.task("unknown", "Unpriced result", status="done", acceptance_state="operator_approved",
                                       accepted_result_cost_microusd=None,
                                       accepted_result_cost_unknown_reasons=["subscription"]),
                    ]
                    serve(page, BoardStub(project(), tasks=tasks), unhandled)
                    for task_id, value in (("priced", "$0.012"), ("unknown", UNKNOWN[language])):
                        page.goto(f"{BASE}/project/{PID}/board?task={task_id}&token=t&lang={language}")
                        row = page.locator(".pboard-attempt-cost")
                        row.wait_for()
                        assert LABEL[language] in row.inner_text()
                        assert value in row.inner_text()
                        if task_id == "unknown":
                            assert ("subscription price unavailable" if language == "en" else "цена подписки недоступна") in row.inner_text()
                        fits(page, f"{language} {width}px {task_id}")
                    assert unhandled.report() == 0
                    page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    check()
