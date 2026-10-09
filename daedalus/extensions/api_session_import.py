"""The routes of importing a session from another agent program.

Everything about the other program is read on the operator's machine by the host terminal daemon
(``sessions.harnesses``, ``sessions.scan``, ``sessions.read``); this module adds what only Daedalus
knows — which project owns a folder, which sessions were imported already — and runs the import as a
background job (:mod:`daedalus.host.session_import`). The host's failures answer in the shapes the
folder browser uses: 503 when the daemon does not answer, 501 when it predates these calls.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.api_folder_browser import HOST_INSTALL, HOST_START, crumbs
from daedalus.host.foreign_sessions import harness_name
from daedalus.host.session_import import ImportRefused, ImportRequest, SessionImporter, normalise_cwd, ordered_harnesses
from daedalus.stores.projects import Project
from daedalus.terminals.bridge import HostDaemonOutdated

if TYPE_CHECKING:
    from daedalus.app import Application

SCAN_LIMIT = 200
_HARNESS_RE = re.compile(r"[a-z0-9_-]{1,32}")


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness: str = Field(min_length=1, max_length=32)
    id: str = Field(min_length=1, max_length=400)
    mode: Literal["", "full", "tail"] = ""
    """Empty lets the size decide: a summary and the last turns for a session too long to start from."""
    model: str = Field(default="", max_length=200)
    """A preset; empty is the session's own model when there is a preset for it, else the default."""
    project_id: str = Field(default="", max_length=64)
    """The project the import window showed as the destination; a different answer now is refused."""
    make_project: bool = False
    """Make a new folder's project a project at once instead of a chat that becomes one with a second session."""
    cwd: str = Field(default="", max_length=4000)
    """Continue in this folder instead of the one the session worked in (that one may be gone)."""
    again: bool = False
    """Import a session that was imported already as another, separate chat."""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None

    async def publish(view: dict[str, Any]) -> None:
        await manager.bus.publish("import.progress", view, session_id=view.get("session_id") or None)

    importer = SessionImporter(manager, publish=publish)


    def terminals_configured() -> bool:
        terminals = app.extensions.get("terminals")
        return bool(terminals is not None and terminals.configured("host"))

    def failure(exc: BaseException | None) -> HTTPException:
        """The host's errors in the folder browser's shapes, and an import refused in its own."""
        if isinstance(exc, ImportRefused):
            return HTTPException(exc.status, exc.view())
        if exc is None or isinstance(exc, ConnectionError):
            message = str(exc) if exc else "this installation has no host terminal bridge"
            return HTTPException(503, {"code": "host_down", "configured": terminals_configured(), "message": message,
                                       "start": HOST_START, "install": HOST_INSTALL})
        if isinstance(exc, HostDaemonOutdated):
            return HTTPException(501, {"code": "host_outdated", "message": str(exc), "install": HOST_INSTALL})
        if isinstance(exc, FileNotFoundError):
            return HTTPException(404, {"code": "missing", "message": str(exc)})
        return HTTPException(403, {"code": "refused", "message": str(exc)})

    def checked(harness: str) -> str:
        if not _HARNESS_RE.fullmatch(harness or ""):
            raise HTTPException(400, {"code": "invalid", "message": "name a program: claude, codex, …"})
        return harness

    def owner_view(project: Project | None) -> dict[str, Any] | None:
        if project is None:
            return None
        kind = "service" if project.settings.system or project.setup_by == "dispatcher" else ("chat" if project.settings.ephemeral else "project")
        return {"id": project.id, "name": project.name, "kind": kind}

    async def machine_home() -> str:
        """The machine's home folder, so the app writes ``~/…`` for paths under it. A listing of no
        folder in particular (the places with sessions, a search) asked the machine for nothing and
        answered an empty home, and the app then wrote every path out in full."""
        try:
            browsed = await importer.bridge().browse("~", limit=1)
        except (OSError, ConnectionError):
            return ""
        return str(browsed.get("home") or browsed.get("path") or "")

    async def owner_of(path: str, projects: list[Project]) -> dict[str, Any] | None:
        destination = await importer.destination(path, check=False, projects=projects)
        return owner_view(destination.project)

    @api.get("/api/imports/harnesses")
    async def harnesses(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The agent programs whose sessions are on the machine, Claude Code and Codex first."""
        try:
            listed = await importer.bridge().sessions_harnesses()
        except (OSError, ConnectionError) as exc:
            raise failure(exc) from None
        rows = [row for row in listed.get("harnesses") or [] if isinstance(row, dict)]
        for row in rows:
            row.setdefault("name", harness_name(str(row.get("id") or "")))
        return {"harnesses": ordered_harnesses(rows), "home": await machine_home()}

    @api.get("/api/imports/scan")
    async def scan(harness: str, path: str = "", q: str = "", deep: bool = False, cursor: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One program's sessions in one folder of the machine, with the folders below that have any.

        Each session says whether it was imported already (``imported_as``) and which project it
        would join (``project``); each folder, which project owns it. A folder below with no sessions
        is listed too, from the folder browser, marked ``empty``, so the explorer can walk the tree."""
        checked(harness)
        try:
            listed = await importer.bridge().sessions_scan(harness, path.strip(), query=q.strip(), deep=deep, limit=SCAN_LIMIT, cursor=cursor)
        except (OSError, ConnectionError) as exc:
            raise failure(exc) from None
        projects = await manager.projects.list()
        here = [row for row in listed.get("here") or [] if isinstance(row, dict)]
        imported = await importer.store.imported(harness, [str(row.get("id") or "") for row in here])
        for row in here:
            done = imported.get(str(row.get("id") or ""))
            row["imported_as"] = done
            flags = row.get("flags") if isinstance(row.get("flags"), dict) else {}
            row["flags"] = {**flags, "imported_as": done["session_id"] if done else None}
            row["project"] = await owner_of(str(row.get("cwd") or ""), projects)
        children = [row for row in listed.get("children") or [] if isinstance(row, dict)]
        for row in children:
            row["project"] = await owner_of(str(row.get("path") or ""), projects)
            row["empty"] = False
        folders = [row for row in listed.get("folders") or [] if isinstance(row, dict)]
        for row in folders:
            row["project"] = await owner_of(str(row.get("path") or ""), projects)
        out: dict[str, Any] = {**listed, "harness": harness, "here": here, "children": children, "folders": folders,
                               "parent": None, "home": "", "crumbs": [], "browse": None}
        target = str(listed.get("path") or path).strip()
        if not target:
            out["home"] = await machine_home()
        else:
            try:
                browsed = await importer.bridge().browse(target, limit=500)
            except FileNotFoundError:
                out["browse"] = "missing"
            except (OSError, ConnectionError) as exc:
                out["browse"] = str(exc)
            else:
                home = str(browsed.get("home") or "")
                out.update(parent=browsed.get("parent") or None, home=home, crumbs=crumbs(str(browsed.get("path") or target), home), browse="ok")
                known = {normalise_cwd(str(row.get("path") or "")) for row in children}
                for entry in browsed.get("entries") or []:
                    if not isinstance(entry, dict) or normalise_cwd(str(entry.get("path") or "")) in known:
                        continue
                    children.append({"name": str(entry.get("name") or ""), "path": str(entry.get("path") or ""), "sessions": 0, "latest": None,
                                     "empty": True, "project": await owner_of(str(entry.get("path") or ""), projects)})
                children.sort(key=lambda row: (bool(row.get("empty")), str(row.get("name") or "").lower()))
        return out

    @api.get("/api/imports/preview")
    async def preview(harness: str, id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:  # noqa: A002 — the contract's name
        """What the import of one session would bring and where it would land, before anything is written."""
        checked(harness)
        try:
            return await importer.preview(harness, id)
        except (ImportRefused, OSError, ConnectionError) as exc:
            raise failure(exc) from None

    @api.post("/api/imports")
    async def start(body: ImportBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Import one session in the background; ``GET /api/imports/{job_id}`` and the ``import.progress``
        events say how far it got and, at the end, which session it made."""
        checked(body.harness)
        if manager.host_bridge is None:
            raise failure(None)
        request = ImportRequest(harness=body.harness, ext_id=body.id, mode=body.mode, model=body.model, project_id=body.project_id,
                                make_project=body.make_project, cwd=body.cwd, again=body.again)
        try:
            job = await importer.start(request)
        except ImportRefused as exc:
            raise failure(exc) from None
        return {"job_id": job.id, "job": job.view()}

    @api.get("/api/imports/{job_id}")
    async def job(job_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = importer.jobs.get(job_id)
        if found is None:
            raise HTTPException(404, {"code": "missing", "message": "no such import"})
        return found.view()

    @api.post("/api/sessions/{session_id}/import/refresh")
    async def refresh(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Pull in what the program wrote to the session since it was imported. Nothing goes back."""
        try:
            return await importer.refresh(session_id)
        except (ImportRefused, OSError, ConnectionError) as exc:
            raise failure(exc) from None

    @api.get("/api/sessions/{session_id}/import/original")
    async def original(session_id: str, _: dict[str, Any] = Depends(auth)) -> Response:
        """The copy of the program's own file the import kept, secrets masked."""
        found = await importer.original(session_id)
        if found is None:
            raise HTTPException(404, {"code": "missing", "message": "no original was kept for this session"})
        data, name = found
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        return Response(content=data, media_type="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="{safe}"'})


__all__ = ["register"]
