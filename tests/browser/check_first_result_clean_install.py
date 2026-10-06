"""A first result crosses the real host, scripted model, browser, and durable review."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from queue import Queue
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from daedalus.app import Application
from daedalus.config import ModelConfig, ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.providers import modelsdev
from tests.unit.test_session_runner import ScriptedProvider

ROOT = Path(__file__).resolve().parents[2]
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ORIGINAL = "Prepared the menu from the approved prices.\nBread 3 EUR."


class RoutedProvider(ScriptedProvider):
    """Give the coordinator and worker independent scripts as their turns interleave."""

    def __init__(self) -> None:
        super().__init__([
            {"tool": "Write", "args": {"path": "menu.txt", "content": "Bread 3 EUR.\n"}},
            {"tool": "Report", "args": {"kind": "done", "note": ORIGINAL, "artifacts": ["menu.txt"]}},
            {"text": "The report is ready for review."},
        ])
        self.coordinator = ScriptedProvider([])

    async def stream_with_tools(self, request):  # type: ignore[no-untyped-def]
        target = self.coordinator if any(tool.name == "Assign" for tool in request.tools) else self
        if target is self:
            async for delta in super().stream_with_tools(request):
                yield delta
        else:
            async for delta in target.stream_with_tools(request):
                yield delta


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    assert not 8100 <= port <= 8119
    return port


class DisposableHost:
    def __init__(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="first-result-browser-")
        self.port = free_port()
        self.ready: Queue[tuple[str, str] | Exception] = Queue(maxsize=1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.provider: RoutedProvider | None = None

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            if self.ready.empty():
                self.ready.put(exc)
            else:
                raise

    async def _serve(self) -> None:
        root = Path(self.scratch.name)
        settings = Settings(_env_file=None, state_dir=root / "state", workspaces_dir=root / "workspaces",
                            bot_repo_dir=ROOT, core_repo_dir=ROOT.parent / "protocore-exp",
                            owner_user_id=1, telegram_bot_token="", native=True, api_port=self.port)
        config = RuntimeConfig(
            seeded=["claude-subscription", "openai-anthropic-keys", "more-provider-endpoints"],
            providers={"fake": ProviderConfig(kind="openai_compat", base_url="http://127.0.0.1:1/v1",
                                               pricing={"fake-model": {"input": 0.1, "output": 0.1,
                                                                       "cache_hit": 0.1, "input_limit": 128000,
                                                                       "limit_source": "scripted provider contract"}})},
            presets={"fake": ModelPresetConfig(provider="fake", model="fake-model", thinking=False,
                                                context_window=32000, max_output_tokens=2048)},
            model=ModelConfig(preset="fake"),
        )
        config.save(settings.config_path)

        async def no_catalog() -> dict:
            return {}

        modelsdev.fetch_catalog = no_catalog
        app = Application(settings)
        try:
            await app.start()
            assert app.manager is not None
            old = app.manager.providers.get("fake")
            provider = RoutedProvider()
            provider.endpoint = old.endpoint
            app.manager.providers._providers["fake"] = provider  # type: ignore[assignment]
            self.provider = provider
            self.ready.put((f"http://127.0.0.1:{self.port}", str(app.extensions["api_token"])))
            await asyncio.to_thread(self.stop.wait)
            await old.aclose()
        finally:
            await app.shutdown()

    def __enter__(self) -> tuple[str, str, RoutedProvider]:
        self.thread.start()
        reply = self.ready.get(timeout=60)
        if isinstance(reply, Exception):
            raise reply
        base, token = reply
        for _ in range(100):
            try:
                import urllib.request
                with urllib.request.urlopen(f"{base}/app/", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("disposable host did not serve the app")
        assert self.provider is not None
        return base, token, self.provider

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive()
        self.scratch.cleanup()


def scenario(language: str, width: int) -> None:
    with DisposableHost() as (base, token, provider), sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": width, "height": 700 if width == 320 else 900},
                                      is_mobile=width == 320, has_touch=width == 320)
        page = context.new_page()
        request = playwright.request.new_context(base_url=base, extra_http_headers={"x-daedalus-token": token})

        def api(method: str, path: str, body: dict | None = None) -> dict:
            response = request.fetch(path, method=method, data=body)
            assert response.ok, f"{method} {path}: {response.status} {response.text()}"
            return response.json()

        page.goto(f"{base}/app/agents?token={token}&lang={language}")
        if width == 320:
            page.get_by_text("Orchestration" if language == "en" else "Оркестрация", exact=True).last.click()
            page.get_by_role("button", name="Add a project" if language == "en" else "Добавить проект").click()
        else:
            page.locator(".project-chip").click()
        sheet = page.locator(".sheet")
        sheet.locator("#project-name").fill("Bakery")
        sheet.locator("#project-start-goal").fill("Publish a clear menu")
        sheet.locator("#project-start-constraints").fill("Keep approved prices")
        sheet.locator("#project-start-task").fill("Prepare menu")
        sheet.locator("#project-start-checks").fill("Bread price is correct")
        sheet.get_by_role("button", name="Create project and first task" if language == "en" else "Создать проект и первую задачу").click()
        page.wait_for_url("**/board?task=*")
        project_id = page.url.split("/project/")[1].split("/")[0]
        task_id = page.url.split("task=")[1].split("&")[0]
        task = next(row for row in api("GET", f"/api/projects/{project_id}/board")["tasks"] if row["id"] == task_id)
        assert task["project_id"] == project_id
        assert task["status"] == "todo"
        if width == 320:
            page.goto(f"{base}/app/orchestration/projects?token={token}&lang={language}")
            page.get_by_role("button", name="Projects" if language == "en" else "Проекты").click()
            expect(page.locator(".sheet .project-row")).to_contain_text("Bakery")
            expect(page.locator(".sheet").get_by_role("button", name="Add a project" if language == "en" else "Добавить проект")).to_be_visible()
        page.goto(f"{base}/app/orchestration/project/{project_id}?token={token}&lang={language}")
        page.get_by_role("button", name="Switch the orchestrator on" if language == "en" else "Включить оркестратор").click()
        sheet = page.locator(".sheet.enable-sheet")
        sheet.locator(".focus-enable-advanced summary").click()
        sheet.locator("#orch-model").select_option("fake")
        sheet.locator("#orch-cap").fill("1")
        sheet.get_by_role("button", name="Switch on" if language == "en" else "Включить", exact=True).click()
        expect(sheet).to_have_count(0)
        page.goto(f"{base}/app/orchestration/project/{project_id}/team?token={token}&lang={language}")
        page.get_by_role("button", name="Hire" if language == "en" else "Нанять").first.click()
        sheet = page.locator(".sheet.staff-sheet")
        sheet.locator("#staff-name").fill("Menu worker")
        sheet.locator("#staff-role").fill("Prepare the menu")
        sheet.get_by_role("button", name="Hire" if language == "en" else "Нанять", exact=True).click()
        expect(sheet).to_have_count(0)
        # Under the default autonomy the coordinator needs no per-task permission: the board offers
        # none, and its first action is what issues the project's standing grant.
        page.goto(f"{base}/app/orchestration/project/{project_id}/board?task={task_id}&token={token}&lang={language}")
        expect(page.locator(".sheet.pboard-sheet")).to_be_visible()
        expect(page.get_by_role("button", name="Let the coordinator assign and run this task" if language == "en" else "Разрешить координатору назначить и запустить задачу")).to_have_count(0)
        authority = api("GET", f"/api/projects/{project_id}/orchestrator/authority")
        team = api("GET", f"/api/projects/{project_id}/staff?archived=0")
        member = next(member for member in team["staff"] if member["name"] == "Menu worker")
        task = next(row for row in api("GET", f"/api/projects/{project_id}/board")["tasks"] if row["id"] == task_id)
        provider.coordinator.script = [
            {"tool": "Assign", "args": {"staff": member["id"], "task_id": task_id,
                                        "expected_entity_revision": task["entity_revision"]}},
            {"text": "The menu worker is preparing the first task."},
        ]
        for _ in range(100):
            if api("GET", f"/api/sessions/{authority['current_coordinator_session_id']}")["status"] != "running":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("coordinator did not become ready for the operator message")
        page.goto(f"{base}/app/orchestration/project/{project_id}?token={token}&lang={language}")
        page.locator(".composer textarea").fill("Assign the first task to the menu worker now.")
        use_current = page.get_by_role("button", name="Use current target" if language == "en" else "Выбрать текущего адресата")
        expect(use_current).to_have_count(0)
        send = page.locator(".composer .roundbtn.primary")
        expect(send).to_be_enabled(timeout=10000)
        send.click()
        for _ in range(120):
            rows = api("GET", f"/api/board/{task_id}/results")
            if rows:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("scripted worker did not hand in a result")
        result = rows[0]
        original = api("GET", f"/api/board/{task_id}/results/{result['result_id']}/original")
        assert original["original_text"] == ORIGINAL
        assert provider.requests, "the configured fake provider was never called"
        assert provider.coordinator.requests, "the coordinator never acted"
        standing = api("GET", f"/api/projects/{project_id}/orchestrator/authority")["grants"]
        assert any(grant["scope"] == {"kind": "project", "id": project_id} and grant["state"] == "active"
                   for grant in standing), "the coordinator acted without its standing project grant"
        page.goto(f"{base}/app/project/{project_id}/board?task={task_id}&token={token}&lang={language}")
        sheet = page.locator(".sheet.pboard-sheet")
        expect(sheet).to_be_visible()
        review = sheet.get_by_role("button", name="Review report and evidence" if language == "en" else "Проверить отчёт и доказательства")
        review.click()
        expect(sheet.locator(".result-original")).to_contain_text("Bread 3 EUR")
        expect(sheet.get_by_role("button", name="Approve reviewed result" if language == "en" else "Одобрить проверенный результат")).to_be_disabled()
        page.reload()
        review = sheet.get_by_role("button", name="Review report and evidence" if language == "en" else "Проверить отчёт и доказательства")
        expect(review).to_have_attribute("aria-expanded", "false")
        review.click()
        expect(sheet.locator(".result-original")).to_contain_text("Bread 3 EUR")

        result_id = result["result_id"]
        task = next(row for row in api("GET", f"/api/projects/{project_id}/board")["tasks"] if row["id"] == task_id)
        bad = request.post(f"/api/board/{task_id}/results/{result_id}/verdicts", data={
            "client_operation_id": str(uuid4()), "expected_entity_revision": task["entity_revision"],
            "verification": "verified", "accepted": True, "evidence_ids": [], "reason": "Prices match",
        })
        assert bad.status == 409 and "evidence" in bad.text().lower()
        sheet.get_by_role("textbox", name="What did you observe?" if language == "en" else "Что вы наблюдали?").fill("Compared bread with the approved price")
        sheet.get_by_role("button", name="Record observation" if language == "en" else "Записать наблюдение").click()
        evidence = api("GET", f"/api/board/{task_id}/results/{result_id}/evidence")
        assert len(evidence) == 1 and evidence[0]["criterion_id"] == "C1"
        assert evidence[0]["manifest_digest_before"] == evidence[0]["manifest_digest_after"]
        assert evidence[0]["manifest_digest_before"] in {item["digest"] for item in result["artifacts"]}
        sheet.get_by_role("textbox", name="Review conclusion" if language == "en" else "Вывод проверки").fill("Bread price matches the approved list")
        sheet.get_by_role("button", name="Approve reviewed result" if language == "en" else "Одобрить проверенный результат").click()
        sheet.get_by_role("button", name="Accept this result" if language == "en" else "Принять этот результат").click()
        expect(page.locator(".toast")).to_contain_text("Result accepted" if language == "en" else "Результат принят")
        page.reload()
        expect(sheet.locator(".result-state")).to_contain_text("Operator approved" if language == "en" else "Оператор принял")
        sheet.locator(".result-details summary").first.click()
        sheet.get_by_role("button", name="Show original report" if language == "en" else "Показать исходный отчёт").click()
        expect(sheet.locator(".result-original")).to_contain_text("Bread 3 EUR")
        task = next(row for row in api("GET", f"/api/projects/{project_id}/board?include_done=1")["tasks"] if row["id"] == task_id)
        assert task["status"] == "done" and task["acceptance_state"] == "operator_approved"
        reviewed = api("GET", f"/api/board/{task_id}/results")
        assert reviewed[0]["result_id"] == result_id and reviewed[0]["accepted"] is True
        assert reviewed[0]["verification"] == "verified" and reviewed[0]["verdict_id"]
        assert api("GET", f"/api/board/{task_id}/results/{result_id}/original")["original_text"] == ORIGINAL
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        print(f"{language} {width}: accepted from {len(provider.requests)} scripted model calls")
        request.dispose()
        context.close()
        browser.close()


if __name__ == "__main__":
    for lang in ("en", "ru"):
        for viewport in (320, 1440):
            scenario(lang, viewport)
