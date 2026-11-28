"""The live link's door: ``/v/<token>/…`` on the services door, and nothing a link opens anywhere else.

A link (:mod:`daedalus.extensions.session_share`) is the operator's own power over one session handed to
whoever holds it. This door decides what "one session" means, the way the side panel's door and the
terminal client's do for theirs: the routes a visitor reaches are listed here and nowhere else, and the
handlers behind them are the operator's own, reached by taking the prefix off the path after the checks.
The visitor's app therefore cannot drift from the operator's, and ``tests/unit/test_session_share.py``
holds the matrix that says what a token reaches.

What a token opens, for its session only:

- the session's own routes: the conversation and its stream, sending (with files), stopping, answering,
  the approvals, the model and the effort, the files of its workspace, its notes, jobs, helpers and
  side questions;
- the session's terminals and browsers, each checked to be the session's own, and their sockets;
- the app's event stream, narrowed by this door to the session's own events;
- the model list, without any provider's address or key, and the slash commands.

A route naming another session, a terminal or browser of anyone else, or anything outside the list
answers 404, as a thing that does not exist; the settings, secrets, providers and the session list
are simply not on it. Neither is anything that turns the link into more than the session: sharing it
or anything in it again, making agent links, deleting it, moving it to a project, forking it into a
new session, the switches that lift the operator's checks.

The token travels in the path, which is what lets a stream, a socket, a picture and a download carry it
without a header a browser cannot set. A path is also what a Referer leaks, so the page tells the
browser to send none. The cookie and any credential header a visitor's browser sends are dropped: the
handlers must see the link, not whatever else the browser holds.

A link with a password opens its page and the page's files to anyone, and nothing else until the visitor
typed it: every route and socket answers ``401 locked`` (sockets close with 4401) until the browser holds the
link's pass, a cookie this door sets on ``POST /api/share/unlock``, scoped to the link's own path, and reads
itself before it drops the cookies like any other. What the link's policy narrows is narrowed here too: the
terminals it shows (``all``, the container's alone, or ``none``, answered as if the session had none), and a
spent budget, which refuses what would start a turn with ``402 budget`` before the handler runs. The model
picks a policy allows are judged by the handlers that take them, which read the policy of the link named on
the request (``share_policy``).

Only the services door serves this. On the app's own port ``/v/`` answers 404: the app's origin holds the
operator's cookie, and a visitor's page there would be the one thing the separate origin is for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, unquote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import FileResponse

from daedalus.extensions import api_services_door, share_policy
from daedalus.extensions.api_panel import picker_settings
from daedalus.extensions.session_share import (
    EXTENSION_KEY,
    TOKEN_RE,
    VISITOR_RE,
    SessionShares,
    clean_name,
    live,
    pass_for,
    pass_valid,
    seconds_left,
)
from daedalus.host import share_roles, share_spend, share_tools

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

SCOPE_KEY = "daedalus.session_share"
"""Set on a request's ASGI scope by this door alone, after it checked the token and the route. Nothing a
client sends can set it, which is what lets the operator's handlers trust it."""
PREFIX = "/v/"
VISITOR_HEADER = b"x-share-visitor"
NAME_HEADER = b"x-share-name"
DROPPED = frozenset({b"cookie", b"authorization", b"x-daedalus-token"})
UPLOAD_MAX = 256 << 20
"""The most one request may send: a message's files, a voice note. A link is a credential in a stranger's
hands, and a leaked one must not be a way to fill this host's disk; anything larger goes in the app."""
PASS_COOKIE = "share_pass"
UNLOCK = "/api/share/unlock"
UNLOCK_BODY_MAX = 4096
STATIC = re.compile(r"/v/(?P<dir>assets|icons)/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,200})")

_SID = r"(?P<sid>[A-Za-z0-9_-]{1,64})"
_S = rf"/api/sessions/{_SID}"
_PART = r"[A-Za-z0-9_.:-]{1,200}"
_ID = r"[A-Za-z0-9_-]{1,80}"

