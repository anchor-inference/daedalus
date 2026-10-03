"""Previewed coordinator approvals are scoped, replayable and withdrawable after office changes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_coordinator_authority import register
from daedalus.extensions.coordinator_authority import resolve_authority
from daedalus.stores.control import ControlDenied
from daedalus.stores.database import Database
from tests.unit.test_coordinator_authority import office


async def test_task_approval_replays_and_withdraws_the_exact_generation(db: Database) -> None:
    app, _ = await office(db)
    api = FastAPI()
    register(api, app, lambda: {"via": "cookie", "user_id": 1})
    path = "/api/projects/project/orchestrator/authority"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        preview = (await client.get(path)).json()
        assert preview["grants"] == []
        assert preview["current_coordinator_session_id"] == "coordinator"
        body = {"client_operation_id": "approve-one", "expected_entity_revision": preview["entity_revision"],
                "expected_coordinator_session_id": "coordinator", "bundle_id": "execution", "task_id": "task",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
        approved = await client.post(path, json=body)
        assert approved.status_code == 200, approved.text
        original = approved.json()
        assert (await client.post(path, json=body)).json() == original
        assert (await client.post(path, json={**body, "task_id": None})).status_code == 422
        actor = await resolve_authority(app, session_id="coordinator", project_id="project", task_id="task", operation="task.launch")
        assert actor.grant_id == original["grant_id"]
        with pytest.raises(ControlDenied):
            await resolve_authority(app, session_id="coordinator", project_id="project", task_id="task", operation="board.task.create")
        preview = (await client.get(path)).json()
        grant = preview["grants"][0]
        assert grant["scope"] == {"kind": "task", "id": "task"} and grant["state"] == "active"
        assert grant["receipt_id"] == original["receipt_id"]
        withdrawal = {"client_operation_id": "withdraw-one", "expected_entity_revision": preview["entity_revision"],
                      "expected_coordinator_session_id": "coordinator", "expected_grant_generation": 1,
                      "reason": "Stop autonomous launches"}
        revoke = f"{path}/{original['grant_id']}/revoke"
        revoked = await client.post(revoke, json=withdrawal)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["generation"] == 2
        assert (await client.post(revoke, json=withdrawal)).json() == revoked.json()
        assert (await client.get(path)).json()["grants"][0]["state"] == "revoked"
        with pytest.raises(ControlDenied):
            await resolve_authority(app, session_id="coordinator", project_id="project", task_id="task", operation="task.launch")
        assert (await db.fetchone("SELECT count(*) FROM grant_events"))[0] == 2


async def test_approval_rejects_changed_office_and_stale_grants_remain_withdrawable(db: Database) -> None:
    app, settings = await office(db)
    api = FastAPI()
    register(api, app, lambda: {"via": "cookie", "user_id": 1})
    path = "/api/projects/project/orchestrator/authority"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        initial = (await client.get(path)).json()
        body = {"client_operation_id": "approve-planning", "expected_entity_revision": initial["entity_revision"],
                "expected_coordinator_session_id": "coordinator", "bundle_id": "planning",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
        result = await client.post(path, json=body)
        assert result.status_code == 200, result.text
        grant_id = result.json()["grant_id"]
        settings["orchestrator"]["enabled"] = False
        await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'", (json.dumps(settings),))
        preview = (await client.get(path)).json()
        assert preview["current_coordinator_session_id"] is None
        assert preview["grants"][0]["state"] == "stale"
        assert preview["readiness_blockers"] == ["no_current_coordinator"]
        assert (await client.post(path, json={**body, "client_operation_id": "another",
                                             "expected_entity_revision": preview["entity_revision"]})).status_code == 403
        withdrawn = await client.post(f"{path}/{grant_id}/revoke", json={
            "client_operation_id": "withdraw-stale", "expected_entity_revision": preview["entity_revision"],
            "expected_coordinator_session_id": None, "expected_grant_generation": 1, "reason": "Office disabled"})
        assert withdrawn.status_code == 200, withdrawn.text
        assert (await client.get(path)).json()["grants"][0]["state"] == "revoked"
