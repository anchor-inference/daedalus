"""Provider presets, project ownership and directory access agree at the API boundary."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from daedalus.config import ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions import api as api_module
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.providers.llamacpp import discover_llamacpp
from daedalus.stores.database import Database
from tests.unit.test_llamacpp import _models, _props
from tests.unit.test_project_unification import HEADERS, _client
from tests.unit.test_session_runner import _wait_finished
from tests.unit.test_winddown_cause import _no_retries


async def test_discovered_llamacpp_preset_runs_and_reports_refusal_inside_a_project(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_retries(monkeypatch)
    config = RuntimeConfig(providers={"local": ProviderConfig(kind="llamacpp", base_url="http://localhost/v1")})
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)

    async def save_config(updated: RuntimeConfig) -> None:
        app.config = updated
        manager.config = updated

    app.save_config = save_config

    async def discover(base_url: str, api_key: str | None = None):  # type: ignore[no-untyped-def]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_props() if request.url.path == "/props" else _models()))) as client:
            return await discover_llamacpp(base_url, api_key, client=client)

    monkeypatch.setattr(api_module, "discover_llamacpp", discover)
    provider = manager.providers.get("local")
    assert provider is not None
    captured: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(400, json={"error": {"message": "failed to parse grammar"}})

    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test", headers=HEADERS) as client:
            lookup = await client.post("/api/providers/lookup-models", json={"provider": "local"})
            assert lookup.status_code == 200
            entry = lookup.json()["entries"][0]
            saved = await client.put("/api/presets/local.model", json={"provider": "local", "model": entry["id"], "context_window": entry["context_length"]})
            assert saved.status_code == 200, saved.text
            catalogue = (await client.get("/api/settings")).json()
            assert catalogue["presets"]["local.model"]["context_window"] == 128000
            project = (await client.post("/api/projects", json={"name": "Local work"})).json()
            for project_args in ({}, {"project_id": project["id"]}):
                response = await client.post("/api/sessions", json={"title": "Local task", "preset": "local.model", **project_args})
                assert response.status_code == 200, response.text
                sid = response.json()["id"]
                state = manager.live_state(sid)
                assert state is not None and state.project is not None
                row = await db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (sid,))
                assert row is not None and row["project_id"] == state.project.id
                if project_args:
                    assert state.project.id == project["id"]
                assert state.workspace == state.project.primary.path
                waiter = asyncio.create_task(_wait_finished(manager))
                submitted = await client.post(f"/api/sessions/{sid}/messages", json={"text": "hello"})
                assert submitted.status_code == 200, submitted.text
                assert (await waiter)[0][2] == "failed"
                detail = (await client.get(f"/api/sessions/{sid}")).json()
                assert detail["status"] == "failed"
                assert "failed to parse grammar" in detail["error"]
                assert not [m for m in detail["messages"] if m["role"] == "assistant"]
            assert captured and all(r.url.path == "/v1/chat/completions" for r in captured)
    finally:
        await manager.close()


async def test_directory_picker_and_explorer_hide_sealed_and_escaping_paths(
    settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        state = await manager.create_session("Files")
        root = state.workspace
        (root / "visible").mkdir()
        (root / "sealed").mkdir()
        (root / "sealed" / "hidden.txt").write_text("private")
        (root / "escape").symlink_to(tmp_path, target_is_directory=True)
        (root / "broken").symlink_to(tmp_path / "missing")
        (root / "sealed-link").symlink_to(root / "sealed", target_is_directory=True)
        protected = manager.protected_paths()
        monkeypatch.setattr(manager, "protected_paths", lambda: (*protected, root / "sealed"))
        async with await _client(settings, config, db, manager) as client:
            picker = await client.get("/api/folders", headers=HEADERS, params={"path": str(root)})
            explorer = await client.get(f"/api/sessions/{state.session.id}/files", headers=HEADERS)
            assert picker.status_code == explorer.status_code == 200
            for response in (picker, explorer):
                names = {e["name"] for e in response.json()["entries"]}
                assert "visible" in names
                assert not names & {"sealed", "sealed-link", "escape", "broken"}
            for name in ("sealed", "sealed-link", "escape"):
                folder = await client.get("/api/folders", headers=HEADERS, params={"path": str(root / name)})
                files = await client.get(f"/api/sessions/{state.session.id}/files", headers=HEADERS, params={"path": name})
                assert folder.status_code == 403
                assert files.status_code in (400, 403)
    finally:
        await manager.close()