SESSION = "session"
"""Names the link's session by ``sid``."""
TERMINAL = "terminal"
"""Names one of the session's terminals by ``tid``."""
BROWSER = "browser"
"""Names one of the session's browsers by ``gid``."""
NOTE = "note"
"""Names a note (``nid``) or a note's file (``fid``) of the session, or of its project's notes."""
NARROW = "narrow"
"""A listing of the whole app: this door replaces its query with one that names the session."""
OWN = "own"
"""The link's own routes under ``/api/share/``."""
OPEN = "open"
"""A route of the operator's that names nobody's data: passed on as it is."""

ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (method, re.compile(pattern), rule)
    for method, pattern, rule in (
        # The conversation: read, streamed, searched, written to, stopped, answered.
        ("GET", _S, SESSION),
        ("GET", _S + "/stream", SESSION),
        ("GET", _S + "/search", SESSION),
        ("GET", _S + "/suggestion", SESSION),
        ("GET", _S + "/steer", SESSION),
        ("GET", _S + "/tasks", SESSION),
        ("GET", _S + f"/tasks/{_PART}/log", SESSION),
        ("GET", _S + "/subagents", SESSION),
        ("GET", _S + "/questions", SESSION),
        ("GET", _S + "/verifications", SESSION),
        ("GET", _S + "/checkpoints", SESSION),
        ("GET", _S + "/tool-groups", SESSION),
        ("GET", _S + "/tools/timing", SESSION),
        ("GET", _S + "/runtime-state", SESSION),
        ("GET", _S + "/browser-approvals", SESSION),
        ("GET", _S + "/services", SESSION),
        # The conversation's own preview servers: read their output, stop them, and give one an address
        # others can open, which is what a person shown a thing being built asks for next.
        ("GET", _S + f"/services/{_PART}/logs", SESSION),
        ("POST", _S + f"/services/{_PART}/stop", SESSION),
        ("POST", _S + f"/services/{_PART}/share", SESSION),
        ("GET", _S + "/agent-talks", SESSION),
        ("GET", _S + "/subagent-models", SESSION),
        ("GET", _S + "/mcp", SESSION),
        ("GET", _S + "/policy/standing", SESSION),
        ("GET", _S + f"/tool-results/{_PART}", SESSION),
        ("GET", _S + f"/tool-results/{_PART}/images/download", SESSION),
        ("GET", _S + r"/policy/similar/[^/]{1,512}", SESSION),
        ("GET", _S + r"/media/[0-9a-f-]{36}/[0-9a-f-]{36}/content", SESSION),
        ("GET", _S + r"/media/[0-9a-f-]{36}/download", SESSION),
        ("GET", _S + "/attachments", SESSION),
        ("GET", _S + "/attachments/download", SESSION),
        ("GET", _S + f"/sent/{_PART}/download", SESSION),
        ("GET", _S + f"/widgets/{_ID}/storage", SESSION),
        # The workspace's files, as the Files tab and Preview read them. The agent this link drives can
        # read them anyway; the tab is the same reach with a better view.
        ("GET", _S + "/files", SESSION),
        ("GET", _S + "/files/grep", SESSION),
        ("GET", _S + "/files/search", SESSION),
        ("GET", _S + "/download", SESSION),
        ("GET", _S + f"/folders/{_ID}/files", SESSION),
        ("GET", _S + f"/folders/{_ID}/files/grep", SESSION),
        ("GET", _S + f"/folders/{_ID}/files/search", SESSION),
        ("GET", _S + f"/folders/{_ID}/download", SESSION),
        ("POST", _S + "/messages", SESSION),
        ("POST", _S + "/upload", SESSION),
        ("POST", _S + "/files/upload", SESSION),
        ("POST", _S + f"/folders/{_ID}/files/upload", SESSION),
        ("POST", _S + "/stop", SESSION),
        ("POST", _S + "/answer", SESSION),
        ("POST", _S + "/retry", SESSION),
        ("POST", _S + "/revert", SESSION),
        ("POST", _S + "/compact", SESSION),
        ("POST", _S + "/asks/answer", SESSION),
        ("POST", _S + f"/asks/{_PART}/dismiss", SESSION),
        ("POST", _S + f"/steer/{_PART}", SESSION),
        ("DELETE", _S + f"/steer/{_PART}", SESSION),
        ("POST", _S + f"/tasks/{_PART}/stop", SESSION),
        ("POST", _S + f"/tool-groups/{_ID}/load", SESSION),
        ("PUT", _S + f"/tool-groups/{_ID}", SESSION),
        ("POST", _S + "/model", SESSION),
        ("POST", _S + "/policy/grant", SESSION),
        ("POST", _S + "/policy/grant-similar", SESSION),
        ("POST", _S + "/policy/grant-server", SESSION),
        ("POST", _S + "/policy/refuse", SESSION),
        ("PUT", _S + "/browser-approvals", SESSION),
        ("POST", _S + "/transcribe", SESSION),
        # A note or an answer in the operator's language on request: the visitor already runs the agent on
        # the operator's account, and this one writes only the display copy of a row of this session.
        ("POST", _S + r"/messages/[0-9]{1,12}/translate", SESSION),
        ("DELETE", _S + f"/transcribe/{_ID}", SESSION),
        ("POST", _S + f"/widgets/{_ID}/page", SESSION),
        ("PUT", _S + f"/widgets/{_ID}/storage/.{{1,400}}", SESSION),
        ("DELETE", _S + f"/widgets/{_ID}/storage/.{{1,400}}", SESSION),
        # The Notes tab: the session's notes, and its project's, which the tab shows beside them.
        ("GET", _S + "/notes", SESSION),
        ("POST", _S + "/notes", SESSION),
        ("POST", _S + "/notes/files", SESSION),
        ("POST", _S + "/notes/order", SESSION),
        ("GET", rf"/api/notes/(?P<nid>{_ID})", NOTE),
        ("PATCH", rf"/api/notes/(?P<nid>{_ID})", NOTE),
        ("DELETE", rf"/api/notes/(?P<nid>{_ID})", NOTE),
        ("POST", rf"/api/notes/(?P<nid>{_ID})/restore", NOTE),
        ("POST", rf"/api/notes/(?P<nid>{_ID})/duplicate", NOTE),
        ("GET", rf"/api/notes/files/(?P<fid>{_ID})/download", NOTE),
        ("GET", rf"/api/notes/files/(?P<fid>{_ID})/references", NOTE),
        ("DELETE", rf"/api/notes/files/(?P<fid>{_ID})", NOTE),
        # The session's terminals, and its browsers' live view, as the dock and the Browser tab use them.
        ("GET", "/api/terminals", NARROW),
        ("GET", rf"/api/terminals/(?P<tid>{_ID})", TERMINAL),
        ("POST", rf"/api/terminals/(?P<tid>{_ID})/ticket", TERMINAL),
        ("POST", rf"/api/terminals/(?P<tid>{_ID})/kill", TERMINAL),
        ("GET", rf"/api/terminals/(?P<tid>{_ID})/commands", TERMINAL),
        ("GET", rf"/api/terminals/(?P<tid>{_ID})/files", TERMINAL),
        ("GET", rf"/api/terminals/(?P<tid>{_ID})/files/diff", TERMINAL),
        ("WS", rf"/ws/terminals/(?P<tid>{_ID})", TERMINAL),
        ("GET", "/api/browsers", NARROW),
        ("GET", rf"/api/browsers/(?P<gid>{_PART})", BROWSER),
        ("POST", rf"/api/browsers/(?P<gid>{_PART})/ticket", BROWSER),
        ("POST", rf"/api/browsers/(?P<gid>{_PART})/control", BROWSER),
        ("POST", rf"/api/browsers/(?P<gid>{_PART})/viewport", BROWSER),
        ("POST", rf"/api/browsers/(?P<gid>{_PART})/dialog", BROWSER),
        ("GET", rf"/api/browsers/(?P<gid>{_PART})/actions", BROWSER),
        ("GET", rf"/api/browsers/(?P<gid>{_PART})/recording", BROWSER),
        ("GET", rf"/api/browsers/(?P<gid>{_PART})/frames/[0-9]{{1,9}}", BROWSER),
        ("WS", rf"/ws/browsers/(?P<gid>{_PART})", BROWSER),
        # The app's whole-app reads, each narrowed by this door to the session.
        ("GET", "/api/events", NARROW),
        ("GET", "/api/canvases", NARROW),
        ("GET", "/api/questions", NARROW),
        # What the composer and the model picker read: no address, no key, nothing of anyone's.
        # The slash commands are not: most of them act on the whole installation (/delete, /rebuild,
        # /settings), so a visitor's composer offers none and the route that runs them is not on this list.
        ("GET", "/api/commands", OWN),
        ("GET", "/api/skills", OPEN),
        ("GET", "/api/asr", OPEN),
        ("GET", "/api/settings", OWN),
        ("GET", "/api/auth/me", OWN),
        ("GET", "/api/share", OWN),
    )
)

