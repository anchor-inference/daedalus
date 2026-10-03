"""A current role name cannot substitute for an operator's scoped review approval."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.coordinator_authority import grant_review, install_authority, resolve_authority
from daedalus.stores.control import ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


async def office(db: Database):
    settings = {"orchestrator": {"enabled": True, "session_id": "coordinator"}}
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01',?)", (json.dumps(settings),))
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,metadata,created_at,last_message_at)"
                     " VALUES ('coordinator','tenant','project',?, '2026-01-01','2026-01-01')",
                     (json.dumps({"orchestrator_of": "project"}),))
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('task','Work','todo',3,'project','2026-01-01','2026-01-01')")
    return SimpleNamespace(db=db, extensions={}), settings


async def test_role_alone_is_denied_and_review_approval_replays_once(db: Database) -> None:
    app, _ = await office(db)
    with pytest.raises(ControlDenied, match="no current grant"):
        await resolve_authority(app, session_id="coordinator", project_id="project", task_id="task", operation="review.verdict")
    args = {"client_operation_id": "review-rights", "expected_entity_revision": 1,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    approved = await grant_review(app, "project", OPERATOR, **args)
    assert await grant_review(app, "project", OPERATOR, **args) == approved
    install_authority(app)
    reviewer = await app.extensions["orchestrator_review_authority"](session_id="coordinator", project_id="project",
                                                                     task_id="task", operation="review.verdict")
    assert reviewer.actor_id == "orchestrator:coordinator"
    assert reviewer.grant_id == approved["grant_id"]
    assert (await db.fetchone("SELECT count(*) FROM actor_grants"))[0] == 1
    with pytest.raises(ControlDenied, match="no current grant"):
        await resolve_authority(app, session_id="coordinator", project_id="project", task_id="task", operation="task.launch")


@pytest.mark.parametrize("change", ["disabled", "replaced", "revoked"])
async def test_pending_review_is_denied_after_its_office_or_grant_changes(db: Database, change: str) -> None:
    app, settings = await office(db)
    await grant_review(app, "project", OPERATOR, client_operation_id="rights", expected_entity_revision=1,
                       expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    principal = await resolve_authority(app, session_id="coordinator", project_id="project", task_id="task", operation="review.verdict")
    if change == "revoked":
        await ControlStore(db).revoke_grant(OPERATOR, principal.grant_id, reason="review approval withdrawn")
    else:
        settings["orchestrator"]["enabled"] = change != "disabled"
        settings["orchestrator"]["session_id"] = "replacement" if change == "replaced" else "coordinator"
        await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'", (json.dumps(settings),))

    async def forbidden(conn, mutation):
        pytest.fail("stale coordinator reached a review mutation")

    with pytest.raises(ControlDenied):
        await ControlStore(db).mutate(principal, Scope("project", "project"), "review.verdict", "verdict", 1,
                                      Entity("task", "task"), {}, forbidden)
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts WHERE operation_kind = 'review.verdict'"))[0] == 0


async def test_generic_tool_session_needs_an_explicit_scoped_grant(db: Database) -> None:
    app, _ = await office(db)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,metadata,created_at,last_message_at)"
                     " VALUES ('agent','tenant','project','{\"operator\":true}', '2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens) VALUES ('project',3,20,100000)")
    install_authority(app)
    hook = app.extensions["board_tool_authority"]
    with pytest.raises(ControlDenied, match="no current grant"):
        await hook("agent", "board.task.create")
    grant = await ControlStore(db).issue_grant(OPERATOR, Principal("session:agent", "agent"), Scope("project", "project"),
                                               operations=["board.task.create"], effects=[],
                                               expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    principal, scope = await hook("agent", "board.task.create")
    assert principal.origin_class == "agent" and principal.grant_id == grant["grant_id"]
    revision = await ControlStore(db).revision(scope, Entity("collection", scope.id))
    receipt = await BoardCommands(db).create(principal, scope, client_operation_id="from-tool",
                                             expected_collection_revision=revision, title="Authorized task")
    with pytest.raises(ControlDenied):
        await hook("agent", "board.task.update", receipt["task_id"])
    await ControlStore(db).revoke_grant(OPERATOR, grant["grant_id"], reason="approval withdrawn")
    with pytest.raises(ControlDenied):
        await hook("agent", "board.task.create")
