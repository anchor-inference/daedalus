"""The export of one conversation over an agent link: a zip to download, or a note in the chat that owns it.

Both directions have the pair. A conversation of ours at another door is read from this side's tables alone; a
guest's conversation at our door needs the door for its messages, and when the door is out of reach the export still
comes, with what this side keeps and a line saying the messages could not be read. Each route is the operator's,
behind the app's own login, like its neighbours. What goes into the export is decided in
:mod:`daedalus.host.link_export`, which names every field it copies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from starlette.background import BackgroundTask

from daedalus.extensions import agent_talk, guest_door
from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable, bridge_of
from daedalus.host import link_export
from daedalus.stores.notes import BODY_MAX, NoteError, scopes_for_session, store_for

if TYPE_CHECKING:
    from daedalus.app import Application

EXPORTS = ".link-export"
"""Where a zip is built before it is sent, under the state directory: it may hold up to the bound of files, more than
a small temporary filesystem in a container takes."""
STALE_SECONDS = 3600.0
"""A zip left behind by a download that was dropped before its clean-up ran is removed by the next export."""
ZIP_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}

Language = Literal["en", "ru"]


class NoteExport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lang: Language = "en"


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    async def rows(sql: str, side: str, owner: str) -> list[Any]:
        return list(await app.db.fetchall(sql, (side, owner)))

    async def talk_record(talk_id: str) -> dict[str, Any]:
        talk = await app.db.fetchone("SELECT * FROM agent_talks WHERE id = ?", (talk_id,))
        if talk is None:
            raise HTTPException(404, "no such conversation")
        messages = await app.db.fetchall("SELECT * FROM agent_talk_messages WHERE talk_id = ? ORDER BY id", (talk_id,))
        tasks = await rows("SELECT * FROM link_tasks WHERE side = ? AND owner = ? ORDER BY id", agent_talk.SIDE, talk_id)
        files = await rows("SELECT * FROM link_files WHERE side = ? AND owner = ? ORDER BY id", agent_talk.SIDE, talk_id)
        return link_export.talk_record(talk, messages, tasks, files)

    async def guest_record(link_id: str, conversation: str = "") -> dict[str, Any]:
        """``conversation`` picks one guest's of a link several guests share; without it the door's newest."""
        bridge = bridge_of(app)
        if bridge is None:
            raise HTTPException(503, "the guest door is not available in this installation")
        known = await bridge.known_link(link_id)
        if known is None:
            raise HTTPException(404, "no such link")
        door_link: dict[str, Any] | None = None
        door_error = ""
        try:
            picked = f"?conversation={conversation}" if re.fullmatch(r"[0-9a-f]{16}", conversation) else ""
            door_link = (await bridge.client.call("GET", f"/control/links/{link_id}{picked}")).get("link") or {}
        except DoorUnavailable as exc:
            door_error = str(exc) or "the door is out of reach"
        except DoorRefused as exc:
            door_error = exc.detail or f"the door answered {exc.status}"
        tasks = await rows("SELECT * FROM link_tasks WHERE side = ? AND owner = ? ORDER BY id", guest_door.SIDE, link_id)
        files = await rows("SELECT * FROM link_files WHERE side = ? AND owner = ? ORDER BY id", guest_door.SIDE, link_id)
        return link_export.guest_record(known, door_link, tasks, files, door_error=door_error)

    def build_zip(export: link_export.Export) -> Path:
        folder = Path(app.settings.state_dir) / EXPORTS
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.zip"):
            with contextlib.suppress(OSError):
                if time.time() - old.stat().st_mtime > STALE_SECONDS:
                    old.unlink()
        handle, name = tempfile.mkstemp(suffix=".zip", dir=folder)
        target = Path(name)
        os.close(handle)
        try:
            link_export.write_zip(export, target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    async def as_zip(record: dict[str, Any], language: str) -> FileResponse:
        now = datetime.now(UTC)
        export = link_export.prepare(record, language=language, now=now)
        try:
            target = await asyncio.to_thread(build_zip, export)
        except OSError as exc:
            raise HTTPException(500, f"the export could not be written: {exc.strerror or exc}") from None
        return FileResponse(
            target, media_type="application/zip", filename=link_export.download_name(record, now=now), content_disposition_type="attachment", headers=ZIP_HEADERS,
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    async def as_note(record: dict[str, Any], language: str) -> dict[str, Any]:
        if getattr(app, "manager", None) is None:
            raise HTTPException(503, "notes are not available in this installation")
        try:
            chat, _, _ = await scopes_for_session(app.db, record["session_id"])
        except NoteError:
            raise HTTPException(404, "the chat of this conversation is gone") from None
        now = datetime.now(UTC)
        export = link_export.prepare(record, language=language, now=now)
        title = link_export.note_title(record, language=language, now=now)
        try:
            note = await store_for(app.manager).create(chat, title=title, body=link_export.note_body(export.transcript, language=language, limit=BODY_MAX))
        except NoteError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return {"note_id": note["id"], "session_id": record["session_id"], "title": note["title"]}

    @api.get("/api/agent-talks/{talk_id}/export")
    async def talk_zip(talk_id: str, lang: Language = "en", _: dict[str, Any] = Depends(auth)) -> FileResponse:
        return await as_zip(await talk_record(talk_id), lang)

    @api.post("/api/agent-talks/{talk_id}/export/note")
    async def talk_note(talk_id: str, body: NoteExport | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await as_note(await talk_record(talk_id), (body or NoteExport()).lang)

    @api.get("/api/guest/links/{link_id}/export")
    async def guest_zip(link_id: str, lang: Language = "en", conversation: str = "", _: dict[str, Any] = Depends(auth)) -> FileResponse:
        return await as_zip(await guest_record(link_id, conversation), lang)

    @api.post("/api/guest/links/{link_id}/export/note")
    async def guest_note(link_id: str, body: NoteExport | None = None, conversation: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await as_note(await guest_record(link_id, conversation), (body or NoteExport()).lang)


__all__ = ["NoteExport", "register"]
