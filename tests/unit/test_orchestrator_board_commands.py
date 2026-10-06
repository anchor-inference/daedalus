"""Coordinator board writes require current office, a live grant and exact revisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.coordinator_authority import approve_authority
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import rig


@pytest.mark.asyncio
async def test_task_tool_create_requires_live_grant_and_collection_revision(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    run = await rig(settings, db, tmp_path)
    try:
        session_id = (await run.orch.enable(run.project.id, autonomy="ask")).settings.orchestrator.session_id
        scope = Scope("project", run.project.id)
        revision = await ControlStore(db).revision(scope, Entity("collection", scope.id))
        assert f"collection revision {revision}" in await run.call(session_id, "tasks", op="list")
        with pytest.raises(Refused, match="grant"):
            await run.call(session_id, "tasks", op="create", title="Check the invoice",
                           client_operation_id="tool-call-one", expected_collection_revision=revision)
        issuer = Principal.operator({"via": "token", "user_id": 1})
        project_revision = await ControlStore(db).revision(scope, Entity("project", scope.id))
        await approve_authority(run.orch.app, run.project.id, issuer, client_operation_id="approve-planning",
                                expected_entity_revision=project_revision,
                                expected_coordinator_session_id=session_id, bundle_id="planning",
                                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        created = await run.call(session_id, "tasks", op="create", title="Check the invoice",
                                 objective="Inspect the invoice total", client_operation_id="tool-call-one",
                                 expected_collection_revision=revision)
        assert "Check the invoice" in created
        with pytest.raises(Refused, match="expected_collection_revision"):
            await run.orch.service("tasks", session_id=session_id, op="create", title="Another task",
                                   client_operation_id="tool-call-two")
    finally:
        await run.manager.close()
