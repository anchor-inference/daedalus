"""HTTP card commands preserve receipts, reject stale writes and archive without deleting evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from daedalus.extensions.api_board import register
from daedalus.host.events import EventBus
from daedalus.stores.database import Database


async def test_card_command_replay_keeps_its_receipt_and_current_projection_separate(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens) VALUES ('project',3,20,100000)")
    app = SimpleNamespace(db=db, manager=SimpleNamespace(bus=EventBus(db)), extensions={})
    api = FastAPI()

    def auth():
        return {"via": "cookie", "user_id": 1}

    register(api, app, auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        path = "/api/projects/project/board"
        assert (await client.post(path, json={"title": "Draft"})).status_code == 422
        create = {"title": "Draft", "client_operation_id": "create", "expected_collection_revision": 1}
        first = await client.post(path, json=create)
        assert first.status_code == 201, first.text
        original = first.json()
        task_id = original["task"]["id"]
        assert original["command"]["entity_revision"] == 2
        assert original["task"]["entity_revision"] == 1
        assert (await db.fetchone("SELECT count(*) FROM effect_outbox"))[0] == 0
        assert (await client.post(path, json={**create, "title": "Different"})).status_code == 409
        assert (await client.post(path, json={**create, "client_operation_id": "other"})).status_code == 409
        assert (await client.post(path, json={**create, "expected_collection_revision": True})).status_code == 422
        edit = {"title": "Edited", "client_operation_id": "edit", "expected_entity_revision": 1}
        changed = await client.put(f"/api/board/{task_id}", json=edit)
        assert changed.status_code == 200, changed.text
        replay = await client.post(path, json=create)
        assert replay.status_code == 201
        assert replay.json()["command"] == original["command"]
        assert replay.json()["task"]["title"] == "Edited"
        assert replay.json()["task"]["entity_revision"] == 2
        assert (await client.put(f"/api/board/{task_id}", json={**edit, "client_operation_id": "stale"})).status_code == 409
        assert (await client.put(f"/api/board/{task_id}", json={"status": "done", "client_operation_id": "done",
                                                              "expected_entity_revision": 2})).status_code == 409
        assert (await client.delete(f"/api/board/{task_id}")).status_code == 422
        archived = await client.delete(f"/api/board/{task_id}", params={"client_operation_id": "archive", "expected_entity_revision": 2})
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived"] is True
        assert (await db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (task_id,)))[0] == "dropped"
        versions = await db.fetchall("SELECT contract_revision,snapshot_json FROM task_contract_versions WHERE task_id = ? ORDER BY contract_revision", (task_id,))
        assert [row["contract_revision"] for row in versions] == [1, 2]
        assert [json.loads(row["snapshot_json"])["title"] for row in versions] == ["Draft", "Edited"]
        assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 3


async def test_source_session_cannot_cross_a_board_scope(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens) VALUES ('project',3,20,100000)")
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,title,created_at,last_message_at)"
                     " VALUES ('source','tenant','project','Work','2026-01-01','2026-01-01')")
    api = FastAPI()
    register(api, SimpleNamespace(db=db, manager=SimpleNamespace(bus=EventBus(db)), extensions={}), lambda: {"via": "cookie", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        body = {"title": "Task", "session_id": "source", "client_operation_id": "create", "expected_collection_revision": 1}
        assert (await client.post("/api/board", json=body)).status_code == 403
        result = await client.post("/api/projects/project/board", json=body)
        assert result.status_code == 201, result.text
        assert result.json()["task"]["origin_session_id"] == "source"


async def test_operator_can_pin_and_revise_a_project_folder_with_exact_contract_receipts(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES"
                     " ('project','Work','now','{}'),('other','Other','now','{}')")
    await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens) VALUES ('project',3,20,100000)")
    await db.execute("INSERT INTO project_folders(id,project_id,label,path,env,created_at) VALUES"
                     " ('folder','project','Source','/tmp/source','container','now'),"
                     " ('replacement','project','New source','/tmp/replacement','container','now'),"
                     " ('foreign','other','Private','/tmp/other','container','now')")
    api = FastAPI()
    register(api, SimpleNamespace(db=db, manager=SimpleNamespace(bus=EventBus(db)), extensions={}), lambda: {"via": "cookie", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        base = "/api/projects/project/board"
        body = {"title": "Pinned work", "folder_id": "folder", "client_operation_id": "create", "expected_collection_revision": 1}
        assert (await client.post(base, json={**body, "folder_id": "foreign"})).status_code == 409
        assert (await db.fetchone("SELECT count(*) FROM board_tasks"))[0] == 0
        assert (await client.post(base, json={**body, "folder_id": ""})).status_code == 422
        response = await client.post(base, json=body)
        assert response.status_code == 201, response.text
        original = response.json()
        task = original["task"]
        assert task["folder_id"] == "folder"
        contract = await db.fetchone("SELECT snapshot_json FROM task_contract_versions WHERE task_id = ? AND contract_revision = 1", (task["id"],))
        assert json.loads(contract[0])["folder_id"] == "folder"
        edit = {"folder_id": "replacement", "client_operation_id": "edit", "expected_entity_revision": task["entity_revision"]}
        changed = await client.put(f"/api/board/{task['id']}", json=edit)
        assert changed.status_code == 200, changed.text
        assert changed.json()["task"]["folder_id"] == "replacement" and changed.json()["task"]["contract_revision"] == 2
        latest = await db.fetchone("SELECT snapshot_json FROM task_contract_versions WHERE task_id = ? AND contract_revision = 2", (task["id"],))
        assert json.loads(latest[0])["folder_id"] == "replacement"
        replay = (await client.post(base, json=body)).json()
        assert replay["command"] == original["command"] and replay["task"]["folder_id"] == "replacement"
        assert (await client.put(f"/api/board/{task['id']}", json=edit)).json()["command"] == changed.json()["command"]
        assert (await client.put(f"/api/board/{task['id']}", json={**edit, "client_operation_id": "stale"})).status_code == 409