NARROWED: dict[str, Callable[[str], dict[str, str]]] = {
    "/api/terminals": lambda sid: {"owner_kind": "session", "owner_id": sid},
    "/api/browsers": lambda sid: {"session": sid},
    "/api/canvases": lambda sid: {"session_id": sid},
    "/api/questions": lambda sid: {"session": sid},
    "/api/events": lambda sid: {"session": sid},
}
"""The query this door gives a whole-app listing: the session alone. What the visitor asked is kept only
where it cannot widen the answer (``after``, ``types``, ``client``, ``kind``, ``last``, ``output``)."""
OWN_PATHS = {"/api/share": "/api/share/link", "/api/settings": "/api/share/settings", "/api/auth/me": "/api/share/me", "/api/commands": "/api/share/commands"}
SPENDING = re.compile(_S + r"/(messages|retry|answer|asks/answer|steer/" + _PART + ")")
"""The routes that start or feed a turn of the agent: refused once the link's budget is spent, since a
turn is what spends it. Stopping, reading and approving are not, so a visitor can still end what runs."""
NO_TERMINALS = {"envs": [], "terminals": [], "capacity": {"running": 0, "cap": 0, "queued": 0}}
KEPT_QUERY = frozenset({"after", "types", "client", "kind", "limit", "tail", "before"})


