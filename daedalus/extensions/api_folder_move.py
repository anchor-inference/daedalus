"""The routes of a folder move between the container and the host (:mod:`daedalus.host.folder_move`).

The dialog first asks what a move of the folder would be — where it would go, and what stops it now —
then starts one, and follows it until it ends. A move runs in the background, so the operator may
close the dialog and come back to it; asking for the plan again names the move still running.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.host.folder_move import FolderMover, MoveRefused, iter_jobs

if TYPE_CHECKING:
    from daedalus.app import Application


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(default="", max_length=4096)
    """Where on the host a container folder goes; empty is the default the plan proposed. A folder
    moved into the container always goes where a new container folder would."""
    remove_original: bool = False
    """Moving into the container: remove the host folder once the copy is verified and in use."""


def mover_of(app: Application) -> FolderMover:
    """The one mover of this process, made the first time a route needs it."""
    mover = app.extensions.get("folder_mover")
    if mover is None:
        manager = app.manager
        assert manager is not None

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            await manager.bus.publish(event_type, payload, project_id=payload.get("project_id"))

        mover = FolderMover(manager, lambda: app.extensions.get("host_bridge"), workspaces_dir=app.settings.workspaces_dir, publish=publish)
        app.extensions["folder_mover"] = mover
    return mover


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/projects/{project_id}/folders/{folder_id}/move")
    async def move_plan(project_id: str, folder_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Where the folder would go, the default destination, and what stops a move now (``problem``)."""
        try:
            return await mover_of(app).plan(project_id, folder_id)
        except KeyError as exc:
            raise HTTPException(404, "no such folder in this project") from exc

    @api.post("/api/projects/{project_id}/folders/{folder_id}/move")
    async def move_start(project_id: str, folder_id: str, body: MoveBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start moving the folder; the answer is the job, to follow at ``/api/folder-moves/{id}``."""
        try:
            job = await mover_of(app).start(project_id, folder_id, destination=body.destination, remove_original=body.remove_original)
        except KeyError as exc:
            raise HTTPException(404, "no such folder in this project") from exc
        except MoveRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        return job.view()

    @api.get("/api/folder-moves")
    async def move_list(folder_id: str = "", active: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"jobs": [job.view() for job in iter_jobs(mover_of(app), folder_id=folder_id, active_only=active)]}

    @api.get("/api/folder-moves/{job_id}")
    async def move_get(job_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return mover_of(app).get(job_id).view()
        except KeyError as exc:
            raise HTTPException(404, "no such move") from exc

    @api.post("/api/folder-moves/{job_id}/cancel")
    async def move_cancel(job_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Stop a move that is still copying; the folder stays where it was and the copy is removed."""
        try:
            return mover_of(app).cancel(job_id).view()
        except KeyError as exc:
            raise HTTPException(404, "no such move") from exc
        except MoveRefused as exc:
            raise HTTPException(409, str(exc)) from exc
