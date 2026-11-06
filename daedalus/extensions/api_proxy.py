"""Forwarding a browser's request to a web server an agent runs on this machine.

Two places put such a server on the site's own address: a shared service at ``/s/<slug>/`` and a
service page at ``/pages/<slug>/``. Both forward any method, any path, a body of any size (streamed,
never read into memory) and an answer of any length (streamed as it comes, so server-sent events and
long downloads work), and both tell the server where it is mounted with ``X-Forwarded-Prefix``.
WebSocket connections are forwarded too, so an existing web app with live updates works in either.

A shared service normally reaches this code through its own listener (``api_services_door``). When no
separate origin is available it arrives on the app's address instead, and ``sandbox`` is what keeps
that safe: the answer gets an opaque origin, so the page holds nothing of the app's.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

# ``websockets`` comes with uvicorn's standard extra, which serves this API.
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from daedalus.host.reveal import THROUGH_HEADER, came_through

logger = logging.getLogger(__name__)

HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host"})
BODYLESS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})
ORIGIN_WIDE_HEADERS = frozenset({"clear-site-data", "strict-transport-security", "alt-svc", "service-worker-allowed"})
"""What a response can say about a whole origin rather than about itself. A server mounted under a
prefix of the app's address does not get to speak for that address: ``Clear-Site-Data`` would sign the
operator out, and ``Service-Worker-Allowed: /`` would let a worker under the prefix take every address
of the app, the app itself among them."""
SANDBOX_POLICY = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
"""The opaque origin of a page that has no origin of its own to run on. Sent on every answer, not only
HTML, because an SVG or an XML document runs script when opened directly. ``allow-same-origin`` is the
one thing it must never carry, and ``allow-popups-to-escape-sandbox`` is left out so that a page cannot
open a window that runs on the app's origin."""


OWN_COOKIE_PREFIXES = ("dshare-", "dphone-")
"""Cookies this door sets for itself, under any service's name: a service hears none of them, its own key
included. A list of names let a neighbour's through."""
FOLDED_FORWARDING = frozenset({"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-prefix", "x-real-ip"})
"""With underscores these reach a service that folds them, and WSGI does: ``X_Forwarded_For`` was the same
variable there as the header set here."""


def without_cookies(header: str, names: Iterable[str]) -> str:
    """A ``Cookie`` header with the named cookies, and all of this door's own, taken out."""
    drop = set(names)
    kept = []
    for part in (p.strip() for p in header.split(";")):
        name = part.split("=", 1)[0].strip()
        if part and name not in drop and not name.startswith(OWN_COOKIE_PREFIXES):
            kept.append(part)
    return "; ".join(kept)


def forged_forwarding(name: str) -> bool:
    return "_" in name and name.lower().replace("_", "-") in FOLDED_FORWARDING


def scoped_set_cookie(value: str, prefix: str) -> str:
    """A ``Set-Cookie`` whose path is the server's own root moved under the prefix it is mounted at.

    A server written for its own origin sets ``Path=/``; under the site's address that cookie would
    travel with every request the app makes, and a second app's cookie of the same name would
    overwrite it. A cookie with no path is scoped by the browser to the request's folder, which is
    already under the prefix.

    A ``Domain`` goes too: it is the one attribute that reaches past this host, and a server that
    named the parent domain would be writing a cookie the app's own host receives.
    """
    def move(match: re.Match[str]) -> str:
        path = match.group(2)
        if path.startswith(prefix + "/") or path == prefix:
            return match.group(0)
        return f"{match.group(1)}{prefix}{path if path != '/' else '/'}"

    return re.sub(r"(;\s*[Pp]ath=)([^;]*)", move, re.sub(r";\s*domain=[^;]*", "", value, flags=re.IGNORECASE))


def frameable_policy(value: str) -> str:
    """A ``Content-Security-Policy`` whose ``frame-ancestors`` lets the app frame the page.

    Many web apps refuse to be framed by default; inside Daedalus the app is the only frame, and it
    is the same origin, so the directive becomes ``'self'`` rather than whatever the server chose.
    """
    if "frame-ancestors" not in value.lower():
        return value
    return re.sub(r"frame-ancestors[^;]*", "frame-ancestors 'self'", value, flags=re.IGNORECASE)


