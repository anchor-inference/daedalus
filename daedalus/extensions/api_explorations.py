"""The explorations a card belongs to, for the card in the app: read-only, decided by the orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/board/{task_id}/explorations")
    async def explorations(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = app.extensions.get("explorations")
        if service is None:
            raise HTTPException(503, "explorations are not available")
        if await app.db.fetchone("SELECT 1 FROM board_tasks WHERE id = ?", (task_id,)) is None:
            raise HTTPException(404, "no such task")
        return {"task_id": task_id, "explorations": await service.for_task(task_id)}


__all__ = ["register"]
