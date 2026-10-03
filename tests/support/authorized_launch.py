"""Launch a fixture worker through the same receipt and attempt path as an operator."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from daedalus.extensions.board_commands import BoardCommands
from daedalus.stores.control import ControlStore, Entity, Principal, Scope


async def operator_task(db: Any, project_id: str, title: str, *, priority: int = 3,
                        brief: dict[str, str] | None = None, folder_id: str | None = None) -> str:
    """Create a task whose contract snapshot exists before any worker can claim it."""
    scope = Scope("project", project_id)
    revision = await ControlStore(db).revision(scope, Entity("collection", project_id))
    response = await BoardCommands(db).create(
        Principal.operator({"via": "token", "user_id": 1}), scope,
        client_operation_id=f"test-task:{uuid.uuid4().hex}",
        expected_collection_revision=revision, title=title, priority=priority, brief=brief or {},
        folder_id=folder_id,
    )
    return response["task_id"]


async def operator_assignment(team: Any, member: Any, task_id: str | dict[str, Any], *,
                              resume_from: str | None = None,
                              wait_for_admission: bool = True) -> dict[str, Any]:
    """Return the real queued receipt; optionally wait until its effect admitted a worker."""
    db = team.app.db
    task_id = str(task_id["id"]) if isinstance(task_id, dict) else task_id
    task = await db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None or not task["project_id"]:
        raise AssertionError("fixture task needs a project and immutable contract")
    scope = Scope("project", task["project_id"])
    revision = await ControlStore(db).revision(scope, Entity("task", task_id))
    result = await team.assign(
        member, task_id, principal=Principal.operator({"via": "token", "user_id": 1}),
        client_operation_id=f"test-launch:{uuid.uuid4().hex}",
        expected_entity_revision=revision, resume_from=resume_from,
    )
    if wait_for_admission:
        async with asyncio.timeout(30):
            while True:
                action = await db.fetchone("SELECT state,error FROM effect_outbox WHERE id = ?",
                                           (result["effect_id"],))
                if action is not None and action["state"] == "completed":
                    break
                if action is not None and action["state"] in ("failed", "unknown"):
                    raise AssertionError(f"fixture launch did not admit a worker: {action['error']}")
                await asyncio.sleep(0.05)
    return result


__all__ = ["operator_task", "operator_assignment"]
