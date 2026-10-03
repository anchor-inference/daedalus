"""Committed launch commands retain priority, project fairness and an inspectable wait."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from daedalus.stores.control import ControlStore, Entity, Principal, Scope, now
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore

OPERATOR = Principal.operator({"via": "token", "user_id": 1})


async def task(db: Database, tmp_path: Path, project_id: str, task_id: str, priority: int) -> None:
    root = tmp_path / project_id
    root.mkdir(exist_ok=True)
    await db.execute("INSERT OR IGNORE INTO projects(id,name,created_at) VALUES (?,?,?)",
                     (project_id, project_id, now()))
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES (?,?,'todo',?,?,?,?)", (task_id, task_id, priority, project_id, now(), now()))


async def enqueue(db: Database, project_id: str, task_id: str, kind: str = "task.launch") -> str:
    control = ControlStore(db)
    scope = Scope("project", project_id)
    principal = OPERATOR
    operation = "task.launch" if kind == "task.launch" else "task.stop"

    async def effect(conn: Any, mutation: Any) -> dict[str, str]:
        action_id = await OutboxStore.enqueue(conn, mutation, principal, kind=kind, operation=operation,
                                              payload={"task_id": task_id}, task_id=task_id)
        return {"effect_id": action_id}

    result = await control.mutate(principal, scope, operation, uuid.uuid4().hex,
                                  await control.revision(scope, Entity("task", task_id)),
                                  Entity("task", task_id), {}, effect)
    return result["effect_id"]


async def test_urgent_task_precedes_older_low_priority_task_within_project(db: Database, tmp_path: Path) -> None:
    await task(db, tmp_path, "project", "low", 4)
    await task(db, tmp_path, "project", "urgent", 1)
    low = await enqueue(db, "project", "low")
    urgent = await enqueue(db, "project", "urgent")
    store = OutboxStore(db)
    assert (await store.view(urgent))["wait_position"] == 1
    assert (await store.view(low))["wait_position"] == 2
    first = await store.claim(("task.launch",))
    assert first is not None and first.id == urgent
    assert await store.finish(first, state="completed")
    second = await store.claim(("task.launch",))
    assert second is not None and second.id == low


async def test_projects_rotate_and_stop_cannot_wait_behind_many_launches(db: Database, tmp_path: Path) -> None:
    for name, project, priority in (("a-low", "a", 4), ("a-urgent", "a", 1),
                                    ("b-low", "b", 4), ("b-urgent", "b", 1)):
        await task(db, tmp_path, project, name, priority)
        await enqueue(db, project, name)
    stop_id = await enqueue(db, "a", "a-low", kind="task.stop")
    store = OutboxStore(db)
    stop = await store.claim(("task.launch", "task.stop"))
    assert stop is not None and stop.id == stop_id
    assert await store.finish(stop, state="completed")
    first = await store.claim(("task.launch",))
    assert first is not None and first.task_id == "a-urgent"
    assert await store.finish(first, state="completed")
    second = await store.claim(("task.launch",))
    assert second is not None and second.task_id == "b-urgent"
    assert await store.finish(second, state="completed")
    third = await store.claim(("task.launch",))
    assert third is not None and third.task_id == "a-low"


async def test_deferred_reason_and_position_survive_store_restart(db: Database, tmp_path: Path) -> None:
    await task(db, tmp_path, "project", "low", 4)
    await task(db, tmp_path, "project", "urgent", 1)
    low = await enqueue(db, "project", "low")
    urgent = await enqueue(db, "project", "urgent")
    store = OutboxStore(db)
    claim = await store.claim(("task.launch",))
    assert claim is not None and claim.id == urgent
    assert await store.defer(claim, reason='{"detail":"all slots occupied","reason":"project"}')
    resumed = OutboxStore(db)
    view = await resumed.view(urgent)
    assert (view["state"], view["wait_reason"], view["wait_detail"], view["wait_position"]) == (
        "pending", "project", "all slots occupied", 1,
    )
    assert (await resumed.view(low))["wait_position"] == 2


async def test_foreground_commands_are_bounded_so_launches_make_progress(db: Database, tmp_path: Path) -> None:
    await task(db, tmp_path, "project", "launch", 1)
    launch = await enqueue(db, "project", "launch")
    for number in range(9):
        task_id = f"stop-{number}"
        await task(db, tmp_path, "project", task_id, 3)
        await enqueue(db, "project", task_id, kind="task.stop")
    store = OutboxStore(db)
    for _ in range(8):
        foreground = await store.claim(("task.stop", "task.launch"))
        assert foreground is not None and foreground.kind == "task.stop"
        assert await store.finish(foreground, state="completed")
    admitted = await store.claim(("task.stop", "task.launch"))
    assert admitted is not None and admitted.id == launch
