"""Only uncertain project stops expose a compact, inspectable recovery control."""

from __future__ import annotations

import os

from api_stub import Unhandled, expect_app
from check_project_board import BASE, PID, board, fits, serve
from playwright.sync_api import expect, sync_playwright

CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")


def stop(id_: str) -> dict:
    return {"id": id_, "task_id": "t-checkout", "state": "running", "host_generation": 7,
            "staff_session_id": "staff-one", "runtime_kind": "cli", "provider_session_ref": "terminal:one",
            "native_run_id": None, "runtime_instance": "instance-one", "parent_kind": "task",
            "parent_id": "t-checkout", "generation": 2, "cancel_state": "unknown",
            "updated_at": "2026-10-01T09:00:00Z", "phase": "observing_exit",
            "deadline_at": "2026-10-01T08:59:00Z", "exit_observed": True,
            "no_entry_observed": False, "generation_matches_host_record": True,
            "exit_evidence": {"runtime_ref": "terminal-one", "host_generation": 7, "contract_revision": 1,
                              "observed_status": "exited", "observed_at": "2026-10-01T09:02:00Z"},
            "no_entry_evidence": None,
            "containment_evidence": {"source": "writer", "state": "released", "host_generation": "7",
                                     "latest_observation": {"id": "observation-one", "host_generation": "7",
                                                            "observation_kind": "exit", "enforced": 1,
                                                            "populated": 0, "observed_at": "2026-10-01T09:03:00Z"},
                                     "release_observation": {"id": "observation-one", "host_generation": "7",
                                                             "observation_kind": "exit", "enforced": 1,
                                                             "populated": 0, "observed_at": "2026-10-01T09:03:00Z"},
                                     "stale_observation": None},
            "recovery_blocker": "ready"}


def check(width: int, language: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        context = browser.new_context(viewport={"width": width, "height": 850})
        page = context.new_page()
        unhandled = Unhandled()
        stub = board()
        serve(page, stub, unhandled)
        page.goto(f"{BASE}/project/{PID}/board?token=t&lang={language}")
        expect_app(BASE)
        expect(page.locator(".pboard-unknown-stops")).to_have_count(0)

        stub.uncertain_launches = [{"id": "launch-one", "task_id": "t-checkout", "state": "unknown",
                                    "claim_generation": 2, "created_at": "2026-10-01T09:00:00Z",
                                    "claimed_at": "2026-10-01T09:01:00Z", "completed_at": None,
                                    "error": "response lost", "attempt_id": "attempt-one",
                                    "attempt_state": "recovering", "provider_session_recorded": False,
                                    "no_entry_observed": False, "exit_observed": False}]
        page.reload()
        launches = page.locator(".pboard-uncertain-launches")
        expect(launches).to_be_visible()
        launches.locator(":scope > summary").click()
        launches.locator(".pboard-unknown-row summary").click()
        expect(launches).to_contain_text("attempt-one")
        expect(launches).to_contain_text("response lost")
        launches.get_by_role("button", name="Check observed outcome" if language == "en" else "Проверить подтверждённый исход").click()
        expect(launches).to_contain_text("launch remains fenced" if language == "en" else "запуск остаётся заблокированным")
        expect(launches.locator(".pboard-unknown-row")).to_have_count(1)
        assert stub.launch_reconciliations == ["launch-one"]
        stub.uncertain_launches[0]["provider_session_recorded"] = True
        launches.get_by_role("button", name="Check observed outcome" if language == "en" else "Проверить подтверждённый исход").click()
        expect(launches).to_contain_text("completed")
        expect(launches.locator(".pboard-unknown-row")).to_have_count(0)
        assert stub.launch_reconciliations == ["launch-one", "launch-one"]
        fits(page, f"{width}px {language} uncertain launch")
        stub.uncertain_launches = []

        stub.unknown_stops_failed = True
        page.reload()
        disclosure = page.locator(".pboard-unknown-stops")
        expect(disclosure).to_be_visible()
        disclosure.locator(":scope > summary").click()
        expect(disclosure).to_contain_text("unavailable")
        stub.unknown_stops_failed = False
        disclosure.get_by_role("button", name="Reload stops" if language == "en" else "Обновить остановки").click()
        expect(disclosure).to_have_count(0)

        blocked = stop("attempt-one")
        blocked["recovery_blocker"] = "containment_unavailable"
        blocked["containment_evidence"] = None
        stub.unknown_stops = [blocked]
        page.reload()
        disclosure = page.locator(".pboard-unknown-stops")
        expect(disclosure).to_be_visible()
        expect(disclosure).not_to_have_attribute("open", "")
        disclosure.locator(":scope > summary").click()
        expect(disclosure.locator(".pboard-unknown-row")).to_have_count(1)
        disclosure.locator(".pboard-unknown-row summary").click()
        expect(disclosure).to_contain_text("attempt-one")
        expect(disclosure).to_contain_text("observing_exit")
        expect(disclosure).to_contain_text("7")
        expect(disclosure).to_contain_text("Yes" if language == "en" else "Да")
        expect(disclosure).to_contain_text("no recorded process containment" if language == "en" else "не записана изоляция процессов")
        expect(disclosure).to_contain_text("No binding recorded" if language == "en" else "Привязка не записана")
        expect(disclosure).to_contain_text("terminal-one")
        reconcile = disclosure.locator(".pboard-unknown-row button")
        expect(reconcile).to_be_disabled()
        assert stub.stop_reconciliations == [], stub.stop_reconciliations
        fits(page, f"{width}px {language} expanded stop")

        stale = stop("attempt-one")
        stale["recovery_blocker"] = "container_not_empty"
        stale["containment_evidence"]["state"] = "released"
        stale["containment_evidence"]["latest_observation"] = None
        stale["containment_evidence"]["release_observation"] = None
        stale["containment_evidence"]["stale_observation"] = {
            "id": "stale-observation", "host_generation": "6", "observation_kind": "exit",
            "enforced": 1, "populated": 0, "observed_at": "2026-10-01T09:03:00Z"}
        stub.unknown_stops = [stale]
        disclosure.get_by_role("button", name="Reload stops" if language == "en" else "Обновить остановки").click()
        expect(disclosure).to_contain_text("stale-observation")
        expect(disclosure).to_contain_text("not release proof" if language == "en" else "не подтверждает освобождение")
        expect(reconcile).to_be_disabled()
        assert stub.stop_reconciliations == []

        stub.unknown_stops = [stop("attempt-one")]
        disclosure.get_by_role("button", name="Reload stops" if language == "en" else "Обновить остановки").click()
        expect(disclosure).to_contain_text("Exact release evidence" if language == "en" else "Точное подтверждение")
        expect(disclosure).to_contain_text("observation-one")
        expect(disclosure).to_contain_text("Writer claim" if language == "en" else "Заявка на запись")
        expect(disclosure).to_contain_text("populated No" if language == "en" else "есть процессы Нет")
        expect(reconcile).to_be_enabled()
        reconcile.click()
        expect(disclosure).to_contain_text("completed")
        expect(disclosure.locator(".pboard-unknown-row")).to_have_count(0)
        assert stub.stop_reconciliations == ["attempt-one"], stub.stop_reconciliations
        fits(page, f"{width}px {language} reconciled stop")
        assert unhandled.report() == 0
        browser.close()


if __name__ == "__main__":
    for language in ("en", "ru"):
        for width in (320, 390, 1440):
            check(width, language)
            print(f"unknown stops {language} {width}px OK")
