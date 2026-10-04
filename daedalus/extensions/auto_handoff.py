"""Start a ready successor only from its saved next action and a current coordinator grant."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from daedalus.extensions.coordinator_authority import current_office, resolve_authority
from daedalus.extensions.orchestrator_domain import dependency_readiness, next_action_readiness
from daedalus.extensions.task_launch import queue_launch
from daedalus.host.launch_queue import Entry
from daedalus.stores.control import ControlConflict, ControlDenied

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)
RECOVERY_BATCH_SIZE = 128
RECOVERY_WAKE_SIZE = 512
_continuations: set[asyncio.Task[None]] = set()
_CANDIDATES = (" FROM board_tasks t WHERE t.status = 'todo'"
               " AND (? IS NULL OR t.project_id = ?)"
               " AND EXISTS (SELECT 1 FROM task_dependency_edges e WHERE e.successor_task_id = t.id)"
               " AND EXISTS (SELECT 1 FROM next_actions n WHERE n.task_id = t.id AND n.state = 'active'"
               " AND n.kind = 'assign' AND n.owner_kind = 'staff')")


def _continuation_done(task: asyncio.Task[None]) -> None:
    _continuations.discard(task)
    if not task.cancelled():
        try:
            task.result()
        except Exception:
            logger.exception("automatic handoff recovery continuation failed")


async def _recovery_count(app: Application, project_id: str | None) -> int:
    row = await app.db.fetchone("SELECT count(*)" + _CANDIDATES, (project_id, project_id))
    return row[0]


async def _recovery_batch(app: Application, project_id: str | None, limit: int) -> list[str]:
    """Reserve a circular page before effects so interrupted wakes cannot pin the first page."""
    key = "auto_handoff_recovery:" + (f"project:{project_id}" if project_id is not None else "global")
    query = "SELECT t.id,t.priority,t.created_at" + _CANDIDATES
    order = " ORDER BY t.priority,t.created_at,t.id LIMIT ?"
    async with app.db.transaction() as conn:
        async with conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as reader:
            saved = await reader.fetchone()
        position = json.loads(saved["value"]) if saved is not None else None
        params = (project_id, project_id)
        after = " AND (t.priority,t.created_at,t.id) > (?,?,?)" if position is not None else ""
        async with conn.execute(query + after + order,
                                params + tuple(position or ()) + (limit,)) as reader:
            rows = list(await reader.fetchall())
        if position is not None and len(rows) < limit:
            async with conn.execute(query + " AND (t.priority,t.created_at,t.id) <= (?,?,?)" + order,
                                    params + tuple(position) + (limit - len(rows),)) as reader:
                rows.extend(await reader.fetchall())
        if rows:
            last = rows[-1]
            # Persist selection, rather than success: blocked tasks and crashes used to starve
            # every later task. Launch authority and replay fencing still belong to advance.
            await conn.execute("INSERT INTO kv(key,value) VALUES (?,?)"
                               " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                               (key, json.dumps([last["priority"], last["created_at"], last["id"]])))
        return [row["id"] for row in rows]


async def advance(app: Application, task_id: str) -> dict[str, Any]:
    """Try one saved assignment; a capacity wait leaves no claim to block a later wake."""
    team = app.extensions.get("staff")
    if team is None or "effects" not in app.extensions:
        return {"state": "waiting_capacity", "reason": "staff runtime unavailable"}
    async with team.queue.admission_guard():
        async with app.db.transaction() as conn:
            async with conn.execute("SELECT project_id,status,entity_revision FROM board_tasks WHERE id = ?",
                                    (task_id,)) as cursor:
                task = await cursor.fetchone()
            if task is None or not task["project_id"] or task["status"] != "todo":
                return {"state": "ineligible"}
            readiness = await dependency_readiness(conn, task_id)
            action = await next_action_readiness(conn, task_id)
            if (not readiness["edges"] or not readiness["ready"] or action is None
                    or not action["enabled"] or action["kind"] != "assign"
                    or action["owner_kind"] != "staff" or not action["owner_id"]):
                return {"state": "ineligible"}
            async with conn.execute("SELECT h.id,COALESCE(h.operation_id,o.receipt_id) AS operation_id,h.state"
                                    " FROM handoff_claims h LEFT JOIN effect_outbox o ON o.id = h.reservation_id"
                                    " WHERE h.task_id = ? AND h.dependency_fingerprint = ?",
                                    (task_id, readiness["dependency_fingerprint"])) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                return {"state": existing["state"], "claim_id": existing["id"],
                        "receipt_id": existing["operation_id"]}
            async with conn.execute("SELECT id,name,harness,archived_at FROM staff"
                                    " WHERE id = ? AND project_id = ?",
                                    (action["owner_id"], task["project_id"])) as cursor:
                member = await cursor.fetchone()
            if member is None or member["archived_at"]:
                return {"state": "ineligible", "reason": "assigned worker unavailable"}
            project_id = task["project_id"]
            revision = task["entity_revision"]
            fingerprint = readiness["dependency_fingerprint"]
            action_id = action["action_id"]
        entry = Entry(project_id, member["id"], member["name"], task_id, 1,
                      member["harness"] != "daedalus", "orchestrator")
        if (await team.queue._free(entry) is not None or
                await team.queue._active(project_id) >= await team.queue._concurrency(project_id)):
            return {"state": "waiting_capacity"}
        if entry.terminal:
            folder = team.folder_for(await team.project(project_id), await team.member(member["id"]),
                                     await team.task(task_id))
            entry.env = folder.env
            if await team.queue._machine(entry.env) is not None:
                return {"state": "waiting_capacity"}
        try:
            async with app.db.transaction() as conn:
                session_id = await current_office(conn, project_id)
            principal = await resolve_authority(app, session_id=session_id, project_id=project_id,
                                                operation="task.launch", task_id=task_id)
            receipt = await queue_launch(app, task_id, principal, staff_id=member["id"],
                                         client_operation_id=f"handoff:{action_id}:{fingerprint}",
                                         expected_entity_revision=revision,
                                         handoff_fingerprint=fingerprint)
        except ControlDenied:
            reference = f"handoff-authority:{action_id}:{fingerprint}"
            asked = await app.db.fetchone("SELECT id FROM asks WHERE request_ref = ?", (reference,))
            if asked is None:
                await app.manager.asks.open(project_id, origin="orchestrator", kind="question",
                                            routed_to="operator", task_id=task_id, request_ref=reference,
                                            title="Approve the next assignment",
                                            text="The saved next assignment is ready, but the coordinator has no current launch grant. Approve its scope before work starts.")
            return {"state": "waiting_authority"}
        except ControlConflict as exc:
            return {"state": "stale", "reason": str(exc)}
        return {"state": "assigned", "claim_id": receipt["claim_id"], "effect_id": receipt["effect_id"],
                "receipt_id": (await app.db.fetchone("SELECT operation_id FROM handoff_claims WHERE id = ?",
                                                      (receipt["claim_id"],)))["operation_id"]}


async def on_task_moved(app: Application, event: Any) -> None:
    if event.type == "task.moved" and event.payload.get("to") == "todo":
        try:
            await advance(app, str(event.payload["task_id"]))
        except Exception:
            logger.exception("automatic handoff could not inspect task %s", event.payload.get("task_id"))


async def on_capacity_released(app: Application, event: Any) -> None:
    """Revisit saved waits when an ordinary worker slot becomes available."""
    if event.type == "staff.status" and event.payload.get("status") not in ("exited", "idle", "turn_done_unseen", "error"):
        return
    if event.type == "task.moved" and event.payload.get("to") not in ("done", "dropped", "review"):
        return
    await recover(app, project_id=event.project_id)


async def recover(app: Application, *, project_id: str | None = None) -> None:
    """Inspect one finite candidate snapshot, yielding between bounded work chunks."""
    await _recover_remaining(app, project_id, await _recovery_count(app, project_id))


async def _recover_remaining(app: Application, project_id: str | None, remaining: int) -> None:
    budget = min(remaining, RECOVERY_WAKE_SIZE)
    while budget:
        task_ids = await _recovery_batch(app, project_id, min(budget, RECOVERY_BATCH_SIZE))
        if not task_ids:
            return
        for task_id in task_ids:
            try:
                await advance(app, task_id)
            except Exception:
                logger.exception("automatic handoff recovery could not inspect task %s", task_id)
        remaining -= len(task_ids)
        budget -= len(task_ids)
    if remaining:
        # A large blocked queue must keep moving without waiting for another capacity event.
        task = asyncio.create_task(_recover_remaining(app, project_id, remaining))
        _continuations.add(task)
        task.add_done_callback(_continuation_done)
