"""The routes of a chat's prompt coach: open it (or resume it) from the composer, and read what it saved.

The window over the chat embeds the coach's session like any other and polls ``GET`` for the result, which
the coach's ``SavePrompt`` writes into the coach's metadata (:mod:`daedalus.host.prompt_coach`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.host import prompt_coach

if TYPE_CHECKING:
    from daedalus.app import Application


class CoachBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(default="", max_length=prompt_coach.PROMPT_MAX_CHARS)
    """The coach's first turn when a new one is started: the draft, framed by the app in the operator's
    language, so the coach answers in it. Ignored when an unfinished coach is resumed."""
    fresh: bool = False
    """Start over: replace the chat's coach even when it is unfinished."""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def manager() -> Any:
        found = getattr(app, "manager", None)
        if found is None:
            raise HTTPException(503, "sessions are not available in this installation")
        return found

    @api.get("/api/sessions/{session_id}/coach")
    async def coach_view(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The chat's coach and what it saved; ``coach`` is null for a chat that has none."""
        sessions = manager()
        if await sessions.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        return {"coach": await prompt_coach.latest(sessions, session_id)}

    @api.post("/api/sessions/{session_id}/coach")
    async def coach_open(session_id: str, body: CoachBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The chat's unfinished coach, or a new one whose first turn is ``message``."""
        try:
            opened = await prompt_coach.open_coach(manager(), session_id, body.message, fresh=body.fresh)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except prompt_coach.CoachRefused as exc:
            raise HTTPException(400, str(exc)) from None
        except (RuntimeError, ValueError) as exc:
            # The chat's folder is out of reach (a host folder with the daemon down, an unmounted one).
            raise HTTPException(409, str(exc)) from None
        return {"coach": opened}
