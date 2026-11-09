"""The canvas routes of the operator's app, and the public read of a shared canvas.

Every write through these routes is the operator's: the board and the list page are the only callers,
and the agent writes through its tools, which mark their saves as its own. That is what lets the
history say who changed a revision without the client claiming it.

Attachments are served from the app's origin, so they are served inert: a ``.svg`` opened by itself
would otherwise run a script with the operator's session, and the converted diagrams' pictures are
SVG. The answer forbids every script, plugin and frame, and anything that is not a picture is a
download.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from daedalus.stores.canvas_doc import CanvasError
from daedalus.stores.canvases import NotFound, Stale, store_for
from daedalus.stores.notes import Scope

if TYPE_CHECKING:
    from daedalus.app import Application

INLINE_PICTURES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
INERT = {
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class CreateBody(BaseModel):
    title: str = Field(max_length=400)
    scope: Literal["chat", "project"]
    scope_id: str = Field(min_length=1, max_length=80)
    owner_session_id: str | None = Field(default=None, max_length=80)
    doc: dict[str, Any] | None = None


class OpsBody(BaseModel):
    base_version: int
    ops: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    title: str | None = Field(default=None, max_length=400)


class RenameBody(BaseModel):
    title: str = Field(max_length=400)
    version: int


class RestoreBody(BaseModel):
    revision: int
    """The revision to bring back."""
    version: int
    """The canvas's current version as the reader saw it; a newer one refuses the restore."""


class DuplicateBody(BaseModel):
    title: str | None = Field(default=None, max_length=400)


class MoveBody(BaseModel):
    scope: Literal["chat", "project"]
    scope_id: str = Field(min_length=1, max_length=80)


class OwnerBody(BaseModel):
    owner_session_id: str = Field(min_length=1, max_length=80)


class ImportBody(BaseModel):
    title: str = Field(max_length=400)
    scope: Literal["chat", "project"]
    scope_id: str = Field(min_length=1, max_length=80)
    owner_session_id: str | None = Field(default=None, max_length=80)
    text: str = Field(max_length=4_200_000)


