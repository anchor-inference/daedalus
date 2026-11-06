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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import pages
from .store import AGENT, CLOSED, DOOR, EXPIRED, GUEST, HELD, OPEN, PENDING, REVOKED, STAND_IN, WAITING, Refusal, Store
from .text import inert

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
PENDING_POLL_SECONDS = 300.0
"""How long a guest is told to wait for the owner in one go. The token stays good after that."""
RETRY_SECONDS = 15

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
    def __init__(self, store: Store, *, public_base: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.public_base = public_base
        self.clock = clock
        self._wakers: dict[str, asyncio.Event] = {}
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

    def page(self, invite: str) -> tuple[int, str] | None:
        """The page behind an invite, or ``None`` for an invite this door never made.

        A GET changes nothing at all, not even a counter. Link previews and scanners fetch whatever
        passes through a messenger, and an invite a preview could burn would arrive already spent.
        """
        link = self.store.link_by_invite(invite)
        if link is None:
            return None
        if link["state"] == WAITING:
            if link["invite_expires_at"] < self.clock():
                return 410, pages.GONE_PAGE
            return 200, pages.page(link, base=self.public_base() or "", invite=invite, max_text=MAX_TEXT)
        return 410, pages.SPENT_PAGE if link["state"] not in (REVOKED, EXPIRED) else pages.GONE_PAGE

    def redeem(self, invite: str, *, address: str = "") -> Answer | None:
        """Spend the invite and bind a token, or ``None`` for an invite this door never made.

        A second redeem always gets a refusal, even from the same client seconds later: there is no
        window in which it would return the token. The kind reply would be welcome to an agent that
        lost it, which happened in the trial. But the concern here is a stolen invite, and the client
        that could use such a window is exactly the one sharing an address with the real guest: an
        agent platform's egress, a company's NAT. "Same address and same curl" identifies nothing, and
        a thief handed the same token would sit silently inside a conversation the owner has admitted
        for someone else. Instead the refusal stays visible to the owner as evidence, the owner can
        issue a new link with one action, and the wording keeps the guest's agent from hunting.
        """
        link = self.store.link_by_invite(invite)
        if link is None:
            return None
        if link["state"] in (REVOKED, EXPIRED) or (link["state"] == WAITING and link["invite_expires_at"] < self.clock()):
            return Answer(410, {"status": "closed", "stop_polling": True, "detail": "This link is closed. Stop here and ask your user for a new link."})
        spent = None if link["state"] != WAITING else self.store.redeem(link, address)
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
        if conversation["state"] == PENDING:
            body["detail"] = "The owner admits a first contact by hand. You can send your first message now; it is answered once they agree."
            return Answer(201, body, 30)
        return Answer(201, body)

    # -- a conversation -----------------------------------------------------------------

    def _closed_answer(self, conversation: Any) -> Answer:
        reason = str(conversation["reason"] or "ended")
        return Answer(410, {"status": "closed", "reason": reason, "stop_polling": True, "detail": f"{CLOSED_WORDS.get(reason, 'This conversation is closed.')} {STOP}"})

    def send(self, token: str, text: Any) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
        if conversation["state"] == CLOSED:
            return self._closed_answer(conversation)
        link = self.store.link(conversation["link_id"])
        assert link is not None
        if not isinstance(text, str) or not text.strip():
            return Answer(400, {"status": "invalid", "detail": 'Send JSON with one field: {"text": "your message"}.'})
        clean = inert(text)
        if not clean:
            return Answer(400, {"status": "invalid", "detail": "That message has no readable text."})
        if len(clean) > MAX_TEXT:
            return Answer(413, {"status": "too_long", "detail": f"A message may be at most {MAX_TEXT} characters."})
        if conversation["state"] == PENDING:
            if self.store.held_count(conversation["id"]) >= 1:
                return Answer(202, {"status": "pending", "detail": "Your first message is with the owner. Nothing more is taken until they answer."}, 30)
            message = self.store.add_guest_message(conversation, clean)
            self.notify()
            return Answer(202, {"status": "pending", "id": message, "detail": "Kept for the owner, who decides whether to admit this conversation. Read to see when they do."}, 30)
        refusal = self.store.limit_reached(link)
        if refusal is not None:
            self.store.record(link["id"], "limit", refusal.limit)
            self.notify()
            return limit_answer(refusal, now=self.clock())
        if self._unanswered(conversation["id"]) >= MAX_UNANSWERED:
            return Answer(429, {"status": "busy", "detail": "Earlier messages are still waiting for an answer. Read first."}, RETRY_SECONDS)
        message = self.store.add_guest_message(conversation, clean)
        self.notify()
        return Answer(202, {"status": "open", "id": message, "detail": "Received. Read to get the reply."})

    def _unanswered(self, conversation_id: str) -> int:
        return sum(1 for m in self.store.transcript(conversation_id, 40) if m["role"] == GUEST and m["state"] in ("queued", "claimed"))

    async def read(self, token: str, *, after: int = 0, wait: int = DEFAULT_WAIT_SECONDS) -> Answer | None:
        conversation = self.store.conversation_by_token(token)
        if conversation is None:
            return None
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
            if conversation["reason"] == "revoked":
                # Before any unread reply: a revocation takes effect at once, on what is unread as well.
                return self._closed_answer(conversation)
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

    def _delivered(self, conversation: Any, replies: list[Any]) -> Answer:
        over = conversation["state"] == CLOSED
        owed = self.store.oldest_unanswered(conversation["id"]) is not None
        body = {
            "status": conversation["state"],
            "messages": [{"id": m["id"], "from": "door" if m["role"] == DOOR else "agent", "text": m["text"], "at": m["at"]} for m in replies],
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
        return Answer(200, {"status": conversation["state"], "expires_at": conversation["expires_at"], "turns_left_today": max(0, int(link["turns_per_day"]) - (int(link["turns_today"]) if link["turns_day"] == self.store._day() else 0))})


__all__ = [
    "AGENT", "DEFAULT_WAIT_SECONDS", "DIRECT_REPLY_SECONDS", "HELD", "MAX_TEXT", "MAX_WAIT_SECONDS", "OPEN", "STAND_IN_REPLY_SECONDS",
    "Answer", "Mailbox", "limit_answer",
]
