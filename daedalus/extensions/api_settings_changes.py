"""The agent's settings changes as the app's card reads and answers them.

The card under a ``SettingsChange`` step asks for the change by the id the tool's answer names, and
its Apply and Reject go through the same decision as the notification's buttons: whichever is
pressed first wins, and the other is answered 409 with the change as it now stands.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from daedalus.extensions.agent_settings import SettingsChanges, public
from daedalus.extensions.notifications import ActionConflict

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> SettingsChanges:
        found = app.extensions.get("agent_settings")
        if not isinstance(found, SettingsChanges):
            raise HTTPException(503, "settings changes are not running on this installation")
        return found

    @api.get("/api/settings/changes/{change_id}")
    async def settings_change(change_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        record = await service().view(change_id)
        if record is None:
            raise HTTPException(404, "no such settings change")
        return public(record)

    @api.post("/api/settings/changes/{change_id}/{action}")
    async def decide_settings_change(change_id: str, action: Literal["apply", "reject"], _: dict[str, Any] = Depends(auth)) -> Any:
        changes = service()
        try:
            record = await changes.decide(change_id, action, via="app")
        except LookupError as exc:
            raise HTTPException(404, "no such settings change") from exc
        except ActionConflict:
            current = await changes.view(change_id)
            return JSONResponse(public(current) if current else {"detail": "no such settings change"}, status_code=409)
        return public(record)


__all__ = ["register"]
