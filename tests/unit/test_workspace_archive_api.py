"""The operator route exports an immutable artifact and restores it through a receipt."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.api_workspace_archive import register
from daedalus.host.session_runner import SessionManager
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.control import ControlStore, Entity, Scope
from daedalus.stores.database import Database
from daedalus.stores.files import FileStore


@pytest.mark.asyncio
async def test_archive_http_export_upload_preview_apply_and_replay(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        files = FileStore(db, FileBlobStore(tmp_path / "blobs"))
        source = tmp_path / "source"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "draft.txt").write_bytes(b"A portable draft")
        await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES (?,?,?,?)",
                         ("source", "Sample", "2026-01-01", "{}"))
        await db.execute("INSERT INTO project_folders(id,project_id,path,label,env,created_at) VALUES (?,?,?,?,?,?)",
                         ("folder", "source", str(source), "Work", db.local_env, "2026-01-01"))
        api = FastAPI()
        host = SimpleNamespace(db=db, manager=SimpleNamespace(files=files),
                               settings=SimpleNamespace(workspaces_dir=tmp_path / "managed"))
        def auth() -> dict:
            return {"via": "token", "user_id": 1}
        register(api, host, auth)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            body = {"expected_entity_revision": 1, "client_operation_id": "export-one",
                    "selected_paths": [{"folder_id": "folder", "path": "nested/draft.txt"}]}
            response = await client.post("/api/projects/source/workspace-archive", json=body)
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt["receipt_id"] and receipt["source_entity_revision"] == 1
            assert receipt["selected_files"][0]["path"] == "nested/draft.txt"
            latest = await client.get("/api/projects/source/workspace-archive")
            assert latest.status_code == 200 and latest.json() == {"latest": receipt, "available": True}
            assert (await client.post("/api/projects/source/workspace-archive", json=body)).json() == receipt
            changed = await client.post("/api/projects/source/workspace-archive", json={**body, "selected_paths": []})
            assert changed.status_code == 409
            downloaded = await client.get("/api/import/archive/" + receipt["archive_artifact_id"])
            assert downloaded.status_code == 200
            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                assert archive.read("workspace-files/folder/nested/draft.txt") == b"A portable draft"
            uploaded = await client.post("/api/import/archive", content=downloaded.content)
            assert uploaded.status_code == 200, uploaded.text
            assert uploaded.json()["preview"]["selected_file_count"] == 1
            preview = await client.post("/api/import/preview", json={"archive_artifact_id": receipt["archive_artifact_id"]})
            assert preview.status_code == 200 and preview.json()["valid"]
            collection_revision = await ControlStore(db).revision(Scope("global", "global"), Entity("collection", "global"))
            applied = await client.post("/api/import/apply", json={"archive_artifact_id": receipt["archive_artifact_id"],
                "expected_collection_revision": collection_revision, "client_operation_id": "import-one"})
            assert applied.status_code == 200, applied.text
            restored = applied.json()["project_id"]
            row = await db.fetchone("SELECT path FROM project_folders WHERE project_id=?", (restored,))
            assert (tmp_path / "managed" / "restored-projects" / restored / "folder" / "nested" / "draft.txt").read_bytes() == b"A portable draft"
            assert row["path"].endswith("/restored-projects/" + restored + "/folder")
            files.blobs.path_of("workspace-archives", receipt["archive_artifact_id"]).unlink()
            assert (await client.post("/api/import/apply", json={"archive_artifact_id": receipt["archive_artifact_id"],
                "expected_collection_revision": collection_revision, "client_operation_id": "import-one"})).json() == applied.json()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_full_host_mount_roundtrips_private_workspace(settings: Settings, config: RuntimeConfig,
                                                             db: Database, tmp_path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager,
                              front=None, extensions={}, guard=None)
        api = build_app(app, "tok")
        headers = {"X-Daedalus-Token": "tok"}
        source = tmp_path / "source"
        source.mkdir()
        (source / "draft.txt").write_bytes(b"Full host path")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            made = await client.post("/api/projects", headers=headers, json={"name": "Portable", "folders": [{"path": str(source)}]})
            assert made.status_code == 200, made.text
            project = made.json()
            exported = await client.post(f"/api/projects/{project['id']}/workspace-archive", headers=headers, json={
                "expected_entity_revision": project["entity_revision"], "client_operation_id": "full-host-export",
                "selected_paths": [{"folder_id": project["folders"][0]["id"], "path": "draft.txt"}],
            })
            assert exported.status_code == 200, exported.text
            artifact_id = exported.json()["archive_artifact_id"]
            downloaded = await client.get(f"/api/import/archive/{artifact_id}", headers=headers)
            assert downloaded.status_code == 200
            uploaded = await client.post("/api/import/archive", headers=headers, content=downloaded.content)
            assert uploaded.status_code == 200 and uploaded.json()["preview"]["valid"]
            revision = (await client.get("/api/control/revisions", headers=headers)).json()["collection_revision"]
            restored = await client.post("/api/import/apply", headers=headers, json={
                "archive_artifact_id": artifact_id, "expected_collection_revision": revision,
                "client_operation_id": "full-host-restore",
            })
            assert restored.status_code == 200, restored.text
            projects = (await client.get("/api/projects", headers=headers)).json()
            fresh = next(item for item in projects if item["id"] == restored.json()["project_id"])
            assert fresh["settings"]["orchestrator"]["enabled"] is False
            assert len(fresh["folders"]) == 1
            assert (settings.workspaces_dir / "restored-projects" / fresh["id"] / project["folders"][0]["id"] / "draft.txt").read_bytes() == b"Full host path"
            assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await manager.close()
