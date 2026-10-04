"""Observe one daemon-owned attempt handle; a terminal exit alone is not whole-tree exit."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.stores.control import ControlConflict
from daedalus.stores.resource_profiles import observe_in

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


async def binding(app: Application, attempt_id: str) -> dict[str, Any] | None:
    row = await app.db.fetchone("SELECT attempt_id,host_generation,daemon_instance,launch_id,env"
                                " FROM attempt_resource_bindings WHERE attempt_id = ?"
                                " UNION ALL SELECT attempt_id,host_generation,daemon_instance,launch_id,env"
                                " FROM writer_attempt_bindings WHERE attempt_id = ?", (attempt_id, attempt_id))
    if row is None or not row["launch_id"]:
        return None
    scope = {"attempt_id": row["attempt_id"], "host_generation": row["host_generation"],
             "launch_id": row["launch_id"]}
    return {"scope": scope, "daemon_instance": row["daemon_instance"], "env": row["env"]}


async def record(app: Application, target: dict[str, Any], *, kind: str,
                 observation: dict[str, Any], terminal_id: str | None = None) -> bool:
    scope = target["scope"]
    evidence = dict(observation.get("evidence") or observation)
    evidence["scope"] = scope
    evidence["daemon_instance"] = target["daemon_instance"]
    async with app.db.transaction() as conn:
        host_generation = await app.executions._host(conn)
        if str(host_generation) != scope["host_generation"] and kind != "unknown":
            raise ControlConflict("the host generation changed before resource evidence was recorded")
        await observe_in(conn, attempt_id=scope["attempt_id"], launch_id=scope["launch_id"],
                         kind=kind, evidence=evidence, terminal_id=terminal_id)
    return bool(evidence.get("enforced") and evidence.get("populated") is False)


async def reconcile(app: Application, attempt_id: str) -> bool:
    target = await binding(app, attempt_id)
    if target is None:
        return False
    async with app.db.transaction() as conn:
        generation = await app.executions._host(conn)
    if str(generation) != target["scope"]["host_generation"]:
        await record(app, target, kind="unknown", observation={"enforced": False,
                                                                 "reason": "the host generation changed"})
        return False
    try:
        observation = await app.extensions["terminals"].observe_attempt(
            target["env"], target["scope"], daemon_instance=target["daemon_instance"])
    except Exception as exc:
        await record(app, target, kind="unknown", observation={"enforced": False,
                                                                 "reason": str(exc)})
        return False
    empty = await record(app, target, kind="exit" if not observation.get("populated") else "sample",
                         observation=observation)
    if empty:
        await app.extensions["terminals"].release_attempt(target["env"], target["scope"],
                                                            daemon_instance=target["daemon_instance"])
    return empty


async def stop(app: Application, attempt_id: str) -> bool:
    target = await binding(app, attempt_id)
    if target is None:
        return False
    async with app.db.transaction() as conn:
        generation = await app.executions._host(conn)
    if str(generation) != target["scope"]["host_generation"]:
        # A host restart invalidates the old authority even when the daemon still answers.
        raise ControlConflict("the host generation changed before attempt containment could be stopped")
    try:
        result = await app.extensions["terminals"].kill_attempt(
            target["env"], target["scope"], daemon_instance=target["daemon_instance"])
    except Exception as exc:
        await record(app, target, kind="unknown", observation={"enforced": False,
                                                                 "reason": str(exc)})
        return False
    evidence = result.get("evidence")
    complete = (result.get("complete") is True and isinstance(evidence, dict)
                and evidence.get("enforced") is True and evidence.get("populated") is False)
    empty = await record(app, target, kind="stop" if complete else "sample", observation=result)
    if not complete:
        return False
    if empty:
        await app.extensions["terminals"].release_attempt(target["env"], target["scope"],
                                                            daemon_instance=target["daemon_instance"])
    return empty


class ResourceMonitor:
    """Reconcile a verified main exit when descendants leave the cgroup later."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="attempt-resource-observer")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(5)
            async with self.app.db.transaction() as conn:
                generation = await self.app.executions._host(conn)
                cursor = await conn.execute(
                    "SELECT b.attempt_id FROM attempt_resource_bindings b"
                    " WHERE (b.state = 'enforced' OR (b.state = 'unknown' AND b.host_generation = ?))"
                    " AND b.launch_id IS NOT NULL"
                    " AND EXISTS(SELECT 1 FROM runtime_exit_observations e WHERE e.attempt_id = b.attempt_id)"
                    " UNION ALL SELECT b.attempt_id FROM writer_attempt_bindings b"
                    " WHERE (b.state = 'enforced' OR (b.state = 'unknown' AND b.host_generation = ?))"
                    " AND b.launch_id IS NOT NULL"
                    " AND EXISTS(SELECT 1 FROM runtime_exit_observations e WHERE e.attempt_id = b.attempt_id)",
                    (str(generation), str(generation)),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            for row in rows:
                try:
                    await reconcile(self.app, row["attempt_id"])
                except Exception:
                    logger.exception("an exited attempt's resource containment could not be reconciled")
