"""A task's durable workflow stays discoverable and approvals bind to the inspected source."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.extensions.api_workflows import register
from daedalus.extensions.board_workflows import BoardWorkflows
from daedalus.stores.database import Database


@pytest.mark.asyncio
async def test_task_workflow_list_and_source_bound_approval(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
            "VALUES ('task-a','Task','todo','project','now','now')"
        )
        api = FastAPI()

        async def authenticated(x_user: int = Header(1)) -> dict[str, int | str]:
            return {"via": "token" if x_user > 0 else "staff", "user_id": x_user}

        service = BoardWorkflows(db)
        register(api, SimpleNamespace(db=db, extensions={"board_workflows": service}), authenticated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            task_path = "/api/board/task-a/board-workflows"
            assert (await client.get(task_path)).json()["items"] == []
            definition = {
                "project_id": "project", "task_id": "task-a",
                "nodes": [{"id": "operator", "kind": "approval", "task_id": "task-a"}],
                "edges": [], "budget": {"max_steps": 1, "max_parallel": 1},
                "expected_entity_revision": 1, "client_operation_id": "start-approval",
            }
            started = await client.post("/api/board-workflows/runs", json=definition)
            assert started.status_code == 200, started.text
            run_id = started.json()["run_id"]
            assert (await client.get(task_path, headers={"x-user": "-1"})).status_code == 403
            assert (await client.get(f"/api/board-workflows/runs/{run_id}", headers={"x-user": "-1"})).status_code == 403
            assert (await client.post(f"/api/board-workflows/runs/{run_id}/reconcile", headers={"x-user": "-1"},
                                      json={"node_id": "operator", "expected_input_hash": "b" * 64})).status_code == 403
            assert (await client.post("/api/board-workflows/runs", json=definition)).json() == started.json()
            listed = (await client.get(task_path)).json()
            assert [item["run_id"] for item in listed["items"]] == [run_id]
            assert listed["task_entity_revision"] == started.json()["entity_revision"]
            await service.advance_run(run_id)
            run_path = f"/api/board-workflows/runs/{run_id}"
            inspected = (await client.get(run_path)).json()
            step = inspected["steps"][0]
            assert step["can_approve"] and step["source_current"]
            body = {
                "expected_entity_revision": listed["task_entity_revision"],
                "expected_step_revision": step["step_revision"],
                "expected_source_contract_revision": step["source_contract_revision"],
                "expected_input_digest": step["current_input_digest"],
                "client_operation_id": "approve-operator",
            }
            approval_path = run_path + "/steps/operator/approve"
            rejected = await client.post(approval_path, json={**body, "expected_input_digest": "0" * 64})
            assert rejected.status_code == 409
            approved = await client.post(approval_path, json=body)
            assert approved.status_code == 200, approved.text
            assert (await client.post(approval_path, json=body)).json() == approved.json()
            assert (await client.get(run_path)).json()["status"] == "completed"
            for index in range(22):
                await db.execute(
                    "INSERT INTO board_workflow_runs(id,project_id,task_id,definition_digest,definition,budget,status,created_at,updated_at) "
                    "VALUES (?,?,?,'digest',?,?,'completed',?,?)",
                    (f"older-{index:02d}", "project", "task-a", json.dumps({key: definition[key] for key in ("nodes", "edges", "budget")}),
                     json.dumps(definition["budget"]), f"2026-01-{index + 1:02d}T00:00:00Z", f"2026-01-{index + 1:02d}T00:00:00Z"),
                )
            first_page = (await client.get(task_path)).json()
            assert len(first_page["items"]) == 20 and first_page["next_before"]
            second_page = (await client.get(task_path, params={"before": first_page["next_before"]})).json()
            assert len(second_page["items"]) == 3 and second_page["next_before"] is None
    finally:
        await db.close()