async def forward_http(
    request: Request,
    upstream: str,
    prefix: str,
    *,
    strip_cookies: Iterable[str] = (),
    page: bool = False,
    sandbox: bool = False,
    extra_headers: dict[str, str] | None = None,
    read_timeout: float | None = 300.0,
) -> StreamingResponse:
    """Send the request on to ``upstream`` (a full URL) and stream the answer back.

    ``page`` makes the answer fit inside the app's frame: no ``X-Frame-Options`` and a
    ``frame-ancestors`` of ``'self'``. ``sandbox`` puts the answer in an opaque origin, for a server
    that has to run on the app's own address. ``strip_cookies`` are the site's own cookies, which the
    agent's server has no business seeing.

    In every case the server's root cookies are scoped under the prefix, and what it says about the
    whole origin is dropped.
    """
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS and not forged_forwarding(k)}
    drop = list(strip_cookies)
    if drop and "cookie" in headers:
        remaining = without_cookies(headers.pop("cookie"), drop)
        if remaining:
            headers["cookie"] = remaining
    headers.pop("x-daedalus-token", None)
    headers.pop(THROUGH_HEADER, None)
    headers["x-forwarded-prefix"] = prefix
    headers["x-forwarded-host"] = request.headers.get("host", "")
    # A tunnel is always https at the visitor's end, and an app building its own links needs to hear so.
    headers["x-forwarded-proto"] = "https" if came_through(request.headers) else request.url.scheme
    has_body = request.method not in BODYLESS or "content-length" in request.headers or "transfer-encoding" in request.headers
    # Streamed, not read: an upload of a few gigabytes into an app goes through at the network's pace
    # without the host holding it in memory.
    content: AsyncIterator[bytes] | None = request.stream() if has_body else None
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=read_timeout))
    try:
        up = await client.send(client.build_request(request.method, upstream, headers=headers, content=content), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"the service did not answer: {exc}") from exc
    out: list[tuple[str, str]] = [(k, v) for k, v in (extra_headers or {}).items()]
    taken = {k.lower() for k, _ in out}
    if sandbox:
        # Beside the server's own policy, not instead of it: two policies both apply, so a server that
        # sends a sandbox of its own, however generous, cannot loosen this one.
        out.append(("Content-Security-Policy", SANDBOX_POLICY))
    for k, v in up.headers.multi_items():
        lk = k.lower()
        if lk in HOP_HEADERS or lk in taken or lk in ORIGIN_WIDE_HEADERS:
            continue
        if lk == "location" and v.startswith("/") and not v.startswith("//") and not v.startswith(f"{prefix}/"):
            v = f"{prefix}{v}"  # a redirect to the service's root stays under the prefix
        if lk == "set-cookie":
            v = scoped_set_cookie(v, prefix)
        if page:
            if lk == "x-frame-options":
                continue
            if lk == "content-security-policy":
                v = frameable_policy(v)
        out.append((k, v))

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose()
            await client.aclose()

    response = StreamingResponse(body(), status_code=up.status_code)
    # Set-Cookie may repeat, so the headers go on as a list rather than through a dict.
    response.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out]
    return response


async def forward_websocket(
    websocket: WebSocket,
    upstream: str,
    prefix: str,
    *,
    strip_cookies: Iterable[str] = (),
) -> None:
    """Join the browser's WebSocket to one opened to ``upstream`` (a ``ws://`` URL) and relay both ways.

    The connection to the server is opened before the browser's is accepted, so a server that is down
    or refuses the path closes the browser's socket instead of accepting it into silence. The
    subprotocols the browser offered go along, and the one the server chose is the one accepted.
    """
    headers = []
    cookie = websocket.headers.get("cookie", "")
    drop = list(strip_cookies)
    if cookie:
        cookie = without_cookies(cookie, drop) if drop else cookie
        if cookie:
            headers.append(("Cookie", cookie))
    secure = websocket.url.scheme == "wss" or came_through(websocket.headers)
    headers += [("X-Forwarded-Prefix", prefix), ("X-Forwarded-Host", websocket.headers.get("host", "")), ("X-Forwarded-Proto", "https" if secure else "http")]
    offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    try:
        server = await connect(upstream, additional_headers=headers, subprotocols=offered or None, open_timeout=30, max_size=None, ping_interval=None)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — any failure to reach the server is the same answer to the browser
        logger.info("page websocket to %s refused: %s", upstream, exc)
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=server.subprotocol)

    async def to_server() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await server.send(message["bytes"])
            elif message.get("text") is not None:
                await server.send(message["text"])

    async def to_browser() -> None:
        with suppress(ConnectionClosed):
            async for data in server:
                if isinstance(data, bytes):
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_text(data)

    tasks = [asyncio.create_task(to_server()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, WebSocketDisconnect, ConnectionClosed, RuntimeError):
                await task
        await server.close()
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


__all__ = ["HOP_HEADERS", "ORIGIN_WIDE_HEADERS", "OWN_COOKIE_PREFIXES", "SANDBOX_POLICY", "forward_http", "forward_websocket", "frameable_policy", "scoped_set_cookie", "without_cookies"]