def refused(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(404, "not found")
    if isinstance(exc, Stale):
        return HTTPException(409, {"message": str(exc) if exc.ids else "the canvas changed since you read it", "conflicts": exc.ids, "current": exc.current})
    return HTTPException(400, str(exc))


def _attachment(found: dict[str, Any], save: bool) -> Response:
    mime = str(found["mime"])
    inline = mime in INLINE_PICTURES and not save
    disposition = "inline" if inline else f"attachment; filename*=UTF-8''{quote(str(found['name']))}"
    return Response(
        bytes(found["data"]),
        media_type=mime if inline else "application/octet-stream",
        headers={**INERT, "Content-Disposition": disposition, "Cache-Control": "private, max-age=3600"},
    )


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def store() -> Any:
        return store_for(app.manager)

    @api.get("/api/canvases")
    async def canvas_list(
        session_id: str | None = Query(default=None, max_length=80),
        project_id: str | None = Query(default=None, max_length=80),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        try:
            return await store().list(session_id=session_id, project_id=project_id)
        except NotFound as exc:
            raise refused(exc) from exc

    @api.post("/api/canvases", status_code=201)
    async def canvas_create(body: CreateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().create(body.title, Scope(body.scope, body.scope_id), owner_session_id=body.owner_session_id, doc=body.doc)
        except (CanvasError, NotFound) as exc:
            raise refused(exc) from exc

    @api.post("/api/canvases/import", status_code=201)
    async def canvas_import(body: ImportBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().import_text(body.title, body.text, Scope(body.scope, body.scope_id), owner_session_id=body.owner_session_id)
        except (CanvasError, NotFound) as exc:
            raise refused(exc) from exc

    @api.get("/api/canvases/{canvas_id}")
    async def canvas_get(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await store().get(canvas_id)
        if found is None:
            raise HTTPException(404, "no such canvas")
        return found

    @api.get("/api/canvases/{canvas_id}/head")
    async def canvas_head(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await store().head(canvas_id)
        if found is None:
            raise HTTPException(404, "no such canvas")
        return found

    @api.post("/api/canvases/{canvas_id}/ops")
    async def canvas_ops(canvas_id: str, body: OpsBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            canvas, merged = await store().apply(canvas_id, body.base_version, body.ops, title=body.title)
        except (CanvasError, NotFound, Stale) as exc:
            raise refused(exc) from exc
        return {"canvas": canvas, "merged": merged}

    @api.patch("/api/canvases/{canvas_id}")
    async def canvas_rename(canvas_id: str, body: RenameBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().rename(canvas_id, body.title, body.version)
        except (CanvasError, NotFound, Stale) as exc:
            raise refused(exc) from exc

    @api.delete("/api/canvases/{canvas_id}")
    async def canvas_delete(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await store().delete(canvas_id)
        except NotFound as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.post("/api/canvases/{canvas_id}/move")
    async def canvas_move(canvas_id: str, body: MoveBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().move(canvas_id, Scope(body.scope, body.scope_id))
        except (CanvasError, NotFound) as exc:
            raise refused(exc) from exc

    @api.post("/api/canvases/{canvas_id}/owner")
    async def canvas_owner(canvas_id: str, body: OwnerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().set_owner(canvas_id, body.owner_session_id)
        except (CanvasError, NotFound) as exc:
            raise refused(exc) from exc

    @api.get("/api/canvases/{canvas_id}/versions")
    async def canvas_versions(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        if await store().head(canvas_id) is None:
            raise HTTPException(404, "no such canvas")
        return await store().versions(canvas_id)

    @api.get("/api/canvases/{canvas_id}/versions/{version}")
    async def canvas_version(canvas_id: str, version: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await store().version(canvas_id, version)
        if found is None:
            raise HTTPException(404, "no such canvas version")
        return found

    @api.get("/api/canvases/{canvas_id}/preview")
    async def canvas_preview(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await store().preview(canvas_id)
        if found is None:
            raise HTTPException(404, "no such canvas")
        return found

    @api.get("/api/canvases/{canvas_id}/versions/{version}/preview")
    async def canvas_version_preview(canvas_id: str, version: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await store().preview(canvas_id, version)
        if found is None:
            raise HTTPException(404, "no such canvas version")
        return found

    @api.post("/api/canvases/{canvas_id}/restore")
    async def canvas_restore(canvas_id: str, body: RestoreBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().restore(canvas_id, body.revision, body.version)
        except (CanvasError, NotFound, Stale) as exc:
            raise refused(exc) from exc

    @api.post("/api/canvases/{canvas_id}/duplicate", status_code=201)
    async def canvas_duplicate(canvas_id: str, body: DuplicateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().duplicate(canvas_id, body.title)
        except (CanvasError, NotFound) as exc:
            raise refused(exc) from exc

    @api.post("/api/canvases/{canvas_id}/share")
    async def canvas_share(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        try:
            token = await store().share(canvas_id)
        except NotFound as exc:
            raise refused(exc) from exc
        return {"url": f"/app/d/{token}"}

    @api.delete("/api/canvases/{canvas_id}/share")
    async def canvas_unshare(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await store().revoke_share(canvas_id)
        except NotFound as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.get("/api/canvases/{canvas_id}/export")
    async def canvas_export(canvas_id: str, _: dict[str, Any] = Depends(auth)) -> Response:
        try:
            title, text = await store().export_text(canvas_id)
        except NotFound as exc:
            raise refused(exc) from exc
        name = " ".join("".join(ch if ch.isalnum() or ch in " -_." else " " for ch in title).split())[:80] or "canvas"
        return Response(text, media_type="application/json", headers={**INERT, "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.canvas"})

    @api.get("/api/canvases/{canvas_id}/files/{file_id}")
    async def canvas_file(canvas_id: str, file_id: str, save: bool = False, _: dict[str, Any] = Depends(auth)) -> Response:
        found = await store().file(canvas_id, file_id)
        if found is None:
            raise HTTPException(404, "no such file")
        return _attachment(found, save)

    @api.get("/api/public/canvases/{token}")
    async def canvas_shared(token: str) -> dict[str, Any]:
        found = await store().shared(token)
        if found is None:
            raise HTTPException(404, "no such shared canvas")
        return {key: value for key, value in found.items() if key != "id"}

    @api.get("/api/public/canvases/{token}/files/{file_id}")
    async def canvas_shared_file(token: str, file_id: str, save: bool = False) -> Response:
        found = await store().shared(token)
        attachment = await store().file(found["id"], file_id) if found is not None else None
        if attachment is None:
            raise HTTPException(404, "no such file")
        return _attachment(attachment, save)


__all__ = ["register"]
