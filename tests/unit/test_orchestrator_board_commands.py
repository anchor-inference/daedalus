"""Coordinator board writes require current office, a live grant and exact revisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import office


@pytest.mark.asyncio
async def test_task_tool_create_requires_live_grant_and_collection_revision(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    run = await rig(settings, db, tmp_path)
    try:
        session_id = await office(run)
        scope = Scope("project", run.project.id)
        revision = await ControlStore(db).revision(scope, Entity("collection", scope.id))
        assert f"collection revision {revision}" in await run.call(session_id, "tasks", op="list")
        with pytest.raises(Refused, match="operator-issued"):
            await run.call(session_id, "tasks", op="create", title="Check the invoice",
                           client_operation_id="tool-call-one", expected_collection_revision=revision)
        issuer = Principal.operator({"via": "token", "user_id": 1})
        actor_id = f"orchestrator:{session_id}"
        grant = await ControlStore(db).issue_grant(
            issuer, Principal(actor_id, "agent"), scope, operations=["board.task.create"],
            effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())

        async def authority(caller: str, operation: str, project_id: str,
                            task_id: str | None = None) -> Principal:
            assert (caller, operation, project_id, task_id) == (
                session_id, "board.task.create", run.project.id, None)
            return Principal(actor_id, "agent", grant["grant_id"], grant["generation"])

        run.orch.app.extensions["orchestrator_board_authority"] = authority
        created = await run.call(session_id, "tasks", op="create", title="Check the invoice",
                                 objective="Inspect the invoice total", client_operation_id="tool-call-one",
                                 expected_collection_revision=revision)
        assert "Check the invoice" in created
        with pytest.raises(Refused, match="expected_collection_revision"):
            await run.call(session_id, "tasks", op="create", title="Another task",
                           client_operation_id="tool-call-two")
    finally:
        await run.manager.close()
