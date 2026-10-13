"""Settings → Rules: the action categories, their levels and the auto-review's decision log.

The levels themselves are a section of the settings (``[action_rules]``) and are saved with them;
this answers what the screen needs beside them: the categories in the order the host knows them,
the levels from least to most restrictive, the defaults, and the latest review decisions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

from daedalus.config import ACTION_CATEGORIES, ACTION_LEVELS, DEFAULT_ACTION_LEVELS
from daedalus.host.action_rules import ReviewLog

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/action-rules")
    async def action_rules(limit: int = 100, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {
            "categories": list(ACTION_CATEGORIES),
            "levels": list(ACTION_LEVELS),
            "defaults": dict(DEFAULT_ACTION_LEVELS),
            "log": await ReviewLog(app.db).recent(limit=limit),
        }

    @api.post("/api/sessions/{session_id}/handoffs/{key}/done")
    async def handoff_done(session_id: str, key: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator did what the agent handed them; the request stops being open everywhere."""
        manager = app.manager
        assert manager is not None
        try:
            return await manager.action_rules.handoff_done(session_id, key, via="app")
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc


__all__ = ["register"]
