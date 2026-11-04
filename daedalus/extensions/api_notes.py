"""The routes of the Notes tab: a chat's notes and its project's, their files, and the edits.

Every write through these routes is the operator's; the agent writes through its ``Notes`` tool, which
marks its saves as its own, so the panel can say who wrote last without the client claiming it. A
chat is the anchor of every listing and creation: it decides which project scope exists (a plain
chat's scratch project has none), so the app never has to know that rule.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.artifact_headers import artifact_headers
from daedalus.stores.notes import (
    FILE_MAX_BYTES,
    Conflict,
    NoteError,
    NoteStore,
    Referenced,
    Scope,
    scopes_for_session,
    store_for,
)

if TYPE_CHECKING:
    from daedalus.app import Application

INLINE = ("image/", "text/plain", "text/markdown", "text/csv", "application/pdf", "application/json", "audio/", "video/")
"""What a browser may show in place; everything else is a download. HTML and SVG are left out of the
inline set on purpose: a file the agent attached is not trusted markup."""
UPLOAD_FILES_MAX = 20


class NewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["chat", "project"] = "chat"
    title: str = ""
    body: str = ""
    color: str = ""
    pinned: bool = False


class NotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    body: str | None = None
    version: int | None = None
    """The version the editor last saw; required with ``title`` or ``body``."""
    force: bool = False
    """The operator chose to keep their words over a newer version."""
    pinned: bool | None = None
    color: str | None = None


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["chat", "project"]
    ids: list[str] = Field(max_length=1000)


class Duplicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class Move(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    scope: Literal["chat", "project"]


def refused(exc: NoteError) -> HTTPException:
    if isinstance(exc, Conflict):
        return HTTPException(409, {"message": str(exc), "note": exc.current})
    if isinstance(exc, Referenced):
        return HTTPException(409, {"message": str(exc), "notes": exc.notes})
    return HTTPException(exc.status, str(exc))


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def store() -> NoteStore:
        return store_for(app.manager)

    async def scopes(session_id: str) -> tuple[Scope, Scope | None, dict[str, Any] | None]:
        try:
            return await scopes_for_session(app.db, session_id)
        except NoteError as exc:
            raise refused(exc) from exc

    async def scope_of(session_id: str, kind: str) -> Scope:
        chat, project, _ = await scopes(session_id)
        if kind == "chat":
            return chat
        if project is None:
            raise HTTPException(400, "this chat is in no project")
        return project

    @api.get("/api/sessions/{session_id}/notes")
    async def notes_listing(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Both scopes of a chat at once, each with its notes and files: the panel shows a count on
        each sub-tab and resolves a chat note's reference to a project file without a second call."""
        chat, project, about = await scopes(session_id)
        notes = store()
        listing: dict[str, Any] = {"session_id": session_id, "project": about, "chat": {"notes": await notes.notes(chat), "files": await notes.files(chat)}, "project_scope": None}
        if project is not None:
            listing["project_scope"] = {"notes": await notes.notes(project), "files": await notes.files(project)}
        return listing

    @api.post("/api/sessions/{session_id}/notes", status_code=201)
    async def note_create(session_id: str, body: NewNote, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scope = await scope_of(session_id, body.scope)
        try:
            return await store().create(scope, title=body.title, body=body.body, color=body.color, pinned=body.pinned)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.post("/api/sessions/{session_id}/notes/order")
    async def note_order(session_id: str, body: Order, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scope = await scope_of(session_id, body.scope)
        return {"notes": await store().reorder(scope, body.ids)}

    @api.post("/api/sessions/{session_id}/notes/files", status_code=201)
    async def note_files_upload(session_id: str, scope: Literal["chat", "project"] = Form("chat"), files: list[UploadFile] = File(...), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        target = await scope_of(session_id, scope)
        if len(files) > UPLOAD_FILES_MAX:
            raise HTTPException(400, f"at most {UPLOAD_FILES_MAX} files at once")
        made: list[dict[str, Any]] = []
        for upload in files:
            # One byte past the cap is enough to know: the whole file is never held to refuse it.
            data = await upload.read(FILE_MAX_BYTES + 1)
            if len(data) > FILE_MAX_BYTES:
                raise HTTPException(413, f"{upload.filename or 'a file'} is larger than {FILE_MAX_BYTES // (1024 * 1024)} MB")
            try:
                made.append(await store().add_file(target, data, name=upload.filename or "file", mime=upload.content_type or ""))
            except NoteError as exc:
                raise refused(exc) from exc
        return {"files": made}

    @api.get("/api/notes/{note_id}")
    async def note_get(note_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().get(note_id)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.patch("/api/notes/{note_id}")
    async def note_patch(note_id: str, body: NotePatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        notes = store()
        try:
            saved: dict[str, Any] | None = None
            if body.title is not None or body.body is not None:
                if body.version is None and not body.force:
                    raise HTTPException(400, "a change to a note's words carries the version it was made on")
                saved = await notes.update(note_id, version=body.version, title=body.title, body=body.body, force=body.force)
            if body.pinned is not None or body.color is not None:
                saved = await notes.set_meta(note_id, pinned=body.pinned, color=body.color)
            return saved if saved is not None else await notes.get(note_id)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.delete("/api/notes/{note_id}")
    async def note_delete(note_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().delete(note_id)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.post("/api/notes/{note_id}/restore")
    async def note_restore(note_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().restore(note_id)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.post("/api/notes/{note_id}/duplicate", status_code=201)
    async def note_duplicate(note_id: str, body: Duplicate, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().duplicate(note_id, title=body.title)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.post("/api/notes/{note_id}/move")
    async def note_move(note_id: str, body: Move, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        target = await scope_of(body.session_id, body.scope)
        try:
            return await store().move(note_id, target)
        except NoteError as exc:
            raise refused(exc) from exc

    @api.get("/api/notes/files/{file_id}/download")
    async def note_file_download(file_id: str, path: str = "", _: dict[str, Any] = Depends(auth)) -> FileResponse:
        """The bytes. ``path`` is only the name the viewers ask by, so the app's preview, lightbox and
        download links treat a note file exactly as they treat a workspace file."""
        notes = store()
        try:
            found = await notes.file(file_id)
        except NoteError as exc:
            raise refused(exc) from exc
        source = notes.file_path(found)
        if not source.is_file():
            raise HTTPException(404, "the file's bytes are gone")
        mime = str(found["mime"])
        inline = mime.startswith(INLINE) and mime != "image/svg+xml"
        disposition = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(str(found['name']))}"
        return FileResponse(source, media_type=mime, headers={**artifact_headers(mime), "Content-Disposition": disposition, "Cache-Control": "private, max-age=31536000, immutable"})

    @api.get("/api/notes/files/{file_id}/references")
    async def note_file_references(file_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        notes = store()
        try:
            await notes.file(file_id)
        except NoteError as exc:
            raise refused(exc) from exc
        return {"notes": await notes.referencing(file_id)}

    @api.delete("/api/notes/files/{file_id}")
    async def note_file_delete(file_id: str, force: bool = Query(False), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await store().delete_file(file_id, force=force)
        except NoteError as exc:
            raise refused(exc) from exc


__all__ = ["register"]
