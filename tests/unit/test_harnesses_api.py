"""The Harnesses routes and the hiring form's catalog over HTTP, and a hire the last check refuses."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.harness.contract import Catalog, InstallInfo, LoginState
from daedalus.harness.manager import HarnessManager
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore

HEADERS = {"X-Daedalus-Token": "tok"}


def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, harness: HarnessManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={"harness": harness}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


def _harness(db: Database, config: RuntimeConfig, *, reachable: bool, working: list[dict[str, Any]] | None = None) -> HarnessManager:
    async def live_staff(harness: str) -> list[dict[str, Any]]:
        return [s for s in working or [] if s["harness"] == harness]

    # An environment port that is never asked anything: every call below is refused or answered from the store first.
    port: Any = SimpleNamespace(name="container", home="/home/operator")
    return HarnessManager(
        HarnessStore(db), ports=lambda env: port if reachable and env == "container" else None, environments=lambda: ["container"] if reachable else [],
        config=lambda: config.harness, live_staff=live_staff,
    )


async def test_the_screen_the_form_and_the_refusals(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    working = [{"harness": "claude", "staff_id": "st-1", "name": "Ada", "project": "Bakery", "project_id": "p1", "env": "container", "staff_session_id": "ss-1", "status": "working"}]
    harness = _harness(db, config, reachable=True, working=working)
    await harness.store.record_check("container", "claude", install=InstallInfo(True, "/home/operator/.local/bin/claude", "2.1.281", "native"), login=LoginState("yes", "claude.ai · max"))
    await harness.store.record_latest("container", "claude", "2.1.290")
    await harness.store.record_check("container", "codex", install=InstallInfo(True, "/home/operator/.npm-global/bin/codex", "0.155.1", "npm"), login=LoginState("no"))
    try:
        async with _client(settings, config, db, manager, harness) as client:
            assert (await client.get("/api/harnesses")).status_code == 401
            screen = (await client.get("/api/harnesses?env=container", headers=HEADERS)).json()
            assert [r["harness"] for r in screen["rows"]] == ["claude", "codex", "opencode", "pi", "grok", "cursor"]
            assert (screen["env"], screen["environments"], screen["updates"], screen["node"]["pinned"]) == ("container", ["container"], 1, "24.21.0")
            claude = screen["rows"][0]
            assert (claude["installed_version"], claude["latest_version"], claude["update_available"], claude["status_channel_label"]) == ("2.1.281", "2.1.290", True, "hooks per launch")

            form = (await client.get("/api/harnesses/catalog?env=container", headers=HEADERS)).json()
            assert set(form) == {"claude", "codex", "opencode", "pi", "grok", "cursor"}
            assert (form["claude"]["installed"], form["claude"]["logged_in"], form["codex"]["logged_in"], form["pi"]["logged_in"]) == (True, True, False, None)
            assert form["claude"]["modes"][0] == "default" and form["pi"]["installed"] is False

            one = (await client.get("/api/harnesses/claude/catalog?env=container", headers=HEADERS)).json()
            assert (one["harness"], one["agents"], one["efforts"][-1]) == ("claude", [], "max")
            assert (await client.get("/api/harnesses/daedalus/catalog", headers=HEADERS)).status_code == 404
            assert (await client.get("/api/harnesses?env=moon", headers=HEADERS)).status_code == 422

            refused = await client.post("/api/harnesses/claude/update", headers=HEADERS, json={"env": "container"})
            assert refused.status_code == 409
            assert "Ada (Bakery)" in refused.json()["detail"] and refused.json()["staff"][0]["staff_id"] == "st-1"
            down = await client.post("/api/harnesses/claude/update", headers=HEADERS, json={"env": "host"})
            assert (down.status_code, down.json()["detail"]) == (503, "the host environment's terminal service is not available")
            assert (await client.post("/api/harnesses/check", headers=HEADERS, json={"env": "host"})).status_code == 503
            assert (await client.post("/api/harnesses/check", headers=HEADERS, json={"env": "container", "harness": "vim"})).status_code == 404
            # Installs and sign-in need a terminal to run in; this installation has none.
            assert (await client.post("/api/harnesses/claude/install", headers=HEADERS, json={"env": "container"})).status_code == 503
            assert (await client.post("/api/harnesses/claude/login-terminal", headers=HEADERS, json={"env": "container"})).status_code == 503
            assert (await client.post("/api/harnesses/node/install", headers=HEADERS, json={"env": "host"})).status_code == 400
            assert (await client.post("/api/harnesses/claude/update", headers=HEADERS, json={"env": "container", "extra": 1})).status_code == 422
    finally:
        await manager.close()


async def test_a_hire_on_a_cli_the_last_check_found_missing_is_refused(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    harness = _harness(db, config, reachable=False)
    await harness.store.record_check("container", "codex", install=InstallInfo(False), login=LoginState("unknown"))
    await harness.store.record_check("container", "claude", install=InstallInfo(True, "/home/operator/.local/bin/claude", "2.1.281", "native"), login=LoginState("yes"))
    await harness.store.record_check("container", "opencode", install=InstallInfo(True, "/home/operator/.npm-global/bin/opencode", "2.0.1", "npm"), login=LoginState("yes"))
    repo = tmp_path / "bakery"
    repo.mkdir()
    try:
        async with _client(settings, config, db, manager, harness) as client:
            pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(repo)}]})).json()["id"]
            missing = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Rex", "harness": "codex"})
            assert (missing.status_code, missing.json()["detail"]) == (400, "Codex is not installed in the container environment")
            wrong_major = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Oc", "harness": "opencode"})
            assert (wrong_major.status_code, wrong_major.json()["detail"]) == (400, "OpenCode 2.0.1 is a major version Daedalus does not support")
            # Installed, or never checked in that environment: hired; a sign-in can come later.
            assert (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Cc", "harness": "claude"})).status_code == 201
            assert (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Pi", "harness": "pi"})).status_code == 201
            assert (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada"})).status_code == 201
    finally:
        await manager.close()


async def test_the_operator_chooses_which_models_are_offered_and_every_one_stays_usable(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    harness = _harness(db, config, reachable=True)
    listed = ("fable", "opus", "sonnet", "haiku", "claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5-5")
    install = InstallInfo(True, "/home/operator/.local/bin/claude", "2.1.281", "native")
    await harness.store.record_check("container", "claude", install=install, login=LoginState("yes"), catalog=Catalog(models=listed))
    try:
        async with _client(settings, config, db, manager, harness) as client:
            row = (await client.get("/api/harnesses?env=container", headers=HEADERS)).json()["rows"][0]
            assert (row["models"], row["all_models"], row["models_chosen"]) == (list(listed), list(listed), False)

            put = await client.put("/api/harnesses/claude/models", headers=HEADERS, json={"env": "container", "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5"]})
            assert put.status_code == 200
            # Chosen models come back in the CLI's own order, once each.
            assert (put.json()["row"]["models"], put.json()["row"]["models_chosen"]) == (["claude-opus-5-5", "claude-sonnet-5-5"], True)
            row = (await client.get("/api/harnesses?env=container", headers=HEADERS)).json()["rows"][0]
            assert (row["models"], row["all_models"]) == (["claude-opus-5-5", "claude-sonnet-5-5"], list(listed))
            form = (await client.get("/api/harnesses/catalog?env=container", headers=HEADERS)).json()["claude"]
            assert (form["models"], form["all_models"], form["models_chosen"]) == (["claude-opus-5-5", "claude-sonnet-5-5"], list(listed), True)
            # The choice is the container's alone, and a hire still takes any model the CLI lists.
            host = (await client.get("/api/harnesses?env=host", headers=HEADERS)).json()["rows"][0]
            assert host["models_chosen"] is False
            assert (await harness.catalog("container", "claude")).models == listed

            # A later check keeps the choice.
            await harness.store.record_check("container", "claude", install=install, login=LoginState("yes"), catalog=Catalog(models=listed))
            assert (await harness.entry("container", "claude"))["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]

            unknown = await client.put("/api/harnesses/claude/models", headers=HEADERS, json={"env": "container", "models": ["claude-opus-9"]})
            assert (unknown.status_code, unknown.json()["detail"]) == (400, "Claude Code in the container environment lists no model 'claude-opus-9'")
            assert (await client.put("/api/harnesses/vim/models", headers=HEADERS, json={"models": None})).status_code == 404
            assert (await client.put("/api/harnesses/claude/models", headers=HEADERS, json={"models": [""]})).status_code == 422

            reset = await client.put("/api/harnesses/claude/models", headers=HEADERS, json={"env": "container", "models": None})
            assert (reset.json()["row"]["models"], reset.json()["row"]["models_chosen"]) == (list(listed), False)
            await client.put("/api/harnesses/claude/models", headers=HEADERS, json={"env": "container", "models": ["haiku"]})
            emptied = await client.put("/api/harnesses/claude/models", headers=HEADERS, json={"env": "container", "models": []})
            assert emptied.json()["row"]["models_chosen"] is False, "an empty choice offers everything, never nothing"
    finally:
        await manager.close()
