"""The control listener: how the host works this process, over a unix socket, with a token.

The host always dials. This process has no address for the host and no way to call it, so everything
it can do to the host is answer: a broken door can say wrong things, not reach further in. What crosses
for a direct link is one thing in each direction: a guest's words out through ``feed``, and a reply's
words back through ``reply``. The session a link belongs to and the tools a turn may use are not here
to be changed; they are in the host's database and this process never hears them.

The token that opens this socket is the host's: the host writes it into a directory this process can only
read, so nothing here can turn the host's own reading of it against the host.

On a listener of its own and in an application of its own, so no request to the public port can name a
route here: there is no such route over there.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

from .mailbox import Mailbox
from .public import read_body
from .standin import FAILED_TEXT
from .store import AGENT, ANSWERED, CLOSED, DIRECT, DOOR, FAILED, HELD, OPEN, PENDING, QUEUED, Store
from .text import inert, one_line

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

FEED_WAIT_SECONDS = 25
REPLY_MAX_CHARS = 16_000
"""A direct turn can write far more than anyone's agent wants to read through a poll."""
INTERRUPTED_TEXT = "The other agent was interrupted and did not answer this message. You can send it again."
DIRECT_TURN_USD = 0.25
"""What one direct turn may spend at most, and what is put aside for it. A share, so that turns running at
once on several links cannot each be handed everything that is left of the day."""

REQUIRED = {"label": str, "topic": str, "mode": str, "briefing": str, "model_path": str, "model_name": str, "guest_tools": str}
NUMBERS = {
    "price_in": (0.0, 10_000.0), "price_out": (0.0, 10_000.0), "usd_limit": (0.0, 10_000.0), "turns_per_day": (1, 100_000), "turns_total": (1, 1_000_000),
    "invite_hours": (0.1, 24.0 * 365), "conversation_hours": (0.1, 24.0 * 365), "idle_hours": (0.1, 24.0 * 365), "retention_days": (0.0, 3650.0),
}


class Invalid(ValueError):
    pass


