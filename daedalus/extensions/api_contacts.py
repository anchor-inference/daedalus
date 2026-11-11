"""The routes of standing contacts, and of the words an operator writes to the other side themselves.

Each is the operator's, behind the app's own login. Offering, accepting, breaking and setting a contact's level are
here and nowhere else: what another installation may do on this one is the operator's to decide, and no tool reaches
it. The address of the other side's link is never in an answer: until this side has redeemed it, it is a credential.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.agent_contacts import LEVELS, ContactError, Contacts, contacts_of
from daedalus.extensions.agent_talk import talks_of
from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable, bridge_of, identity_of

if TYPE_CHECKING:
    from daedalus.app import Application

Level = Literal["read", "all", "trusted"]


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    via: str = Field(max_length=120)
    """``talk:<id>``, our conversation at their door, or ``link:<link>:<conversation>``, theirs at ours."""
    name: str = Field(default="", max_length=80)
    """What this side calls them."""
    tools: Level = "read"
    text: str = Field(default="", max_length=600)
    as_name: str = Field(default="", max_length=80)
    """What this side is called in the offer; kept for the next one."""


class Accept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default="", max_length=64)
    """The chat the contact belongs to; empty is a chat of its own."""
    tools: Level = "read"
    name: str = Field(default="", max_length=80)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=80)
    tools: Level | None = None
    usd_limit: float | None = Field(default=None, ge=0, le=10_000)
    """0 is no limit, as on any link."""
    turns_per_day: int | None = Field(default=None, ge=0, le=100_000)


class Words(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=16_000)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def contacts() -> Contacts:
        found = contacts_of(app)
        if found is None:
            raise HTTPException(503, "contacts are not available in this installation")
        return found

    async def listed_links() -> dict[str, dict[str, Any]]:
        bridge = bridge_of(app)
        if bridge is None or not bridge.client.configured:
            return {}
        try:
            return {str(link["id"]): link for link in (await bridge.client.call("GET", "/control/links")).get("links") or []}
        except (DoorUnavailable, DoorRefused):
            return {}

    async def one(contact_id: str) -> dict[str, Any]:
        row = await contacts().contact(contact_id)
        if row is None:
            raise HTTPException(404, "no such contact")
        return await contacts().describe(row, await listed_links())

    @api.get("/api/contacts")
    async def every(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        identity = identity_of(app)
        links = await listed_links()
        rows = [await contacts().describe(row, links) for row in await contacts().every()]
        return {"contacts": rows, "fingerprint": identity.fingerprint if identity is not None else "", "name": await contacts().self_name(), "levels": list(LEVELS)}

    @api.post("/api/contacts/offer")
    async def offer(body: Offer, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            row = await contacts().offer(session_id="", via=body.via, name=body.name, tools=body.tools, text=body.text, as_name=body.as_name)
        except ContactError as exc:
            raise HTTPException(409, str(exc)) from None
        return await one(str(row["id"]))

    @api.post("/api/contacts/{contact_id}/accept")
    async def accept(contact_id: str, body: Accept, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if body.session_id and (app.manager is None or await app.manager.get_state(body.session_id) is None):
            raise HTTPException(404, "no such chat")
        try:
            await contacts().accept(contact_id, session_id=body.session_id, tools=body.tools, name=body.name)
        except ContactError as exc:
            raise HTTPException(409, str(exc)) from None
        return await one(contact_id)

    @api.post("/api/contacts/{contact_id}/decline")
    async def decline(contact_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await contacts().decline(contact_id)
        return {"ok": True}

    @api.post("/api/contacts/{contact_id}/break")
    async def break_off(contact_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await contacts().break_contact(contact_id)
        except ContactError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"ok": True}

    @api.put("/api/contacts/{contact_id}")
    async def configure(contact_id: str, body: Settings, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await contacts().configure(contact_id, name=body.name, tools=body.tools, usd_limit=body.usd_limit, turns_per_day=body.turns_per_day)
        except ContactError as exc:
            raise HTTPException(409, str(exc)) from None
        return await one(contact_id)

    @api.post("/api/contacts/{contact_id}/say")
    async def contact_say(contact_id: str, body: Words, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        row = await contacts().contact(contact_id)
        if row is None or not row["talk_id"]:
            raise HTTPException(404, "this contact has no conversation to write in")
        await say_in_talk(str(row["talk_id"]), body.text)
        return await one(contact_id)

    async def say_in_talk(talk_id: str, text: str) -> None:
        talks = talks_of(app)
        if talks is None or await talks.talk(talk_id) is None:
            raise HTTPException(404, "no such conversation")
        try:
            await talks.say_as_owner(talk_id, text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @api.post("/api/agent-talks/{talk_id}/say")
    async def talk_say(talk_id: str, body: Words, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's own message over a conversation of ours: marked as theirs on the other side."""
        await say_in_talk(talk_id, body.text)
        talks = talks_of(app)
        assert talks is not None
        return await talks.describe(await talks.talk(talk_id), messages=True)

    @api.post("/api/guest/links/{link_id}/conversations/{conversation_id}/say")
    async def guest_say(link_id: str, conversation_id: str, body: Words, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's own message to a guest of one of our links: the guest reads it as the owner's, not the agent's."""
        bridge = bridge_of(app)
        if bridge is None or await bridge.known_link(link_id) is None:
            raise HTTPException(404, "no such link")
        try:
            await bridge.say_as_owner(link_id, conversation_id, body.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except DoorUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except DoorRefused as exc:
            raise HTTPException(exc.status, exc.detail) from None
        return {"ok": True}


__all__ = ["Accept", "Offer", "Settings", "Words", "register"]
