"""The routes of a chat's side question (``/btw``): ask one from the composer, and close its window.

The window embeds the side question's session like any other, so its answer streams, renders and takes
follow-ups the way a chat does (:mod:`daedalus.host.btw`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.host import btw

if TYPE_CHECKING:
    from daedalus.app import Application


class BtwBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=btw.QUESTION_MAX_CHARS)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def manager() -> Any:
        found = getattr(app, "manager", None)
        if found is None:
            raise HTTPException(503, "sessions are not available in this installation")
        return found

    @api.post("/api/sessions/{session_id}/btw")
    async def btw_open(session_id: str, body: BtwBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A new side question about the chat, replacing its earlier one; the chat's run goes on untouched."""
        try:
            opened = await btw.open_btw(manager(), session_id, body.question)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except btw.BtwRefused as exc:
            raise HTTPException(400, str(exc)) from None
        except (RuntimeError, ValueError) as exc:
            # The chat's folder is out of reach (a host folder with the daemon down, an unmounted one).
            raise HTTPException(409, str(exc)) from None
        return {"btw": opened}

    @api.delete("/api/sessions/{session_id}/btw")
    async def btw_close(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The window closed: the chat's side question goes, an answer still being written included."""
        return {"deleted": await btw.close_btw(manager(), session_id)}
