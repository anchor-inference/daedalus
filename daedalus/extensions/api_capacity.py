"""Authenticated projections of host admission and durable waiting claims."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI

from daedalus.stores.capacity import snapshot_in

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/admission/capacity")
    async def capacity(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            generation = await app.executions._host(conn)
            return await snapshot_in(conn, generation=generation,
                                     cap=app.config.terminals.running_cap)

    @api.get("/api/admission/queue")
    async def queue(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            generation = await app.executions._host(conn)
            await snapshot_in(conn, generation=generation, cap=app.config.terminals.running_cap)
            async with conn.execute(
                "SELECT project_id,role_class,reason FROM scheduler_claims WHERE state = 'waiting'"
                " ORDER BY CASE role_class WHEN 'coordinator' THEN 0 WHEN 'reviewer' THEN 1 ELSE 2 END,"
                " age_credit DESC,enqueue_seq") as cursor:
                rows = await cursor.fetchall()
        return {"entries": [{"project_id": row["project_id"], "role_class": row["role_class"],
                             "position": index, "reason": row["reason"] or "capacity"}
                            for index, row in enumerate(rows, start=1)]}
