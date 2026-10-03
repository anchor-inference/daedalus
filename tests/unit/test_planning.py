"""Plan admission reserves bounded work under a project collection receipt."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.extensions.api_orchestrator_domain import install_routes
from daedalus.stores.database import Database


@pytest.mark.asyncio
async def test_plan_create_replays_and_rejects_exhausted_task_budget(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens)"
                         " VALUES ('project1',2,1,100000)")
        api = FastAPI()

        async def authenticated(x_user: int = Header(1)) -> dict[str, int | str]:
            return {"via": "token", "user_id": x_user}

        install_routes(api, SimpleNamespace(db=db, extensions={}), authenticated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            readiness = await client.get("/api/projects/project1/plans/readiness")
            assert readiness.status_code == 200, readiness.text
            assert readiness.json()["status"] == "measured"
            request = {"client_operation_id": "plan-one", "expected_collection_revision": 1,
                       "tasks": [{"title": "First bounded task"}], "fanout_reason": ""}
            created = await client.post("/api/projects/project1/plans", json=request)
            assert created.status_code == 200, created.text
            task_id = created.json()["accepted_tasks"][0]["task_id"]
            assert (await db.fetchone("SELECT contract_revision FROM board_tasks WHERE id = ?",
                                     (task_id,)))["contract_revision"] == 1
            replay = await client.post("/api/projects/project1/plans", json=request)
            assert replay.status_code == 200 and replay.json() == created.json()
            another = await client.post("/api/projects/project1/plans", json={
                "client_operation_id": "plan-two", "expected_collection_revision": created.json()["entity_revision"],
                "tasks": [{"title": "Second task"}]})
            assert another.status_code == 409, another.text
            assert (await db.fetchone("SELECT count(*) AS n FROM board_tasks WHERE project_id = 'project1'"))["n"] == 1
    finally:
        await db.close()
