"""The routes of the operator's own Chrome: the card in Settings → Environments, a conversation's switch,
and — on a server — the two public doors a paired Chrome comes in by.

``/api/browser/user*`` are the operator's (the app's authentication). ``POST /api/browser/pair`` and
``wss /api/browser/relay`` are public, because the extension holds no app session: the first spends a
one-time code the operator read in the app for a device token, the second takes that token in its
first frame. Both share one limit on failed attempts (``daedalus.browser.user.Attempts``), compare in
constant time, keep only hashes, and log nothing of what was presented.

Everything they do is in ``daedalus.browser``; this module adapts the framework to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from daedalus.browser.model import USER, InvalidRequest, NotFound, Owner, Unsupported
from daedalus.browser.user import CONSENT, Pairing, RelayDesk
from daedalus.host import launcher_bridge

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.browser.service import Browsers


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    env: Literal["agent", "user"]
    consent: bool = False
    """The operator said yes to the consent summary; needed for a conversation's first switch."""


class PairBody(BaseModel):
    """What the extension sends with the code. Unknown fields are ignored rather than refused: an
    extension a release ahead must still be able to pair."""

    code: str = Field(min_length=1, max_length=32)
    profile_id: str = Field(default="", max_length=200)
    profile_name: str = Field(default="", max_length=80)
    ext_version: str = Field(default="", max_length=64)
    chrome_version: str = Field(default="", max_length=64)


