"""The routes of autopilot and of the operator's style profile.

A session's switch is read and set here; the session's own view carries the same status so its menu
needs no request of its own. The overview is what the settings show beside the configuration: the
day's spend against the allowance and the day's verdicts. The style profile is shown as it is and
can be forgotten; it is never edited by hand, since the next update would write over the edit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from daedalus.extensions.autopilot import Refused
from daedalus.host import user_style

if TYPE_CHECKING:
    from daedalus.app import Application


class AutopilotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> Any:
        found = getattr(app, "extensions", {}).get("autopilot")
        if found is None:
            raise HTTPException(503, "autopilot is not available in this installation")
        return found

    @api.get("/api/sessions/{session_id}/autopilot")
    async def autopilot_status(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().status(session_id)
        except KeyError:
            raise HTTPException(404, "no such session") from None

    @api.post("/api/sessions/{session_id}/autopilot")
    async def autopilot_switch(session_id: str, body: AutopilotBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().set_switch(session_id, body.on)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except Refused as exc:
            raise HTTPException(400, str(exc)) from None

    @api.get("/api/autopilot")
    async def autopilot_overview(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await service().overview()

    @api.get("/api/user-style")
    async def style_view(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return (await user_style.load(app.db)).view()

    @api.delete("/api/user-style")
    async def style_reset(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return (await user_style.reset(app.db)).view()
