"""The operator's routes for what agents do together over a link: the answers to one question, summaries and what
becomes of them, a guest's subscriptions, and a service shown to a guest.

Each is the operator's, behind the app's own login, and none is reachable from a tool: ending a subscription or
taking back a guest's view of a service is the operator's word over what a guest's turn set up.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

from daedalus.extensions.link_flows import LinkFlows, flows_of

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def flows() -> LinkFlows:
        found = flows_of(app)
        if found is None:
            raise HTTPException(503, "agent links are not available in this installation")
        return found

    @api.post("/api/agent-polls/{poll_id}/dismiss")
    async def dismiss_poll(poll_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        if not await flows().dismiss_poll(poll_id):
            raise HTTPException(404, "no such question")
        return {"ok": True}

    @api.post("/api/agent-talks/{talk_id}/summary")
    async def summarise_talk(talk_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await flows().summarise_talk(talk_id)
        except KeyError:
            raise HTTPException(404, "no such conversation") from None
        return {"cards": await flows().cards_of("talk", talk_id)}

    @api.post("/api/guest/links/{link_id}/summary")
    async def summarise_link(link_id: str, conversation: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await flows().summarise_link(link_id, conversation=conversation)
        except KeyError:
            raise HTTPException(404, "no such link") from None
        except Exception as exc:  # noqa: BLE001 — a door out of reach is said, not a 500
            raise HTTPException(503, f"the door did not answer: {exc}") from None
        return {"cards": await flows().cards_of("door", link_id)}

    @api.post("/api/link-cards/{card_id}/note")
    async def card_to_note(card_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await flows().to_notes(card_id)
        except KeyError:
            raise HTTPException(404, "no such summary") from None

    @api.post("/api/link-cards/{card_id}/board")
    async def card_to_board(card_id: int, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await flows().to_board(card_id, authenticated)
        except KeyError:
            raise HTTPException(404, "no such summary") from None
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, str(exc)) from None

    @api.post("/api/link-subscriptions/{sub_id}/end")
    async def end_subscription(sub_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        if not await flows().end_subscription(sub_id, "operator"):
            raise HTTPException(409, "this subscription is not running")
        return {"ok": True}

    @api.post("/api/link-actions/{action_id}/revoke")
    async def revoke_action(action_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        if not await flows().revoke_service(action_id, "operator"):
            raise HTTPException(409, "nothing of this is open")
        return {"ok": True}


__all__ = ["register"]
