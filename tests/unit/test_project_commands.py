"""Project writes return one committed receipt and one event, including after a lost reply."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.unit.test_project_folders_api import HEADERS


@pytest.fixture
async def running(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager,
                          front=None, extensions={}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            yield manager, client
    finally:
        await manager.close()


async def _revision(client: Any, project_id: str) -> int:
    projects = (await client.get("/api/projects", headers=HEADERS)).json()
    return next(project["entity_revision"] for project in projects if project["id"] == project_id)


async def test_project_settings_receipt_replay_and_stale_write(
    running: Any, tmp_path: Path, db: Database,
) -> None:
    _, client = running
    folder = tmp_path / "site"
    folder.mkdir()
    created = (await client.post("/api/projects", headers=HEADERS,
                                 json={"name": "Bakery", "folders": [{"path": str(folder)}]})).json()
    project_id = created["id"]
    revision = await _revision(client, project_id)
    command = {"client_operation_id": "project-settings-one", "expected_entity_revision": revision,
               "name": "Bread", "snapshots": True}
    path = f"/api/projects/{project_id}"
    first = await client.patch(path, headers=HEADERS, json=command)
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt["entity_revision"] == revision + 1 and receipt["receipt_id"]
    assert (await client.patch(path, headers=HEADERS, json=command)).json() == receipt
    later = await client.patch(path, headers=HEADERS,
                               json={"client_operation_id": "project-settings-later",
                                     "expected_entity_revision": revision + 1, "name": "New bread"})
    assert later.status_code == 200
    assert (await client.patch(path, headers=HEADERS, json=command)).json() == receipt
    reused_key = await client.patch(path, headers=HEADERS, json={**command, "name": "Cake"})
    assert reused_key.status_code == 409
    stale = await client.patch(path, headers=HEADERS,
                               json={"client_operation_id": "project-settings-two",
                                     "expected_entity_revision": revision, "name": "Cake"})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == revision + 2
    current = next(item for item in (await client.get("/api/projects", headers=HEADERS)).json()
                   if item["id"] == project_id)
    assert current["name"] == "New bread" and current["settings"]["snapshots"] is True
    rows = await db.fetchall("SELECT payload_json FROM app_events WHERE type = 'project.changed' AND project_id = ?", (project_id,))
    settings_events = [json.loads(row["payload_json"]) for row in rows if json.loads(row["payload_json"]).get("change") == "settings"]
    assert len(settings_events) == 2 and settings_events[0]["receipt_id"] == receipt["receipt_id"]


async def test_folder_create_replay_and_remove_replay_are_stable(
    running: Any, tmp_path: Path, db: Database,
) -> None:
    _, client = running
    site = tmp_path / "site"
    docs = tmp_path / "docs"
    site.mkdir()
    docs.mkdir()
    created = (await client.post("/api/projects", headers=HEADERS,
                                 json={"name": "Bakery", "folders": [{"path": str(site)}]})).json()
    project_id = created["id"]
    revision = await _revision(client, project_id)
    path = f"/api/projects/{project_id}/folders"
    body = {"client_operation_id": "folder-add-one", "expected_entity_revision": revision,
            "path": str(docs), "readonly": True}
    first = await client.post(path, headers=HEADERS, json=body)
    assert first.status_code == 200, first.text
    added = first.json()
    assert added["folder_id"] and added["entity_revision"] == revision + 1
    assert (await client.post(path, headers=HEADERS, json=body)).json() == added
    current = next(item for item in (await client.get("/api/projects", headers=HEADERS)).json()
                   if item["id"] == project_id)
    assert len(current["folders"]) == 2 and current["folders"][1]["readonly"] is True
    delete_path = f"{path}/{added['folder_id']}"
    delete_body = {"client_operation_id": "folder-remove-one", "expected_entity_revision": revision + 1}
    removed = await client.request("DELETE", delete_path, headers=HEADERS, json=delete_body)
    assert removed.status_code == 200, removed.text
    assert (await client.request("DELETE", delete_path, headers=HEADERS, json=delete_body)).json() == removed.json()
    assert docs.is_dir()
    assert (await db.fetchone("SELECT id FROM project_folders WHERE id = ?", (added["folder_id"],))) is None
    assert len(await db.fetchall("SELECT id FROM project_journal WHERE project_id = ? AND kind = 'folder'", (project_id,))) == 2


async def test_stale_folder_write_cannot_change_walls_or_emit_event(
    running: Any, tmp_path: Path, db: Database,
) -> None:
    manager, client = running
    site = tmp_path / "site"
    docs = tmp_path / "docs"
    site.mkdir()
    docs.mkdir()
    created = (await client.post("/api/projects", headers=HEADERS,
                                 json={"name": "Bakery", "folders": [{"path": str(site)}, {"path": str(docs)}]})).json()
    project_id = created["id"]
    docs_id = created["folders"][1]["id"]
    revision = await _revision(client, project_id)
    session = (await client.post("/api/sessions", headers=HEADERS,
                                 json={"title": "Writer", "project_id": project_id})).json()
    path = f"/api/projects/{project_id}/folders/{docs_id}"
    lock = await client.patch(path, headers=HEADERS,
                              json={"client_operation_id": "folder-lock-one", "expected_entity_revision": revision,
                                    "readonly": True})
    assert lock.status_code == 200, lock.text
    assert manager.live_state(session["id"]).project.folder(docs_id).readonly is True
    before_events = await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'project.changed' AND project_id = ?", (project_id,))
    stale = await client.patch(path, headers=HEADERS,
                               json={"client_operation_id": "folder-stale-two", "expected_entity_revision": revision,
                                     "readonly": False})
    assert stale.status_code == 409
    after_events = await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'project.changed' AND project_id = ?", (project_id,))
    assert after_events["n"] == before_events["n"]
    assert (await db.fetchone("SELECT readonly FROM project_folders WHERE id = ?", (docs_id,)))["readonly"] == 1


async def test_invalid_overlap_rolls_back_receipt_revision_journal_and_event(
    running: Any, tmp_path: Path, db: Database,
) -> None:
    _, client = running
    site = tmp_path / "site"
    site.mkdir()
    created = (await client.post("/api/projects", headers=HEADERS,
                                 json={"name": "Bakery", "folders": [{"path": str(site)}]})).json()
    project_id = created["id"]
    revision = await _revision(client, project_id)
    before = await db.fetchone("SELECT count(*) AS n FROM app_events WHERE project_id = ?", (project_id,))
    refused = await client.post(f"/api/projects/{project_id}/folders", headers=HEADERS,
                                json={"client_operation_id": "folder-overlap-one",
                                      "expected_entity_revision": revision, "path": str(site / "nested")})
    assert refused.status_code == 400 and "inside the project" in refused.json()["detail"]
    assert await _revision(client, project_id) == revision
    assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE project_id = ?", (project_id,)))["n"] == before["n"]
    assert (await db.fetchone("SELECT count(*) AS n FROM project_journal WHERE project_id = ?", (project_id,)))["n"] == 0
    assert (await db.fetchone("SELECT count(*) AS n FROM operation_receipts WHERE client_operation_id = 'folder-overlap-one'"))["n"] == 0


async def test_busy_folder_refusal_does_not_commit_a_receipt_or_event(
    running: Any, tmp_path: Path, db: Database,
) -> None:
    _, client = running
    site = tmp_path / "site"
    docs = tmp_path / "docs"
    site.mkdir()
    docs.mkdir()
    created = (await client.post("/api/projects", headers=HEADERS,
                                 json={"name": "Bakery", "folders": [{"path": str(site)}, {"path": str(docs)}]})).json()
    project_id = created["id"]
    docs_id = created["folders"][1]["id"]
    revision = await _revision(client, project_id)
    started = await client.post("/api/sessions", headers=HEADERS,
                                json={"title": "Writer", "project_id": project_id, "folder_id": docs_id})
    assert started.status_code == 200
    before = await db.fetchone("SELECT count(*) AS n FROM app_events WHERE project_id = ?", (project_id,))
    refused = await client.request("DELETE", f"/api/projects/{project_id}/folders/{docs_id}",
                                   headers=HEADERS,
                                   json={"client_operation_id": "folder-busy-one",
                                         "expected_entity_revision": revision})
    assert refused.status_code == 409 and "Writer" in refused.json()["detail"]
    assert await _revision(client, project_id) == revision
    assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE project_id = ?", (project_id,)))["n"] == before["n"]
    assert (await db.fetchone("SELECT count(*) AS n FROM operation_receipts WHERE client_operation_id = 'folder-busy-one'"))["n"] == 0


async def test_lost_reload_response_replays_receipt_and_announces_one_event(
    running: Any, tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, client = running
    site = tmp_path / "site"
    site.mkdir()
    created = (await client.post("/api/projects", headers=HEADERS,
                                 json={"name": "Bakery", "folders": [{"path": str(site)}]})).json()
    project_id = created["id"]
    original = manager.reload_project
    attempts = 0

    async def once(project: Any, changed_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("project reload did not finish")
        await original(project, changed_id)

    monkeypatch.setattr(manager, "reload_project", once)
    body = {"client_operation_id": "project-reload-one",
            "expected_entity_revision": created["entity_revision"], "name": "Bread"}
    with pytest.raises(RuntimeError, match="reload did not finish"):
        await client.patch(f"/api/projects/{project_id}", headers=HEADERS, json=body)
    receipt = await db.fetchone("SELECT response_json FROM operation_receipts WHERE client_operation_id = ?",
                                (body["client_operation_id"],))
    assert receipt is not None
    replay = await client.patch(f"/api/projects/{project_id}", headers=HEADERS, json=body)
    assert replay.status_code == 200 and replay.json() == json.loads(receipt["response_json"])
    assert (await db.fetchone("SELECT count(*) AS n FROM app_events WHERE type = 'project.changed'"
                             " AND project_id = ? AND json_extract(payload_json,'$.change') = 'settings'",
                             (project_id,)))["n"] == 1
