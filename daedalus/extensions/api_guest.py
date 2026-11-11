"""The routes of the guest screen: making links, reading what was said, admitting and revoking.

Each one is the operator's, behind the app's own login, and most are a translation of a call on the
door's control socket (:mod:`daedalus.extensions.guest_door`). Nothing here is reachable from the door's
public port, which serves another process altogether.

Limits and lifetimes default here and travel with the link. Each may be 0, which is none at all: the app
offers that as its own choice, "no limit", and never as a number to guess. The invite comes back once, in the
answer to the call that made it, and neither this process nor the door keeps it.

A link's settings can all be changed after it is made. The door holds what it enforces and this side holds
what a turn may do, so an edit writes both, the door first: a door that refuses leaves this side as it was.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock
from pydantic import BaseModel, ConfigDict, Field

from daedalus import public_address
from daedalus.extensions.guest_door import (
    DoorRefused,
    DoorUnavailable,
    GuestBridge,
    bridge_of,
    model_choices,
    model_options,
)
from daedalus.host import guest_turns
from daedalus.host.engine_factory import TENANT
from daedalus.host.notify_text import render

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "mode": "stand_in", "approval": True, "tools": guest_turns.DEFAULT_TOOLS, "usd_limit": 1.0, "turns_per_day": 40, "turns_total": 200,
    "invite_hours": 72.0, "conversation_hours": 168.0, "idle_hours": 24.0, "retention_days": 30.0,
}
"""What the form opens with. Numbers from no standard: a guest is paid for by the owner, so they start small.
A trusted agent is the operator's own choice of somebody to serve, and starts with no limits at all."""
TRUSTED_DEFAULTS: dict[str, Any] = {"usd_limit": 0.0, "turns_per_day": 0, "turns_total": 0, "invite_hours": 0.0, "conversation_hours": 0.0, "idle_hours": 0.0}

