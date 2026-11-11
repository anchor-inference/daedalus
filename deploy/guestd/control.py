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

from starlette.responses import JSONResponse, Response

from .files import CHUNK_BYTES, FileRefused
from .limits import Rate
from .mailbox import KEY, SIGNATURE, Mailbox, contact_of, signed_of
from .public import read_body
from .standin import FAILED_TEXT
from .store import (
    AGENT,
    ANSWERED,
    BY_OWNER,
    CANCELLED,
    CLOSED,
    CONTACT,
    DEFAULT_DAY_MB,
    DEFAULT_FILE_MB,
    DIRECT,
    DONE,
    DOOR,
    FAILED,
    FILE,
    HELD,
    NEEDS_INPUT,
    NO_LIMIT,
    OPEN,
    OPEN_TASK_STATES,
    OWNER,
    PENDING,
    QUESTION,
    QUEUED,
    REFUSED_TASK,
    STATUS,
    TEXT,
    WORKING,
    Store,
)
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
    "price_in": (0.0, 10_000.0), "price_out": (0.0, 10_000.0), "usd_limit": (0.0, 10_000.0), "turns_per_day": (0, 100_000), "turns_total": (0, 1_000_000),
    "invite_hours": (0.0, 24.0 * 365), "conversation_hours": (0.0, 24.0 * 365), "idle_hours": (0.0, 24.0 * 365), "retention_days": (0.0, 3650.0),
}
"""Each limit and lifetime may be :data:`~.store.NO_LIMIT`, which is none at all; a lifetime that is not is a few minutes at least."""
FILE_LIMITS = {"file_mb": DEFAULT_FILE_MB, "day_mb": DEFAULT_DAY_MB}
"""Optional, as the host may not name them: a link made without them has the defaults. Each may be 0, no limit."""
GUEST_LIMITS = {"max_guests": (1, 0, 10_000), "total_usd": (0.0, 0.0, 10_000.0)}
"""How many guests a link takes, and what they may spend together: optional, a link of one guest with no shared
budget by default. 0 is no limit for both, as for every other limit."""
LIFETIMES = ("invite_hours", "conversation_hours", "idle_hours")
HOST_KINDS = (TEXT, FILE, QUESTION, STATUS, CONTACT)
"""What the host may write into a conversation outside the answer to a turn: a progress line, files the operator let
go, a word of the host's own or of the operator's, an offer of a contact. A task is never set on a guest through
this door; a guest sets tasks here."""
HOST_SAYS_PER_MINUTE = 30
WORKING_TEXT = "The other agent is working on this task."
ROUTE_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+){0,3}")
ROUTE_MAX_SLUGS = 32


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
    for name, default in FILE_LIMITS.items():
        value = body.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1_000_000:
            raise Invalid(f"{name} is 0 for no limit, or a number of megabytes")
        fields[name] = value
    for name, (default, low, high) in GUEST_LIMITS.items():
        value = body.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
            raise Invalid(f"{name} is 0 for no limit, or a number up to {high:g}")
        fields[name] = int(value) if name == "max_guests" else float(value)
    for name in LIFETIMES:
        if fields[name] and fields[name] < 0.1:
            raise Invalid(f"{name} is 0 for none, or at least 0.1")
    if fields["mode"] not in ("stand_in", "direct"):
        raise Invalid("mode is stand_in or direct")
    if fields["mode"] == "stand_in" and not fields["briefing"].strip():
        raise Invalid("a stand-in needs a briefing")
    fields["label"] = one_line(fields["label"], 120)
    fields["topic"] = one_line(fields["topic"], 240)
    fields["briefing"] = inert(fields["briefing"])[:20_000]
    fields["approval"] = bool(body.get("approval", True))
    return fields


