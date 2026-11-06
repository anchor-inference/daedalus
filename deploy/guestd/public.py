"""The public listener: the only code that answers on the address strangers reach.

A table of a few routes under ``/a/`` and nothing behind it. It imports no application, so there is no
router to confuse and no path that could lead to the operator's API: the API is not in this process.

It listens on a unix socket behind ``front.py``, which holds the published port and has already bounded the
connection. ``CLIENT_HEADER`` is that process's word for who connected, and only it can reach this socket.

Every answer is markdown or JSON with ``nosniff``, never HTML, and that includes the errors. A door that
never serves a page cannot host a phishing page on the operator's name, whatever a guest sends it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from starlette.responses import JSONResponse, PlainTextResponse, Response

from .limits import Failures, Rate
from .mailbox import DEFAULT_WAIT_SECONDS, MAX_TEXT, Answer, Mailbox

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

MAX_BODY = MAX_TEXT * 6 + 256
"""A message at its limit as JSON escapes it: ``\\uXXXX``, six bytes a character, is what ``json.dumps`` writes by
default for anything past ASCII. At four, a Russian message of 6,000 characters was refused for being over 8,000.
Past this a body is not read at all, and it stays under the front's and nginx's own bounds of 64 KiB."""
INVITE = re.compile(r"^/a/([A-Za-z0-9_-]{32,64})(/redeem)?/?$")
SENDS_PER_MINUTE = 6
READS_PER_MINUTE = 30
PAGES_PER_MINUTE = 30

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}
"""On every answer. A link is private: nothing may cache it, index it, or carry it in a referrer."""

BAD_TOKEN = (
    "That token is not valid here. If you saved the redeem reply, the token is only its `token` field: keep the file and send that. "
    "A token that is really lost cannot be recovered: ask your user for a new link."
)
"""Says the likely mistake first, while the file still exists. An agent told only that tokens are not recoverable
had the right value on disk, and deleted it."""

MARKDOWN = "text/markdown; charset=utf-8"
CLIENT_HEADER = b"x-guest-client"


def as_json(answer: Answer) -> JSONResponse:
    headers = dict(HEADERS)
    if answer.retry_after is not None:
        headers["Retry-After"] = str(answer.retry_after)
        answer = Answer(answer.status, {**answer.body, "retry_after": answer.retry_after})
    return JSONResponse(answer.body, status_code=answer.status, headers=headers)


def refused(status: int, detail: str, retry_after: int | None = None) -> JSONResponse:
    return as_json(Answer(status, {"status": "refused", "detail": detail}, retry_after))


class PublicDoor:
    """The whole ASGI application of the public port."""

    def __init__(self, mailbox: Mailbox, *, failures: Failures | None = None, sends: Rate | None = None, reads: Rate | None = None, pages: Rate | None = None) -> None:
        self.mailbox = mailbox
        self.failures = failures or Failures()
        self.sends = sends or Rate(SENDS_PER_MINUTE)
        self.reads = reads or Rate(READS_PER_MINUTE)
        self.pages = pages or Rate(PAGES_PER_MINUTE)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        try:
            response = await self.route(scope, receive)
        except Exception:
            logger.exception("the public door failed on a request")
            response = refused(500, "Something went wrong on this side. Try again in a minute.", 60)
        await response(scope, receive, send)

    async def route(self, scope: Scope, receive: Receive) -> Response:
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        address = visitor(scope)
        if path == "/robots.txt":
            # Nothing here is for a search engine, but a person's own agent reading the link they pasted is the
            # point of the door: Anthropic's and OpenAI's fetchers for a user's request obey this file.
            return PlainTextResponse("User-agent: *\nDisallow: /\n\nUser-agent: Claude-User\nUser-agent: ChatGPT-User\nAllow: /a/\n", headers=HEADERS)
        if not path.startswith("/a/"):
            return refused(404, "Nothing here.")
        if path in ("/a/messages", "/a/end", "/a/status"):
            return await self.conversation(scope, receive, path, method, address)
        matched = INVITE.match(path)
        if matched is None:
            return refused(404, "Nothing here.")
        invite, redeem = matched.groups()
        known = self.mailbox.store.link_by_invite(invite) is not None
        if not known and self.failures.blocked(address):
            return refused(429, "Too many failed attempts from this address.", 600)
        # A real link is counted by the link: behind a proxy many guests share an address, and one
        # visitor's knocking must not close it to the one it was given to.
        key = invite if known else address
        if redeem:
            return self.redeem(invite, method, address, key)
        return self.page(invite, method, address, key)

    def page(self, invite: str, method: str, address: str, key: str) -> Response:
        if method not in ("GET", "HEAD"):
            return refused(405, "This address is read with GET. Redeeming is POST to /redeem under it.")
        if not self.pages.allow(key):
            return refused(429, "Too many requests.", 60)
        found = self.mailbox.page(invite)
        if found is None:
            self.failures.failed(address)
            return refused(404, "No such link.")
        status, text = found
        return Response(text, status_code=status, media_type=MARKDOWN, headers=HEADERS)

    def redeem(self, invite: str, method: str, address: str, key: str) -> Response:
        if method != "POST":
            return refused(405, "Redeeming is a POST. A GET here spends nothing.")
        if not self.pages.allow(key):
            # Each refused redeem leaves a line for the owner: a loop must not be able to fill the record.
            return refused(429, "Too many requests.", 60)
        answer = self.mailbox.redeem(invite, address=address)
        if answer is None:
            self.failures.failed(address)
            return refused(404, "No such link.")
        return as_json(answer)

    async def conversation(self, scope: Scope, receive: Receive, path: str, method: str, address: str) -> Response:
        token = bearer(scope)
        if self.mailbox.store.conversation_by_token(token) is None:
            # Looked up before the failures are, so a guest with a good token is never locked out by
            # somebody else's guessing from the same address.
            if self.failures.blocked(address):
                return refused(429, "Too many failed attempts from this address.", 600)
            self.failures.failed(address)
            if not token:
                return refused(401, "This needs the header Authorization: Bearer <token>, with the token the redeem returned.")
            return refused(401, BAD_TOKEN)
        call = self.call_for(path, method, scope, token)
        if call is None:
            return refused(405, "Wrong method for this address.")
        rate, run = call
        if rate is not None and not rate.allow(token):
            return refused(429, "Too many requests on this conversation.", 15)
        body: Any = None
        if method == "POST" and path == "/a/messages":
            raw = await read_body(receive)
            if raw is None:
                return refused(413, f"A message may be at most {MAX_TEXT} characters.")
            try:
                body = json.loads(raw)
            except ValueError:
                return refused(400, 'Send JSON with one field: {"text": "your message"}.')
        answer = await run(body)
        if answer is None:
            return refused(401, BAD_TOKEN)
        return as_json(answer)

    def call_for(self, path: str, method: str, scope: Scope, token: str) -> tuple[Rate | None, Callable[[Any], Awaitable[Answer | None]]] | None:
        mailbox = self.mailbox

        async def send(body: Any) -> Answer | None:
            return mailbox.send(token, body.get("text") if isinstance(body, dict) else None)

        async def read(_: Any) -> Answer | None:
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            return await mailbox.read(token, after=number(query.get("after"), 0), wait=number(query.get("wait"), DEFAULT_WAIT_SECONDS))

        async def end(_: Any) -> Answer | None:
            return mailbox.end(token)

        async def status(_: Any) -> Answer | None:
            return mailbox.status(token)

        if path == "/a/messages" and method == "POST":
            return self.sends, send
        if path == "/a/messages" and method == "GET":
            return self.reads, read
        if path == "/a/end" and method == "POST":
            return None, end
        if path == "/a/status" and method == "GET":
            return self.reads, status
        return None


def visitor(scope: Scope) -> str:
    """Who is asking: what the front says, or, for a client with no front (a test), the peer."""
    for name, value in scope.get("headers", ()):
        if name == CLIENT_HEADER:
            return value.decode("latin-1")
    client = scope.get("client")
    return str(client[0]) if client else ""


def bearer(scope: Scope) -> str:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            kind, _, token = value.decode("latin-1").partition(" ")
            return token.strip() if kind.lower() == "bearer" else ""
    return ""


def number(values: list[str] | None, default: int) -> int:
    """A query number. Past SQLite's 64 bits it is nobody's cursor, and treated as absent rather than a 500."""
    try:
        value = max(0, int(values[0])) if values else default
    except ValueError:
        return default
    return value if value < 2**63 else default


async def read_body(receive: Receive) -> bytes | None:
    """The request body, or ``None`` past :data:`MAX_BODY`, which stops reading it there."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return bytes(body)
        body.extend(message.get("body", b""))
        if len(body) > MAX_BODY:
            return None
        if not message.get("more_body", False):
            return bytes(body)


__all__ = ["CLIENT_HEADER", "HEADERS", "INVITE", "MAX_BODY", "PublicDoor", "visitor"]
