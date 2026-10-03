"""An approved agent launch conveys narrowly bound worker authority until its approval changes."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from daedalus.extensions.launch_controls import prepare_attempt
from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope
from daedalus.stores.database import Database
from tests.unit.test_launch_controls import OPERATOR, launch_fixture


async def approve_coordinator(db: Database, *, effects: list[str] | None = None) -> tuple[Principal, dict]:
    await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'",
                     (json.dumps({"orchestrator": {"enabled": True, "session_id": "coordinator"}}),))
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,metadata,created_at,last_message_at)"
                     " VALUES ('coordinator','tenant','project','{\"orchestrator_of\":\"project\"}','now','now')")
    grant = await ControlStore(db).issue_grant(OPERATOR, Principal("orchestrator:coordinator", "agent"),
                                               Scope("project", "project"), operations=["task.launch"],
                                               effects=["execution.start"] if effects is None else effects,
                                               expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat())
    return Principal("orchestrator:coordinator", "agent", grant["grant_id"], grant["generation"]), grant


@pytest.mark.parametrize("withdraw", ["revoked", "expired", "replaced", "disabled", "session_removed"])
async def test_worker_reports_depend_on_the_exact_current_launch_approval(db: Database, withdraw: str) -> None:
    app, member, task, session = await launch_fixture(db)
    control = ControlStore(db)
    try:
        coordinator, approval = await approve_coordinator(db)
        identity = await prepare_attempt(app, coordinator, member, task, session, fence_token=secrets.token_urlsafe(32))
        child = await db.fetchone("SELECT * FROM actor_grants WHERE id = ?", (identity.principal.grant_id,))
        assert child["parent_grant_id"] == approval["grant_id"]
        assert child["parent_grant_generation"] == approval["generation"]
        assert child["staff_session_id"] == session.id
        assert child["expires_at"] == approval["expires_at"]
        async with db.transaction() as conn:
            assert await app.executions.check_staff(conn, session.id, operation="staff.report") == identity
            with pytest.raises(ControlDenied, match="outside"):
                await control.authorize(conn, identity.principal, Scope("project", "project"), "task.launch", task_id=task.id)
        if withdraw == "revoked":
            await control.revoke_grant(OPERATOR, approval["grant_id"], reason="launch approval withdrawn")
        elif withdraw == "expired":
            await db.execute("UPDATE actor_grants SET expires_at = ? WHERE id = ?",
                             ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), approval["grant_id"]))
        elif withdraw == "session_removed":
            await db.execute("DELETE FROM sessions WHERE id = 'coordinator'")
        else:
            await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'",
                             (json.dumps({"orchestrator": {"enabled": withdraw != "disabled",
                                                          "session_id": "replacement" if withdraw == "replaced" else "coordinator"}}),))
        async with db.transaction() as conn:
            with pytest.raises(ControlDenied):
                await app.executions.check_staff(conn, session.id, operation="staff.report")
        assert (await db.fetchone("SELECT count(*) FROM execution_attempts"))[0] == 1
    finally:
        app.executions.release()


async def test_launch_without_effect_approval_cannot_leave_a_worker_grant(db: Database) -> None:
    app, member, task, session = await launch_fixture(db)
    try:
        coordinator, _ = await approve_coordinator(db, effects=[])
        with pytest.raises(ControlDenied, match="effect"):
            await prepare_attempt(app, coordinator, member, task, session, fence_token=secrets.token_urlsafe(32))
        assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 1
        assert not await db.fetchall("SELECT * FROM execution_attempts")
    finally:
        app.executions.release()


async def test_global_approval_does_not_convey_cross_project_authority(db: Database) -> None:
    app, _, _, _ = await launch_fixture(db)
    control = ControlStore(db)
    try:
        subject = Principal("session:agent", "agent")
        grant = await control.issue_grant(OPERATOR, subject, Scope("global", "global"),
                                          operations=["board.task.create"], effects=[],
                                          expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        principal = Principal(subject.actor_id, "agent", grant["grant_id"], 1)
        async with db.transaction() as conn:
            await control.authorize(conn, principal, Scope("global", "global"), "board.task.create")
            with pytest.raises(ControlDenied, match="global board"):
                await control.authorize(conn, principal, Scope("project", "project"), "board.task.create")
    finally:
        app.executions.release()
