"""The Terminals workspace's side panel and its named layouts, as REST routes.

- ``GET/POST /api/terminals/layouts``, ``PATCH/DELETE /api/terminals/layouts/{id}``: the layouts
  kept on the host (:mod:`daedalus.terminals.layouts`). They do not need the terminal daemons, so they
  answer while every environment is down: a layout is restored into empty panes then, with a note.
- ``GET /api/terminals/{id}/files`` and ``/files/diff?path=``: what changed in the terminal's folder
  (:mod:`daedalus.terminals.changes`). A staff member's terminal is read in the member's worktree
  against the base its session started from, which is the member's own work; any other terminal in
  the folder its shell is in now, against the last commit.

These are registered before ``/api/terminals/{terminal_id}``: that route would otherwise take
``layouts`` for a terminal id and answer 404.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.terminals.changes import GIT_ENV, ChangesRefused, changed_files, file_diff
from daedalus.terminals.layouts import LayoutError, TerminalLayouts

if TYPE_CHECKING:
    from daedalus.terminals.service import Terminals

CHANGES_TIMEOUT_S = 20.0


class LayoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    workspace: dict[str, Any]


class LayoutRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


def register(api: FastAPI, app: Any, auth: Callable[..., Any], service: Callable[[], Terminals]) -> None:
    layouts = TerminalLayouts(app.db)

    def refused(exc: LayoutError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    @api.get("/api/terminals/layouts")
    async def terminal_layouts(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every saved layout, the one saved or changed last first."""
        return {"layouts": await layouts.list()}

    @api.post("/api/terminals/layouts", status_code=201)
    async def terminal_layout_save(body: LayoutBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Saves the workspace under a name; saving under a name already kept replaces that layout."""
        try:
            return await layouts.save(body.name, body.workspace)
        except LayoutError as exc:
            raise refused(exc) from None

    @api.patch("/api/terminals/layouts/{layout_id}")
    async def terminal_layout_rename(layout_id: str, body: LayoutRename, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await layouts.rename(layout_id, body.name)
        except LayoutError as exc:
            raise refused(exc) from None

    @api.delete("/api/terminals/layouts/{layout_id}")
    async def terminal_layout_delete(layout_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await layouts.delete(layout_id)
        except LayoutError as exc:
            raise refused(exc) from None
        return {"ok": True}

    async def folder_of(terminal_id: str) -> tuple[str, str, str | None, str]:
        """The environment, the folder and the base to compare against, and which of the two sources
        it is (``staff`` or ``shell``)."""
        terminals = service()
        view = await terminals.get(terminal_id)
        env = str(view["env"])
        member = await app.db.fetchone(
            "SELECT worktree_path, base_ref FROM staff_sessions WHERE terminal_id = ? AND COALESCE(worktree_path, '') != '' ORDER BY started_at DESC LIMIT 1",
            (terminal_id,),
        )
        if member is not None:
            return env, str(member["worktree_path"]), (str(member["base_ref"]) if member["base_ref"] else None), "staff"
        return env, str(view["cwd"] or ""), None, "shell"

    def runner(env: str) -> Any:
        terminals = service()

        async def run(argv: list[str], max_output: int) -> Any:
            return await terminals.exec_run(env, argv, env_vars=dict(GIT_ENV), timeout=CHANGES_TIMEOUT_S, max_output=max_output, actor="operator")

        return run

    @api.get("/api/terminals/{terminal_id}/files")
    async def terminal_files(terminal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The files changed in the terminal's folder; ``reason`` says why there are none to list
        (``not_git``, ``git_failed``) and ``detail`` is git's own sentence."""
        env, folder, base, source = await folder_of(terminal_id)
        if not folder:
            return {"source": source, "root": "", "branch": "", "base": "", "files": [], "truncated": False, "reason": "no_folder", "detail": ""}
        return {"source": source, **await changed_files(runner(env), folder, base=base)}

    @api.get("/api/terminals/{terminal_id}/files/diff")
    async def terminal_file_diff(terminal_id: str, path: str = Query(min_length=1, max_length=4096), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        env, folder, base, source = await folder_of(terminal_id)
        if not folder:
            raise HTTPException(409, "the terminal has no folder to compare")
        try:
            return {"source": source, **await file_diff(runner(env), folder, path, base=base)}
        except ChangesRefused as exc:
            raise HTTPException(400, str(exc)) from None


__all__ = ["register"]
