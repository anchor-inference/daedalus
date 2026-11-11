"""The routes of a chat's conversations with other agents: seeing them, ending them, and what the agent may not set.

Each is the operator's, behind the app's own login. Review and waking are here and nowhere else on purpose: they
are what stands between a stranger and an unattended turn, so no tool can reach them, whatever a reply talks the
agent into. A token and an invite are never in an answer: :meth:`AgentTalks.describe` names its fields.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.agent_talk import MAX_SENDS, NO_LIMIT, WAKE_TURNS_MAX, WAKE_USD_MAX, AgentTalks, talks_of
from daedalus.extensions.guest_door import bridge_of
from daedalus.extensions.link_flows import flows_of
from daedalus.host import link_protocol

FILE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "private, max-age=300"}
"""On every file of a link the app is handed: a stranger chose its bytes, so nothing in it runs and nothing guesses its type."""

if TYPE_CHECKING:
    from daedalus.app import Application


class TalkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: Literal["first", "all", "off"] | None = None
    wake_turns: int | None = Field(default=None, ge=NO_LIMIT, le=WAKE_TURNS_MAX)
    """-1 is waking on every reply; 0 is not waking."""
    wake_usd: float | None = Field(default=None, ge=NO_LIMIT, le=WAKE_USD_MAX)
    """-1 is no budget for those turns but the cap on any run."""
    sends_limit: int | None = Field(default=None, ge=0, le=10_000)
    """0 is no limit on the messages this side sends."""
    file_mb: float | None = Field(default=None, ge=0, le=1_000_000)
    """The largest file either way, in megabytes; 0 is no limit."""
    day_mb: float | None = Field(default=None, ge=0, le=1_000_000)
    """What files may take in a day, each way; 0 is no limit."""


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=64_000)
    """The operator's edit of a held message, sent in its place. Only with ``send``."""
    drop: list[int] = Field(default_factory=list, max_length=link_protocol.MAX_FILES)
    """Files of the message to hold back; the rest go with it. Only with ``send``."""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def talks() -> AgentTalks:
        found = talks_of(app)
        if found is None:
            raise HTTPException(503, "agent links are not available in this installation")
        return found

    async def known(talk_id: str) -> Any:
        talk = await talks().talk(talk_id)
        if talk is None:
            raise HTTPException(404, "no such conversation")
        return talk

    @api.get("/api/sessions/{session_id}/agent-talks")
    async def of_session(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        bridge = bridge_of(app)
        # The files this chat's links want to send, beside its own conversations: both wait for the operator over the composer.
        held = await bridge.held(session_id) if bridge is not None else []
        return {
            "talks": [await talks().describe(talk) for talk in await talks().talks_of(session_id)], "sends_limit": MAX_SENDS, "wake_turns_max": WAKE_TURNS_MAX, "wake_usd_max": WAKE_USD_MAX,
            "guest_held": held, "file_mb": link_protocol.DEFAULT_FILE_MB, "day_mb": link_protocol.DEFAULT_DAY_MB,
            "polls": await flows.polls_of(session_id) if (flows := flows_of(app)) is not None else [],
        }

    @api.get("/api/agent-talks/{talk_id}")
    async def one(talk_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await talks().describe(await known(talk_id), messages=True)

    @api.post("/api/agent-talks/{talk_id}/end")
    async def end(talk_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await known(talk_id)
        await talks().end(talk_id)
        return await talks().describe(await known(talk_id))

    @api.put("/api/agent-talks/{talk_id}/settings")
    async def settings(talk_id: str, body: TalkSettings, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        talk = await known(talk_id)
        if talk["state"] == "closed":
            raise HTTPException(409, "this conversation is over")
        if body.wake_usd is not None and NO_LIMIT < body.wake_usd < 0:
            raise HTTPException(422, "wake_usd is -1 for no limit, or 0 and more")
        await talks().configure(talk_id, review=body.review, wake_turns=body.wake_turns, wake_usd=body.wake_usd, sends_limit=body.sends_limit, file_mb=body.file_mb, day_mb=body.day_mb)
        return await talks().describe(await known(talk_id))

    @api.post("/api/agent-talks/{talk_id}/tasks/{task}/cancel")
    async def cancel_task(talk_id: str, task: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await known(talk_id)
        outcome = await talks().cancel(talk_id, task)
        if outcome.error:
            raise HTTPException(409, outcome.text)
        return await talks().describe(await known(talk_id), messages=True)

    @api.get("/api/link-files/{file_id}")
    async def link_file(file_id: int, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        """A file of an agent link, either way: a picture shown as one, anything else saved, never opened as a page."""
        row = await app.db.fetchone("SELECT name, mime, path FROM link_files WHERE id = ?", (file_id,))
        path = Path(str(row["path"])) if row is not None and row["path"] else None
        if path is None or not path.is_file():
            raise HTTPException(404, "this file is not kept here")
        image = str(row["mime"]) in link_protocol.IMAGE_TYPES
        return FileResponse(
            path, media_type=str(row["mime"]) if image else "application/octet-stream", filename=str(row["name"]), content_disposition_type="inline" if image else "attachment",
            headers=FILE_HEADERS,
        )

    @api.post("/api/agent-talks/{talk_id}/messages/{message_id}/{decision}")
    async def decide(talk_id: str, message_id: int, decision: Literal["send", "refuse"], body: Decision | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await known(talk_id)
        try:
            decided = await talks().decide(talk_id, message_id, decision == "send", via="app", text=body.text if body is not None else None, drop=body.drop if body is not None else None)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if not decided:
            raise HTTPException(409, "this message is not waiting")
        return await talks().describe(await known(talk_id), messages=True)


__all__ = ["Decision", "TalkSettings", "register"]
