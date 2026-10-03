"""Authenticated task-scoped access to the complete immutable worker report."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Response

from daedalus.extensions.staff_results import StaffReportService

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/board/{task_id}/staff-reports/{report_id}/original")
    async def original(task_id: str, report_id: str,
                       _: dict[str, Any] = Depends(auth)) -> Response:
        task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
        if task is None or not task["project_id"]:
            raise HTTPException(404, "no such project task")
        try:
            content = await StaffReportService(app).original(task["project_id"], task_id, report_id)
        except KeyError as exc:
            raise HTTPException(404, "no such report for this task") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(content, media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