def conversation_view(store: Store, conversation: Any) -> dict[str, Any]:
    """One guest of a link: where its conversation stands, what it spent, and what it said it is at the redeem."""
    today = conversation["turns_today"] if conversation["turns_day"] == store._day() else 0
    out = {
        "id": conversation["id"], "state": conversation["state"], "reason": conversation["reason"], "created_at": conversation["created_at"],
        "decide_by": conversation["decide_by"], "expires_at": conversation["expires_at"], "last_guest_at": conversation["last_guest_at"],
        "client": conversation["client"], "client_key": conversation["client_key"], "client_proof": conversation["client_proof"],
        "spent_usd": conversation["spent_usd"] + conversation["reserved_usd"], "turns_used": conversation["turns_used"], "turns_today": today,
    }
    if conversation["state"] == PENDING:
        held = [m for m in store.transcript(conversation["id"], 5) if m["state"] == HELD]
        out["first_message"] = held[0]["text"] if held else ""
    return out


def view(store: Store, link: Any, *, full: bool = False, conversation_id: str = "") -> dict[str, Any]:
    """A link as the host reads it. ``conversation`` is the newest of its guests, the one a link of one guest has, and
    ``conversations`` all of them; a full view carries the transcript of ``conversation_id``, or of the newest."""
    every = store.conversations_of(link["id"])
    conversation = next((c for c in every if c["id"] == conversation_id), None) if conversation_id else (every[-1] if every else None)
    day_key_matches = link["turns_day"] == store._day()
    out: dict[str, Any] = {
        "id": link["id"], "state": link["state"], "label": link["label"], "topic": link["topic"], "mode": link["mode"], "model_name": link["model_name"], "model_path": link["model_path"],
        "approval": bool(link["approval"]), "usd_limit": link["usd_limit"], "priced": link["mode"] == DIRECT or float(link["price_in"]) + float(link["price_out"]) > 0, "turns_per_day": link["turns_per_day"], "turns_total": link["turns_total"],
        "guest_tools": link["guest_tools"], "created_at": link["created_at"], "invite_expires_at": link["invite_expires_at"], "conversation_hours": link["conversation_hours"],
        "idle_hours": link["idle_hours"], "retention_days": link["retention_days"], "spent_usd": link["spent_usd"] + link["reserved_usd"], "turns_used": link["turns_used"],
        "turns_today": link["turns_today"] if day_key_matches else 0, "refused_redeems": store.refused_redeems(link["id"]), "conversation": None,
        "file_mb": float(link["file_mb"]), "day_mb": float(link["day_mb"]), "files_today_bytes": store.bytes_today(link["id"]),
        "max_guests": int(link["max_guests"]), "total_usd": float(link["total_usd"]), "invite_hash": link["invite_hash"],
        "conversations": [conversation_view(store, c) for c in every],
    }
    if conversation is not None:
        out["conversation"] = conversation_view(store, conversation)
    if full:
        out["briefing"] = link["briefing"]
        out["events"] = [{"id": e["id"], "kind": e["kind"], "at": e["at"]} for e in store.events_of(link["id"])]
        out["messages"] = [message_view(m) for m in store.transcript(conversation["id"]) if m["role"] != OWNER] if conversation is not None else []
        out["tasks"] = [task_view(t) for t in store.tasks_of(conversation["id"])] if conversation is not None else []
        out["files"] = [{**file_view(f), "created_at": f["created_at"]} for f in store.files_of(conversation["id"])] if conversation is not None else []
    return out


def message_view(message: Any) -> dict[str, Any]:
    data = json.loads(message["data"]) if message["data"] else {}
    return {
        "id": message["id"], "role": message["role"], "text": message["text"], "at": message["at"], "state": message["state"], "cost_usd": message["cost_usd"],
        "kind": message["kind"] or TEXT, "task": message["task_id"], "task_state": data.get("state", ""), "files": list(data.get("files") or []),
        "by": data.get("from", ""), "signed": data.get("signed"), "contact": data.get("contact"),
    }


