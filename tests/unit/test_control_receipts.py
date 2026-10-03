"""A committed command survives retries; stale or revoked work has no second effect."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})
GLOBAL = Scope("global", "global")


async def project(db: Database, project_id: str = "project") -> Scope:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES (?,?,'2026-01-01','{}')", (project_id, project_id))
    return Scope("project", project_id)


async def task(db: Database, scope: Scope, task_id: str = "task") -> Entity:
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,created_at,updated_at,project_id) VALUES (?,'Original','todo',3,'2026-01-01','2026-01-01',?)", (task_id, scope.id if scope.kind == "project" else None))
    return Entity("task", task_id)


async def test_retry_after_response_loss_replays_before_stale_cas(db: Database) -> None:
    store = ControlStore(db)
    scope = await project(db)
    entity = await task(db, scope)
    calls = 0

    async def edit(conn: Any, mutation: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await conn.execute("UPDATE board_tasks SET title = 'Changed' WHERE id = 'task'")
        return {"task_id": "task", "title": "Changed"}

    args = (OPERATOR, scope, "task.edit", "command", 1, entity, {"title": "Changed"}, edit)
    committed = await store.mutate(*args)
    await db.execute("UPDATE board_tasks SET notes = 'A later operation' WHERE id = 'task'")
    assert await store.mutate(*args) == committed
    assert calls == 1
    assert committed["entity_revision"] == 2
    with pytest.raises(ControlConflict, match="different request"):
        await store.mutate(OPERATOR, scope, "task.edit", "command", 1, entity, {"title": "Different"}, edit)
    with pytest.raises(ControlConflict) as stale:
        await store.mutate(OPERATOR, scope, "task.edit", "another", 1, entity, {}, edit)
    assert stale.value.current_revision == 3
    assert calls == 1


async def test_simultaneous_connections_commit_one_command(db: Database) -> None:
    scope = await project(db)
    entity = await task(db, scope)
    second = Database(db.path, workspaces_dir=db.workspaces_dir)
    await second.open()
    calls = 0

    async def edit(conn: Any, mutation: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await conn.execute("UPDATE board_tasks SET title = 'One effect' WHERE id = 'task'")
        return {"task_id": "task"}

    try:
        args = (OPERATOR, scope, "task.edit", "same-command", 1, entity, {}, edit)
        first, repeated = await asyncio.gather(ControlStore(db).mutate(*args), ControlStore(second).mutate(*args))
        assert first == repeated
        assert calls == 1
        assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 1
    finally:
        await second.close()


async def test_failed_effect_rolls_back_revision_receipt_and_changes(db: Database) -> None:
    scope = await project(db)
    entity = await task(db, scope)

    async def fail(conn: Any, mutation: Any) -> dict[str, Any]:
        await conn.execute("UPDATE board_tasks SET title = 'Must roll back' WHERE id = 'task'")
        raise RuntimeError("effect failed")

    with pytest.raises(RuntimeError, match="effect failed"):
        await ControlStore(db).mutate(OPERATOR, scope, "task.edit", "failed", 1, entity, {}, fail)
    row = await db.fetchone("SELECT title,entity_revision FROM board_tasks WHERE id = 'task'")
    assert (row["title"], row["entity_revision"]) == ("Original", 1)
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 0


async def test_create_identity_and_collection_revision_are_stable(db: Database) -> None:
    store = ControlStore(db)

    async def create(conn: Any, mutation: Any) -> dict[str, Any]:
        await conn.execute("INSERT INTO projects(id,name,created_at,settings) VALUES (?,'New','2026-01-01','{}')", (mutation.object_id,))
        return {"project_id": mutation.object_id}

    args = (OPERATOR, GLOBAL, "project.create", "new-project", 1, Entity("collection", "global"), {"name": "New"}, create)
    result = await store.mutate(*args)
    assert result["entity_revision"] == 2
    assert await store.mutate(*args) == result
    assert (await db.fetchone("SELECT count(*) FROM projects"))[0] == 1
    assert await store.revision(Scope("project", result["project_id"]), Entity("collection", result["project_id"])) == 1


async def test_revoked_authority_cannot_replay_a_successful_command(db: Database) -> None:
    store = ControlStore(db)
    scope = await project(db)
    entity = await task(db, scope)
    subject = Principal("agent:worker", "agent")
    grant = await store.issue_grant(OPERATOR, subject, scope, operations=["task.edit"], effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), task_id="task")
    actor = Principal(subject.actor_id, "agent", grant["grant_id"], grant["generation"])

    async def edit(conn: Any, mutation: Any) -> dict[str, Any]:
        await conn.execute("UPDATE board_tasks SET title = 'Authorized' WHERE id = 'task'")
        return {"task_id": "task"}

    args = (actor, scope, "task.edit", "one", 1, entity, {}, edit)
    await store.mutate(*args)
    await store.revoke_grant(OPERATOR, grant["grant_id"], reason="scope withdrawn")
    with pytest.raises(ControlDenied, match="revoked"):
        await store.mutate(*args)
    new_grant = await store.issue_grant(OPERATOR, subject, scope, operations=["task.edit"], effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), task_id="task")
    new_actor = Principal(subject.actor_id, "agent", new_grant["grant_id"], 1)
    result = await store.mutate(new_actor, *args[1:])
    assert result["entity_revision"] == 2
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts"))[0] == 1


async def test_forged_origin_and_cross_project_grants_are_denied(db: Database) -> None:
    store = ControlStore(db)
    scope = await project(db)
    other = await project(db, "other")
    entity = await task(db, scope)
    foreign = await task(db, other, "foreign")
    subject = Principal("plugin:widget", "plugin")
    grant = await store.issue_grant(OPERATOR, subject, scope, operations=["task.edit"], effects=["filesystem.write"], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())

    async def forbidden(conn: Any, mutation: Any) -> dict[str, Any]:
        pytest.fail("a denied command reached its effect")

    with pytest.raises(ControlDenied, match="host-issued"):
        await store.mutate(subject, scope, "task.edit", "forged", 1, entity, {"origin": "operator"}, forbidden)
    actor = Principal(subject.actor_id, "plugin", grant["grant_id"], 1)
    with pytest.raises(ControlDenied, match="another project"):
        await store.mutate(actor, other, "task.edit", "cross", 1, foreign, {}, forbidden)
    with pytest.raises(ControlDenied, match="command scope"):
        await store.mutate(OPERATOR, scope, "task.edit", "wrong-entity", 1, foreign, {}, forbidden)
    with pytest.raises(ControlDenied, match="effect"):
        await store.mutate(actor, scope, "task.edit", "unapproved-effect", 1, entity, {}, forbidden, effects=("network.external",))
    with pytest.raises(ControlDenied, match="authenticated operator"):
        Principal.operator({"via": "tool-output", "user_id": 1})


async def test_expired_or_changed_request_does_not_modify_state(db: Database) -> None:
    store = ControlStore(db)
    scope = await project(db)
    entity = await task(db, scope)
    subject = Principal("agent:worker", "agent")
    grant = await store.issue_grant(OPERATOR, subject, scope, operations=["task.edit"], effects=[], expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    await db.execute("UPDATE actor_grants SET expires_at = ? WHERE id = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), grant["grant_id"]))

    async def forbidden(conn: Any, mutation: Any) -> dict[str, Any]:
        pytest.fail("expired authority reached its effect")

    with pytest.raises(ControlDenied, match="expired"):
        await store.mutate(Principal(subject.actor_id, "agent", grant["grant_id"], 1), scope, "task.edit", "expired", 1, entity, {}, forbidden)
    assert await store.revision(scope, entity) == 1