DRAFT_MESSAGES = 30
DRAFT_CHARS = 16_000
DRAFT_PROMPT = (
    "Below is the start of a conversation between a person and their agent. Write a briefing for a stand-in: a separate model that will answer somebody "
    "else's agent on this person's behalf, knowing only this text. Keep to what a stranger may be told about the subject. Leave out names, addresses, "
    "passwords, keys, paths and anything private. Plain short paragraphs, in the language of the conversation, nothing else.\n\n"
)
PRIVATE_KINDS = ("subagent_of", "staff_id", "unattended", "heartbeat")
REFUSED_WORDS = {
    "host_folder": "this chat works in a folder on the host, where a guest's command cannot be sandboxed",
    "no_limits": "the limits a guest's commands need cannot be set here",
    "no_sandbox": "the sandbox a guest's commands need cannot be made here",
    "project_folder": "this chat works in a project's folder, which other chats read as the operator's",
}


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=120)
    topic: str = Field(default="", max_length=240)
    mode: Literal["stand_in", "direct"] = DEFAULTS["mode"]
    model_id: str = Field(default="", max_length=200)
    briefing: str = Field(default="", max_length=20_000)
    instructions: str = Field(default="", max_length=4000)
    """A direct link's note to the chat's agent, beside the topic. Kept on this side only: the door never has it."""
    briefing_approved: bool = False
    tools: guest_turns.GuestTools = guest_turns.DEFAULT_TOOLS
    approval: bool = DEFAULTS["approval"]
    usd_limit: float = Field(default=DEFAULTS["usd_limit"], ge=0, le=10_000)
    turns_per_day: int = Field(default=DEFAULTS["turns_per_day"], ge=0, le=100_000)
    turns_total: int = Field(default=DEFAULTS["turns_total"], ge=0, le=1_000_000)
    invite_hours: float = Field(default=DEFAULTS["invite_hours"], ge=0, le=8760)
    conversation_hours: float = Field(default=DEFAULTS["conversation_hours"], ge=0, le=8760)
    idle_hours: float = Field(default=DEFAULTS["idle_hours"], ge=0, le=8760)
    retention_days: float = Field(default=DEFAULTS["retention_days"], ge=0, le=3650)


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usd_per_day: float = Field(ge=0, le=100_000)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def bridge() -> GuestBridge:
        found = bridge_of(app)
        if found is None:
            raise HTTPException(503, "the guest door is not available in this installation")
        return found

    async def door(method: str, path: str, **options: Any) -> dict[str, Any]:
        try:
            return await bridge().client.call(method, path, **options)
        except DoorUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except DoorRefused as exc:
            raise HTTPException(exc.status, exc.detail) from None

    def link_url(invite: str) -> str:
        return f"{public_address.guest_base(app.settings)}/a/{invite}"

    async def door_fields(state: Any, body: LinkBody) -> dict[str, Any]:
        """What the door is to hold for a link, or the reason this chat cannot have it. Shared by making a link and editing one."""
        manager = app.manager
        fields = body.model_dump(exclude={"model_id", "briefing_approved", "tools", "instructions"})
        if body.mode == "direct":
            if any(state.metadata.get(key) for key in PRIVATE_KINDS) or manager.is_dispatcher(state) or manager.is_orchestrator(state) or manager.is_companion(state) or manager.is_prompt_coach(state) or manager.is_btw(state):
                raise HTTPException(409, "a direct link needs an ordinary chat")
            refused = await manager.guest_ready(state.session.id, body.tools)
            if refused:
                raise HTTPException(409, REFUSED_WORDS.get(refused, refused))
            if body.usd_limit and await manager.guest_price(state.session.id) is None:
                raise HTTPException(409, "this chat's model has no known price, so a dollar limit could not hold")
            fields.update(model_path="", model_name="", price_in=0.0, price_out=0.0, briefing="", guest_tools=body.tools)
        else:
            option = next((o for o in model_options(app) if o["id"] == body.model_id), None)
            if option is None:
                raise HTTPException(422, "that model is not one the door can reach")
            if not body.briefing.strip():
                raise HTTPException(422, "a stand-in needs a briefing")
            if not body.briefing_approved:
                # The briefing is published to whoever holds the link, so a person reads it first.
                raise HTTPException(422, "the briefing must be approved before it is used")
            fields.update(model_path=option["path"], model_name=option["model"], price_in=option["price_in"], price_out=option["price_out"], guest_tools="none")
        return fields

    def kept(body: LinkBody) -> dict[str, Any]:
        """What this side keeps of a link: what its turns may do and what the operator wrote for the agent."""
        direct = body.mode == "direct"
        return {
            "label": body.label.strip() or body.topic.strip()[:120], "mode": body.mode, "tools": body.tools if direct else "none", "usd_limit": body.usd_limit,
            "topic": body.topic.strip(), "instructions": body.instructions.strip() if direct else "",
        }

    @api.get("/api/guest/door")
    async def door_state(session_id: str | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Whether links can be made here, and everything the form needs to open."""
        models, left_out = model_choices(app)
        state = await app.manager.get_state(session_id) if session_id and app.manager is not None else None
        why = await app.manager.guest_full_tools(state) if state is not None else "no_sandbox"
        price = await app.manager.guest_price(state.session.id) if state is not None else None
        out: dict[str, Any] = {
            "available": False, "public_base": public_address.guest_base(app.settings), "defaults": DEFAULTS, "trusted_defaults": TRUSTED_DEFAULTS, "models": models, "left_out": left_out, "usd_per_day": 0.0, "spent_today": 0.0,
            "full_tools": not why, "full_tools_why": why, "chat_priced": bool(any(price)) if price else True,
        }
        try:
            health = await bridge().client.call("GET", "/control/health")
        except (DoorUnavailable, DoorRefused):
            return out
        today = health.get("today") or {}
        return {**out, "available": True, "usd_per_day": health.get("usd_per_day", 0.0), "spent_today": float(today.get("usd") or 0.0) + float(today.get("reserved") or 0.0)}

    @api.put("/api/guest/settings")
    async def door_settings(body: SettingsBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await door("PUT", "/control/settings", json={"usd_per_day": body.usd_per_day})

    @api.get("/api/guest/links")
    async def links(session_id: str | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        mine = await bridge().links_of(session_id)
        listed = await door("GET", "/control/links")
        return {"links": [link for link in listed.get("links") or [] if link.get("id") in mine]}

    @api.get("/api/guest/links/{link_id}")
    async def one_link(link_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        known = await bridge().known_link(link_id)
        if known is None:
            raise HTTPException(404, "no such link")
        out = await door("GET", f"/control/links/{link_id}")
        return {**out, "link": described(out.get("link") or {}, known)}

    def described(link: dict[str, Any], known: Any) -> dict[str, Any]:
        """The door's view of a link, with what only this side holds: the turn's level and the note to the agent,
        and the model as the form names it, so an edit opens on what the link has."""
        model_id = next((o["id"] for o in model_options(app) if o["path"] == link.get("model_path") and o["model"] == link.get("model_name")), "")
        tools = str(known["tools"]) if known["mode"] == "direct" else "none"
        return {**link, "guest_tools": tools, "instructions": str(known["instructions"] or ""), "model_id": model_id}

    @api.put("/api/guest/links/{link_id}")
    async def edit_link(link_id: str, body: LinkBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every setting of a link, changed in place: the invite and the conversation stay what they are."""
        known = await bridge().known_link(link_id)
        if known is None or known["revoked_at"]:
            raise HTTPException(404, "no such link")
        manager = app.manager
        state = await manager.get_state(str(known["session_id"])) if manager is not None else None
        if state is None:
            raise HTTPException(404, "no such session")
        out = await door("PUT", f"/control/links/{link_id}", json=await door_fields(state, body))
        await bridge().relabel_link(link_id, **kept(body))
        return {**out, "link": described(out.get("link") or {}, await bridge().known_link(link_id))}

    @api.post("/api/sessions/{session_id}/guest/links")
    async def create_link(session_id: str, body: LinkBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        manager = app.manager
        state = await manager.get_state(session_id) if manager is not None else None
        if state is None:
            raise HTTPException(404, "no such session")
        if not public_address.guest_base(app.settings):
            raise HTTPException(409, "the guest door has no public address: set GUEST_PUBLIC_URL")
        made = await door("POST", "/control/links", json=await door_fields(state, body))
        link = made["link"]
        mine = kept(body)
        await bridge().remember_link(link["id"], session_id, mine.pop("label"), mine.pop("mode"), mine.pop("tools"), mine.pop("usd_limit"), **mine)
        return {"link": link, "url": link_url(made["invite"])}

    @api.post("/api/guest/links/{link_id}/revoke")
    async def revoke_link(link_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if await bridge().known_link(link_id) is None:
            raise HTTPException(404, "no such link")
        # This side first, marked as not yet told: with the door out of reach the turn must still stop,
        # the operator must still hear so, and the next connection finishes the job.
        await bridge().revoked(link_id, door_told=False)
        out = await door("POST", f"/control/links/{link_id}/revoke")
        await bridge().revoked(link_id)
        await bridge().contact_settled(str(((out.get("link") or {}).get("conversation") or {}).get("id") or ""), render("guest.revoked", app.notifications.language()) if app.notifications is not None else "")
        return out

    @api.post("/api/guest/links/{link_id}/reissue")
    async def reissue_link(link_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The same settings on a new invite, for a guest who lost their token."""
        known = await bridge().known_link(link_id)
        if known is None:
            raise HTTPException(404, "no such link")
        made = await door("POST", f"/control/links/{link_id}/reissue")
        await bridge().revoked(link_id)
        await bridge().remember_link(
            made["link"]["id"], str(known["session_id"]), str(known["label"]), str(known["mode"]), str(known["tools"]), float(known["usd_limit"]),
            topic=str(known["topic"] or ""), instructions=str(known["instructions"] or ""),
        )
        return {"link": made["link"], "url": link_url(made["invite"])}

    @api.post("/api/guest/conversations/{conversation_id}/{decision}")
    async def decide(conversation_id: str, decision: Literal["admit", "refuse"], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        out = await door("POST", f"/control/conversations/{conversation_id}/{decision}")
        if app.notifications is not None:
            await bridge().contact_settled(conversation_id, render("guest.admitted" if decision == "admit" else "guest.refused", app.notifications.language()))
        return out

    @api.post("/api/sessions/{session_id}/guest/draft")
    async def draft_briefing(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A briefing to start from, written once from the chat. It goes to the form and nowhere else:
        nothing here stores it, and the link does not exist until the operator has read and approved it."""
        manager = app.manager
        if manager is None or await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        return {"briefing": await write_draft(manager, session_id)}


async def write_draft(manager: Any, session_id: str) -> str:
    """A cheap call on the chat's own model, over the operator's and the agent's plain words only."""
    spoken: list[str] = []
    for message in (await manager.sessions.list_transcript(session_id, limit=400))[-DRAFT_MESSAGES:]:
        if guest_turns.is_guest(str(message.metadata.get("daedalus.origin") or "")):
            continue
        if message.role in (MessageRole.user, MessageRole.assistant):
            text = "".join(block.text for block in message.content_blocks if isinstance(block, TextBlock)).strip()
            if text:
                spoken.append(f"{message.role.value}: {text[:2000]}")
    rungs, _ = manager.resolve_model(await manager.live.load(session_id))
    if not rungs:
        raise HTTPException(409, "no model is configured")
    provider, model = rungs[0]
    request = LLMRequest(
        model=model, messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=DRAFT_PROMPT + "\n".join(spoken)[-DRAFT_CHARS:])])], max_tokens=500, temperature=0.2,
        extra={"enable_thinking": False}, observability=LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, call_purpose="guest_briefing", call_category="session"),
    )
    try:
        response = await asyncio.wait_for(provider.complete_text(request), timeout=60)
    except TimeoutError:
        raise HTTPException(504, "the model did not answer in time") from None
    return "".join(block.text for block in response.message.content_blocks if isinstance(block, TextBlock)).strip()


__all__ = ["DEFAULTS", "TRUSTED_DEFAULTS", "LinkBody", "register", "write_draft"]
