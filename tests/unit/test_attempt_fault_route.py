"""Attempt diagnostics stay attached to their task and exclude raw fault material."""

from __future__ import annotations

import secrets

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions.api_attempt_diagnostics import register
from daedalus.extensions.launch_controls import prepare_attempt
from daedalus.stores.attempt_faults import record_fault
from daedalus.stores.database import Database
from tests.unit.test_launch_controls import OPERATOR, launch_fixture


async def test_diagnostics_are_durable_redacted_and_task_bound(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        identity = await prepare_attempt(app, OPERATOR, member, task, session,
                                         fence_token=secrets.token_urlsafe(32))
        async with db.transaction() as conn:
            fault_id = await record_fault(conn, identity.id, "adapter_error")
            await conn.execute("UPDATE execution_attempts SET state = 'failed' WHERE id = ?", (identity.id,))
        api = FastAPI()

        async def operator() -> dict[str, str | int]:
            return {"via": "token", "user_id": 1}

        register(api, app, operator)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            result = await client.get(f"/api/board/{task.id}/attempts/{identity.id}/diagnostics")
            assert result.status_code == 200
            assert result.json()["faults"] == [{"kind": "adapter_error", "diagnostic_ref": f"fault-{fault_id}",
                                                 "created_at": result.json()["faults"][0]["created_at"]}]
            assert result.json()["state"] == "failed"
            assert result.json()["redacted"] is True
            assert (await client.get(f"/api/board/another_task/attempts/{identity.id}/diagnostics")).status_code == 404
    finally:
        app.executions.release()


async def test_fault_writer_rejects_unclassified_or_unowned_material(db: Database) -> None:
    async with db.transaction() as conn:
        with pytest.raises(ValueError):
            await record_fault(conn, "missing", "adapter_error:secret")
        with pytest.raises(KeyError):
            await record_fault(conn, "missing", "adapter_error")
