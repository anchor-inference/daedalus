"""Authenticated diagram routes for the operator's web app, and the one public read of a shared diagram.

Every write through these routes is the operator's: the editor and the list page are the only callers,
and the agent writes through its tools, which mark their saves as its own. That is what lets the
history say who drew a revision without the client claiming it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from daedalus.stores.diagrams import DiagramStore

if TYPE_CHECKING:
    from daedalus.app import Application


class DiagramBody(BaseModel):
    title: str
    scene: dict[str, Any] = Field(default_factory=lambda: {"elements": [], "appState": {}, "files": {}})
    version: int | None = None


class RenameBody(BaseModel):
    title: str
    version: int


class RestoreBody(BaseModel):
    revision: int
    """The revision to bring back."""
    version: int
    """The diagram's current version as the reader saw it; a newer one refuses the restore."""


class DuplicateBody(BaseModel):
    title: str | None = None


def refused(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "not found")
    if isinstance(exc, RuntimeError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    diagrams = DiagramStore(app.db)

    @api.get("/api/diagrams")
    async def diagram_list(session_id: str | None = Query(default=None, max_length=64), _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await diagrams.list(session_id)

    @api.post("/api/diagrams", status_code=201)
    async def diagram_create(body: DiagramBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.create(body.title, body.scene)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.get("/api/diagrams/{diagram_id}")
    async def diagram_get(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await diagrams.get(diagram_id)
        if found is None:
            raise HTTPException(404, "no such diagram")
        return found

    @api.get("/api/diagrams/{diagram_id}/head")
    async def diagram_head(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await diagrams.head(diagram_id)
        if found is None:
            raise HTTPException(404, "no such diagram")
        return found

    @api.get("/api/diagrams/{diagram_id}/preview")
    async def diagram_preview(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await diagrams.preview(diagram_id)
        if found is None:
            raise HTTPException(404, "no such diagram")
        return found

    @api.get("/api/diagrams/{diagram_id}/versions")
    async def diagram_versions(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        if await diagrams.head(diagram_id) is None:
            raise HTTPException(404, "no such diagram")
        return await diagrams.versions(diagram_id)

    @api.get("/api/diagrams/{diagram_id}/versions/{version}")
    async def diagram_version(diagram_id: str, version: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await diagrams.version(diagram_id, version)
        if found is None:
            raise HTTPException(404, "no such diagram version")
        return found

    @api.get("/api/diagrams/{diagram_id}/versions/{version}/preview")
    async def diagram_version_preview(diagram_id: str, version: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await diagrams.preview(diagram_id, version)
        if found is None:
            raise HTTPException(404, "no such diagram version")
        return found

    @api.post("/api/diagrams/{diagram_id}/restore")
    async def diagram_restore(diagram_id: str, body: RestoreBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.restore(diagram_id, body.revision, body.version)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.post("/api/diagrams/{diagram_id}/duplicate", status_code=201)
    async def diagram_duplicate(diagram_id: str, body: DuplicateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.duplicate(diagram_id, body.title)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.post("/api/diagrams/{diagram_id}/share")
    async def diagram_share(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        try:
            token = await diagrams.share(diagram_id)
        except KeyError as exc:
            raise refused(exc) from exc
        return {"url": f"/app/d/{token}"}

    @api.delete("/api/diagrams/{diagram_id}/share")
    async def diagram_revoke(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await diagrams.revoke_share(diagram_id)
        except KeyError as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.get("/api/public/diagrams/{token}")
    async def diagram_shared(token: str) -> dict[str, Any]:
        found = await diagrams.shared(token)
        if found is None:
            raise HTTPException(404, "no such shared diagram")
        return found

    @api.put("/api/diagrams/{diagram_id}")
    async def diagram_update(diagram_id: str, body: DiagramBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.save(diagram_id, body.title, body.scene, body.version or 0)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.patch("/api/diagrams/{diagram_id}")
    async def diagram_rename(diagram_id: str, body: RenameBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.rename(diagram_id, body.title, body.version)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/diagrams/{diagram_id}")
    async def diagram_delete(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await diagrams.delete(diagram_id)
        except KeyError as exc:
            raise refused(exc) from exc
        return {"ok": True}


__all__ = ["register"]
