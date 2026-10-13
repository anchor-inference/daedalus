"""The composer's meeting notes: send a recording, follow its progress, then the calendar or the chat."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from daedalus.extensions.meetings import UPLOAD_MAX, MeetingService, safe_suffix

KEPT_DIR = ".meetings"
"""Where an upload waits in the session's inbox while it is transcribed; hidden, and emptied by the job."""


class CalendarBody(BaseModel):
    day: str
    """The day the entry goes on, ``YYYY-MM-DD``."""


def register(api: FastAPI, app: Any, auth: Callable[..., Any]) -> None:
    def service() -> MeetingService:
        found = app.extensions.get("meetings")
        if found is None:
            raise HTTPException(503, "meeting notes are not installed")
        return found  # type: ignore[no-any-return]

    def job_of(session_id: str, job_id: str) -> Any:
        try:
            return service().get(job_id, session_id)
        except KeyError:
            raise HTTPException(404, "no such meeting notes; a restart forgets them, send the recording again") from None

    @api.post("/api/sessions/{session_id}/meeting")
    async def start_meeting(
        session_id: str,
        audio: UploadFile = File(...),
        keep: bool = Form(False),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """A recording to make meeting notes from; answers at once with the job to follow."""
        meetings = service()
        if not meetings.available():
            raise HTTPException(409, "speech-to-text is not set up (Settings → Voice → Speech recognition)")
        state = await app.manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        folder = state.workspace / "inbox" / KEPT_DIR
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{secrets.token_hex(6)}{safe_suffix(audio.filename or '', audio.content_type or '')}"
        size = 0
        try:
            with target.open("wb") as fh:
                while chunk := await audio.read(1 << 20):
                    size += len(chunk)
                    if size > UPLOAD_MAX:
                        raise HTTPException(413, f"a recording may be up to {UPLOAD_MAX >> 20} MB")
                    fh.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        job = meetings.start(session_id, target, name=audio.filename or target.name, keep=keep)
        return job.view()

    @api.get("/api/sessions/{session_id}/meeting/{job_id}")
    async def meeting_progress(session_id: str, job_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return job_of(session_id, job_id).view()  # type: ignore[no-any-return]

    @api.post("/api/sessions/{session_id}/meeting/{job_id}/calendar")
    async def meeting_calendar(session_id: str, job_id: str, body: CalendarBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The action items as one calendar entry with a checklist on the chosen day."""
        job = job_of(session_id, job_id)
        try:
            day = date.fromisoformat(body.day)
        except ValueError:
            raise HTTPException(400, "the day is YYYY-MM-DD") from None
        try:
            event = await service().add_to_calendar(job, day)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"event_id": event.get("id"), "day": day.isoformat()}

    @api.post("/api/sessions/{session_id}/meeting/{job_id}/send")
    async def meeting_send(session_id: str, job_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The notes as the operator's message in the session."""
        job = job_of(session_id, job_id)
        try:
            return {"run_id": await service().send_to_chat(job)}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
