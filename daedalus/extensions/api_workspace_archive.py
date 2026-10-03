"""Operator-only private workspace archive routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.workspace_archive import (
    MAX_ARCHIVE_BYTES,
    ArchiveRefused,
    WorkspaceArchive,
    _stage,
    check_archive,
)
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class ArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_artifact_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class ImportInput(ArchiveInput):
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> WorkspaceArchive:
        manager = app.manager
        if manager is None or not getattr(manager, "files", None):
            raise HTTPException(503, "file store is unavailable")
        return WorkspaceArchive(app.db, manager.files)

    async def load_archive(artifact_id: str) -> bytes:
        path = service().files.blobs.path_of("workspace-archives", artifact_id)
        if not path.is_file():
            raise HTTPException(404, "no such private archive")
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, "archive exceeds the private import limit")
        data = path.read_bytes()
        if check_archive(data).digest != artifact_id:
            raise HTTPException(409, "staged private archive has changed")
        return data

    @api.post("/api/import/archive")
    async def upload_archive(request: Request, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > MAX_ARCHIVE_BYTES:
                    raise HTTPException(413, "archive exceeds the private import limit")
            except ValueError as exc:
                raise HTTPException(400, "invalid archive length") from exc
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise HTTPException(413, "archive exceeds the private import limit")
            chunks.append(chunk)
        data = b"".join(chunks)
        try:
            checked = check_archive(data)
        except ArchiveRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        preview = await service().preview(checked)
        if preview["valid"]:
            _stage(service().files.blobs.path_of("workspace-archives", checked.digest).parent, checked.digest, data)
        return {"archive_artifact_id": checked.digest, "private": True,
                "preview": preview}

    @api.get("/api/import/archive/{archive_artifact_id}")
    async def download_archive(archive_artifact_id: str, who: dict[str, Any] = Depends(auth)) -> Response:
        Principal.operator(who)
        if len(archive_artifact_id) != 64 or any(letter not in "0123456789abcdef" for letter in archive_artifact_id):
            raise HTTPException(400, "invalid archive identity")
        data = await load_archive(archive_artifact_id)
        return Response(data, media_type="application/vnd.daedalus.workspace+zip",
                        headers={"Content-Disposition": 'attachment; filename="workspace-private.zip"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @api.get("/api/projects/{project_id}/workspace-archive")
    async def export_archive(project_id: str, who: dict[str, Any] = Depends(auth)) -> Response:
        Principal.operator(who)
        try:
            data = await service().export(project_id)
        except KeyError as exc:
            raise HTTPException(404, "no such project") from exc
        except (ArchiveRefused, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(data, media_type="application/vnd.daedalus.workspace+zip",
                        headers={"Content-Disposition": 'attachment; filename="workspace-private.zip"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @api.post("/api/import/preview")
    async def preview_archive(body: ArchiveInput, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        try:
            return await service().preview(check_archive(await load_archive(body.archive_artifact_id)))
        except ArchiveRefused as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/import/apply")
    async def import_archive(body: ImportInput, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            archive = check_archive(await load_archive(body.archive_artifact_id))
            return await service().import_command(
                Principal.operator(who), archive,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except ArchiveRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