def task_view(task: Any) -> dict[str, Any]:
    return {"id": task["id"], "state": task["state"], "text": task["text"], "created_at": task["created_at"], "updated_at": task["updated_at"]}


def file_view(file: Any) -> dict[str, Any]:
    return {
        "id": file["id"], "name": file["name"], "size": int(file["size"]), "sha256": file["sha256"], "received": int(file["received"]), "state": file["state"],
        "from": file["origin"], "fetched": bool(file["fetched"]),
    }


def model_routes(body: Any) -> list[dict[str, Any]]:
    """The table of which providers a model is limited to, whole or refused: half a table would let the
    models missing from it be routed anywhere. Checked here too because it ends up in a call the key proxy
    judges by the same rule, and a refusal there would only read as a model that stopped answering."""
    if not isinstance(body, list):
        raise Invalid("model_routes is a list")
    routes: list[dict[str, Any]] = []
    for route in body:
        if not isinstance(route, dict) or not isinstance(route.get("path"), str) or not isinstance(route.get("model"), str):
            raise Invalid("a model route names a path and a model")
        only = route.get("only")
        if not isinstance(only, list) or not 0 < len(only) <= ROUTE_MAX_SLUGS or not all(isinstance(slug, str) and ROUTE_SLUG.fullmatch(slug) for slug in only):
            raise Invalid("a model route lists provider slugs")
        prices = [route.get("price_in"), route.get("price_out")]
        # None is a model the host cannot price today: the limit holds and the link's own price stands.
        if not all(price is None or (not isinstance(price, bool) and isinstance(price, int | float) and 0 <= price <= 10_000) for price in prices):
            raise Invalid("a model route has its prices")
        routes.append({"path": route["path"], "model": route["model"], "only": only, "price_in": prices[0], "price_out": prices[1]})
    return routes


