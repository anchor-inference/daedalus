"""Authorized reads of saved execution attempt diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

from daedalus.stores.attempt_diagnostics import attempt_diagnostics
from daedalus.stores.attempt_faults import attempt_fault_rows
from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope, one

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    """Register task-bound diagnostics under the host's operator authentication."""
    @api.get("/api/board/{task_id}/attempts/{attempt_id}/diagnostics")
    async def diagnostics(task_id: str, attempt_id: str,
                          who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            principal = Principal.operator(who)
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        async with app.db.transaction() as conn:
            # The requested task and attempt must match before reading faults, including old attempts.
            attempt = await one(conn, "SELECT a.id,a.task_id,a.state,t.project_id FROM execution_attempts a"
                                " JOIN board_tasks t ON t.id = a.task_id"
                                " WHERE a.id = ? AND a.task_id = ?", (attempt_id, task_id))
            if attempt is None:
                raise HTTPException(404, "no such attempt for task")
            scope = Scope("project", attempt["project_id"]) if attempt["project_id"] else Scope("global", "global")
            try:
                await ControlStore(app.db).authorize(conn, principal, scope, "control.read", task_id=task_id)
            except ControlDenied as exc:
                raise HTTPException(403, str(exc)) from exc
            faults = await attempt_fault_rows(conn, attempt_id)
        return attempt_diagnostics(dict(attempt), faults)
