"""The largest WebSocket message each socket of the API takes.

uvicorn has one cap for the whole server (``ws_max_size``), and the sockets want different ones: a
terminal or a live view takes a few kilobytes of typing and is refused past a megabyte, while the
operator's Chrome sends each DevTools reply as one message, and a snapshot's listener list or a
retina screenshot is several megabytes. The server-wide cap was once the terminal's 1 MiB, and every
such reply closed the Chrome relay with 1009 mid-call: browserd ended the browser, and the agent was
told there was no tab, with no id. So the server takes the largest message any socket takes, and
this middleware holds every other socket to the small default with the same close code the server
would have used.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from contextlib import suppress
from typing import Any

from daedalus.browser.user import MAX_FROM_EXTENSION
from daedalus.terminals.gateway import TERMINAL_WS_MAX_BYTES

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SOCKET_MAX_BYTES = TERMINAL_WS_MAX_BYTES
"""Every socket's cap unless :data:`WIDER` names it: the terminal's and the live view's frames are
typing and resizes, and a page proxied under /pages is a preview server's hot reload."""

WIDER: dict[str, int] = {"/api/browser/relay": MAX_FROM_EXTENSION}
"""The sockets that take more, by path."""

SERVER_MAX_BYTES = max(SOCKET_MAX_BYTES, *WIDER.values())
"""What uvicorn itself is told: the largest any socket takes. It reads a message whole before this
middleware sees it, which is the price of one server cap; the relay accepts that much before its
hello anyway."""

TOO_BIG = 1009


def too_big(message: Message, limit: int) -> bool:
    """Whether a received message is past ``limit`` bytes."""
    data = message.get("bytes")
    if data is not None:
        return len(data) > limit
    text = message.get("text") or ""
    # A character is at most four bytes, so most text is judged without encoding it.
    return len(text) * 4 > limit and len(text.encode("utf-8", errors="replace")) > limit


class SocketLimits:
    """ASGI middleware: a message past its socket's cap closes the socket with 1009, and the
    application hears a disconnect, as it would had the server refused the message itself."""

    def __init__(self, app: ASGIApp, *, default: int = SOCKET_MAX_BYTES, wider: Mapping[str, int] | None = None) -> None:
        self.app = app
        self.default = default
        self.wider = dict(WIDER if wider is None else wider)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        limit = self.wider.get(str(scope.get("path") or ""), self.default)
        refused = False

        async def capped() -> Message:
            nonlocal refused
            if refused:
                return {"type": "websocket.disconnect", "code": TOO_BIG}
            message = await receive()
            if message["type"] == "websocket.receive" and too_big(message, limit):
                refused = True
                with suppress(Exception):
                    await send({"type": "websocket.close", "code": TOO_BIG, "reason": "message too big"})
                return {"type": "websocket.disconnect", "code": TOO_BIG}
            return message

        async def guarded(message: Message) -> None:
            # The application closes the socket it was told went away; it is closed already.
            if refused and message["type"] == "websocket.close":
                return
            await send(message)

        await self.app(scope, capped, guarded)


__all__ = ["SERVER_MAX_BYTES", "SOCKET_MAX_BYTES", "WIDER", "SocketLimits", "too_big"]
