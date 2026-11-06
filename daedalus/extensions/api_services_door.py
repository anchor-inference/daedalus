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
import logging
import socket
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


class ServicesDoor:
    """The whole application of the door's listener: ``/s/`` reaches the API's routes, nothing else does."""

    def __init__(self, api: ASGIApp) -> None:
        self.api = api

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/s/"):
            scope[SCOPE_KEY] = True
            await self.api(scope, receive, send)
        elif path == "/robots.txt":
            # The one address a crawler asks: nothing on this host is for an index.
            await PlainTextResponse("User-agent: *\nDisallow: /\n")(scope, receive, send)
        else:
            await PlainTextResponse("nothing here", status_code=404)(scope, receive, send)


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


__all__ = ["SCOPE_KEY", "ServicesDoor", "open_door"]
