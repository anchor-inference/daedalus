"""How much disk each workspace takes, and the operator's clean-up of it.

The overview answers from the guard's last measurements; the per-session and per-project views
measure on the spot (bounded, in a thread), because the operator opening them is asking about now.
A clean-up removes only what the operator chose, inside one top-level directory of the managed
workspaces root, and refuses a link anywhere on the path, the workspace itself, the directories the
installation keeps there, and anything a checkout tracks unless they said to include it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.disk_guard import DiskGuard, level_for
from daedalus.host import disk_usage
from daedalus.host.disk_usage import Measurement

if TYPE_CHECKING:
    from daedalus.app import Application

LISTED_ENTRIES = 12
LISTED_CHILDREN = 5


class CleanupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=200)
    paths: list[str] = Field(min_length=1, max_length=200)
    include_tracked: bool = False


def _iso(timestamp: float) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp > 0 else None


def _entries(workspace: Path, measurement: Measurement, patterns: list[str]) -> list[dict[str, Any]]:
    """The largest folders and, inside each, its largest; synchronous, for a thread (git is asked)."""

    def entry(path: str, slot: disk_usage.DirStat, children: list[dict[str, Any]]) -> dict[str, Any]:
        name = path.rsplit("/", 1)[-1]
        protected = path.split("/", 1)[0] in disk_usage.PROTECTED_NAMES or name == ".git"
        return {
            "path": path,
            "bytes": slot.bytes,
            "files": slot.files,
            "newest": _iso(slot.newest),
            "throwaway": disk_usage.matches(name, patterns),
            "tracked": False if protected else disk_usage.git_tracked(workspace / path, workspace),
            "protected": protected,
            "children": children,
        }

    out = []
    for path, slot in measurement.children("")[:LISTED_ENTRIES]:
        kids = [entry(inner, child, []) for inner, child in measurement.children(path)[:LISTED_CHILDREN]]
        out.append(entry(path, slot, kids))
    return out


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def guard() -> DiskGuard:
        found = app.extensions.get("disk_guard")
        if isinstance(found, DiskGuard):
            return found
        # The extension did not install (or this is a test's bare app): the routes still answer.
        made = DiskGuard(app)
        app.extensions["disk_guard"] = made
        return made

    async def details(names: list[tuple[str, str]], outside: list[dict[str, str]]) -> dict[str, Any]:
        g = guard()
        free, total = await asyncio.to_thread(disk_usage.free_space, g.root)
        patterns = list(g.config.throwaway_patterns)
        workspaces = []
        for name, label in names:
            workspace = g.target_workspace(name)
            if workspace is None:
                continue
            measurement = await g.measure(name)
            entries = await asyncio.to_thread(_entries, workspace, measurement, patterns)
            workspaces.append({
                "name": name, "label": label, "bytes": measurement.bytes, "truncated": measurement.truncated,
                "over": level_for(measurement.bytes, g.limit), "entries": entries,
            })
        return {
            "limit_bytes": g.limit, "free_bytes": free, "total_bytes": total,
            "low_disk": bool(total) and free < g.floor(total),
            "workspaces": workspaces, "outside": outside,
        }

    @api.get("/api/disk")
    async def disk_overview(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        g = guard()
        free, total = await asyncio.to_thread(disk_usage.free_space, g.root)
        owners = await g.owners()
        rows = [
            {
                "name": name, "bytes": m.bytes, "truncated": m.truncated, "measured_at": _iso(m.measured_at),
                "over": level_for(m.bytes, g.limit), "owner": owners[name].view() if name in owners else {"kind": "none", "id": "", "title": ""},
            }
            for name, m in sorted(g.sizes.items(), key=lambda item: item[1].bytes, reverse=True)
        ]
        return {
            "free_bytes": free, "total_bytes": total, "low_disk": bool(total) and free < g.floor(total),
            "limit_bytes": g.limit, "checked_at": _iso(g.checked_at or 0.0), "workspaces": rows,
        }

    @api.get("/api/disk/session/{session_id}")
    async def disk_of_session(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT id, project_id FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            raise HTTPException(404, "no such session")
        owners = await guard().owners()
        names = [(name, name) for name, owner in owners.items() if owner.kind == "session" and owner.id == session_id]
        outside: list[dict[str, str]] = []
        if not names and row["project_id"]:
            # A session working in its project's folder: that folder is where its files are.
            names, outside = await _project_folders(str(row["project_id"]))
        return await details(names, outside)

    @api.get("/api/disk/project/{project_id}")
    async def disk_of_project(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if await app.db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)) is None:
            raise HTTPException(404, "no such project")
        names, outside = await _project_folders(project_id)
        return await details(names, outside)

    async def _project_folders(project_id: str) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
        root = guard().root
        names: list[tuple[str, str]] = []
        outside: list[dict[str, str]] = []
        rows = await app.db.fetchall("SELECT path, label, env FROM project_folders WHERE project_id = ? ORDER BY position", (project_id,))
        for folder in rows:
            path = Path(os.path.normpath(str(folder["path"])))
            label = str(folder["label"] or path.name)
            # Only a folder directly under the managed root is measured and cleaned here; one the
            # operator pointed at is their own directory, and this screen does not reach into it.
            if path.parent == root:
                names.append((path.name, label))
            else:
                outside.append({"label": label, "env": str(folder["env"] or "")})
        return names, outside

    @api.post("/api/disk/cleanup")
    async def disk_cleanup(body: CleanupBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await guard().clean(body.workspace, body.paths, include_tracked=body.include_tracked)
        except KeyError:
            raise HTTPException(404, "no such workspace") from None
