"""The routes of a chat's conversations with other agents: seeing them, ending them, and what the agent may not set.

Each is the operator's, behind the app's own login. Review and waking are here and nowhere else on purpose: they
are what stands between a stranger and an unattended turn, so no tool can reach them, whatever a reply talks the
agent into. A token and an invite are never in an answer: :meth:`AgentTalks.describe` names its fields.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.agent_talk import MAX_SENDS, WAKE_TURNS_MAX, WAKE_USD_MAX, AgentTalks, talks_of

if TYPE_CHECKING:
    from daedalus.app import Application


class TalkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: Literal["first", "all", "off"] | None = None
    wake_turns: int | None = Field(default=None, ge=0, le=WAKE_TURNS_MAX)
    wake_usd: float | None = Field(default=None, ge=0, le=WAKE_USD_MAX)


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
        return {"talks": [await talks().describe(talk) for talk in await talks().talks_of(session_id)], "sends_limit": MAX_SENDS, "wake_turns_max": WAKE_TURNS_MAX, "wake_usd_max": WAKE_USD_MAX}

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
        await talks().configure(talk_id, review=body.review, wake_turns=body.wake_turns, wake_usd=body.wake_usd)
        return await talks().describe(await known(talk_id))

    @api.post("/api/agent-talks/{talk_id}/messages/{message_id}/{decision}")
    async def decide(talk_id: str, message_id: int, decision: Literal["send", "refuse"], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await known(talk_id)
        if not await talks().decide(talk_id, message_id, decision == "send", via="app"):
            raise HTTPException(409, "this message is not waiting")
        return await talks().describe(await known(talk_id), messages=True)


__all__ = ["TalkSettings", "register"]
