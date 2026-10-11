"""Forwarding a browser's request to a web server an agent runs on this machine.

Two places put such a server on the site's own address: a shared service at ``/s/<slug>/`` and a
service page at ``/pages/<slug>/``. Both forward any method, any path, a body of any size (streamed,
never read into memory) and an answer of any length (streamed as it comes, so server-sent events and
long downloads work), and both tell the server where it is mounted with ``X-Forwarded-Prefix``.
A page also forwards WebSocket connections, so an existing web app with live updates works inside it.
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

logger = logging.getLogger(__name__)

HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host"})
BODYLESS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


def without_cookies(header: str, names: Iterable[str]) -> str:
    """A ``Cookie`` header with the named cookies taken out."""
    drop = set(names)
    kept = [part for part in (p.strip() for p in header.split(";")) if part and part.split("=", 1)[0].strip() not in drop]
    return "; ".join(kept)


def scoped_set_cookie(value: str, prefix: str) -> str:
    """A ``Set-Cookie`` whose path is the server's own root moved under the prefix it is mounted at.

    A server written for its own origin sets ``Path=/``; under the site's address that cookie would
    travel with every request the app makes, and a second app's cookie of the same name would
    overwrite it. A cookie with no path is scoped by the browser to the request's folder, which is
    already under the prefix.
    """
    def move(match: re.Match[str]) -> str:
        path = match.group(2)
        if path.startswith(prefix + "/") or path == prefix:
            return match.group(0)
        return f"{match.group(1)}{prefix}{path if path != '/' else '/'}"

    return re.sub(r"(;\s*[Pp]ath=)([^;]*)", move, value)


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
    extra_headers: dict[str, str] | None = None,
    read_timeout: float | None = 300.0,
) -> StreamingResponse:
    """Send the request on to ``upstream`` (a full URL) and stream the answer back.

    ``page`` makes the answer fit inside the app's frame: no ``X-Frame-Options``, a ``frame-ancestors``
    of ``'self'``, and the server's root cookies scoped under the prefix. ``strip_cookies`` are the
    site's own cookies, which the agent's server has no business seeing.
    """
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    drop = list(strip_cookies)
    if drop and "cookie" in headers:
        remaining = without_cookies(headers.pop("cookie"), drop)
        if remaining:
            headers["cookie"] = remaining
    headers.pop("x-daedalus-token", None)
    headers["x-forwarded-prefix"] = prefix
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-proto"] = request.url.scheme
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
    for k, v in up.headers.multi_items():
        lk = k.lower()
        if lk in HOP_HEADERS or lk in taken:
            continue
        if lk == "location" and v.startswith("/") and not v.startswith("//") and not v.startswith(f"{prefix}/"):
            v = f"{prefix}{v}"  # a redirect to the service's root stays under the prefix
        if page:
            if lk == "x-frame-options":
                continue
            if lk == "content-security-policy":
                v = frameable_policy(v)
            if lk == "set-cookie":
                v = scoped_set_cookie(v, prefix)
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
    # ``websockets`` comes with uvicorn's standard extra, which serves this API; it is imported here
    # so the module loads in a test that never opens a socket.
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    headers = []
    cookie = websocket.headers.get("cookie", "")
    drop = list(strip_cookies)
    if cookie:
        cookie = without_cookies(cookie, drop) if drop else cookie
        if cookie:
            headers.append(("Cookie", cookie))
    headers += [("X-Forwarded-Prefix", prefix), ("X-Forwarded-Host", websocket.headers.get("host", "")), ("X-Forwarded-Proto", "https" if websocket.url.scheme == "wss" else "http")]
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


__all__ = ["HOP_HEADERS", "forward_http", "forward_websocket", "frameable_policy", "scoped_set_cookie", "without_cookies"]