def route_of(method: str, path: str) -> tuple[re.Match[str], str] | None:
    for want, pattern, rule in ROUTES:
        if want == method or (method == "HEAD" and want == "GET"):
            found = pattern.fullmatch(path)
            if found:
                return found, rule
    return None


def split(path: str) -> tuple[str, str] | None:
    """``/v/<token>/<rest>`` as ``(token, /rest)``; None when the path names no token."""
    if not path.startswith(PREFIX):
        return None
    token, _, rest = path[len(PREFIX):].partition("/")
    if not TOKEN_RE.fullmatch(token):
        return None
    return token, "/" + rest


def header(scope: dict[str, Any], name: bytes) -> str:
    return next((value.decode("latin-1") for key, value in scope.get("headers") or [] if key == name), "")


async def answer(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, detail: str, code: str = "") -> None:
    body = json.dumps({"detail": detail, **({"code": code} if code else {})}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")],
    })
    await send({"type": "http.response.body", "body": body})


async def answer_json(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, payload: Any, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"), *(extra or [])],
    })
    await send({"type": "http.response.body", "body": body})


def cookies(scope: dict[str, Any], name: str) -> list[str]:
    """Every value the request carries for one cookie. All of them, not the first: cookies ignore the
    port, so a preview server on the same host can set one of the same name, and the browser would send
    it beside the link's own; a planted one is then only one more value that fails the check."""
    found = []
    for key, value in scope.get("headers") or []:
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            label, _, content = part.strip().partition("=")
            if label == name:
                found.append(content)
    return found[:8]


async def refuse_socket(receive: Any, send: Any, code: int = 4404) -> None:
    message = await receive()
    if message.get("type") != "websocket.connect":
        return
    await send({"type": "websocket.close", "code": code})


def capped(receive: Any, limit: int) -> Callable[[], Awaitable[dict[str, Any]]]:
    """``receive`` that ends the request once more than ``limit`` bytes came, whatever length was declared."""
    seen = 0

    async def wrapped() -> dict[str, Any]:
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body") or b"")
            if seen > limit:
                return {"type": "http.disconnect"}
        return cast("dict[str, Any]", message)

    return wrapped


SHELL_HEAD = (
    '<meta name="referrer" content="no-referrer" />\n'
    '    <meta name="robots" content="noindex, nofollow" />\n'
)


def shell(index: str) -> str:
    """The app's page as a link serves it: its files under ``/v/``, and without what only the operator's
    app uses. Telegram's script is another origin's code on a page that holds a credential in its address,
    and the manifest would offer to install the link as if it were the app."""
    page = index.replace('"/app/assets/', '"/v/assets/').replace('"/app/icons/', '"/v/icons/')
    page = re.sub(r'\s*<link rel="manifest"[^>]*>', "", page)
    page = re.sub(r'\s*<link rel="apple-touch-icon"[^>]*>', "", page)
    page = re.sub(r"\s*<script async src=\"https://telegram\.org/[^>]*></script>", "", page)
    return page.replace("<head>", "<head>\n    " + SHELL_HEAD.rstrip(), 1)