class ControlDoor:
    """The whole ASGI application of the control socket."""

    def __init__(self, store: Store, mailbox: Mailbox, token: str) -> None:
        self.store = store
        self.mailbox = mailbox
        self.token = token
        self.says = Rate(HOST_SAYS_PER_MINUTE)
        """The host's own words per conversation: a progress line from a loop that never stops must not bury the answer."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        try:
            status, body = await self.route(scope, receive)
        except Invalid as exc:
            status, body = 422, {"detail": str(exc)}
        except FileRefused as exc:
            status, body = exc.status, {"detail": exc.detail, "code": exc.code, **exc.extra}
        except Exception:
            logger.exception("the control door failed on a request")
            status, body = 500, {"detail": "the door failed"}
        if isinstance(body, bytes):
            await Response(body, status_code=status, media_type="application/octet-stream")(scope, receive, send)
            return
        await JSONResponse(body, status_code=status)(scope, receive, send)

    def authorised(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                return hmac.compare_digest(value.decode("latin-1"), f"Bearer {self.token}")
        return False

    async def route(self, scope: Scope, receive: Receive) -> tuple[int, Any]:
        if not self.authorised(scope):
            return 401, {"detail": "no"}
        # Any call of the host's says it is there; a guest is told the owner is offline only after it has gone quiet.
        self.mailbox.host_seen = self.mailbox.clock()
        method, path = scope.get("method", "GET"), scope.get("path", "")
        body: Any = None
        if method == "PUT" and (piece := re.fullmatch(r"/control/files/(f_[0-9a-f]{16})", path)):
            raw = await read_body(receive, CHUNK_BYTES)
            if raw is None:
                return 413, {"detail": f"a piece is at most {CHUNK_BYTES} bytes"}
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            return self.put_piece(piece.group(1), int((query.get("offset") or ["0"])[0]), raw)
        if method in ("POST", "PUT"):
            raw = await read_body(receive) if not path.startswith("/control/links") else await read_large(receive)
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
        if matched := re.fullmatch(r"/control/links/([0-9a-f]+)/identity", path):
            return self.identity(matched.group(1), method, body)
        if (method, path) == ("GET", "/control/feed"):
            return 200, await self.feed(int((query.get("after") or ["0"])[0]), int((query.get("wait") or [str(FEED_WAIT_SECONDS)])[0]))
        if (method, path) == ("POST", "/control/deliveries/reset"):
            return 200, {"reset": self.reset()}
        if matched := re.fullmatch(r"/control/links/([0-9a-f]+)(?:/(revoke|reissue))?", path):
            return self.one_link(method, *matched.groups(), body, conversation_id=(query.get("conversation") or [""])[0])
        if matched := re.fullmatch(r"/control/conversations/([0-9a-f]+)/(admit|refuse)", path):
            if method != "POST":
                return 405, {"detail": "POST"}
            return self.decide(*matched.groups())
        if matched := re.fullmatch(r"/control/conversations/([0-9a-f]+)/(messages|resume|files)", path):
            if method != "POST":
                return 405, {"detail": "POST"}
            conversation_id, action = matched.groups()
            return {"messages": self.say, "resume": self.resume, "files": self.announce}[action](conversation_id, body if isinstance(body, dict) else {})
        if matched := re.fullmatch(r"/control/files/(f_[0-9a-f]{16})(?:/(content|fetched))?", path):
            return self.one_file(method, *matched.groups(), parse_qs(scope.get("query_string", b"").decode("latin-1")))
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
                raise Invalid("usd_per_day is 0 for no limit, or more")
            self.store.set_setting("usd_per_day", str(float(value)))
        if "public_base" in body:
            self.store.set_setting("public_base", str(body["public_base"]).rstrip("/"))
        if "model_routes" in body:
            self.store.set_setting("model_routes", json.dumps(model_routes(body["model_routes"])))
        return 200, {"ok": True, "usd_per_day": self.store.usd_per_day(), "public_base": self.store.setting("public_base")}

    def one_link(self, method: str, link_id: str, action: str | None, body: Any = None, *, conversation_id: str = "") -> tuple[int, dict[str, Any]]:
        store = self.store
        link = store.link(link_id)
        if link is None:
            return 404, {"detail": "no such link"}
        if action is None and method == "PUT":
            store.update_link(link_id, link_fields(body))
            self._woken(link_id)
            return 200, {"link": view(store, store.link(link_id), full=True)}
        if action is None:
            return (200, {"link": view(store, link, full=True, conversation_id=conversation_id)}) if method == "GET" else (405, {"detail": "GET or PUT"})
        if method != "POST":
            return 405, {"detail": "POST"}
        if action == "reissue":
            # The same settings on a new invite, for a guest who lost their token. The old link goes
            # first, so at no moment do two invites speak for one set of settings.
            store.revoke(link_id)
            fields = {
                name: link[name] for name in (
                    *REQUIRED, "price_in", "price_out", "usd_limit", "turns_per_day", "turns_total", "conversation_hours", "idle_hours", "retention_days", "approval", "file_mb", "day_mb",
                    "max_guests", "total_usd",
                )
            }
            fields["invite_hours"] = max(0.1, (link["invite_expires_at"] - link["created_at"]) / 3600) if link["invite_expires_at"] else NO_LIMIT
            new_id, invite = store.create_link(fields)
            self._woken(link_id)
            return 200, {"link": view(store, store.link(new_id)), "invite": invite, "replaces": link_id}
        store.revoke(link_id)
        self._woken(link_id)
        return 200, {"link": view(store, store.link(link_id))}

    def identity(self, link_id: str, method: str, body: Any) -> tuple[int, dict[str, Any]]:
        """The owner's key on a link and its signature over the invite, for the card to hand a guest."""
        if method != "PUT":
            return 405, {"detail": "PUT"}
        if self.store.link(link_id) is None:
            return 404, {"detail": "no such link"}
        key, proof = body.get("key") if isinstance(body, dict) else None, body.get("proof") if isinstance(body, dict) else None
        if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(proof, str) or not SIGNATURE.fullmatch(proof):
            raise Invalid("key and proof are a public key and a signature, base64url")
        self.store.set_identity(link_id, key, proof)
        return 200, {"ok": True}

    def _woken(self, link_id: str) -> None:
        for conversation in self.store.conversations_of(link_id):
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
                # A feed held open is the host waiting, not gone: seen at its end as well as its start.
                self.mailbox.host_seen = self.mailbox.clock()
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

    # -- what the host writes besides an answer ------------------------------------------

    def _open_conversation(self, conversation_id: str) -> Any:
        conversation = self.store.conversation(conversation_id)
        if conversation is None:
            raise FileRefused(404, "no_conversation", "no such conversation")
        if conversation["state"] != OPEN:
            raise FileRefused(409, "closed", "this conversation is not open")
        return conversation

    def say(self, conversation_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """A message of the host's own that answers no claimed turn: a task's progress or its new state, files the
        operator let go after the turn that made them had ended, a word of the host's. No model turn is counted:
        the turn that wrote any of it already was."""
        conversation = self._open_conversation(conversation_id)
        kind = body.get("kind") or TEXT
        if kind not in HOST_KINDS:
            raise Invalid(f"kind is one of {', '.join(HOST_KINDS)}")
        if not self.says.allow(conversation_id):
            return 429, {"detail": "the host has said a lot in this conversation in the last minute"}
        text = inert(str(body.get("text") or ""))[:REPLY_MAX_CHARS]
        data: dict[str, Any] = self._marks(body)
        if kind == CONTACT:
            offer = contact_of(body.get("contact"))
            if offer is None:
                raise Invalid("a contact message carries its contact")
            data["contact"] = offer
        task_id = str(body.get("task") or "")
        if task_id:
            task = self.store.task(task_id)
            if task is None or task["conversation_id"] != conversation_id:
                raise FileRefused(404, "no_task", "no such task in this conversation")
            if kind == STATUS:
                state = body.get("state")
                if state not in (WORKING, NEEDS_INPUT, DONE, REFUSED_TASK, CANCELLED):
                    raise Invalid("a status names its state")
                if state == CANCELLED:
                    done = self.mailbox.cancel_task(conversation, task, by=AGENT)
                    return (200, {"ok": True}) if done else (409, {"detail": f"that task is already {task['state']}"})
                if state != task["state"] and not self.store.move_task(task_id, state):
                    return 409, {"detail": f"that task is already {task['state']}"}
                if state == task["state"]:
                    # A progress line moves nothing, but it says the task is alive.
                    self.store.move_task(task_id, state)
                data["state"] = state
        elif kind == STATUS:
            raise Invalid("a status names its task")
        if body.get("files"):
            data["files"] = [file["id"] for file in self.mailbox.desk.ready_of(conversation_id, body.get("files"), AGENT)]
        if not text and not data.get("files") and kind not in (STATUS, CONTACT):
            raise Invalid("say something, or send files")
        message_id = self.store.add_reply(conversation_id, AGENT, text, kind=kind, task_id=task_id, data=data)
        self.mailbox.wake(conversation_id)
        return 200, {"ok": True, "message_id": message_id}

    @staticmethod
    def _marks(body: Any) -> dict[str, Any]:
        """Who wrote a message the host sends, when it was the operator, and the host's signature over it."""
        data: dict[str, Any] = {}
        if isinstance(body, dict) and body.get("from") == BY_OWNER:
            data["from"] = BY_OWNER
        signed = signed_of(body.get("signed")) if isinstance(body, dict) else None
        if signed is not None:
            data["signed"] = signed
        return data

    def resume(self, conversation_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """A turn for an answer the operator gave: queued like a guest's message, so it is counted and bounded."""
        self._open_conversation(conversation_id)
        task_id = str(body.get("task") or "")
        if task_id:
            task = self.store.task(task_id)
            if task is None or task["conversation_id"] != conversation_id or task["state"] not in OPEN_TASK_STATES:
                return 409, {"detail": "that task takes no more turns"}
        ask = str(body.get("ask") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ask):
            raise Invalid("ask names the operator's answer")
        message_id = self.store.add_resume(conversation_id, task_id, {"ask": ask})
        self.mailbox.notify()
        return 200, {"ok": True, "message_id": message_id}

    def announce(self, conversation_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        conversation = self._open_conversation(conversation_id)
        link = self.store.link(conversation["link_id"])
        assert link is not None
        return 200, self.mailbox.desk.announce(conversation, link, AGENT, body)

    def put_piece(self, file_id: str, offset: int, data: bytes) -> tuple[int, dict[str, Any]]:
        file = self.store.file(file_id)
        if file is None or file["origin"] != AGENT:
            return 404, {"detail": "no such file of the host's"}
        return 200, self.mailbox.desk.put(file, offset, data)

    def one_file(self, method: str, file_id: str, action: str | None, query: dict[str, list[str]]) -> tuple[int, Any]:
        file = self.store.file(file_id)
        if file is None:
            return 404, {"detail": "no such file"}
        if action is None and method == "GET":
            return 200, file_view(file)
        if action == "content" and method == "GET":
            return 200, self.mailbox.desk.content(file, int((query.get("offset") or ["0"])[0]))
        if action == "fetched" and method == "POST":
            if file["origin"] == AGENT:
                return 409, {"detail": "that file is the host's own"}
            self.mailbox.desk.fetched(file)
            return 200, {"ok": True}
        return 405, {"detail": "wrong method"}

    def claim(self) -> list[dict[str, Any]]:
        """Hand out what waits on direct links, with the money for it put aside first.

        What was handed out before and never acknowledged goes out again, reservation and all: the
        answer to a feed can be lost, and one lost claim must not hold its conversation for ever. The
        host keeps the ids it has seen, so a message offered twice is run once.
        """
        store = self.store
        out = [self._offer(message, self._put_aside(message)) for message in store.unacknowledged(DIRECT)]
        while (message := store.next_queued(DIRECT)) is not None:
            conversation = store.conversation(message["conversation_id"])
            assert conversation is not None
            link = store.link(conversation["link_id"])
            assert link is not None
            task = store.task(message["task_id"]) if message["task_id"] else None
            if task is not None and task["state"] not in OPEN_TASK_STATES:
                store.drop(message["id"])
                continue
            refusal, share = store.reserve_turn(message["id"], link, self.ceiling(link))
            if refusal is not None:
                store.drop(message["id"])
                store.record(link["id"], "limit", refusal.limit)
                store.add_reply(conversation["id"], DOOR, "A limit of this link was reached before this message could be answered. Stop here and tell your user.", data=answers(message))
                self.mailbox.wake(conversation["id"])
                continue
            if task is not None and task["state"] != WORKING and store.move_task(task["id"], WORKING):
                # Said by the door at once, without a word of the model's: the guest knows its task is in hand.
                store.add_reply(conversation["id"], DOOR, WORKING_TEXT, kind=STATUS, task_id=task["id"], data={"state": WORKING})
                self.mailbox.wake(conversation["id"])
            out.append(self._offer(message, share))
        return out

    def ceiling(self, link: Any) -> float | None:
        """A direct turn's share of what it may spend: a quarter of whichever limit bounds it, and ``None`` with none."""
        bound = float(link["usd_limit"]) or self.store.usd_per_day()
        return max(DIRECT_TURN_USD, bound / 4) if bound != NO_LIMIT else None

    def _put_aside(self, message: Any) -> float | None:
        """What a turn offered again was given the first time: ``None`` where nothing bounded it then."""
        conversation = self.store.conversation(message["conversation_id"])
        link = self.store.link(conversation["link_id"]) if conversation is not None else None
        reserved = float(message["reserved_usd"])
        return reserved if reserved or link is None or self.ceiling(link) is not None else None

    def _offer(self, message: Any, share: float | None) -> dict[str, Any]:
        conversation = self.store.conversation(message["conversation_id"])
        assert conversation is not None
        data = json.loads(message["data"]) if message["data"] else {}
        files = [file_view(file) for file in (self.store.file(file_id) for file_id in data.get("files") or []) if file is not None]
        return {
            "message_id": message["id"], "link_id": conversation["link_id"], "conversation_id": conversation["id"], "text": message["text"], "allowance_usd": share,
            "role": message["role"], "kind": message["kind"] or TEXT, "task": message["task_id"], "files": files, "ask": str(data.get("ask") or ""),
            "by": str(data.get("from") or ""), "signed": data.get("signed"), "client_key": conversation["client_key"],
        }

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
            self._answer(conversation, message, body)
        self.mailbox.wake(conversation["id"])
        return 200, {"ok": True}

    def _answer(self, conversation: Any, message: Any, body: Any) -> None:
        """What a turn said, as the guest will read it. A turn on a task ends in the task's next state, with its words
        and files on that status; a task cancelled while the turn ran stays cancelled, and the turn's words go nowhere."""
        store = self.store
        ok = body.get("ok", True)
        text = inert(str(body.get("text") or ""))[:REPLY_MAX_CHARS] if ok else ""
        marks = self._marks(body) if ok else {}
        try:
            files = [file["id"] for file in self.mailbox.desk.ready_of(conversation["id"], body.get("files"), AGENT)] if ok and body.get("files") else []
        except FileRefused:
            logger.warning("a reply named files the door does not hold whole; sent without them")
            files = []
        task = store.task(message["task_id"]) if message["task_id"] else None
        if task is not None:
            if task["state"] not in OPEN_TASK_STATES:
                return
            wanted = body.get("task_state") if body.get("task_state") in (DONE, NEEDS_INPUT, REFUSED_TASK) else DONE if ok else REFUSED_TASK
            store.move_task(task["id"], wanted)
            store.add_reply(conversation["id"], AGENT if ok else DOOR, text if ok else FAILED_TEXT, kind=STATUS, task_id=task["id"], data={"state": wanted, **({"files": files} if files else {}), **marks})
            return
        if not ok:
            store.add_reply(conversation["id"], DOOR, FAILED_TEXT, data=answers(message))
        elif files:
            store.add_reply(conversation["id"], AGENT, text, kind=FILE, data={"files": files, **marks, **answers(message)})
        elif text:
            store.add_reply(conversation["id"], AGENT, text, data={**marks, **answers(message)} or None)

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
                task = store.task(message["task_id"]) if message["task_id"] else None
                if task is not None and store.move_task(task["id"], REFUSED_TASK):
                    # A task whose turn was cut off would otherwise say "working" for ever.
                    store.add_reply(conversation["id"], DOOR, INTERRUPTED_TEXT, kind=STATUS, task_id=task["id"], data={"state": REFUSED_TASK})
                else:
                    store.add_reply(conversation["id"], DOOR, INTERRUPTED_TEXT, data=answers(message))
                self.mailbox.wake(conversation["id"])
        return len(stale)


def answers(message: Any) -> dict[str, Any]:
    """Which guest message a reply answers, for a face that shows a message and its answer as one thing (A2A's task).
    A guest's own message only: a turn the host asked for to carry the owner's answer is nobody's question."""
    return {"answers": int(message["id"])} if message["role"] != OWNER else {}


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


__all__ = ["CLOSED", "HOST_KINDS", "conversation_view", "WORKING_TEXT", "ControlDoor", "FEED_WAIT_SECONDS", "INTERRUPTED_TEXT", "Invalid", "QUEUED", "file_view", "link_fields", "message_view", "task_view", "view"]
