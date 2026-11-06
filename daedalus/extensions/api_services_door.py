"""The services door: a listener of its own that answers ``/s/`` and nothing else.

A shared service runs pages an agent wrote. On the app's origin such a page holds everything the
operator's browser holds there: its script reads the API with the session cookie, however carefully
the proxy strips that cookie from what the service receives. So the pages live on another origin.

A port is the one way to another origin that needs nothing from anyone: no DNS, no certificate, and it
works on a bare address and on a laptop's loopback interface alike. Behind a proxy with a name, the
proxy gives the same port a host of its own. The two answers are one listener.

It is a listener rather than a check of ``Host`` because the listener cannot be argued with: nothing a
client sends turns a request on this port into a request to the API. ``SCOPE_KEY`` marks the requests
that came through here, and no client can set it.

Another origin is not the whole answer. Cookies ignore ports and a sibling subdomain is the same site,
so a page here can still make the browser send the operator's cookie to the API; measured in Chromium,
a POST from another port of the same host carried it and was obeyed. What stops that is the check in
``daedalus.gateway.origin``, and the two only work together.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING

import uvicorn
from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketClose

from daedalus.extensions.api_page_check import QuietServer, bind_free
from daedalus.host.pages import PageError

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from daedalus.config import Settings

logger = logging.getLogger(__name__)

SCOPE_KEY = "daedalus.api_services_door"
LAN_KEY = "daedalus.lan_door"
"""Set on a request that came in by the door on this machine's home network: plain http, whatever the
door's address says, so a cookie set there cannot be marked for https."""
LAN_MINUTES = 60
"""How long the home network's door stays open after the last QR that named it."""


class ServicesDoor:
    """The whole application of the door's listener: ``/s/`` reaches the API's routes, nothing else does.

    A WebSocket under ``/s/`` goes the same way: a hot reload and a live app need it, and its route holds
    it to the same rules as a page. Anywhere else one is closed before it opens.
    """

    def __init__(self, api: ASGIApp) -> None:
        self.api = api

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "websocket" and not path.startswith("/s/"):
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        if path.startswith("/s/"):
            scope[SCOPE_KEY] = True
            await self.api(scope, receive, send)
        elif path == "/robots.txt":
            # The one address a crawler asks: nothing on this host is for an index.
            await PlainTextResponse("User-agent: *\nDisallow: /\n")(scope, receive, send)
        else:
            await PlainTextResponse("nothing here", status_code=404)(scope, receive, send)


class LanDoor(ServicesDoor):
    """The services door on the machine's home network, for a phone beside it.

    Natively the door stands on loopback and a phone cannot reach it. So this opens on the one address the
    machine has on that network, when a QR asks for it, and closes itself an hour later: the operator asked
    for a phone to reach one thing, and a port that stays open on someone's Wi-Fi was not part of that. It
    serves what the door serves: nothing but a service that is shared or has a pass.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope[LAN_KEY] = True
        await super().__call__(scope, receive, send)


def lan_address() -> str:
    """This machine's address on its home network, or empty. No packet is sent: connecting a datagram
    socket only asks the system which interface it would use."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            address = str(probe.getsockname()[0])
        except OSError:
            return ""
    parsed = ipaddress.ip_address(address)
    return address if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local else ""


class Lan:
    """The one open home-network door, with its clock."""

    def __init__(self, make: Callable[[ASGIApp], uvicorn.Config], api: ASGIApp) -> None:
        self.make = make
        self.api = api
        self.host = ""
        self.port = 0
        self.server: QuietServer | None = None
        self.closer: asyncio.TimerHandle | None = None

    def base(self, settings: Settings, address: str) -> str:
        """The door's address on ``address``, opening it if need be; empty when no port can be had."""
        if self.server is None or self.host != address:
            self.close()
            port = settings.services_door_port or 8766
            try:
                sock = bind_free(port, port + 16, address, set())
            except PageError:
                logger.warning("no port on the home network could be had for the services door")
                return ""
            self.host, self.port = address, int(sock.getsockname()[1])
            self.server = QuietServer(self.make(LanDoor(self.api)))
            asyncio.create_task(_serve(self.server, sock), name="lan-door")  # noqa: RUF006 — close() ends it, and it holds no result
        if self.closer is not None:
            self.closer.cancel()
        self.closer = asyncio.get_running_loop().call_later(LAN_MINUTES * 60, self.close)
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        """Told to end, not cancelled. A cancelled server stops accepting and leaves every connection it
        holds serving: a keep-alive or a WebSocket from the Wi-Fi would have outlived the door."""
        if self.closer is not None:
            self.closer.cancel()
            self.closer = None
        if self.server is not None:
            self.server.should_exit = True
            self.server.force_exit = True
            self.server = None


def open_door(settings: Settings, config: uvicorn.Config) -> tuple[QuietServer, asyncio.Task[None]] | None:
    """Start serving ``config`` on the door's port; None when the port cannot be had.

    A port somebody else holds must not stop the bot, and must not leave links pointing at a door
    that is not there: the port is cleared, which is what ``services_base`` reads, so shares fall back
    to the sandbox on the app's own address.
    """
    port = settings.services_door_port
    if not port:
        return None
    try:
        sock = bind_free(port, port, settings.api_host, set())
    except PageError:
        logger.warning("the services door cannot listen on %s; shared services stay in a sandbox on the app's address", port)
        settings.services_door_port = 0
        return None
    server = QuietServer(config)
    return server, asyncio.create_task(_serve(server, sock), name="services-door")


async def _serve(server: QuietServer, sock: socket.socket) -> None:
    """Serve until cancelled, which is how the application stops this task, and then stop listening.

    A cancelled uvicorn never reaches its own shutdown, and a socket it was handed goes on accepting:
    the door would keep answering into an application that has closed its database.
    """
    try:
        await server.serve(sockets=[sock])
    finally:
        for listener in server.servers:
            listener.close()
        sock.close()


__all__ = ["LAN_KEY", "LAN_MINUTES", "SCOPE_KEY", "Lan", "LanDoor", "ServicesDoor", "lan_address", "open_door"]
