"""Typed task gates require ordered, current-contract transitions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header

from daedalus.extensions.api_orchestrator_domain import install_routes
from daedalus.stores.database import Database


@pytest.mark.asyncio
async def test_human_ack_and_work_step_are_ordered_and_replayable(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                         " created_at,updated_at,brief_json) VALUES"
                         " ('task1','Work','todo',3,'','[]','[]','2026-01-01','2026-01-01','{}')")
        api = FastAPI()

        async def authenticated(x_user: int = Header(1)) -> dict[str, int | str]:
            return {"via": "token", "user_id": x_user}

        install_routes(api, SimpleNamespace(db=db, extensions={}), authenticated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            base = "/api/board/task1/workflow"
            configured = await client.put(base, json={
                "client_operation_id": "workflow-one", "expected_entity_revision": 1,
                "steps": [{"id": "human1", "kind": "human"}, {"id": "work1", "kind": "work"}],
                "edges": [["human1", "work1"]]})
            assert configured.status_code == 200, configured.text
            revision = configured.json()["entity_revision"]
            premature = await client.post(base + "/work1/start", json={
                "client_operation_id": "start-premature", "expected_entity_revision": revision})
            assert premature.status_code == 409
            acknowledgement = {"client_operation_id": "ack-one", "expected_entity_revision": revision}
            ack = await client.post(base + "/human1/ack", json=acknowledgement)
            assert ack.status_code == 200, ack.text
            assert (await client.post(base + "/human1/ack", json=acknowledgement)).json() == ack.json()
            started = await client.post(base + "/work1/start", json={
                "client_operation_id": "start-one", "expected_entity_revision": ack.json()["entity_revision"]})
            assert started.status_code == 200, started.text
            completed = await client.post(base + "/work1/complete", json={
                "client_operation_id": "finish-one", "expected_entity_revision": started.json()["entity_revision"]})
            assert completed.status_code == 200, completed.text
            steps = (await client.get(base)).json()
            assert {step["step_id"]: step["state"] for step in steps} == {"human1": "complete", "work1": "complete"}
    finally:
        await db.close()