def link_fields(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise Invalid("a JSON object is expected")
    fields: dict[str, Any] = {}
    for name, kind in REQUIRED.items():
        value = body.get(name)
        if not isinstance(value, kind):
            raise Invalid(f"{name} is required")
        fields[name] = value
    for name, (low, high) in NUMBERS.items():
        value = body.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
            raise Invalid(f"{name} must be a number from {low} to {high}")
        fields[name] = value
    if fields["mode"] not in ("stand_in", "direct"):
        raise Invalid("mode is stand_in or direct")
    if fields["mode"] == "stand_in" and not fields["briefing"].strip():
        raise Invalid("a stand-in needs a briefing")
    fields["label"] = one_line(fields["label"], 120)
    fields["topic"] = one_line(fields["topic"], 240)
    fields["briefing"] = inert(fields["briefing"])[:20_000]
    fields["approval"] = bool(body.get("approval", True))
    return fields


def view(store: Store, link: Any, *, full: bool = False) -> dict[str, Any]:
    conversation = store.conversation_of_link(link["id"])
    day_key_matches = link["turns_day"] == store._day()
    out: dict[str, Any] = {
        "id": link["id"], "state": link["state"], "label": link["label"], "topic": link["topic"], "mode": link["mode"], "model_name": link["model_name"],
        "approval": bool(link["approval"]), "usd_limit": link["usd_limit"], "priced": link["mode"] == DIRECT or float(link["price_in"]) + float(link["price_out"]) > 0, "turns_per_day": link["turns_per_day"], "turns_total": link["turns_total"],
        "guest_tools": link["guest_tools"], "created_at": link["created_at"], "invite_expires_at": link["invite_expires_at"], "conversation_hours": link["conversation_hours"],
        "idle_hours": link["idle_hours"], "retention_days": link["retention_days"], "spent_usd": link["spent_usd"] + link["reserved_usd"], "turns_used": link["turns_used"],
        "turns_today": link["turns_today"] if day_key_matches else 0, "refused_redeems": store.refused_redeems(link["id"]), "conversation": None,
    }
    if conversation is not None:
        out["conversation"] = {
            "id": conversation["id"], "state": conversation["state"], "reason": conversation["reason"], "created_at": conversation["created_at"],
            "decide_by": conversation["decide_by"], "expires_at": conversation["expires_at"], "last_guest_at": conversation["last_guest_at"],
        }
        if conversation["state"] == PENDING:
            held = [m for m in store.transcript(conversation["id"], 5) if m["state"] == HELD]
            out["conversation"]["first_message"] = held[0]["text"] if held else ""
    if full:
        out["briefing"] = link["briefing"]
        out["events"] = [{"id": e["id"], "kind": e["kind"], "at": e["at"]} for e in store.events_of(link["id"])]
        out["messages"] = [{"id": m["id"], "role": m["role"], "text": m["text"], "at": m["at"], "state": m["state"], "cost_usd": m["cost_usd"]} for m in store.transcript(conversation["id"])] if conversation is not None else []
    return out


class ControlDoor:
    """The whole ASGI application of the control socket."""

    def __init__(self, store: Store, mailbox: Mailbox, token: str) -> None:
        self.store = store
        self.mailbox = mailbox
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        try:
            status, body = await self.route(scope, receive)
        except Invalid as exc:
            status, body = 422, {"detail": str(exc)}
        except Exception:
            logger.exception("the control door failed on a request")
            status, body = 500, {"detail": "the door failed"}
        await JSONResponse(body, status_code=status)(scope, receive, send)

    def authorised(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                return hmac.compare_digest(value.decode("latin-1"), f"Bearer {self.token}")
        return False

    async def route(self, scope: Scope, receive: Receive) -> tuple[int, dict[str, Any]]:
        if not self.authorised(scope):
            return 401, {"detail": "no"}
        method, path = scope.get("method", "GET"), scope.get("path", "")
        body: Any = None
        if method in ("POST", "PUT"):
            raw = await read_body(receive) if not path.endswith("/links") else await read_large(receive)
            body = json.loads(raw) if raw else {}
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        store = self.store
        if (method, path) == ("GET", "/control/health"):
            return 200, {"ok": True, "usd_per_day": store.usd_per_day(), "today": store.spend_today(), "public_base": store.setting("public_base")}
        if (method, path) == ("PUT", "/control/settings"):
            return self.settings(body)
        if (method, path) == ("POST", "/control/links"):
            link_id, invite = store.create_link(link_fields(body))
            link = store.link(link_id)
            return 200, {"link": view(store, link), "invite": invite}
        if (method, path) == ("GET", "/control/links"):
            return 200, {"links": [view(store, link) for link in store.links()]}
        if (method, path) == ("GET", "/control/feed"):
            return 200, await self.feed(int((query.get("after") or ["0"])[0]), int((query.get("wait") or [str(FEED_WAIT_SECONDS)])[0]))
        if (method, path) == ("POST", "/control/deliveries/reset"):
            return 200, {"reset": self.reset()}
        if matched := re.fullmatch(r"/control/links/([0-9a-f]+)(?:/(revoke|reissue))?", path):
            return self.one_link(method, *matched.groups())
        if matched := re.fullmatch(r"/control/conversations/([0-9a-f]+)/(admit|refuse)", path):
            if method != "POST":
                return 405, {"detail": "POST"}
            return self.decide(*matched.groups())
        if matched := re.fullmatch(r"/control/deliveries/([0-9]+)/(reply|ack)", path):
            if method != "POST":
                return 405, {"detail": "POST"}
            if matched.group(2) == "ack":
                self.store.acknowledge(int(matched.group(1)))
                return 200, {"ok": True}
            return self.reply(int(matched.group(1)), body)
        return 404, {"detail": "no such route"}

    def settings(self, body: Any) -> tuple[int, dict[str, Any]]:
        if "usd_per_day" in body:
            value = body["usd_per_day"]
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise Invalid("usd_per_day must not be negative")
            self.store.set_setting("usd_per_day", str(float(value)))
        if "public_base" in body:
            self.store.set_setting("public_base", str(body["public_base"]).rstrip("/"))
        return 200, {"ok": True, "usd_per_day": self.store.usd_per_day(), "public_base": self.store.setting("public_base")}

    def one_link(self, method: str, link_id: str, action: str | None) -> tuple[int, dict[str, Any]]:
        store = self.store
        link = store.link(link_id)
        if link is None:
            return 404, {"detail": "no such link"}
        if action is None:
            return (200, {"link": view(store, link, full=True)}) if method == "GET" else (405, {"detail": "GET"})
        if method != "POST":
            return 405, {"detail": "POST"}
        conversation = store.conversation_of_link(link_id)
        if action == "reissue":
            # The same settings on a new invite, for a guest who lost their token. The old link goes
            # first, so at no moment do two invites speak for one set of settings.
            store.revoke(link_id)
            fields = {name: link[name] for name in (*REQUIRED, "price_in", "price_out", "usd_limit", "turns_per_day", "turns_total", "conversation_hours", "idle_hours", "retention_days", "approval")}
            fields["invite_hours"] = max(0.1, (link["invite_expires_at"] - link["created_at"]) / 3600)
            new_id, invite = store.create_link(fields)
            self._woken(conversation)
            return 200, {"link": view(store, store.link(new_id)), "invite": invite, "replaces": link_id}
        store.revoke(link_id)
        self._woken(conversation)
        return 200, {"link": view(store, store.link(link_id))}

    def _woken(self, conversation: Any) -> None:
        if conversation is not None:
            self.mailbox.wake(conversation["id"])
        self.mailbox.notify()

    def decide(self, conversation_id: str, decision: str) -> tuple[int, dict[str, Any]]:
        done = self.store.admit(conversation_id) if decision == "admit" else self.store.refuse(conversation_id)
        if not done:
            return 409, {"detail": "this conversation is not waiting for a decision"}
        self.mailbox.wake(conversation_id)
        self.mailbox.notify()
        return 200, {"ok": True}

    async def feed(self, after: int, wait: int) -> dict[str, Any]:
        """What happened since ``after``, and the guest messages of direct links, each handed out once.

        Held open until there is something, so a guest's message reaches the host at once without
        the host asking every second.
        """
        deadline = time.monotonic() + max(0, min(wait, FEED_WAIT_SECONDS))
        while True:
            # Cleared before looking, so what arrives after the look still wakes the wait below.
            self.mailbox.changed.clear()
            events = self.store.events_after(after)
            deliveries = self.claim()
            latest = self.store.last_event_id()
            if events or deliveries or latest < after or time.monotonic() >= deadline:
                return {
                    "events": [{"id": e["id"], "link_id": e["link_id"], "kind": e["kind"], "at": e["at"], "detail": e["detail"]} for e in events],
                    "deliveries": deliveries,
                    "cursor": events[-1]["id"] if events else after,
                    "latest": latest,
                }
            try:
                await asyncio.wait_for(self.mailbox.changed.wait(), min(1.0, max(0.05, deadline - time.monotonic())))
            except TimeoutError:
                pass

    def claim(self) -> list[dict[str, Any]]:
        """Hand out what waits on direct links, with the money for it put aside first.

        What was handed out before and never acknowledged goes out again, reservation and all: the
        answer to a feed can be lost, and one lost claim must not hold its conversation for ever. The
        host keeps the ids it has seen, so a message offered twice is run once.
        """
        store = self.store
        out = [self._offer(message, float(message["reserved_usd"])) for message in store.unacknowledged(DIRECT)]
        while (message := store.next_queued(DIRECT)) is not None:
            conversation = store.conversation(message["conversation_id"])
            assert conversation is not None
            link = store.link(conversation["link_id"])
            assert link is not None
            refusal, share = store.reserve_turn(message["id"], link, max(DIRECT_TURN_USD, float(link["usd_limit"]) / 4))
            if refusal is not None:
                store.drop(message["id"])
                store.record(link["id"], "limit", refusal.limit)
                store.add_reply(conversation["id"], DOOR, "A limit of this link was reached before this message could be answered. Stop here and tell your user.")
                self.mailbox.wake(conversation["id"])
                continue
            out.append(self._offer(message, share))
        return out

    def _offer(self, message: Any, share: float) -> dict[str, Any]:
        conversation = self.store.conversation(message["conversation_id"])
        assert conversation is not None
        return {"message_id": message["id"], "link_id": conversation["link_id"], "conversation_id": conversation["id"], "text": message["text"], "allowance_usd": share}

    def reply(self, message_id: int, body: Any) -> tuple[int, dict[str, Any]]:
        """The one way a word of the host's reaches a guest: the answer to the message that was claimed."""
        store = self.store
        message = store.message(message_id)
        if message is None or message["state"] != "claimed":
            return 409, {"detail": "that message is not waiting for a reply"}
        conversation = store.conversation(message["conversation_id"])
        assert conversation is not None
        cost = max(0.0, float(body.get("cost_usd") or 0.0))
        if body.get("ran") is False:
            store.give_back(message_id)
        else:
            # A turn that ran counts as a turn, whatever it cost and however it ended. Only the host's
            # plain word that it never started gives the count back.
            store.settle(message_id, cost=cost, state=ANSWERED if body.get("ok", True) else FAILED)
        # What was revoked or closed meanwhile stays closed: the reply goes nowhere.
        if conversation["state"] == OPEN and store.conversation(conversation["id"])["state"] == OPEN:
            if body.get("ok", True):
                text = inert(str(body.get("text") or ""))[:REPLY_MAX_CHARS]
                if text:
                    store.add_reply(conversation["id"], AGENT, text)
            else:
                store.add_reply(conversation["id"], DOOR, FAILED_TEXT)
        self.mailbox.wake(conversation["id"])
        return 200, {"ok": True}

    def reset(self) -> int:
        """The host has started: it remembers no turn it claimed before, so none of them will be answered.

        Failed rather than handed out again: a turn that was cut off may have run tools, and running the
        same guest message twice would do what it did twice. It costs what was put aside, since nobody
        can say any more what it really cost.
        """
        store = self.store
        stale = store.claimed_of(DIRECT)
        for message in stale:
            store.write_off(message["id"])
            conversation = store.conversation(message["conversation_id"])
            if conversation is not None and conversation["state"] == OPEN:
                store.add_reply(conversation["id"], DOOR, INTERRUPTED_TEXT)
                self.mailbox.wake(conversation["id"])
        return len(stale)


async def read_large(receive: Receive) -> bytes:
    """A link's briefing is the operator's and may be long; the socket is theirs too."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return bytes(body)
        body.extend(message.get("body", b""))
        if len(body) > 1_000_000 or not message.get("more_body", False):
            return bytes(body)


__all__ = ["CLOSED", "ControlDoor", "FEED_WAIT_SECONDS", "INTERRUPTED_TEXT", "Invalid", "QUEUED", "link_fields", "view"]
