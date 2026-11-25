"""Does every task's model in Settings have its effort beside it, and does a pick save the right field?

Each task that names a model (the translator, the main orchestrator, the heartbeat…) has an effort
picker under its model picker. This check opens Settings → Translation, Settings → Models → Tasks and
Settings → Schedules at a desktop and a phone width, in English and Russian, and asserts that:

- the first row says which default the task has: "no reasoning" for the translator, a helper's call,
  and "as the model is set" for the main orchestrator and the heartbeat, which run whole runs;
- choosing an effort sends exactly that field: ``translation.effort`` and ``dispatcher.effort`` to
  ``PUT /api/settings``, ``effort`` to ``PUT /api/heartbeat``;
- nothing on the page is wider than the window.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8187 /tmp/app-root &
    APP_URL=http://127.0.0.1:8187/app python3 tests/browser/check_task_effort.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"off": "Default — no reasoning", "model": "Default — as the model is set"},
    "ru": {"off": "По умолчанию — без рассуждений", "model": "По умолчанию — как у модели"},
}
HEARTBEAT = {"enabled": True, "armed": True, "interval_minutes": 60, "active_hours": "08:00-23:00", "preset": "", "effort": "", "max_runs_per_day": 24,
             "last_run": None, "runs_today": 0, "session_id": None, "running": False, "file": "HEARTBEAT.md", "text": "Check the board."}


def _merge(into: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def _pick(page: Page, picker_id: str, effort: str) -> str:
    """Open the effort picker with ``picker_id``, choose ``effort``, and return what its first row said."""
    picker = page.locator(f"#{picker_id}")
    picker.scroll_into_view_if_needed()
    shown = picker.locator(".sel-current .sel-name").inner_text()
    picker.locator(".sel-current button").click()
    picker.locator(f"[role=option][data-id='{effort}']").click()
    page.wait_for_timeout(400)
    return shown


def _too_wide(page: Page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))


def run() -> int:
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang, words in WORDS.items():
            for width in (1280, 390):
                where = f"{lang}/{width}"
                settings = copy.deepcopy(SETTINGS)
                settings["revision"] = "r1"
                heartbeat = dict(HEARTBEAT)
                saved: list[dict[str, Any]] = []
                beats: list[dict[str, Any]] = []
                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                page.route("**/api/**", stub)

                def settings_route(route, *, settings=settings, saved=saved):  # type: ignore[no-untyped-def]
                    request = route.request
                    if request.method == "PUT":
                        body = request.post_data_json
                        saved.append(body)
                        _merge(settings, {k: v for k, v in body.items() if k != "base_revision"})
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))

                def validate_route(route):  # type: ignore[no-untyped-def]
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"valid": True, "stale": False, "problems": []}))

                def heartbeat_route(route, *, heartbeat=heartbeat, beats=beats):  # type: ignore[no-untyped-def]
                    request = route.request
                    if request.method == "PUT":
                        body = request.post_data_json
                        beats.append(body)
                        heartbeat.update(body)
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(heartbeat))

                page.route("**/api/settings", settings_route)
                page.route("**/api/settings/validate", validate_route)
                page.route("**/api/heartbeat", heartbeat_route)

                # The translator: a helper's call, so its default is no thinking.
                page.goto(f"{BASE}/settings/translation?token=t&lang={lang}")
                page.wait_for_selector("#translation-effort")
                shown = _pick(page, "translation-effort", "high")
                if shown != words["off"]:
                    failures.append(f"{where}: the translator's default reads {shown!r}, not {words['off']!r}")
                sent = [b for b in saved if "translation" in b]
                if not sent or sent[-1]["translation"].get("effort") != "high":
                    failures.append(f"{where}: choosing high for the translator sent {sent[-1:]!r}")
                if _too_wide(page) > 0:
                    failures.append(f"{where}: the translation page is {_too_wide(page)} px wider than the window")

                # The main orchestrator: a run, so its default is the model's own.
                page.goto(f"{BASE}/settings/tasks?token=t&lang={lang}")
                page.wait_for_selector("#main-effort")
                for picker in ("subagents-effort", "quick-effort", "orchestrator-effort", "vision-effort", "models-review-effort"):
                    if page.locator(f"#{picker}").count() != 1:
                        failures.append(f"{where}: Settings → Models → Tasks has no effort {picker!r}")
                shown = _pick(page, "main-effort", "none")
                if shown != words["model"]:
                    failures.append(f"{where}: the main orchestrator's default reads {shown!r}, not {words['model']!r}")
                sent = [b for b in saved if "dispatcher" in b]
                if not sent or sent[-1]["dispatcher"].get("effort") != "none":
                    failures.append(f"{where}: choosing off for the main orchestrator sent {sent[-1:]!r}")
                if _too_wide(page) > 0:
                    failures.append(f"{where}: the tasks page is {_too_wide(page)} px wider than the window")

                # The heartbeat: a run too, saved through its own endpoint.
                page.goto(f"{BASE}/settings/schedules?token=t&lang={lang}")
                page.wait_for_selector("#heartbeat-effort")
                shown = _pick(page, "heartbeat-effort", "low")
                if shown != words["model"]:
                    failures.append(f"{where}: the heartbeat's default reads {shown!r}")
                if not beats or beats[-1] != {"effort": "low"}:
                    failures.append(f"{where}: choosing low for the heartbeat sent {beats!r}")
                context.close()
        browser.close()
    for failure in failures:
        print("FAIL", failure)
    if not failures:
        print("ok: every checked task's model has its effort beside it, saying its default, and a pick saves its own field")
    return (1 if failures else 0) or UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
