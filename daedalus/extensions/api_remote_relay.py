"""``wss /api/cli/relay``: the door a paired terminal client's executor comes in by, and the audit of what it ran.

The relay is public in the sense that it holds no app session: the client presents its device token as
``Authorization: Device <token>`` on the upgrade request. The device door in front of the app checks it
when it is installed and hands the device over in the request's scope; otherwise it is checked here. A
socket opened from a web page (an ``http`` or ``https`` ``Origin``) is refused before anything is read,
so a page the operator visits cannot borrow a token it does not have and probe the door from their own
network. Everything after the handshake is :meth:`daedalus.remote.relay.DeviceHub.serve`.

``GET /api/remote/calls`` is the operator's audit: every request the server sent, or held, for a
terminal, newest first, by conversation or by device.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from daedalus.remote.relay import DeviceHub, RemoteDevice, device_token

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.remote.journal import Journal

DOOR_SCOPE_KEY = "daedalus.device"
"""Where the device door leaves the device it checked (``daedalus.remote.door.SCOPE_KEY``)."""


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
            await self.websocket.close(code, reason[:120])


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def hub() -> DeviceHub | None:
        return cast("DeviceHub | None", app.extensions.get("remote_relay"))

    @api.websocket("/api/cli/relay")
    async def cli_relay(websocket: WebSocket) -> None:
        origin = (websocket.headers.get("origin") or "").lower()
        desk = hub()
        if desk is None or origin.startswith(("http://", "https://")):
            await websocket.close(1008)
            return
        await websocket.accept()
        checked = websocket.scope.get(DOOR_SCOPE_KEY)
        device = RemoteDevice(str(checked["device"]), str(checked.get("device_name") or "")) if isinstance(checked, dict) and checked.get("device") else None
        await desk.serve(
            RelaySocket(websocket),
            token=device_token(websocket.headers.get("authorization") or ""),
            address=websocket.client.host if websocket.client else "",
            device=device,
        )

    @api.get("/api/remote/calls")
    async def remote_calls(session: str = "", device: str = "", limit: int = 200, caller: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the agent ran on the operator's terminals: one row per request, with its delivery and outcome."""
        if caller.get("via") == "device":
            raise HTTPException(403, "a terminal's credential does not open the audit")
        journal = cast("Journal | None", app.extensions.get("remote_journal"))
        if journal is None:
            raise HTTPException(404, "this installation has no relay for terminals")
        return {"calls": await journal.list(session_id=session or None, device_id=device or None, limit=limit)}


__all__ = ["register"]
