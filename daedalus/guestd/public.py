"""The public listener: the only code that answers on the address strangers reach.

A table of a few routes under ``/a/`` and nothing behind it. It imports no application, so there is no
router to confuse and no path that could lead to the operator's API: the API is not in this process.

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

MAX_BODY = MAX_TEXT * 4 + 256
"""A message at its limit in the worst JSON escaping. Past this a body is not read at all."""
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

MARKDOWN = "text/markdown; charset=utf-8"


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
        client = scope.get("client")
        address = str(client[0]) if client else ""
        if path == "/robots.txt":
            return PlainTextResponse("User-agent: *\nDisallow: /\n", headers=HEADERS)
        if not path.startswith("/a/"):
            return refused(404, "Nothing here.")
        if path in ("/a/messages", "/a/end", "/a/status"):
            return await self.conversation(scope, receive, path, method, address)
        matched = INVITE.match(path)
        if matched is None:
            return refused(404, "Nothing here.")
        if self.failures.blocked(address):
            return refused(429, "Too many failed attempts from this address.", 600)
        invite, redeem = matched.groups()
        if redeem:
            return self.redeem(invite, method, address)
        return self.page(invite, method, address)

    def page(self, invite: str, method: str, address: str) -> Response:
        if method not in ("GET", "HEAD"):
            return refused(405, "This address is read with GET. Redeeming is POST to /redeem under it.")
        if not self.pages.allow(address):
            return refused(429, "Too many requests.", 60)
        found = self.mailbox.page(invite)
        if found is None:
            self.failures.failed(address)
            return refused(404, "No such link.")
        status, text = found
        return Response(text, status_code=status, media_type=MARKDOWN, headers=HEADERS)

    def redeem(self, invite: str, method: str, address: str) -> Response:
        if method != "POST":
            return refused(405, "Redeeming is a POST. A GET here spends nothing.")
        if not self.pages.allow(address):
            # Each refused redeem leaves a line for the owner: a loop must not be able to fill the record.
            return refused(429, "Too many requests.", 60)
        answer = self.mailbox.redeem(invite, address=address)
        if answer is None:
            self.failures.failed(address)
            return refused(404, "No such link.")
        return as_json(answer)

    async def conversation(self, scope: Scope, receive: Receive, path: str, method: str, address: str) -> Response:
        if self.failures.blocked(address):
            return refused(429, "Too many failed attempts from this address.", 600)
        token = bearer(scope)
        if not token:
            self.failures.failed(address)
            return refused(401, "This needs the header Authorization: Bearer <token>, with the token the redeem returned.")
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
            self.failures.failed(address)
            return refused(401, "That token is not valid here. Tokens are not recoverable: ask your user for a new link.")
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


def bearer(scope: Scope) -> str:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            kind, _, token = value.decode("latin-1").partition(" ")
            return token.strip() if kind.lower() == "bearer" else ""
    return ""


def number(values: list[str] | None, default: int) -> int:
    try:
        return max(0, int(values[0])) if values else default
    except ValueError:
        return default


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


__all__ = ["HEADERS", "INVITE", "MAX_BODY", "PublicDoor"]
