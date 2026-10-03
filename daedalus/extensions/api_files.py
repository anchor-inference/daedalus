"""The routes of the files orchestration keeps by handle (``att:<id>``): what the app shows as a file
card wherever a chat names one, the file itself to download or preview, and a project's list.

A handle is the operator's to open from anywhere in the app: the operator attached or received every
one of them. What models may use is scoped (``daedalus.stores.files``); the operator is not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from daedalus.extensions.artifact_headers import artifact_headers
from daedalus.stores.files import HANDLE_RE, FileStore

if TYPE_CHECKING:
    from daedalus.app import Application

IDS_MAX = 100
INLINE = ("image/", "text/", "application/pdf", "application/json", "audio/", "video/")
"""What a browser may show in place; everything else is a download."""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None

    def store() -> FileStore:
        # Looked up per request: an application built around a stand-in manager has no store at all,
        # and its routes that are not these must still be served.
        return cast("FileStore", manager.files)

    @api.get("/api/files")
    async def files_by_id(ids: str = Query("", max_length=4000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The files named by handles, for the cards a chat draws: ``ids`` comma-separated, with or
        without ``att:``. An unknown id is simply left out."""
        wanted = [HANDLE_RE.sub(r"\1", i.strip()) for i in ids.split(",") if i.strip()][:IDS_MAX]
        return {"files": [f.view() for f in await store().many(wanted)]}

    @api.get("/api/files/{file_id}")
    async def file_detail(file_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One file with where it may be used and every movement of it: the audit."""
        stored = await store().get(file_id)
        if stored is None:
            raise HTTPException(404, "no such file")
        return {**stored.view(), "scopes": await store().scopes(stored.id), "transfers": await store().transfers(stored.id)}

    @api.get("/api/files/{file_id}/download")
    async def file_download(file_id: str, path: str = "", _: dict[str, Any] = Depends(auth)) -> FileResponse:
        """The bytes. ``path`` is ignored beyond being the name the preview asks by, so the app's viewers
        work on a kept file exactly as on a workspace file."""
        stored = await store().get(file_id)
        if stored is None:
            raise HTTPException(404, "no such file")
        source = store().path_of(stored)
        if not source.is_file():
            raise HTTPException(404, "the file's bytes are gone")
        inline = stored.mime.startswith(INLINE)
        disposition = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(stored.name)}"
        return FileResponse(source, media_type=stored.mime, headers={**artifact_headers(stored.mime), "Content-Disposition": disposition, "Access-Control-Allow-Origin": "https://web.telegram.org"})

    @api.get("/api/projects/{project_id}/files")
    async def project_files(project_id: str, limit: int = Query(50, ge=1, le=200), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if await manager.projects.get(project_id) is None:
            raise HTTPException(404, "no such project")
        return {"files": [f.view() for f in await store().listing(project_id, limit=limit)]}


__all__ = ["register"]
