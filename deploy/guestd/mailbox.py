"""The mailbox: one conversation, as a guest's agent sees it, in no protocol's shape.

Everything a guest can do is a method here, and each returns an :class:`Answer`. The HTTP routes in
:mod:`.public` are a thin translation of these and nothing else, so another face (MCP, A2A) attaches by
translating the same calls, with the same limits and the same words.

The replies are written for a program that decides its next step from them. The trial's lesson: an
agent that gets no clear answer does not stop politely. Given silence, it polled until it was killed;
given a bare refusal after losing its token, it went through the disk and, once, through another
process's memory with sudo. So every answer says what the state is and what is sensible next.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import pages
from .files import CHUNK_BYTES, Bodies, Desk, FileRefused
from .store import (
    ACCEPTED,
    AGENT,
    ANSWERED,
    BY_OWNER,
    CANCELLED,
    CLOSED,
    CONTACT,
    DOOR,
    EXPIRED,
    FILE,
    GUEST,
    HELD,
    KINDS,
    NEEDS_INPUT,
    NO_LIMIT,
    OPEN,
    OPEN_TASK_STATES,
    PENDING,
    PROTOCOL_KINDS,
    QUESTION,
    REVOKED,
    STAND_IN,
    STATUS,
    TASK,
    TEXT,
    WAITING,
    WORKING,
    Refusal,
    Store,
    ended,
)
from .text import inert, one_line

logger = logging.getLogger(__name__)

MAX_TEXT = 8000
MAX_WAIT_SECONDS = 30
"""A read holds at most this long. Under the 60 s a tool call may take in Codex, and short enough that
nothing between the guest and this door has to keep a quiet connection alive."""
DEFAULT_WAIT_SECONDS = 20
MAX_UNANSWERED = 3
"""Guest messages that may wait for an answer at once: a turn each, so a burst is not a backlog of spending."""
STAND_IN_REPLY_SECONDS = 120.0
DIRECT_REPLY_SECONDS = 900.0
"""How long a guest is told to keep polling for an answer. A direct turn waits for the chat to be free."""
LATE_VISIT_SECONDS = 600.0
"""How often one spent link may tell its owner that somebody came back to it."""
PENDING_POLL_SECONDS = 300.0
"""How long a guest is told to wait for the owner in one go. The token stays good after that."""
RETRY_SECONDS = 15
DOOR_MARK = "agent-link"
KNOWN_CLIENTS = frozenset({"daedalus"})
MAX_POLLS = 2
"""Reads one token may hold open at once. Each one is a connection a stranger gets to keep for 30 s."""
TASK_PATIENCE_SECONDS = 3600.0
"""How long a guest is told to keep reading while a task is being worked on and nothing has moved."""
TEXT_ONLY = "This link takes text messages only: the agent behind it has no files and takes no tasks."
CONTACTS_PER_CONVERSATION = 6
"""Offers of a contact, and answers to one, a conversation may carry. Each reaches the owner, so a loop of them must
not become a way to page somebody."""
KEY = re.compile(r"[A-Za-z0-9_-]{43}")
"""An Ed25519 public key as the hosts write it: 32 bytes, base64url without padding."""
SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}")
PROOF = re.compile(r"[0-9]{1,16}\.[A-Za-z0-9_-]{86}")
"""``<milliseconds>.<signature>``: what a redeem is signed with."""


def signed_of(value: Any) -> dict[str, Any] | None:
    """A message's signature as the sending host made it, each field in its shape, or ``None``. The door checks
    nothing else about it: it holds no keys, and the receiving host does the checking."""
    if not isinstance(value, dict):
        return None
    key, sig, at = value.get("key"), value.get("sig"), value.get("at")
    if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(sig, str) or not SIGNATURE.fullmatch(sig):
        return None
    if isinstance(at, bool) or not isinstance(at, int) or not 0 < at < 10**16:
        return None
    return {"key": key, "sig": sig, "at": at}


def contact_of(value: Any) -> dict[str, Any] | None:
    """An offer of a contact, or the answer to one, as plain data: the offering host's key, a name, the address of a
    link of its own for the other side, and its signature over them. ``None`` for anything else."""
    if not isinstance(value, dict):
        return None
    key, name, invite, sig, at = value.get("key"), value.get("name"), value.get("invite", ""), value.get("sig"), value.get("at")
    if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(sig, str) or not SIGNATURE.fullmatch(sig):
        return None
    if isinstance(at, bool) or not isinstance(at, int) or not 0 < at < 10**16 or not isinstance(name, str) or not isinstance(invite, str) or len(invite) > 400:
        return None
    if value.get("answer") not in ("accept", "decline", "offer"):
        return None
    return {"key": key, "name": one_line(name, 80), "invite": inert(invite), "sig": sig, "at": at, "answer": value["answer"]}

CLOSED_WORDS = {
    "ended": "This conversation has ended.",
    "expired": "This conversation has reached the end of its lifetime.",
    "idle": "This conversation was closed because nothing was said for too long.",
    "refused": "The owner of this link declined this conversation.",
    "approval_timeout": "The owner of this link did not admit this conversation in time, which counts as declining it.",
    "revoked": "The owner of this link has revoked it.",
}

STOP = "Stop here and tell your user what was said. To talk again they need a new link."

LIMIT_WORDS = {
    "usd_per_link": "This link has used up its budget.",
    "usd_all_guests": "This link has used up the budget all its guests share.",
    "usd_per_day": "The owner's budget for guests is used up for today.",
    "turns_per_day": "This link has used up its messages for today.",
    "turns_total": "This link has used up its messages.",
}


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: dict[str, Any]
    retry_after: int | None = None


def limit_answer(refusal: Refusal, *, now: float) -> Answer:
    words = LIMIT_WORDS.get(refusal.limit, "A limit of this link was reached.")
    if refusal.until is None:
        return Answer(403, {"status": "limit", "limit": refusal.limit, "stop_polling": True, "detail": f"{words} {STOP}"})
    wait = max(1, int(refusal.until - now))
    return Answer(429, {"status": "limit", "limit": refusal.limit, "stop_polling": True, "detail": f"{words} Tell your user; the limit lifts in about {wait // 3600 + 1} hours."}, wait)


class Mailbox:
    def __init__(self, store: Store, *, public_base: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.time, desk: Desk | None = None) -> None:
        self.store = store
        self.desk = desk or Desk(store, Bodies(store.path.parent / "files"))
        self.public_base = public_base
        self.clock = clock
        self._wakers: dict[str, asyncio.Event] = {}
        self._polling: dict[str, int] = {}
        self._late_visits: dict[str, float] = {}
        self.queued = asyncio.Event()
        """Set when a guest message may be waiting for the stand-in."""
        self.changed = asyncio.Event()
        """Set when the host's feed has something new. Apart from ``queued``: each has one waiter that
        clears it, and sharing one would lose a wake-up for whichever cleared second."""

    def wake(self, conversation_id: str) -> None:
        waker = self._wakers.get(conversation_id)
        if waker is not None:
            waker.set()

    def notify(self) -> None:
        self.queued.set()
        self.changed.set()

    # -- before a conversation exists --------------------------------------------------

    def card(self, invite: str) -> Answer | None:
        """The page's facts as data, for a client that reads fields rather than prose; ``None`` as :meth:`page`.

        The same address and the same rules as the page: a GET that spends nothing, on a spent link the same
        line for the owner. It exists because a Daedalus on the other side shows its operator who is at the door
        before anything is redeemed, and doing that from prose would mean parsing a stranger's sentences. Any
        client may ask for it. ``door`` is what tells a door from any other page that answers 200.
        """
        found = self.page(invite)
        if found is None:
            return None
        status, _ = found
        link = self.store.link_by_invite(invite)
        assert link is not None
        if status != 200:
            return Answer(status, {"door": DOOR_MARK, "status": "closed" if link["state"] in (REVOKED, EXPIRED, WAITING) else "used", "stop_polling": True})
        direct = link["mode"] != STAND_IN
        return Answer(200, {
            "door": DOOR_MARK, "status": "waiting", "topic": link["topic"], "mode": link["mode"], "approval": bool(link["approval"]),
            "retention_days": float(link["retention_days"]), "max_text": MAX_TEXT, "kinds": list(PROTOCOL_KINDS) if direct else [TEXT],
            **({"file_mb": float(link["file_mb"]), "day_mb": float(link["day_mb"]), "chunk_bytes": CHUNK_BYTES} if direct else {}),
            # The owner's key and its signature over this invite: a guest's host checks it, the door cannot.
            **({"key": link["owner_key"], "proof": link["owner_proof"]} if link["owner_key"] else {}),
        })

    def page(self, invite: str) -> tuple[int, str] | None:
        """The page behind an invite, or ``None`` for an invite this door never made.

        A GET changes nothing about a link that can still be used, not even a counter. Link previews and
        scanners fetch whatever passes through a messenger, and an invite a preview could burn would arrive
        already spent.

        On a spent link it leaves a line for the owner, at most one per :data:`LATE_VISIT_SECONDS`. Real agents
        open with a GET, read the 410 and stop, as they should, so they never reach a refused redeem: the
        guest whose invite had been stolen came and went and the owner heard nothing.
        """
        link = self.store.link_by_invite(invite)
        if link is None:
            return None
        if link["state"] == WAITING:
            if ended(link["invite_expires_at"], self.clock()):
                return 410, pages.GONE_PAGE
            return 200, pages.page(link, base=self.public_base() or "", invite=invite, max_text=MAX_TEXT, chunk_bytes=CHUNK_BYTES)
        if link["state"] in (REVOKED, EXPIRED):
            return 410, pages.GONE_PAGE
        self._late_visit(link)
        return 410, pages.SPENT_PAGE

    def _late_visit(self, link: Any) -> None:
        now = self.clock()
        if now - self._late_visits.get(link["id"], -LATE_VISIT_SECONDS) < LATE_VISIT_SECONDS:
            return
        if len(self._late_visits) > 4096:
            self._late_visits.clear()
        self._late_visits[link["id"]] = now
        self.store.record(link["id"], "redeem_refused", "page")
        self.notify()

    def redeem(self, invite: str, *, address: str = "", client: str = "", key: str = "", proof: str = "") -> Answer | None:
        """Spend the invite and bind a token, or ``None`` for an invite this door never made.

        ``client`` is what the caller says it is. Kept for the owner to read and nothing else: it opens nothing,
        changes no limit and no answer, because any stranger can type it. Only a known word is kept, so a header
        cannot put a stranger's words into the owner's list.

        A second redeem always gets a refusal, even from the same client seconds later: there is no
        window in which it would return the token. The kind reply would be welcome to an agent that
        lost it, which happened in the trial. But the concern here is a stolen invite, and the client
        that could use such a window is exactly the one sharing an address with the real guest: an
        agent platform's egress, a company's NAT. "Same address and same curl" identifies nothing, and
        a thief handed the same token would sit silently inside a conversation the owner has admitted
        for someone else. Instead the refusal stays visible to the owner as evidence, the owner can
        issue a new link with one action, and the wording keeps the guest's agent from hunting.

        ``key`` and ``proof`` are another installation's public key and its signature over this invite. Kept for the
        owner's host, which checks them; a malformed pair is dropped rather than refused, since all it would prove is
        who holds a key.
        """
        link = self.store.link_by_invite(invite)
        if link is None:
            return None
        if link["state"] in (REVOKED, EXPIRED) or (link["state"] == WAITING and ended(link["invite_expires_at"], self.clock())):
            return Answer(410, {"status": "closed", "stop_polling": True, "detail": "This link is closed. Stop here and ask your user for a new link."})
        if not (KEY.fullmatch(key) and PROOF.fullmatch(proof)):
            key = proof = ""
        spent = None if link["state"] != WAITING else self.store.redeem(link, client=client if client in KNOWN_CLIENTS else "", key=key, proof=proof)
        if spent is None:
            self.store.record(link["id"], "redeem_refused", address)
            self.notify()
            return Answer(410, {"status": "already_redeemed", "stop_polling": True, "detail": pages.LOST_TOKEN})
        token, conversation = spent
        self.notify()
        body: dict[str, Any] = {
            "token": token,
            "status": conversation["state"],
            "save_token": "Save this token to a file now. It is shown only this once and cannot be recovered.",
            "send": "POST /a/messages with Authorization: Bearer <token> and JSON {\"text\": \"...\"}",
            "read": "GET /a/messages?after=<id>&wait=20 with the same header",
        }
        if link["mode"] != STAND_IN:
            body["more"] = "Files and tasks are described on the link's page, under the same address you redeemed."
        if conversation["state"] == PENDING:
            body["detail"] = "The owner admits a first contact by hand. You can send your first message now; it is answered once they agree."
            return Answer(201, body, 30)
        return Answer(201, body)

    # -- a conversation -----------------------------------------------------------------

    def _closed_answer(self, conversation: Any) -> Answer:
        reason = str(conversation["reason"] or "ended")
        return Answer(410, {"status": "closed", "reason": reason, "stop_polling": True, "detail": f"{CLOSED_WORDS.get(reason, 'This conversation is closed.')} {STOP}"})

    def send(self, token: str, body: Any) -> Answer | None:
        """A guest's message of any kind. ``{"text": ...}`` alone is a text message, as it always was."""
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        if conversation["state"] == CLOSED:
            return self._closed_answer(conversation)
        link = self.store.link(conversation["link_id"])
        assert link is not None
        if not isinstance(body, dict):
            return Answer(400, {"status": "invalid", "detail": 'Send JSON with one field: {"text": "your message"}.'})
        kind = body.get("kind") or TEXT
        if kind not in KINDS:
            return Answer(400, {"status": "invalid", "detail": f"kind is one of {', '.join(KINDS)}."})
        if link["mode"] == STAND_IN and (kind not in (TEXT, CONTACT) or body.get("files")):
            return Answer(403, {"status": "refused", "detail": TEXT_ONLY})
        if kind == STATUS:
            return self._guest_status(conversation, body)
        if kind == CONTACT:
            return self._contact(conversation, body)
        text = body.get("text")
        if text is None and kind == FILE:
            text = ""
        if not isinstance(text, str) or (not text.strip() and kind != FILE):
            return Answer(400, {"status": "invalid", "detail": 'Send JSON with one field: {"text": "your message"}.'})
        clean = inert(text)
        if not clean and kind != FILE:
            return Answer(400, {"status": "invalid", "detail": "That message has no readable text."})
        if len(clean) > MAX_TEXT:
            return Answer(413, {"status": "too_long", "detail": f"A message may be at most {MAX_TEXT} characters."})
        if conversation["state"] == PENDING:
            if kind != TEXT or body.get("files") or body.get("task"):
                return Answer(409, {"status": "pending", "detail": "Until the owner admits this conversation, only a text message is taken."}, 30)
            if self.store.held_count(conversation["id"]) >= 1:
                return Answer(202, {"status": "pending", "detail": "Your first message is with the owner. Nothing more is taken until they answer."}, 30)
            message = self.store.add_guest_message(conversation, clean)
            self.notify()
            return Answer(202, {"status": "pending", "id": message, "detail": "Kept for the owner, who decides whether to admit this conversation. Read to see when they do."}, 30)
        task_id = ""
        if kind == TASK:
            if body.get("task"):
                return Answer(400, {"status": "invalid", "detail": "A new task gets its id from the door: leave task out."})
        elif body.get("task") is not None:
            task = self.store.task(str(body.get("task")))
            if task is None or task["conversation_id"] != conversation["id"]:
                return Answer(404, {"status": "invalid", "detail": "There is no such task in this conversation."})
            if task["state"] not in OPEN_TASK_STATES:
                return Answer(409, {"status": "invalid", "task": task["id"], "state": task["state"], "detail": f"That task is {task['state']} and takes no more messages."})
            task_id = task["id"]
        data: dict[str, Any] = {}
        if body.get("from") == BY_OWNER:
            data["from"] = BY_OWNER
        signed = signed_of(body.get("signed"))
        if signed is not None:
            data["signed"] = signed
        if kind == FILE or body.get("files"):
            try:
                data["files"] = [file["id"] for file in self.desk.ready_of(conversation["id"], body.get("files"), GUEST)]
            except FileRefused as exc:
                return Answer(exc.status, {"status": "invalid", "code": exc.code, "detail": exc.detail, **exc.extra})
        refusal = self.store.limit_reached(link, conversation=conversation)
        if refusal is not None:
            self.store.record(link["id"], "limit", refusal.limit)
            self.notify()
            return limit_answer(refusal, now=self.clock())
        if self._unanswered(conversation["id"]) >= MAX_UNANSWERED:
            return Answer(429, {"status": "busy", "detail": "Earlier messages are still waiting for an answer. Read first."}, RETRY_SECONDS)
        if kind == TASK:
            task_id = self.store.create_task(conversation["id"], clean)
        message = self.store.add_guest_message(conversation, clean, kind=kind, task_id=task_id, data=data)
        self.notify()
        out: dict[str, Any] = {"status": "open", "id": message, "detail": "Received. Read to get the reply."}
        if task_id:
            out["task"] = task_id
            if kind == TASK:
                out.update(state=ACCEPTED, detail="Task accepted. Read to follow it: each change of its state comes as a status message naming it.")
        return Answer(202, out)

    def _contact(self, conversation: Any, body: dict[str, Any]) -> Answer:
        """An offer of a contact, or the answer to one: kept for the owner and handed to the host as an event. No
        turn answers it, so it costs no turn; what the owner decides comes back as a message."""
        if conversation["state"] != OPEN:
            return Answer(409, {"status": "pending", "detail": "A contact is offered once the owner has admitted this conversation."}, 30)
        offer = contact_of(body.get("contact"))
        if offer is None:
            return Answer(400, {"status": "invalid", "detail": "contact carries key, name, invite, answer, at and sig, as a Daedalus writes them."})
        sent = sum(1 for m in self.store.transcript(conversation["id"], 200) if m["kind"] == CONTACT and m["role"] == GUEST)
        if sent >= CONTACTS_PER_CONVERSATION:
            return Answer(429, {"status": "busy", "stop_polling": True, "detail": "This conversation has carried enough offers of a contact."})
        text = inert(str(body.get("text") or ""))[:MAX_TEXT]
        message = self.store.add_guest_message(conversation, text, kind=CONTACT, data={"contact": offer}, state=ANSWERED)
        self.store.record(conversation["link_id"], "contact", json.dumps({"conversation": conversation["id"], "message": message, "contact": offer, "text": text[:600]}))
        self.notify()
        return Answer(202, {"status": "open", "id": message, "detail": "Kept for the owner, who answers in this conversation."})

    def _guest_status(self, conversation: Any, body: dict[str, Any]) -> Answer:
        """The one status a guest sends: cancelling a task it set. A turn already running on it is stopped by the host."""
        task = self.store.task(str(body.get("task") or ""))
        if task is None or task["conversation_id"] != conversation["id"]:
            return Answer(404, {"status": "invalid", "detail": "There is no such task in this conversation."})
        if body.get("state") != CANCELLED:
            return Answer(400, {"status": "invalid", "detail": 'The status you can send is {"kind": "status", "task": "<id>", "state": "cancelled"}.'})
        if not self.cancel_task(conversation, task, by=GUEST):
            return Answer(409, {"status": "invalid", "task": task["id"], "state": task["state"], "detail": f"That task is already {task['state']}."})
        return Answer(200, {"status": "open", "task": task["id"], "state": CANCELLED, "detail": "Task cancelled."})

    def cancel_task(self, conversation: Any, task: Any, *, by: str) -> bool:
        """Cancel ``task``: what waits for it goes, and the host hears, so a turn running on it stops."""
        if not self.store.move_task(task["id"], CANCELLED):
            return False
        for message in self.store.queued_of_task(task["id"]):
            self.store.drop(message["id"])
        self.store.record(conversation["link_id"], "task_cancelled", task["id"])
        words = "You cancelled this task." if by == GUEST else "The other side cancelled this task."
        self.store.add_reply(conversation["id"], DOOR, words, kind=STATUS, task_id=task["id"], data={"state": CANCELLED})
        self.wake(conversation["id"])
        self.notify()
        return True

    def shown(self, message: Any) -> dict[str, Any]:
        """A message as a guest reads it: its kind, and the ids that tie it to a task and to files."""
        data = json.loads(message["data"]) if message["data"] else {}
        sender = "door" if message["role"] == DOOR else BY_OWNER if data.get("from") == BY_OWNER else "agent"
        out: dict[str, Any] = {"id": message["id"], "from": sender, "kind": message["kind"] or TEXT, "text": message["text"], "at": message["at"]}
        if data.get("signed"):
            out["signed"] = data["signed"]
        if data.get("contact"):
            out["contact"] = data["contact"]
        if message["task_id"]:
            out["task"] = message["task_id"]
        if data.get("state"):
            out["state"] = data["state"]
        if data.get("files"):
            out["files"] = [self.desk.view(file) for file in (self.store.file(file_id) for file_id in data["files"]) if file is not None]
        return out

    def task_view(self, token: str, task_id: str) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        task = self.store.task(task_id)
        if task is None or task["conversation_id"] != conversation["id"]:
            return Answer(404, {"status": "invalid", "detail": "There is no such task in this conversation."})
        return Answer(200, {"task": task["id"], "state": task["state"], "created_at": task["created_at"], "updated_at": task["updated_at"]})

    # -- files ------------------------------------------------------------------------

    def _file_call(self, token: str, work: Callable[[Any, Any], Any], *, uploading: bool = False) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        link = self.store.link(conversation["link_id"])
        assert link is not None
        if link["mode"] == STAND_IN:
            return Answer(403, {"status": "refused", "detail": TEXT_ONLY})
        if conversation["state"] == CLOSED:
            return self._closed_answer(conversation)
        if uploading and conversation["state"] == PENDING:
            return Answer(409, {"status": "pending", "detail": "Files are taken once the owner admits this conversation."}, 30)
        try:
            return Answer(200, work(conversation, link))
        except FileRefused as exc:
            return Answer(exc.status, {"status": "refused", "code": exc.code, "detail": exc.detail, **exc.extra})

    def announce_file(self, token: str, body: Any) -> Answer | None:
        answer = self._file_call(token, lambda conversation, link: self.desk.announce(conversation, link, GUEST, body), uploading=True)
        if answer is not None and answer.status == 200:
            return Answer(201, {**answer.body, "detail": "Send the bytes with PUT to this file's address, in pieces of at most chunk_bytes, each at ?offset= the bytes sent so far."})
        return answer

    def own_file(self, conversation: Any, file_id: str) -> Any:
        file = self.store.file(file_id)
        if file is None or file["conversation_id"] != conversation["id"]:
            raise FileRefused(404, "no_file", "There is no such file in this conversation.")
        return file

    def put_piece(self, token: str, file_id: str, offset: int, data: bytes) -> Answer | None:
        def work(conversation: Any, _: Any) -> dict[str, Any]:
            file = self.own_file(conversation, file_id)
            if file["origin"] != GUEST:
                raise FileRefused(403, "not_yours", "That file is the other side's.")
            return self.desk.put(file, offset, data)

        return self._file_call(token, work, uploading=True)

    def file_view(self, token: str, file_id: str) -> Answer | None:
        return self._file_call(token, lambda conversation, _: self.desk.view(self.own_file(conversation, file_id)))

    def file_content(self, token: str, file_id: str, offset: int) -> tuple[Answer | None, bytes, Any]:
        """A piece of a file, or the answer that says why not. A body is never one long response: the front gives a
        connection under a minute, and a large file read in pieces can also be resumed."""
        found: list[Any] = []

        def work(conversation: Any, _: Any) -> dict[str, Any]:
            file = self.own_file(conversation, file_id)
            found.extend([self.desk.content(file, offset), file])
            return {}

        answer = self._file_call(token, work)
        if answer is None or answer.status != 200:
            return answer, b"", None
        return answer, found[0], found[1]

    def _unanswered(self, conversation_id: str) -> int:
        return sum(1 for m in self.store.transcript(conversation_id, 40) if m["role"] == GUEST and m["state"] in ("queued", "claimed"))

    async def read(self, token: str, *, after: int = 0, wait: int = DEFAULT_WAIT_SECONDS) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        conversation_id = conversation["id"]
        if self._polling.get(conversation_id, 0) >= MAX_POLLS:
            return Answer(429, {"status": "busy", "stop_polling": False, "detail": "Another read is already waiting on this conversation."}, RETRY_SECONDS)
        self._polling[conversation_id] = self._polling.get(conversation_id, 0) + 1
        try:
            return await self._read(conversation, after, wait)
        finally:
            self._polling[conversation_id] -= 1
            if not self._polling[conversation_id]:
                del self._polling[conversation_id]

    async def _read(self, conversation: Any, after: int, wait: int) -> Answer:
        link = self.store.link(conversation["link_id"])
        assert link is not None
        conversation_id = conversation["id"]
        deadline = time.monotonic() + max(0, min(wait, MAX_WAIT_SECONDS))
        waker = self._wakers.setdefault(conversation_id, asyncio.Event())
        while True:
            waker.clear()
            conversation = self.store.conversation(conversation_id)
            if conversation is None:
                return Answer(410, {"status": "closed", "stop_polling": True, "detail": STOP})
            fresh = self.store.link(conversation["link_id"])
            if fresh is None or fresh["state"] == REVOKED:
                # Asked of the link, not of the conversation, whose reason may already be "ended", and
                # before any unread reply: a revocation takes effect at once, on what is unread as well.
                return Answer(410, {"status": "closed", "reason": "revoked", "stop_polling": True, "detail": f"{CLOSED_WORDS['revoked']} {STOP}"})
            replies = self.store.replies_after(conversation_id, after)
            if replies:
                return self._delivered(conversation, replies)
            if conversation["state"] == CLOSED:
                return self._closed_answer(conversation)
            left = deadline - time.monotonic()
            if left <= 0:
                return self._quiet(conversation, link, after)
            try:
                await asyncio.wait_for(waker.wait(), left)
            except TimeoutError:
                return self._quiet(conversation, link, after)

    def _working(self, conversation_id: str) -> list[Any]:
        """Tasks the other side has in hand: while there are any, a guest has a reason to keep reading."""
        return [task for task in self.store.tasks_of(conversation_id) if task["state"] in (ACCEPTED, WORKING)]

    def _delivered(self, conversation: Any, replies: list[Any]) -> Answer:
        over = conversation["state"] == CLOSED
        owed = self.store.oldest_unanswered(conversation["id"]) is not None or bool(self._working(conversation["id"]))
        body = {
            "status": conversation["state"],
            "messages": [self.shown(m) for m in replies],
            "cursor": replies[-1]["id"],
            "stop_polling": over or not owed,
            "detail": "The conversation is closed; these are the last replies." if over else "Use cursor as after on your next read. Send a message, or end the conversation when your user's questions are answered.",
        }
        return Answer(200, body)

    def _quiet(self, conversation: Any, link: Any, after: int) -> Answer:
        """Nothing new. What that means decides whether the guest should keep waiting or stop."""
        now = self.clock()
        base = {"messages": [], "cursor": after}
        if conversation["state"] == PENDING:
            waited = now - conversation["created_at"]
            if waited > PENDING_POLL_SECONDS:
                return Answer(202, {**base, "status": "pending", "waiting_for": "owner", "stop_polling": True, "detail": "The owner has not answered yet. Stop polling and tell your user; this token stays good until they decide, so you can read again later."}, 300)
            return Answer(202, {**base, "status": "pending", "waiting_for": "owner", "stop_polling": False, "detail": "Waiting for the owner to admit this conversation."}, 30)
        oldest = self.store.oldest_unanswered(conversation["id"])
        tasks = self.store.tasks_of(conversation["id"])
        working = [task for task in tasks if task["state"] in (ACCEPTED, WORKING)]
        if oldest is None and working:
            latest = max(task["updated_at"] for task in working)
            if now - latest > TASK_PATIENCE_SECONDS:
                return Answer(200, {**base, "status": "open", "waiting_for": "task", "stop_polling": True, "detail": "A task has shown no change for a long while. Stop polling and tell your user. Your token stays good, so you can read again later."})
            return Answer(200, {**base, "status": "open", "waiting_for": "task", "stop_polling": False, "detail": "The other agent is working on your task. Read again."}, RETRY_SECONDS)
        if oldest is None and any(task["state"] == NEEDS_INPUT for task in tasks):
            return Answer(200, {**base, "status": "open", "waiting_for": "input", "stop_polling": True, "detail": "A task needs input: its last status message says what. Answer with a message that names the task, or read again later if it waits for the other side's owner."})
        if oldest is None:
            return Answer(200, {**base, "status": "open", "waiting_for": "nothing", "stop_polling": True, "detail": "You have every reply; the other agent may have chosen not to answer. Send another message, or end the conversation and tell your user what was said."})
        patience = STAND_IN_REPLY_SECONDS if link["mode"] == STAND_IN else DIRECT_REPLY_SECONDS
        if now - oldest["at"] > patience:
            return Answer(200, {**base, "status": "open", "waiting_for": "reply", "stop_polling": True, "detail": "No reply has come for a long while. Stop polling and tell your user. Your token stays good, so you can read again later."})
        return Answer(200, {**base, "status": "open", "waiting_for": "reply", "stop_polling": False, "detail": "The other agent is working on it. Read again."}, RETRY_SECONDS)

    def end(self, token: str) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        if conversation["state"] == CLOSED:
            return self._closed_answer(conversation) if conversation["reason"] != "ended" else Answer(200, {"status": "closed", "reason": "ended", "stop_polling": True, "detail": STOP})
        self.store.end(conversation)
        self.wake(conversation["id"])
        self.notify()
        return Answer(200, {"status": "closed", "reason": "ended", "stop_polling": True, "detail": "Conversation closed. Tell your user what was said."})

    def status(self, token: str) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        if conversation["state"] == CLOSED:
            return self._closed_answer(conversation)
        link = self.store.link(conversation["link_id"])
        assert link is not None
        today = int(conversation["turns_today"]) if conversation["turns_day"] == self.store._day() else 0
        # ``None`` where the owner set no such limit: a number would be read as one.
        left = None if int(link["turns_per_day"]) == NO_LIMIT else max(0, int(link["turns_per_day"]) - today)
        return Answer(200, {"status": conversation["state"], "expires_at": conversation["expires_at"] or None, "turns_left_today": left})


__all__ = [
    "AGENT", "DEFAULT_WAIT_SECONDS", "DIRECT_REPLY_SECONDS", "DOOR_MARK", "HELD", "KNOWN_CLIENTS", "MAX_TEXT", "MAX_WAIT_SECONDS", "OPEN", "QUESTION", "STAND_IN_REPLY_SECONDS",
    "TASK_PATIENCE_SECONDS", "TEXT_ONLY", "WORKING", "Answer", "Mailbox", "limit_answer",
]