class ShareDoor:
    """ASGI middleware in front of the app: every ``/v/`` request passes here first, and stops here
    unless it is one of :data:`ROUTES` for the link's own session."""

    def __init__(self, asgi: Any, *, shares: Callable[[], SessionShares | None], owner_of: Callable[[str, str], Awaitable[str | None]], dist: Path,
                 app: Any = None) -> None:
        self.asgi = asgi
        self.app = app
        self.shares = shares
        self.owner_of = owner_of
        self.dist = dist

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        path = str(scope.get("path") or "")
        if kind not in ("http", "websocket") or not path.startswith(PREFIX):
            # A client cannot set this itself, but a request elsewhere must not carry one either.
            scope.pop(SCOPE_KEY, None)
            await self.asgi(scope, receive, send)
            return
        socket = kind == "websocket"
        if not scope.get(api_services_door.SCOPE_KEY):
            # The app's own port: the operator's origin is no place for a visitor's page.
            await (refuse_socket(receive, send) if socket else answer(send, 404, "a live link opens at the services door, not at the app's address"))
            return
        if not socket and (found := STATIC.fullmatch(path)):
            await self.static(scope, receive, send, found["dir"], found["name"])
            return
        parts = split(path)
        shares = self.shares()
        if parts is None or shares is None:
            await (refuse_socket(receive, send) if socket else answer(send, 404, "no such link"))
            return
        token, rest = parts
        row = await shares.by_token(token)
        if row is None or not live(row):
            # A link that was handed out and has stopped says so: the visitor's page tells them it ended,
            # rather than leave them retrying a door that will not open again.
            status, detail, code = (404, "no such link", "unknown") if row is None else (410, "this link was revoked or has expired", "ended")
            await (refuse_socket(receive, send, 4410 if row else 4404) if socket else answer(send, status, detail, code))
            return
        if not rest.startswith(("/api/", "/ws/")):
            if socket or str(scope.get("method") or "") not in ("GET", "HEAD"):
                await (refuse_socket(receive, send) if socket else answer(send, 405, "the link's page is read with GET"))
                return
            await self.page(scope, receive, send)
            return
        method = "WS" if socket else str(scope.get("method") or "")
        if row.get("password_hash"):
            if not socket and method == "POST" and rest == UNLOCK:
                await self.unlock(shares, row, token, scope, receive, send)
                return
            if not any(pass_valid(row, value) for value in cookies(scope, PASS_COOKIE)):
                await (refuse_socket(receive, send, 4401) if socket else answer(send, 401, "this link asks for its password", "locked"))
                return
        elif not socket and method == "POST" and rest == UNLOCK:
            await answer(send, 200, "this link has no password", "open")
            return
        matched = route_of(method, rest)
        if matched is None:
            logger.info("live link refused %s %s", method, rest)
            await (refuse_socket(receive, send) if socket else answer(send, 404, "not a route this link opens"))
            return
        match, rule = matched
        sid = str(row["session_id"])
        policy = share_policy.parse(row.get("policy"))
        terminals = share_policy.terminals(policy)
        if not await self.allowed(rule, match, sid, terminals):
            await (refuse_socket(receive, send) if socket else answer(send, 404, "not in this conversation"))
            return
        if rule == NARROW and rest == "/api/terminals" and terminals == "none":
            await answer_json(send, 200, NO_TERMINALS)
            return
        if not socket and method == "POST" and SPENDING.fullmatch(rest) and (spent := await share_spend.exhausted(cast("Application", self.app), str(row["id"]))):
            await answer(send, 402, f"this link's limit is spent ({spent})", "budget")
            return
        visitor = header(scope, VISITOR_HEADER)
        visitor = visitor if VISITOR_RE.fullmatch(visitor) else ""
        name = clean_name(unquote(header(scope, NAME_HEADER))[:400])
        await shares.touch(str(row["id"]), visitor, name)
        inner = dict(scope)
        inner["headers"] = [(key, value) for key, value in scope.get("headers") or [] if key not in DROPPED]
        inner[SCOPE_KEY] = {"share": str(row["id"]), "session_id": sid, "visitor": visitor, "name": name}
        target = rest
        if rule == OWN:
            target = OWN_PATHS[rest]
        inner["path"] = target
        inner["raw_path"] = target.encode()
        if rule == NARROW:
            asked = [(k, v) for k, v in parse_qsl(scope.get("query_string", b"").decode("latin-1")) if k in KEPT_QUERY]
            narrowed = NARROWED[rest](sid)
            if rest == "/api/terminals" and terminals == "container":
                narrowed = {**narrowed, "env": "container"}
            inner["query_string"] = urlencode([*asked, *narrowed.items()]).encode()
        if not socket and str(scope.get("method") or "") in ("POST", "PUT", "PATCH"):
            declared = header(scope, b"content-length")
            if declared.isdigit() and int(declared) > UPLOAD_MAX:
                await answer(send, 413, f"a link sends up to {UPLOAD_MAX >> 20} MB at a time")
                return
            receive = capped(receive, UPLOAD_MAX)
        await self.held(shares, row, inner, receive, send)

    async def allowed(self, rule: str, match: re.Match[str], sid: str, terminals: str = "all") -> bool:
        groups = match.groupdict()
        if rule == SESSION:
            return groups.get("sid") == sid
        if rule == TERMINAL:
            # ``container`` keeps the host's terminals out even when the session owns one: the operator's
            # own machine is what that setting withholds.
            if terminals == "none":
                return False
            if terminals == "container" and await self.owner_of("terminal_env", groups["tid"]) != "container":
                return False
            return await self.owner_of("terminal", groups["tid"]) == sid
        if rule == BROWSER:
            return await self.owner_of("browser", groups["gid"]) == sid
        if rule == NOTE:
            # A chat's note is its own; a project's note is shown in every session of that project.
            owner = await self.owner_of("note" if groups.get("nid") else "note_file", groups.get("nid") or groups["fid"])
            return owner is not None and (owner == sid or owner == await self.owner_of("project", sid))
        return rule in (NARROW, OWN, OPEN)

    async def held(self, shares: SessionShares, row: dict[str, Any], scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Run the request until it ends, or until the link is revoked or expires, whichever comes first.

        On a cut the handler is cancelled rather than told the client left: a stream that only notices a
        disconnect at its next keep-alive would have gone on showing a revoked visitor the agent's work for
        that long. A response already under way is ended where it stands, which is what makes the visitor's
        page ask again and hear 410; one not started yet gets the 410 itself."""
        share_id = str(row["id"])
        cut = shares.hold(share_id)
        state = {"started": False, "ended": False, "accepted": False, "closed": False}
        socket = scope.get("type") == "websocket"

        async def tracked(message: dict[str, Any]) -> None:
            kind = message.get("type")
            if kind == "http.response.start":
                state["started"] = True
            elif kind == "http.response.body" and not message.get("more_body"):
                state["ended"] = True
            elif kind == "websocket.accept":
                state["accepted"] = True
            elif kind == "websocket.close":
                state["closed"] = True
            await send(message)

        work = asyncio.ensure_future(self.asgi(scope, receive, tracked))
        stop = asyncio.ensure_future(cut.wait())
        left = seconds_left(row)
        expiry = asyncio.ensure_future(asyncio.sleep(left)) if left is not None else None
        waiting = {work, stop, *((expiry,) if expiry else ())}
        try:
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                work.result()
                return
            work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work
            # Cut for a change rather than an end (a new password, a new policy): a socket is told which,
            # and a request not answered yet hears what the next one would, so the page asks for the
            # password instead of saying the link ended.
            now = await shares.by_id(share_id)
            ended = now is None or not live(now)
            locked = not ended and bool(now and now.get("password_hash"))
            with suppress(Exception):
                if socket:
                    if not state["closed"]:
                        await send({"type": "websocket.close", "code": 4410 if ended else 4401 if locked else 1012})
                elif not state["started"]:
                    if ended:
                        await answer(send, 410, "this link was revoked or has expired", "ended")
                    elif locked:
                        await answer(send, 401, "this link asks for its password", "locked")
                    else:
                        await answer(send, 503, "the link's settings changed; ask again", "changed")
                elif not state["ended"]:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            for pending in waiting:
                if not pending.done():
                    pending.cancel()
            shares.release(share_id, cut)

    async def unlock(self, shares: SessionShares, row: dict[str, Any], token: str, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """``{"password"}`` → the pass, as a cookie only this link's path is sent, or 403 ``wrong`` or 429
        ``slow``. Read here rather than by a handler, because the inner app never sees a visitor's cookies."""
        body = b""
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                return
            body += message.get("body") or b""
            if len(body) > UNLOCK_BODY_MAX:
                await answer(send, 413, "a password is shorter than that")
                return
            if not message.get("more_body"):
                break
        try:
            password = json.loads(body or b"{}").get("password")
        except (ValueError, AttributeError):
            password = None
        if not isinstance(password, str):
            await answer(send, 400, "password: a string")
            return
        verdict, wait = await shares.unlock(str(row["id"]), password)
        if verdict == "slow":
            await answer_json(send, 429, {"detail": "too many wrong passwords; wait", "code": "slow", "retry_after": max(1, round(wait + 0.5))})
            return
        if verdict != "ok":
            await answer(send, 403, "that is not the link's password", "wrong")
            return
        fresh = await shares.by_id(str(row["id"]))
        if fresh is None or not live(fresh) or not fresh.get("password_hash"):
            await answer(send, 410, "this link was revoked or has expired", "ended")
            return
        secure = "; Secure" if shares.base.startswith("https:") or scope.get("scheme") in ("https", "wss") else ""
        # No Max-Age: the pass lasts as long as the browser's session, and the link's own end ends it anyway.
        cookie_value = f"{PASS_COOKIE}={pass_for(fresh)}; Path=/v/{token}/; HttpOnly; SameSite=Strict{secure}"
        await answer_json(send, 200, {"ok": True}, [(b"set-cookie", cookie_value.encode())])

    async def page(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        index = self.dist / "index.html"
        if not index.is_file():
            await answer(send, 503, "the app is not built")
            return
        body = shell(index.read_text(encoding="utf-8")).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"referrer-policy", b"no-referrer"),
                (b"x-robots-tag", b"noindex, nofollow"),
                (b"x-content-type-options", b"nosniff"),
                (b"content-security-policy", b"frame-ancestors 'none'"),
            ],
        })
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})

    async def static(self, scope: dict[str, Any], receive: Any, send: Any, folder: str, name: str) -> None:
        """The app's built files, the same for anyone: the page's scripts, styles, fonts and icons."""
        root = (self.dist / folder).resolve()
        found = (root / name).resolve()
        if found.parent != root or not found.is_file():
            await answer(send, 404, "no such file")
            return
        headers = {"cache-control": "public, max-age=31536000, immutable" if folder == "assets" else "public, max-age=3600", "x-content-type-options": "nosniff"}
        await FileResponse(found, headers=headers)(scope, receive, send)


class ShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_hours: float | None = Field(default=None, gt=0, le=24 * 90)
    """Hours until the link stops by itself; none for a link that lasts until it is revoked."""
    label: str = Field(default="", max_length=400)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    policy: dict[str, Any] | None = None


class SharePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=400)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    clear_password: bool = False
    policy: dict[str, Any] | None = None
    expires_hours: float | None = Field(default=None, gt=0, le=24 * 90)
    """Hours from now; the link's end is moved, never removed, since a link made to end should not
    quietly become one that does not."""




async def pick_refused(app: Application, who: dict[str, Any], body: dict[str, Any]) -> str:
    """Why a visitor may not make this pick of the conversation's model (``POST …/model``); "" when they
    may. The operator's own picks never come here.

    A field the body leaves out keeps the session's value, so it is not judged: changing the preset alone
    asks only that the preset be one the link offers. A raw provider and model, or ``clear``, would step
    around the presets altogether and are refused whenever the role is limited."""
    row = await app.db.fetchone("SELECT policy FROM session_shares WHERE id = ?", (str(who.get("share") or ""),))
    policy = share_policy.parse(row["policy"] if row is not None else None)
    entry = share_policy.role(policy, "main")
    if entry is None and not policy.get("broken"):
        return ""
    if body.get("clear") or body.get("provider") or body.get("model"):
        return "this link lets its visitor choose only among the presets it offers"
    current = next((role.get("current") or {} for role in await share_roles.describe(app, str(who["session_id"])) if role.get("id") == "main"), {})
    preset = str(body.get("preset") or current.get("preset") or (entry or {}).get("preset") or "")
    effort = "none" if body.get("thinking") is False else body.get("reasoning_effort")
    account = body.get("account")
    if (body.get("preset") or effort is not None or account is not None) and not share_policy.allows(
        policy, "main", preset, None if effort is None else str(effort), None if account is None else str(account)
    ):
        return "this link does not offer that model, effort or account"
    return ""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    """Registered last, so its middleware stands outside the operator's own guards."""
    manager = app.manager
    assert manager is not None

    def shares() -> SessionShares | None:
        return cast("SessionShares | None", app.extensions.get(EXTENSION_KEY))

    async def owner_of(kind: str, thing: str) -> str | None:
        """The session a terminal, a browser group or a note belongs to; None when it is nobody's here."""
        if kind == "terminal":
            row = await app.db.fetchone("SELECT owner_id FROM terminals WHERE id = ? AND owner_kind = 'session'", (thing,))
            return str(row["owner_id"]) if row is not None and row["owner_id"] else None
        if kind == "terminal_env":
            row = await app.db.fetchone("SELECT env FROM terminals WHERE id = ?", (thing,))
            return str(row["env"]) if row is not None else None
        if kind == "browser":
            row = await app.db.fetchone("SELECT session_id FROM browser_groups WHERE id = ?", (thing,))
            return str(row["session_id"]) if row is not None and row["session_id"] else None
        if kind == "project":
            row = await app.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (thing,))
            return f"project:{row['project_id']}" if row is not None and row["project_id"] else None
        table = "notes" if kind == "note" else "note_files"
        row = await app.db.fetchone(f"SELECT session_id, project_id FROM {table} WHERE id = ?", (thing,))  # noqa: S608 — one of two names
        if row is None:
            return None
        if row["session_id"]:
            return str(row["session_id"])
        # Several sessions share a project, so a project's note has no one session: the door compares it
        # with the project of the session that asks.
        return f"project:{row['project_id']}"

    dist = app.settings.bot_repo_dir / "miniapp" / "dist"

    api.add_middleware(ShareDoor, shares=shares, owner_of=owner_of, dist=dist, app=app)

    async def place_of(session_id: str) -> str:
        """``container`` for a session whose commands run in this installation's container; ``host`` for
        one on the operator's machine, a native installation, or a remote terminal's, none of which has a
        container of the session's own to narrow the terminals to."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return "container" if manager.env_of(session_id, state.metadata, state.project) == "container" else "host"

    async def policy_of(session_id: str, raw: dict[str, Any] | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        place = await place_of(session_id)
        try:
            return share_policy.validate(raw, await share_roles.describe(app, session_id), place)
        except share_policy.PolicyError as exc:
            raise HTTPException(400, str(exc)) from exc

    async def link_policy(who: dict[str, Any]) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT policy FROM session_shares WHERE id = ?", (who["share"],))
        return share_policy.parse(row["policy"] if row is not None else None)

    def visitor(request: Request) -> dict[str, Any]:
        found = request.scope.get(SCOPE_KEY)
        if not isinstance(found, dict):
            raise HTTPException(404, "not a live link's route")
        return found

    @api.get("/api/share/link")
    async def share_link(who: dict[str, Any] = Depends(visitor)) -> dict[str, Any]:
        """What the visitor's page opens on: the session, and when the link stops by itself."""
        row = await app.db.fetchone("SELECT expires_at, policy, spent_usd, spent_weekly_pct FROM session_shares WHERE id = ?", (who["share"],))
        state = await manager.get_state(str(who["session_id"]))
        policy = share_policy.parse(row["policy"] if row is not None else None)
        return {
            "session_id": who["session_id"],
            "title": state.session.title if state is not None else "",
            "expires_at": row["expires_at"] if row is not None else None,
            "limits": share_policy.limits(policy, row["spent_usd"] if row is not None else 0, row["spent_weekly_pct"] if row is not None else 0),
        }

    @api.get("/api/share/settings")
    async def share_settings(who: dict[str, Any] = Depends(visitor)) -> dict[str, Any]:
        """The model picker's list, as the side panel gets it: no address, no key, and only the presets
        the link lets the visitor use for the conversation's own model."""
        picked = picker_settings(app)
        allowed = share_policy.presets_allowed(await link_policy(who), "main")
        if allowed is not None:
            picked["presets"] = {pid: preset for pid, preset in picked["presets"].items() if pid in allowed}
            used = {str((preset or {}).get("provider") or "") for preset in picked["presets"].values()}
            picked["providers"] = {pid: conf for pid, conf in picked["providers"].items() if pid in used}
            if picked["model"].get("preset") not in allowed:
                main = share_policy.role(await link_policy(who), "main") or {}
                picked["model"] = {"preset": str(main.get("preset") or "")}
        return picked

    @api.get("/api/share/commands")
    async def share_commands(_: dict[str, Any] = Depends(visitor)) -> list[dict[str, Any]]:
        return []

    @api.get("/api/share/me")
    async def share_me(who: dict[str, Any] = Depends(visitor)) -> dict[str, Any]:
        return {"user_id": app.settings.owner_user_id, "via": "share", "session_id": who["session_id"]}

    # The operator's side: make, list and revoke the links of a session. The door never lets a visitor
    # reach these; the check here is for the day a route list is edited carelessly.

    def operator(who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if who.get("via") == "share":
            raise HTTPException(404, "not a route this link opens")
        return who

    @api.get("/api/sessions/{session_id}/live-shares")
    async def live_shares(session_id: str, _: dict[str, Any] = Depends(operator)) -> dict[str, Any]:
        found = shares()
        if found is None:
            raise HTTPException(503, "live links are not installed")
        return await found.for_session(session_id)

    @api.post("/api/sessions/{session_id}/live-shares")
    async def live_share_create(session_id: str, body: ShareBody, _: dict[str, Any] = Depends(operator)) -> dict[str, Any]:
        found = shares()
        if found is None:
            raise HTTPException(503, "live links are not installed")
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        policy = await policy_of(session_id, body.policy)
        try:
            made = await found.create(session_id, expires_hours=body.expires_hours, label=body.label, password=body.password, policy=policy)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # A turn already under way was given its tools before the link existed; a visitor's steer into
        # it must not find the ones a shared conversation does not have.
        share_tools.mark_shared(manager, session_id)
        return made

    @api.get("/api/sessions/{session_id}/live-shares/options")
    async def live_share_options(session_id: str, _: dict[str, Any] = Depends(operator)) -> dict[str, Any]:
        """What the share dialog offers for this session: its model roles, where it runs, and the
        terminal settings that mean something there."""
        place = await place_of(session_id)
        return {
            "roles": await share_roles.describe(app, session_id),
            "place": place,
            "terminals": share_policy.terminal_choices(place),
            "withheld": share_tools.withheld(app, session_id),
        }

    @api.patch("/api/sessions/{session_id}/live-shares/{share_id}")
    async def live_share_change(session_id: str, share_id: str, body: SharePatch, _: dict[str, Any] = Depends(operator)) -> dict[str, Any]:
        """Change a live link's name, password, policy or end; answers the link as the list shows it."""
        found = shares()
        if found is None:
            raise HTTPException(503, "live links are not installed")
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        policy = await policy_of(session_id, body.policy)
        try:
            return await found.update(session_id, share_id, label=body.label, password=body.password, clear_password=body.clear_password,
                                      policy=policy, expires_hours=body.expires_hours)
        except KeyError as exc:
            raise HTTPException(404, "no such link") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.delete("/api/sessions/{session_id}/live-shares/{share_id}")
    async def live_share_revoke(session_id: str, share_id: str, _: dict[str, Any] = Depends(operator)) -> dict[str, Any]:
        found = shares()
        if found is None:
            raise HTTPException(503, "live links are not installed")
        if not await found.revoke(session_id, share_id):
            raise HTTPException(404, "no such link")
        return await found.for_session(session_id)


__all__ = ["ROUTES", "SCOPE_KEY", "ShareDoor", "pick_refused", "register", "route_of", "shell", "split"]
