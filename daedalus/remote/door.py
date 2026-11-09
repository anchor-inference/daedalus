"""The terminal client's door: every request that carries ``Authorization: Device <token>`` passes here.

A device token is the operator at a terminal, for conversations, and not the operator's whole
installation. The generic authentication accepts it only on a request this door let through and marked
(:data:`SCOPE_KEY`), so the list below is the one place that says what a device reaches:

- its own routes (``/api/cli/*``), the model list, the slash commands and skills, presence;
- starting a ``remote`` conversation (the handler refuses any other kind for a device);
- the session routes the client's screen needs, for a session in the device's scope only
  (:meth:`daedalus.remote.devices.Devices.in_scope`); outside it they answer 404, as a session that
  does not exist, so a stolen token cannot even learn which other conversations there are;
- the global event stream narrowed to one session in scope (``?session=``);
- the relay's WebSocket.

Everything else answers 403. Unlike the side panel's door this one rewrites no path: the client calls
the app's own routes, so it cannot drift from the app, and ``tests/unit/test_remote_door.py`` holds the
matrix. The contract is ``docs/architecture/cli.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from daedalus.remote.devices import Devices

SCOPE_KEY = "daedalus.device"
"""Set on a request's ASGI scope by the door alone, after it checked the token and the route. Nothing a
client sends can set it, which is what lets the operator's handlers trust it."""
SCHEME = "Device"

_SID = r"(?P<sid>[A-Za-z0-9_-]{1,64})"
_SESSION = rf"/api/sessions/{_SID}"
_PART = r"[A-Za-z0-9_.:-]{1,200}"

OPEN = "open"
"""Any device, no session named."""
ANY_SESSION = "any_session"
"""Names a session the device need not have in scope yet: the attach of ``/resume``."""
SESSION = "session"
"""Names a session that must be in the device's scope."""
EVENTS = "events"
"""The global stream, which must be narrowed to one session in scope with ``?session=``."""

ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (method, re.compile(pattern), rule)
    for method, pattern, rule in (
        ("GET", "/api/cli/me", OPEN),
        ("GET", "/api/cli/sessions", OPEN),
        ("POST", "/api/cli/token/rotate", OPEN),
        ("WS", "/api/cli/relay", OPEN),
        ("POST", rf"/api/cli/sessions/{_SID}/attach", ANY_SESSION),
        ("DELETE", rf"/api/cli/sessions/{_SID}/attach", SESSION),
        ("POST", rf"/api/cli/sessions/{_SID}/hands", SESSION),
        ("PUT", rf"/api/cli/sessions/{_SID}/guard", SESSION),
        ("GET", "/api/models", OPEN),
        ("GET", "/api/commands", OPEN),
        ("GET", "/api/skills", OPEN),
        ("GET", "/api/auth/me", OPEN),
        ("POST", "/api/sessions", OPEN),
        ("POST", "/api/presence", OPEN),
        ("GET", "/api/events", EVENTS),
        ("GET", _SESSION, SESSION),
        ("PATCH", _SESSION, SESSION),
        *(("GET", _SESSION + "/" + tail, SESSION) for tail in (
            "stream", "events", "subagents", "tasks", "checkpoints", "steer", "suggestion", "search",
            "export/download", "attachments/download",
            rf"tool-results/{_PART}", rf"tool-results/{_PART}/images/download", rf"sent/{_PART}/download",
            r"policy/similar/[^/]{1,512}", "policy/standing", rf"media/{_PART}/download", rf"media/{_PART}/{_PART}/content",
        )),
        *(("POST", _SESSION + "/" + tail, SESSION) for tail in (
            "messages", "upload", "stop", "answer", "model", "compact", "clear", "command", "revert", "retry",
            "fork", "btw", "policy/grant", "policy/grant-similar", "policy/grant-server", "policy/refuse",
            rf"tasks/{_PART}/stop", rf"steer/{_PART}",
        )),
        ("DELETE", _SESSION + rf"/steer/{_PART}", SESSION),
        ("DELETE", _SESSION + rf"/policy/standing/{_PART}", SESSION),
        ("DELETE", _SESSION + "/btw", SESSION),
    )
)
"""``(method, path, rule)``: what a device token opens. ``WS`` is the WebSocket upgrade."""


def device_token(header: str) -> str:
    scheme, _, value = (header or "").partition(" ")
    return value.strip() if scheme == SCHEME else ""


def route_of(method: str, path: str) -> tuple[re.Match[str], str] | None:
    """The match and rule of the route a device may use, or None."""
    for wanted, pattern, rule in ROUTES:
        if wanted == method:
            found = pattern.fullmatch(path)
            if found is not None:
                return found, rule
    return None


async def _answer(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, detail: str, code: str) -> None:
    body = json.dumps({"detail": detail, "code": code}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _refuse_socket(receive: Any, send: Any, code: int) -> None:
    """Accepted and closed at once with ``code``: a close before the accept reaches the client only as
    an HTTP 403, and the client must tell a dead token (log in again) from anything else."""
    message = await receive()
    if message.get("type") != "websocket.connect":
        return
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": code})


class DeviceDoor:
    """ASGI middleware in front of the app."""

    def __init__(self, asgi: Any, *, devices: Callable[[], Devices | None]) -> None:
        self.asgi = asgi
        self.devices = devices

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.asgi(scope, receive, send)
            return
        # A client cannot set this itself, but a request without the device's credential must not
        # carry one either.
        scope.pop(SCOPE_KEY, None)
        header = next((value.decode("latin-1") for key, value in scope.get("headers") or [] if key == b"authorization"), "")
        if not header.startswith(SCHEME + " "):
            await self.asgi(scope, receive, send)
            return
        token = device_token(header)
        socket = kind == "websocket"
        if socket:
            origin = next((value.decode("latin-1") for key, value in scope.get("headers") or [] if key == b"origin"), "").lower()
            if origin.startswith(("http://", "https://")):
                # A web page is no terminal: refused before anything is read, so a page the operator
                # visits cannot probe the door with the operator's own network.
                await send({"type": "websocket.close", "code": 1008})
                return
        devices = self.devices()
        client = scope.get("client") or ("", 0)
        device = await devices.check(token, address=str(client[0] or "")) if devices is not None and token else None
        if device is None:
            if socket:
                await _refuse_socket(receive, send, 4401)
            else:
                await _answer(send, 401, "this terminal's credential is missing, revoked or lapsed; run `daedalus-cli login` again", "device_token_invalid")
            return
        path = str(scope.get("path") or "")
        method = "WS" if socket else str(scope.get("method") or "")
        found = route_of(method, path)
        if found is None:
            if socket:
                await _refuse_socket(receive, send, 4403)
            else:
                await _answer(send, 403, "a terminal's credential does not open this route", "device_route")
            return
        match, rule = found
        session_id = match.groupdict().get("sid")
        if rule == EVENTS:
            asked = parse_qs((scope.get("query_string") or b"").decode("latin-1")).get("session") or [""]
            session_id = asked[0]
            if not session_id:
                await _answer(send, 403, "a terminal reads the event stream of one of its conversations: name it with ?session=", "device_route")
                return
        if rule in (SESSION, EVENTS) and session_id and not await devices.in_scope(device.id, session_id):  # type: ignore[union-attr]
            await _answer(send, 404, "no such session", "not_found")
            return
        inner = dict(scope)
        inner[SCOPE_KEY] = {"device": device.id, "device_name": device.name}
        await self.asgi(inner, receive, send)


__all__ = ["ROUTES", "SCHEME", "SCOPE_KEY", "DeviceDoor", "device_token", "route_of"]
