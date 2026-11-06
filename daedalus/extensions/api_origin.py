"""The guard in front of the app: the session cookie counts only when the app itself sent it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, PlainTextResponse

from daedalus import public_address
from daedalus.extensions.services_door import SCOPE_KEY
from daedalus.gateway.origin import cookie_may_act

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from daedalus.config import Settings

CALLER_CREDENTIALS = ("authorization", "x-daedalus-token")
"""A request carrying one of these holds a credential its sender put there. A page cannot add such a
header to a request elsewhere without a preflight, and this app answers no preflight, so these are
never ambient. The side panel depends on it: an extension's request has a foreign origin, and goes
out with whatever cookie its browser holds."""


class CookieOriginGuard:
    """ASGI middleware: ``cookie_may_act`` for every request that holds the session cookie.

    In front of every route rather than of ``/api``, because ``/pages`` signs in by the same cookie.
    A WebSocket passes: each socket that takes the cookie has its own ``Origin`` check.
    """

    def __init__(self, app: ASGIApp, *, cookie: str, settings: Settings) -> None:
        self.app = app
        self.cookie = cookie
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get(SCOPE_KEY):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if self._misdirected(host):
            # A proxy pointed at the API's port with the services name: the app would answer on the
            # origin that exists to keep it away from those pages, and /s/ would redirect to itself.
            answer = PlainTextResponse("this address belongs to the services door: the proxy must forward it to SERVICES_DOOR_PORT", status_code=421)
            await answer(scope, receive, send)
            return
        if self.cookie in cookie_parser(headers.get("cookie", "")) and not any(name in headers for name in CALLER_CREDENTIALS):
            allowed = cookie_may_act(
                method=scope["method"], origin=headers.get("origin"), fetch_site=headers.get("sec-fetch-site"), fetch_mode=headers.get("sec-fetch-mode"),
                fetch_dest=headers.get("sec-fetch-dest"), host_header=host, public_url=public_address.app_base(self.settings),
            )
            if not allowed:
                await JSONResponse({"detail": "this request did not come from the app"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _misdirected(self, host: str) -> bool:
        named = self.settings.services_public_url
        return bool(named and host) and public_address.services_base(self.settings).partition("://")[2].lower() == host.lower()


__all__ = ["CookieOriginGuard"]
