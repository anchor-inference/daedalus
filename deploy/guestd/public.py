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

from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from . import a2a
from .files import CHUNK_BYTES
from .limits import Failures, Rate
from .mailbox import DEFAULT_WAIT_SECONDS, MAX_TEXT, RETRY_SECONDS, Answer, Mailbox
from .store import CLOSED, REVOKED, WAITING, ended
from .text import CARD_BYTES

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

MAX_BODY = MAX_TEXT * 6 + CARD_BYTES + 256
"""A message at its limit as JSON escapes it, with a card at its limit beside it: ``\\uXXXX``, six bytes a character,
is what ``json.dumps`` writes by default for anything past ASCII. At four, a Russian message of 6,000 characters was
refused for being over 8,000. Past this a body is not read at all, and it stays under the front's and nginx's own
bounds of 64 KiB."""
INVITE = re.compile(r"^/a/([A-Za-z0-9_-]{32,64})(/redeem)?/?$")
FILE_PATH = re.compile(r"^/a/files/(f_[0-9a-f]{16})(/content)?$")
TASK_PATH = re.compile(r"^/a/tasks/(t_[0-9a-f]{12})$")
SENDS_PER_MINUTE = 6
READS_PER_MINUTE = 30
PAGES_PER_MINUTE = 30
PIECES_PER_MINUTE = 240
"""Pieces of files up or down, per conversation: 120 MB a minute, so a 50 MB file is well under a minute of calls
and a loop that asks for the same piece for ever is held to a rate a disk does not notice."""

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

RPC_MAX_BODY = 63 * 1024
"""An A2A call, which may carry a small file: what the front and the tunnel let through, less room for the head."""

