"""``POST /api/reveal``: show a session's or a project's folder, or a file in it, in the operator's
file manager.

Only on a native installation and only for a request from this same machine — anywhere else the
file manager is not the operator's, and the route answers as if it did not exist (404) or refuses
the caller (403). The path is always relative to a folder the session or the project already owns,
and :func:`daedalus.host.reveal.confine` refuses anything that resolves outside those folders.

Inside the desktop window the shell does the opening itself: the app asks with ``run: false`` and
gets back the confined absolute path to hand to the shell, so the shell never opens a path the host
has not checked.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from daedalus.host.reveal import (
    Platform,
    RevealRefused,
    Runner,
    confine,
    is_local_client,
    platform_family,
    reveal,
    start_detached,
)

if TYPE_CHECKING:
    from daedalus.app import Application


class RevealRequest(BaseModel):
    session_id: str = ""
    project_id: str = ""
    folder_id: str = ""
    path: str = ""
    run: bool = True


def available(native: bool, request: Request) -> bool:
    """Whether this request may reveal anything: a native installation, asked from its own machine."""
    return native and is_local_client(request.client.host if request.client else None)


def register(api: FastAPI, app: Application, auth: Callable[..., Any], *, runner: Runner = start_detached, platform: Platform | None = None) -> None:
    manager = app.manager
    assert manager is not None
    family = platform or platform_family()

    async def session_roots(session_id: str, folder_id: str) -> tuple[list[Path], Path]:
        """The workspace and the project's folders this session can read, and the one a relative path is taken from."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        services = state.services
        project = state.project
        local_env = manager.projects.local_env
        readable = [f.path for f in (project.local_folders(local_env) if project is not None else ()) if services is None or services.contains(f.path)]
        roots = [state.workspace, *readable]
        if not folder_id:
            return roots, state.workspace
        folder = project.folder(folder_id) if project is not None else None
        if folder is None or folder.path not in readable:
            raise HTTPException(404, "no such folder in this session's project")
        return roots, folder.path

    async def project_roots(project_id: str, folder_id: str) -> tuple[list[Path], Path]:
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        # Only this machine's folders: a folder of the container environment names a path that means
        # something else here, or nothing at all.
        folders = [f for f in project.local_folders(manager.projects.local_env) if f.reachable]
        if not folders:
            raise HTTPException(404, "this project has no folder on this machine")
        chosen = next((f for f in folders if f.id == folder_id), None) if folder_id else folders[0]
        if chosen is None:
            raise HTTPException(404, "no such folder in this project")
        return [f.path for f in folders], chosen.path

    @api.post("/api/reveal")
    async def reveal_path(body: RevealRequest, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Show a folder, or select a file in its folder, in the file manager of this machine.

        Exactly one of ``session_id`` and ``project_id``; ``folder_id`` picks another folder of it,
        and ``path`` is relative to that folder (empty for the folder itself). With ``run`` false
        nothing is opened and only the checked path comes back, for the desktop shell to open.
        """
        if not app.settings.native:
            raise HTTPException(404, "revealing files is offered only on a native installation")
        if not available(True, request):
            raise HTTPException(403, "only a page on the machine Daedalus runs on can open its file manager")
        if bool(body.session_id) == bool(body.project_id):
            raise HTTPException(400, "name either a session or a project")
        roots, base = await (session_roots(body.session_id, body.folder_id) if body.session_id else project_roots(body.project_id, body.folder_id))
        try:
            target = confine(body.path, roots, base=base)
            if body.run:
                reveal(target, platform=family, runner=runner)
        except RevealRefused as exc:
            raise HTTPException(403, str(exc)) from None
        except OSError as exc:
            raise HTTPException(500, f"the file manager could not be started: {exc.strerror or exc}") from None
        return {"path": str(target.path), "directory": target.directory, "opened": body.run, "platform": family}


__all__ = ["RevealRequest", "available", "register"]
