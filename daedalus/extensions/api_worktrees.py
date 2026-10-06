"""The worker worktrees of a project: the list the operator cleans up from, and the one safe removal.

A staff member's worktree outlives its tasks on purpose (its installed dependencies stay installed),
so over months a project folder gathers worktrees of members long dismissed. Nothing else lists
them. Removal is offered only for a worktree nobody works in and that holds nothing uncommitted, and
it goes through :class:`StaffWorktrees` so the same per-repository lock and the same refusal rules
apply as when the team removes one itself. Like ``api.py`` this module may import the HTTP framework.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.host.gitrun import GitError
from daedalus.host.worktrees import (
    StaffWorktrees,
    WorktreeEntry,
    WorktreeError,
    WorktreeRefused,
    WorktreeUnavailable,
    staff_slug,
)
from daedalus.stores.projects import ProjectFolder
from daedalus.stores.staff import Staff, StaffSession

if TYPE_CHECKING:
    from daedalus.app import Application


class RemoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=4096)
    delete_branch: bool = False
    """Also delete the branch, which happens only when it is merged into the folder's branch."""


def _same(a: str | Path | None, b: str | Path) -> bool:
    return bool(a) and os.path.normpath(str(a)) == os.path.normpath(str(b))


def blocked_by(entry: WorktreeEntry, owner: StaffSession | None, folder: ProjectFolder) -> str:
    """Why the worktree may not be removed now, as a code the app words; empty when it may."""
    if owner is not None:
        return "live"
    if entry.changes:
        return "changes"
    if folder.readonly:
        return "readonly"
    return ""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None
    fallback: list[StaffWorktrees] = []

    def worktrees() -> StaffWorktrees:
        """The team's own instance when it runs, so removal shares its per-repository locks; a plain
        local one otherwise, which still lists and removes in folders of this process's environment."""
        team = app.extensions.get("staff")
        if team is not None and isinstance(getattr(team, "worktrees", None), StaffWorktrees):
            return team.worktrees
        if not fallback:
            fallback.append(StaffWorktrees(manager.projects.local_env))
        return fallback[0]

    async def project_of(project_id: str) -> Any:
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        return project

    async def owner_of(project_id: str, path: Path) -> StaffSession | None:
        """The live staff session working in ``path``, if any: an open session row names its worktree."""
        for session in (await manager.staff.live_sessions(project_id)).values():
            if _same(session.worktree_path, path):
                return session
        return None

    @api.get("/api/projects/{project_id}/worktrees")
    async def list_worktrees(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every staff worktree in the project's folders: who it belongs to, its branch, its size,
        what is uncommitted in it, whether a live worker owns it, and its last commit. ``problems``
        names the folders that could not be asked (a host folder without the bridge)."""
        project = await project_of(project_id)
        members: list[Staff] = await manager.staff.list(project_id, archived=True)
        by_slug: dict[str, Staff] = {}
        for member in members:
            # An active member wins the slug over a dismissed one who had it before.
            if member.archived_at is None or staff_slug(member.name) not in by_slug:
                by_slug[staff_slug(member.name)] = member
        live = await manager.staff.live_sessions(project_id)
        names = {m.id: m.name for m in members}
        trees = worktrees()

        async def one_folder(folder: ProjectFolder) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
            try:
                entries = await trees.inventory(folder)
            except (WorktreeUnavailable, GitError, OSError) as exc:
                return [], {"folder_id": folder.id, "reason": str(exc)[:300]}
            try:
                base = await trees.current_branch(folder) if entries else ""
            except (WorktreeError, GitError):
                base = ""
            rows = []
            for entry in entries:
                owner = next((s for s in live.values() if _same(s.worktree_path, entry.path)), None)
                member = by_slug.get(entry.path.name)
                rows.append({
                    "folder_id": folder.id,
                    "path": str(entry.path),
                    "name": entry.path.name,
                    "member": {"id": member.id, "name": member.name, "dismissed": member.archived_at is not None} if member is not None else None,
                    "branch": entry.branch,
                    "base": base,
                    "size_bytes": entry.size_bytes,
                    "changes": entry.changes,
                    "last_commit_at": entry.last_commit_at,
                    "merged": entry.merged,
                    "prunable": entry.prunable,
                    "live": {"staff_id": owner.staff_id, "name": names.get(owner.staff_id, ""), "status": owner.status} if owner is not None else None,
                    "blocked": blocked_by(entry, owner, folder),
                })
            return rows, None

        results = await asyncio.gather(*(one_folder(f) for f in project.folders))
        return {
            "worktrees": [row for rows, _ in results for row in rows],
            "problems": [problem for _, problem in results if problem is not None],
        }

    @api.post("/api/projects/{project_id}/worktrees/remove")
    async def remove_worktree(project_id: str, body: RemoveBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """``git worktree remove`` for one worktree nobody works in and with nothing uncommitted.

        Both conditions are checked again here, not trusted from the list the app drew: a worker may
        have been launched into it since. The branch is kept unless asked and merged."""
        project = await project_of(project_id)
        folder = project.folder(body.folder_id)
        if folder is None:
            raise HTTPException(404, "no such folder in this project")
        path = Path(body.path)
        owner = await owner_of(project_id, path)
        if owner is not None:
            member = await manager.staff.get(owner.staff_id)
            raise HTTPException(409, f"{member.name if member else 'a worker'} is working in this worktree; release it first")
        try:
            deleted = await worktrees().remove_listed(folder, path, delete_branch=body.delete_branch)
        except WorktreeRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        except WorktreeUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except (WorktreeError, GitError) as exc:
            raise HTTPException(409, f"git did not remove the worktree: {exc}"[:1500]) from exc
        await manager.bus.publish("project.changed", {"change": "worktrees", "actor": "operator"}, project_id=project_id)
        return {"removed": True, "branch_deleted": deleted}
