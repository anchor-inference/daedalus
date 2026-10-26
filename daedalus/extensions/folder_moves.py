"""Folder moves between the container and the host (:mod:`daedalus.host.folder_move`).

The one mover of this process is made at the start rather than by the first request for it, so the
moves the last process left running are settled before the app asks about them, and the partial
copies they left on the host are removed once the host daemon answers. A move that ends is told of
in the notifications as well as on its card, because the operator who started a move of half a
gigabyte has usually gone to do something else by the time it ends.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft, Tone
from daedalus.host.folder_move import TO_HOST, FolderMover, MoveJob

if TYPE_CHECKING:
    from daedalus.app import Application


def mover_of(app: Application) -> FolderMover:
    """The one mover of this process, made the first time anything needs it."""
    mover = app.extensions.get("folder_mover")
    if mover is None:
        manager = app.manager
        assert manager is not None

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            await manager.bus.publish(event_type, payload, project_id=payload.get("project_id"))

        async def notify(job: MoveJob) -> None:
            await tell(app, job)

        mover = FolderMover(manager, lambda: app.extensions.get("host_bridge"), workspaces_dir=app.settings.workspaces_dir, publish=publish, notify=notify)
        app.extensions["folder_mover"] = mover
    return mover


async def tell(app: Application, job: MoveJob) -> None:
    """The notification of a move that ended: where the folder is now, or why it is where it was."""
    notifications = getattr(app, "notifications", None)
    manager = app.manager
    if notifications is None or manager is None:
        return
    project = await manager.projects.get(job.project_id)
    name = project.name if project is not None else job.source
    link = ""
    if project is not None:
        link = f"/app/project/{project.id}"
        if project.settings.ephemeral:
            sessions = await manager.projects.sessions_of(project.id)
            if sessions:
                link = f"/app/agents/{sessions[0]['id']}"
    where = "to the host" if job.direction == TO_HOST else "into the container"
    tone: Tone
    if job.state == "done":
        title, tone, body = f"Moved {name} {where}", "ok", f"The folder is now at {job.destination}."
    elif job.state == "interrupted":
        title, tone, body = f"Moving {name} {where} was interrupted", "warning", f"{job.error}. Open the move to start it again."
    else:
        title, tone, body = f"Moving {name} {where} failed", "error", job.error
    if job.notes:
        body = "\n".join([body, *job.notes])
    await notifications.post(Draft(
        "system", title, body, kind="folder_move", tone=tone, project_id=job.project_id, link=link,
        dedupe_key=f"folder-move:{job.id}",
    ))


async def install(app: Application) -> list[asyncio.Task[None]]:
    mover = mover_of(app)
    await mover.recover()
    return [asyncio.create_task(mover.clean_leftovers(), name="folder-move-leftovers")]


__all__ = ["install", "mover_of", "tell"]