class RelaySocket:
    """The framework's WebSocket as the relay's socket: text frames, both ways."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def receive(self) -> bytes | str | None:
        try:
            message = await self.websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if message["type"] == "websocket.disconnect":
            return None
        if message.get("text") is not None:
            return str(message["text"])
        return bytes(message.get("bytes") or b"")

    async def send_text(self, text: str) -> None:
        await self.websocket.send_text(text)

    async def close(self, code: int, reason: str = "") -> None:
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self.websocket.close(code, reason)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    settings = app.settings

    def service() -> Browsers:
        found = app.extensions.get("browser")
        if found is None:
            raise HTTPException(404, "this installation has no browser")
        return cast("Browsers", found)

    def pairing() -> Pairing:
        found = app.extensions.get("browser_pairing")
        if found is None:
            raise HTTPException(404, "this installation has no browser for your Chrome")
        return cast("Pairing", found)

    def relays() -> RelayDesk | None:
        return cast("RelayDesk | None", app.extensions.get("browser_relays"))

    async def launcher_report() -> tuple[dict[str, Any] | None, str]:
        """The desktop launcher's report on Chrome and the connector, natively; nothing on a server."""
        if not settings.native:
            return None, ""
        launcher = await asyncio.to_thread(launcher_bridge.read, settings.state_dir)
        if launcher is None:
            return None, "the desktop launcher is not running"
        try:
            return await launcher_bridge.user_browser(launcher), ""
        except launcher_bridge.LauncherUnavailable as exc:
            return None, str(exc)

    async def devices() -> list[dict[str, Any]]:
        found = app.extensions.get("browser_pairing")
        if found is None or settings.native:
            return []
        live = relays()
        connected = live.connected_device() if live is not None else ""
        return [d.view(connected=d.id == connected) for d in await cast("Pairing", found).devices()]

    @api.get("/api/browser/user")
    async def user_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The card "Your Chrome": the daemon and the Chrome connected to it, natively the launcher's
        report on the connector and the extension, on a server the paired devices."""
        found = cast("Browsers | None", app.extensions.get("browser"))
        status: dict[str, Any] = await found.user_status() if found is not None else {"configured": False, "enabled": False, "daemon": False, "reason": "not_configured", "detail": "", "relay": None}
        report, error = await launcher_report()
        return {
            **status,
            "native": bool(settings.native),
            "launcher": report,
            "launcher_error": error,
            "devices": await devices(),
            "server_url": settings.miniapp_public_url.rstrip("/"),
            "consent": CONSENT,
        }

    @api.post("/api/browser/user/register")
    async def user_register(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Write Chrome's native-messaging host for the extension, through the launcher, which owns the
        machine's files; answers its report (``restart_chrome`` when a running Chrome must restart)."""
        return await connector("register")

    @api.post("/api/browser/user/unregister")
    async def user_unregister(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await connector("unregister")

    async def connector(action: str) -> dict[str, Any]:
        if not settings.native:
            raise Unsupported("the connector is for Daedalus on this computer; on a server, pair the browser instead")
        launcher = await asyncio.to_thread(launcher_bridge.read, settings.state_dir)
        if launcher is None:
            raise HTTPException(503, "the desktop launcher is not running")
        try:
            report = await launcher_bridge.user_browser(launcher, action)
        except launcher_bridge.LauncherUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return {"launcher": report, "error": str(report.get("error") or "")}

    @api.post("/api/browser/user/check")
    async def user_check(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Host → daemon → extension and back: the round trip, while the extension flashes."""
        return await service().check_user()

    @api.post("/api/browser/user/pairing")
    async def user_pairing(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A one-time code for 10 minutes, and the address the operator types beside it."""
        if settings.native:
            raise InvalidRequest("on this computer Chrome connects through the connector; pairing is for a server")
        if not service().configured(USER):
            raise Unsupported("this installation has no browser for your Chrome (the browser-user service is not configured)")
        minted = await pairing().mint()
        return {**minted, "server_url": settings.miniapp_public_url.rstrip("/")}

    @api.get("/api/browser/user/devices")
    async def user_devices(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"devices": await devices()}

    @api.delete("/api/browser/user/devices/{device_id}")
    async def user_device_revoke(device_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a paired Chrome: its token stops working, and a relay it holds now ends."""
        if not await pairing().revoke(device_id):
            raise NotFound("no such paired browser")
        live = relays()
        if live is not None:
            await live.revoked(device_id)
        return {"ok": True}

    async def session_choice(session_id: str) -> dict[str, Any]:
        found = service()
        if not await found.owners.exists(Owner("session", session_id)):
            raise NotFound(f"no session {session_id}")
        choice = await found.choices.get(session_id)
        return {**choice, "offered": found.user_offered(), "consent": CONSENT}

    @api.get("/api/browser/user/sessions/{session_id}")
    async def user_session(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await session_choice(session_id)

    @api.put("/api/browser/user/sessions/{session_id}")
    async def user_session_switch(session_id: str, body: SwitchBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's switch of a conversation between the agent's browser and their own Chrome.
        The first switch there answers 409 ``consent_needed`` with the summary until it carries
        ``consent: true``. Nothing the agent can call reaches this."""
        found = service()
        await session_choice(session_id)
        if body.env == USER and not found.user_offered():
            raise Unsupported("this installation does not offer your Chrome (Settings → Environments → Your Chrome)")
        await found.choices.switch(session_id, body.env, consent=body.consent)
        if found.bus is not None:
            with suppress(Exception):
                await found.bus.publish("browser.user", {"kind": "switched", "profile_name": "", "reason": body.env}, session_id=session_id)
        return await session_choice(session_id)

    # -- the public doors ---------------------------------------------------------------------

    @api.post("/api/browser/pair")
    async def browser_pair(body: PairBody, request: Request) -> dict[str, Any]:
        """A paired Chrome's first contact: a one-time code for a device token, returned once."""
        if settings.native:
            raise HTTPException(404, "not here")
        address = request.client.host if request.client else ""
        token = await pairing().redeem(body.code, address=address, profile_id=body.profile_id, profile_name=body.profile_name, ext_version=body.ext_version, chrome_version=body.chrome_version)
        return {"token": token}

    @api.websocket("/api/browser/relay")
    async def browser_relay(websocket: WebSocket) -> None:
        """A paired Chrome's relay. A web page is no extension: a socket opened from one (an http or
        https ``Origin``) is refused before anything is read, so a page the operator visits cannot
        borrow a token it does not have and probe the door with the operator's own network."""
        origin = (websocket.headers.get("origin") or "").lower()
        desk = relays()
        if settings.native or desk is None or origin.startswith(("http://", "https://")):
            await websocket.close(1008)
            return
        await websocket.accept()
        await desk.serve(RelaySocket(websocket), address=websocket.client.host if websocket.client else "")


__all__ = ["register"]