MARKDOWN = "text/markdown; charset=utf-8"
CLIENT_HEADER = b"x-guest-client"
CLIENT_KIND_HEADER = b"x-agent-client"
"""What the caller says it is, as opposed to :data:`CLIENT_HEADER`, which is who the front saw connect."""
KEY_HEADER = b"x-agent-key"
PROOF_HEADER = b"x-agent-proof"
"""Another installation's public key and its signature over the invite it redeems: kept for the owner's host to check."""


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

    def __init__(
        self, mailbox: Mailbox, *, failures: Failures | None = None, sends: Rate | None = None, reads: Rate | None = None, pages: Rate | None = None, pieces: Rate | None = None,
    ) -> None:
        self.mailbox = mailbox
        self.failures = failures or Failures()
        self.sends = sends or Rate(SENDS_PER_MINUTE)
        self.reads = reads or Rate(READS_PER_MINUTE)
        self.pages = pages or Rate(PAGES_PER_MINUTE)
        self.pieces = pieces or Rate(PIECES_PER_MINUTE)
        self.a2a = a2a.A2A(mailbox)

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
        if path == a2a.RPC_PATH:
            return await self.rpc(scope, receive, method, address)
        if card := a2a.CARD_PATH.match(path):
            return self.agent_card(scope, card.group(1), method, address)
        if path in ("/a/messages", "/a/end", "/a/status", "/a/files") or FILE_PATH.match(path) or TASK_PATH.match(path):
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
            return self.redeem(invite, method, address, key, header(scope, CLIENT_KIND_HEADER), raw_header(scope, KEY_HEADER), raw_header(scope, PROOF_HEADER))
        return self.page(invite, method, address, key, "application/json" in header(scope, b"accept"))

    def page(self, invite: str, method: str, address: str, key: str, as_data: bool = False) -> Response:
        if method not in ("GET", "HEAD"):
            return refused(405, "This address is read with GET. Redeeming is POST to /redeem under it.")
        if not self.pages.allow(key):
            return refused(429, "Too many requests.", 60)
        if as_data and (card := self.mailbox.card(invite)) is not None:
            return as_json(card)
        found = self.mailbox.page(invite)
        if found is None:
            self.failures.failed(address)
            return refused(404, "No such link.")
        status, text = found
        return Response(text, status_code=status, media_type=MARKDOWN, headers=HEADERS)

    def agent_card(self, scope: Scope, invite: str, method: str, address: str) -> Response:
        """A link's A2A card. Like the page, a GET that spends nothing, and gone once the link is spent, except to the
        guest holding that link's token: a client that fetches the card again at each start must not lose it."""
        if method not in ("GET", "HEAD"):
            return refused(405, "The agent card is read with GET.")
        link = self.mailbox.store.link_by_invite(invite)
        if link is None and self.failures.blocked(address):
            return refused(429, "Too many failed attempts from this address.", 600)
        if not self.pages.allow(invite if link is not None else address):
            return refused(429, "Too many requests.", 60)
        if link is None:
            self.failures.failed(address)
            return refused(404, "No such link.")
        if link["state"] == WAITING and not ended(link["invite_expires_at"], self.mailbox.clock()):
            return JSONResponse(self.a2a.card(link, invite=invite), headers=HEADERS)
        conversation = self.mailbox.store.conversation_by_token(bearer(scope)) if link["state"] != REVOKED else None
        if conversation is not None and conversation["link_id"] == link["id"] and conversation["state"] != CLOSED:
            return JSONResponse(self.a2a.card(link, invite=invite, conversation=conversation), headers=HEADERS)
        return refused(410, "This link has been used or is closed. A card is shown again only with the conversation's token.")

    async def rpc(self, scope: Scope, receive: Receive, method: str, address: str) -> Response:
        """The A2A entrance: one JSON-RPC call on a conversation, found by its token like every other call here."""
        if method != "POST":
            return refused(405, "A2A calls are JSON-RPC over POST.")
        token = bearer(scope)
        conversation = self.mailbox.store.conversation_by_token(token)
        if conversation is None:
            if self.failures.blocked(address):
                return refused(429, "Too many failed attempts from this address.", 600)
            self.failures.failed(address)
            error = a2a.RpcError(a2a.REFUSED, BAD_TOKEN if token else "This needs the header Authorization: Bearer <token>, with the token the link's redeem returned.", reason="TOKEN")
            return JSONResponse(a2a.envelope(None, error=error), status_code=401, headers={**HEADERS, "WWW-Authenticate": "Bearer"})
        raw = await read_body(receive, RPC_MAX_BODY)
        if raw is None:
            return self.rpc_answer(None, error=a2a.RpcError(a2a.INVALID_REQUEST, f"A call is at most {RPC_MAX_BODY} bytes. Send a larger file in pieces first.", reason="TOO_LARGE"))
        try:
            call = a2a.parse(raw)
        except a2a.RpcError as exc:
            return self.rpc_answer(json.loads(exc.metadata.pop("id", "null")), error=exc)
        version = header(scope, b"a2a-version") or (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("A2A-Version") or [""])[0]
        if call.method in a2a.OLD_METHODS:
            version = version or "0.3"
        refusal = a2a.version_refused(version)
        if refusal is not None:
            return self.rpc_answer(call.id, error=refusal)
        rate = self.sends if call.method in a2a.SENDING else self.reads
        if not rate.allow(token):
            return self.rpc_answer(call.id, error=a2a.RpcError(a2a.REFUSED, "Too many requests on this conversation.", reason="BUSY", retry_after=RETRY_SECONDS))
        try:
            if call.method in a2a.STREAMING:
                return await self.rpc_stream(call, token, conversation)
            result = await self.rpc_call(call, token, conversation)
        except a2a.RpcError as exc:
            return self.rpc_answer(call.id, error=exc)
        if call.notification:
            return Response(status_code=204, headers=HEADERS)
        return self.rpc_answer(call.id, result=result)

    def rpc_answer(self, call_id: Any, *, result: Any = None, error: a2a.RpcError | None = None) -> Response:
        """Every JSON-RPC answer, an error too, is a 200: the error is in the body, where a client of the protocol looks."""
        return JSONResponse(a2a.envelope(call_id, result=result, error=error), headers=HEADERS)

    async def rpc_call(self, call: a2a.Call, token: str, conversation: Any) -> Any:
        method, params, translation = call.method, call.params, self.a2a
        if method == "SendMessage":
            task_id, at_once, history = translation.send(token, conversation, params)
            if at_once:
                return {"task": translation.task(self.mailbox.store.conversation(conversation["id"]), task_id, history=history)}
            with self.mailbox.held_open(conversation["id"]) as free:
                # Held only while there is room: a third wait on one conversation gets the task as it stands.
                return {"task": await translation.settle(conversation["id"], task_id, a2a.HOLD_SECONDS if free else 0.0, history)}
        if method == "GetTask":
            return translation.get_task(conversation, params)
        if method == "ListTasks":
            return translation.list_tasks(conversation, params)
        if method == "CancelTask":
            return translation.cancel_task(conversation, params)
        if method == "GetExtendedAgentCard":
            return translation.extended_card(conversation)
        if method in a2a.PUSH_METHODS:
            raise a2a.RpcError(a2a.PUSH_NOT_SUPPORTED, a2a.NO_PUSH, reason="NO_PUSH")
        raise a2a.RpcError(a2a.METHOD_NOT_FOUND, f"There is no method {method[:64]!r} here. This door speaks A2A {a2a.PROTOCOL_VERSION} over JSON-RPC.")

    async def rpc_stream(self, call: a2a.Call, token: str, conversation: Any) -> Response:
        """A task followed over server-sent events. What goes wrong before the first event is a plain JSON-RPC answer."""
        translation = self.a2a
        if call.method == "SendStreamingMessage":
            task_id, _, history = translation.send(token, conversation, call.params)
        else:
            found = translation.get_task(conversation, {"id": call.params.get("id")})
            if found["status"]["state"] in a2a.TERMINAL:
                raise a2a.RpcError(a2a.UNSUPPORTED, f"That task is {found['status']['state']}: there is nothing more to follow. GetTask reads it.", reason="TASK_SETTLED")
            task_id, history = found["id"], None
        slot = self.mailbox.held_open(conversation["id"])
        if not slot.__enter__():
            slot.__exit__(None, None, None)
            if call.method == "SendStreamingMessage":
                # The message is in: answer with its task rather than an error, so it is not sent twice.
                envelope = a2a.envelope(call.id, result={"task": translation.task(self.mailbox.store.conversation(conversation["id"]), task_id, history=history)})
                return StreamingResponse(iter([sse(envelope)]), media_type="text/event-stream", headers=STREAM_HEADERS)
            raise a2a.RpcError(a2a.REFUSED, "Another wait is already open on this conversation.", reason="BUSY", retry_after=RETRY_SECONDS)

        async def events() -> Any:
            try:
                async for event in translation.follow(conversation["id"], task_id, history=history):
                    yield ": still here\n\n" if event is None else sse(a2a.envelope(call.id, result=event))
            finally:
                slot.__exit__(None, None, None)

        return StreamingResponse(events(), media_type="text/event-stream", headers=STREAM_HEADERS)

    def redeem(self, invite: str, method: str, address: str, key: str, client: str = "", agent_key: str = "", proof: str = "") -> Response:
        if method != "POST":
            return refused(405, "Redeeming is a POST. A GET here spends nothing.")
        if not self.pages.allow(key):
            # Each refused redeem leaves a line for the owner: a loop must not be able to fill the record.
            return refused(429, "Too many requests.", 60)
        answer = self.mailbox.redeem(invite, address=address, client=client, key=agent_key, proof=proof)
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
        if method == "POST" and path in ("/a/messages", "/a/files"):
            raw = await read_body(receive)
            if raw is None:
                return refused(413, f"A message may be at most {MAX_TEXT} characters.")
            try:
                body = json.loads(raw)
            except ValueError:
                return refused(400, 'Send JSON with one field: {"text": "your message"}.')
        elif method == "PUT":
            body = await read_body(receive, CHUNK_BYTES)
            if body is None:
                return refused(413, f"A piece is at most {CHUNK_BYTES} bytes.")
        answer = await run(body)
        if answer is None:
            return refused(401, BAD_TOKEN)
        if isinstance(answer, Response):
            return answer
        return as_json(answer)

    def call_for(self, path: str, method: str, scope: Scope, token: str) -> tuple[Rate | None, Callable[[Any], Awaitable[Answer | Response | None]]] | None:
        mailbox = self.mailbox
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))

        async def send(body: Any) -> Answer | None:
            return mailbox.send(token, body)

        async def read(_: Any) -> Answer | None:
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
        if path == "/a/files" and method == "POST":
            return self.pieces, lambda body: answered(mailbox.announce_file(token, body))
        if matched := TASK_PATH.match(path):
            return (self.reads, lambda _: answered(mailbox.task_view(token, matched.group(1)))) if method == "GET" else None
        matched = FILE_PATH.match(path)
        if matched is None:
            return None
        file_id, content = matched.groups()
        if content and method == "GET":
            return self.pieces, lambda _: answered(self.content(token, file_id, number(query.get("offset"), 0)))
        if not content and method == "PUT":
            return self.pieces, lambda body: answered(mailbox.put_piece(token, file_id, number(query.get("offset"), 0), body))
        if not content and method == "GET":
            return self.reads, lambda _: answered(mailbox.file_view(token, file_id))
        return None

    def content(self, token: str, file_id: str, offset: int) -> Answer | Response | None:
        """A piece of a file as bytes. ``attachment`` and ``nosniff`` with the sandbox of every answer here: whatever a
        file holds, a browser that opens this address saves it and draws nothing."""
        answer, data, file = self.mailbox.file_content(token, file_id, offset)
        if answer is None or file is None:
            return answer
        headers = {
            **HEADERS, "Content-Disposition": "attachment", "X-File-Size": str(file["size"]), "X-File-Sha256": file["sha256"], "X-Offset": str(offset),
            "X-Next-Offset": str(offset + len(data)),
        }
        return Response(data, media_type="application/octet-stream", headers=headers)


STREAM_HEADERS = {**HEADERS, "X-Accel-Buffering": "no"}
"""``X-Accel-Buffering`` keeps nginx from holding a stream's events back until it has a buffer's worth."""


def sse(envelope: dict[str, Any]) -> str:
    return "data: " + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1").lower()
    return ""


def raw_header(scope: Scope, name: bytes) -> str:
    """A header as sent: a key is base64, where case is the value."""
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")[:256]
    return ""


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


async def answered(answer: Any) -> Any:
    """A call of the mailbox's, which is not a coroutine, in the shape the routes await."""
    return answer


async def read_body(receive: Receive, limit: int = MAX_BODY) -> bytes | None:
    """The request body, or ``None`` past ``limit``, which stops reading it there."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return bytes(body)
        body.extend(message.get("body", b""))
        if len(body) > limit:
            return None
        if not message.get("more_body", False):
            return bytes(body)


__all__ = ["CLIENT_HEADER", "RPC_MAX_BODY", "STREAM_HEADERS", "FILE_PATH", "HEADERS", "INVITE", "MAX_BODY", "PIECES_PER_MINUTE", "TASK_PATH", "PublicDoor", "visitor"]
