"""This installation's agent as the guest of somebody else's door.

A client of the door's ``/a/*`` and nothing more: the same redeem, the same send, the same read a curl agent
makes. What a Daedalus adds is only that it is still running when the answer comes. So one task per conversation
holds the door's long read while something is owed, stores what arrives, and everything else reads from that
store: the tool in a turn, the app, and a turn opened by a reply. Nothing is lost to a restart on either side,
because the cursor and the unread replies are rows.

**Everything from the far side is data.** A door is a stranger's server. Only the fields named here are read, each
checked for what it should be; its ``detail`` sentences are never shown to anyone, because a sentence chosen by
that server would reach the model outside a fence. What the agent is told about the state of a conversation is
written here from an enumerated code. A reply reaches a model only through :func:`agent_talk.fence_reply`, and
the moment it does the run is touched (:mod:`daedalus.host.agent_talk`).

**What starts a conversation.** A link in a chat does nothing. The agent has to call ``open``, in a run of the
operator's, with a link the operator wrote in this chat: an address that came in a reply, a page or a file
does not open, so a stranger cannot point the agent at a door of their choosing. The first GET asks only
what the door says it is; the redeem is the agent's deliberate step.

**The token.** It lives in one column, goes out in one header, and leaves no other way: not to the model, the app
or the log. It belongs to the chat that opened the link, so a conversation is only ever found through its
session, and the column is emptied when the conversation closes.

**What goes out.** ``first`` by default: the first message of a conversation waits for the operator, who then
sees who this is and how the agent means to speak to it, and the rest flow so that a dialogue stays one. A later
message waits too when the run went and looked at something after reading a reply, or when it looks like a
secret: that is the shape of the leak a stranger would ask for. ``all`` and ``off`` are the operator's to choose,
per conversation. Approved text is what is sent, byte for byte, by this code and not by a retried tool call.

**Money.** By default a reply is waited for inside a turn, for a limited time, and after that it waits here and
costs nothing. Waking the chat on a reply is switched on per conversation by the operator, from the app, with a
number of turns and a number of dollars; the agent has no way to do it. Such a turn is bounded as a guest's is.

**Loops.** Sends per conversation are capped, a reply that repeats itself turns waking off, a conversation
nobody spoke in for a day is ended, and the door keeps its own limits on its own side.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json as jsonlib
import logging
import math
import os
import re
import secrets
import socket
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from daedalus.extensions import link_flows as flows_module
from daedalus.extensions.guest_door import bridge_of, chat_link, file_view, identity_of, task_view
from daedalus.extensions.notifications import Draft
from daedalus.host import agent_talk, guest_turns, link_flows, link_identity, link_protocol
from daedalus.host.guest_turns import GuestTurn, GuestTurnBusy
from daedalus.host.link_protocol import LinkFileError, Staged
from daedalus.host.notify_text import render
from daedalus.host.prompts import without_host_notes
from daedalus.security.untrusted import inert

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionState

logger = logging.getLogger(__name__)

EXTENSION_KEY = "agent_talk"
ASK_HOOK = "agent_talk.send"
CLIENT_KIND = "daedalus"
"""What the door is told this client is: for its owner's list, and it opens nothing there."""

MAX_ANSWER_BYTES = 256 * 1024
"""A door's whole answer. A message is at most 8,000 characters on an honest one, a few of them in one read."""
MAX_TEXT = 8000
MAX_TOPIC = 240
MAX_PER_READ = 20
MAX_READ_CHARS = 16_000
"""What one tool result or one wake takes in. What is past it stays unread: a tool's result is clipped at its
budget, so a reply marked read and then cut would be lost to the model, and a turn opened on a pile of them would
cost past what its first call was priced at."""
CARD_CHARS = 600
MAX_MESSAGES = 300
"""Everything kept of a conversation. A door that talks without end must not fill this database."""
MAX_SENDS = 30
"""Messages this side sends in one conversation unless the operator sets another number or none (0, the column's
``sends_limit``). Past it the operator starts a new one, with a new link, or lifts the limit."""
NO_LIMIT = -1
"""``wake_turns`` and ``wake_usd`` with no limit: waking on every reply, at no cap but the one on any run. Not 0,
which is waking switched off and a spent budget, as it always was."""
DOOR_WAIT_SECONDS = 25
"""How long one read is held at the door, under the door's own limit of 30."""
CALL_SECONDS = 15.0
MIN_CYCLE_SECONDS = 5.0
"""The least a read takes, whatever the door does: one that answers at once must not get a tight loop out of this."""
QUIET_POLL_SECONDS = 300.0
"""How often to look once the door has said it is no use waiting: a reply can still come, it costs a request."""
FAILURE_BACKOFF_MAX = 300.0
SWEEP_SECONDS = 60.0
IDLE_SECONDS = 24 * 3600.0
WAIT_DEFAULT = 120
WAIT_MAX = 300
CHAT_FREE_SECONDS = 900.0
REPLY_NOTICE_SECONDS = 3600.0
REPEATS_ALLOWED = 2
TURN_USD = guest_turns.TURN_USD
WAKE_TURNS_MAX = 20
WAKE_USD_MAX = 5.0
RUNS_KEPT = 12
"""Runs of a conversation a turn opened by a reply reads: the most recent, each a few messages."""
SIDE = "talk"
"""``link_files.side`` and ``link_tasks.side`` for a conversation of this side's at somebody else's door."""
TASK_WAKES_MAX = 3
"""Turns the changes of the tasks this side set may still open when waking is off: each task set adds one. A task's
result is what the agent set it for, and it should not wait for the operator to ask; three is a few tasks in flight,
not a conversation that runs itself."""
SECRET_SCAN_BYTES = 1024 * 1024
"""How much of a file going out is read for a secret: a text file of that size is a config or a log, where one is."""
PIECE_SECONDS = 60.0
FILE_WORDS = {
    "file_limit": "a file is larger than that link takes",
    "day_limit": "that link's files for today are used up",
    "door_full": "that door has no room for files now",
}

ORIGINS = frozenset({"operator", "core", "events", "autopilot"})
"""Whose runs may use the tool: the operator's and those that continue one. A schedule or a loop reads with nobody there."""

REVIEWS = ("first", "all", "off")
OPENING, PENDING, OPEN, CLOSED = "opening", "pending", "open", "closed"
US, THEM, DOOR = "us", "them", "door"
HELD, SENDING, SENT, REFUSED, FAILED, UNSURE, UNREAD, READ = "held", "sending", "sent", "refused", "failed", "unsure", "unread", "read"
WAITING = "waiting_door"
"""A message the door was not there to take: kept here and tried again by itself (``attempts``, ``next_at``)."""
OFFLINE = "offline"
"""``agent_talks.waiting_for`` while the other side is away: its door does not answer, or its owner's agent is not running."""
RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)
"""How long a message waits before its next attempt, by the attempts already made, and hourly past the end: the first
few catch a server whose agent restarts, the later ones a desktop that was closed for the night."""
GIVE_UP_SECONDS = 72 * 3600.0
"""How long a message waits for a door before it is failed: a weekend away, and not a conversation nobody remembers."""
OFFLINE_AFTER = 2
"""Reads in a row that find nobody before the conversation shows the other side as offline: one dropped long read is
the network, not a door that has gone."""
ABSENT_STATUSES = frozenset({502, 503, 521, 522, 523, 525, 526, 530})
"""What a front says when it could not reach the door at all: its own 502 when the door's socket is closed, a proxy's
503, and Cloudflare's codes for an origin it could not connect to or a tunnel with nobody at the other end. The message
never reached the door, so sending it again cannot say it twice."""
UNKNOWN_STATUSES = frozenset({504, 520, 524})
"""What a front says when it gave up waiting on a door it had reached, or got something it could not read back:
the door may have taken the message, so these stay "may or may not have arrived" whatever page comes with them."""
REASONS = ("ended", "expired", "idle", "refused", "approval_timeout", "revoked")
"""The door's reasons this code has words for. Anything else reads as plain ``ended``."""

CLOSED_WORDS = {
    "ended": "The conversation has ended.",
    "expired": "The conversation reached the end of its lifetime.",
    "idle": "The conversation was closed because nobody spoke for too long.",
    "refused": "The owner of that link declined this conversation.",
    "approval_timeout": "The owner of that link did not admit this conversation in time.",
    "revoked": "The owner of that link revoked it.",
    "token": "The door no longer takes this conversation's token.",
    "too_long": "The conversation grew past what is kept here, so it was closed.",
}
LIMIT_WORDS = {
    "usd_per_link": "That link's budget is used up.",
    "usd_per_day": "Its owner's budget for guests is used up for today.",
    "turns_per_day": "That link's messages are used up for today.",
    "turns_total": "That link's messages are used up.",
}
HELD_WORDS = {
    "first": "the first message of a conversation",
    "all": "the operator reviews every message of this conversation",
    "looked": "this turn read or ran something after a stranger's reply",
    "secret": "it looks as if it holds a secret",
    "review": "the automatic review of what goes out did not pass it",
    "outside": "a file comes from outside this chat's folder",
}
KEPT_WORDS = "The other side is offline; the message is kept and will be sent when it is back. You will be woken when it is delivered or given up."
OWNER_AWAY_WORDS = "Received and kept; the other side's owner is offline, the reply comes when they are back."


class NotReachable(Exception):
    """The address is one this client will not dial, whatever is there. Says why, in this code's words."""


class DoorGone(Exception):
    """The door did not answer. Nothing can be said about what it did with what it was sent, unless ``unsent``: the
    name did not resolve or nothing took the connection, so nothing was sent at all."""

    def __init__(self, why: str = "", *, unsent: bool = False) -> None:
        super().__init__(why)
        self.unsent = unsent


@dataclass(frozen=True, slots=True)
class DoorAnswer:
    status: int
    body: dict[str, Any]
    retry_after: float | None = None
    parsed: bool = True
    """Whether the answer was a JSON object. A door always answers with one; a page of HTML is a tunnel's or a
    proxy's, standing where the door should be."""


def door_absent(answer: DoorAnswer) -> bool:
    """Whether something in front of the door answered for it, so that the door cannot have taken what was sent."""
    if answer.status in UNKNOWN_STATUSES:
        return False
    return answer.status in ABSENT_STATUSES or not answer.parsed


@dataclass(frozen=True, slots=True)
class _Answered:
    status_code: int
    retry: float | None


@dataclass(frozen=True, slots=True)
class Outcome:
    text: str
    error: bool = False
    talk: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _seconds(value: Any) -> float | None:
    """A wait a door asked for, within what this side will honour."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None  # ``nan`` passes every comparison, and a sleep of it takes the floor off the loop
    return min(max(float(value), 0.0), QUIET_POLL_SECONDS)


def _hash(invite: str) -> str:
    return hashlib.sha256(invite.encode()).hexdigest()


def ask_key(message_id: int) -> str:
    return hashlib.sha256(f"agent-talk:{message_id}".encode()).hexdigest()[:12]


def _same(text: str) -> str:
    return " ".join(text.lower().split())


class HideDoorPaths(logging.Filter):
    """``httpx`` logs every address it calls at INFO, and a door's address before the redeem is a credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/a/" not in record.getMessage()


async def public_addresses(host: str, port: int) -> list[str]:
    """Where ``host`` resolves to, refused unless all of it is on the public internet.

    All and not any: a name with one public and one private address is exactly how a stranger aims a client
    at the inside of its own network, and the choice among them is the resolver's.
    """
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DoorGone(f"{host} does not resolve", unsent=True) from exc
    addresses = list(dict.fromkeys(str(info[4][0]) for info in found))
    for address in addresses:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
        mapped = getattr(parsed, "ipv4_mapped", None)
        if not (mapped or parsed).is_global:
            raise NotReachable(f"{host} is not on the public internet")
    if not addresses:
        raise DoorGone(f"{host} does not resolve", unsent=True)
    return addresses


class Dialer:
    """One call to a stranger's address, and nothing in it trusted.

    The name is resolved here and the connection is made to the address that was checked, with the name kept
    for TLS: resolved twice, the second answer is the attacker's to choose. Redirects are not followed, the
    answer is read only up to :data:`MAX_ANSWER_BYTES`, and the environment's proxy settings are not read. A
    proxy the operator set for web tools is used, and then the name goes to it unchecked past the first look.
    """

    def __init__(self, *, private: bool = False, proxy: str = "", transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.private = private
        self.proxy = proxy
        self.transport = transport

    async def _pinned(self, url: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" and not self.private:
            raise NotReachable("a door is reached over https")
        if not host or self.private or self.transport is not None:
            return url, {}, {}
        try:
            port = parts.port or 443
        except ValueError as exc:
            raise NotReachable("that address has no valid port") from exc
        address = (await public_addresses(host, port))[0]
        if self.proxy:
            return url, {}, {}
        place = f"[{address}]" if ":" in address else address
        target = parts._replace(netloc=f"{place}:{port}").geturl()
        return target, {"host": parts.netloc}, {"sni_hostname": host}

    async def call(
        self, method: str, url: str, *, token: str = "", body: Any = None, data: bool = True, timeout: float = CALL_SECONDS, extra: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> DoorAnswer:
        """One call whose answer is JSON. ``content`` sends raw bytes in place of ``body``: a piece of a file."""
        answer, raw = await self._exchange(method, url, token=token, body=body, data=data, timeout=timeout, extra=extra, content=content, cap=MAX_ANSWER_BYTES)
        try:
            parsed = jsonlib.loads(raw) if len(raw) <= MAX_ANSWER_BYTES else None
        except (ValueError, RecursionError):
            parsed = None
        whole = isinstance(parsed, dict)
        parsed = parsed if isinstance(parsed, dict) else {}
        return DoorAnswer(answer.status_code, parsed, _seconds(answer.retry if answer.retry is not None else parsed.get("retry_after")), whole)

    async def fetch(self, url: str, *, token: str, timeout: float = PIECE_SECONDS) -> tuple[int, bytes]:
        """A piece of a file: the status and the raw bytes, never more than a piece."""
        answer, raw = await self._exchange("GET", url, token=token, data=False, timeout=timeout, cap=link_protocol.CHUNK_BYTES)
        return answer.status_code, bytes(raw[: link_protocol.CHUNK_BYTES])

    async def _exchange(
        self, method: str, url: str, *, token: str = "", body: Any = None, data: bool = True, timeout: float = CALL_SECONDS, extra: dict[str, str] | None = None,
        content: bytes | None = None, cap: int,
    ) -> tuple[_Answered, bytearray]:
        target, headers, extensions = await self._pinned(url)
        headers = {**headers, "user-agent": "daedalus-agent-talk", **(extra or {})}
        if data:
            headers["accept"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        try:
            # The whole call, and not httpx's own bound: that one is per read, and a door dripping a byte
            # at a time would hold the tool, a lock and the sweep for as long as it liked. The connection has
            # half of it, so that an address nobody answers at fails as a connect, which says nothing was sent,
            # before the whole call's bound fails it as a timeout, which cannot.
            sent: dict[str, Any] = {"content": content} if content is not None else {"json": body}
            async with asyncio.timeout(timeout), httpx.AsyncClient(
                transport=self.transport, proxy=self.proxy or None, timeout=httpx.Timeout(timeout, connect=timeout / 2), follow_redirects=False, trust_env=False,
            ) as client:
                async with client.stream(method, target, headers=headers, extensions=extensions, **sent) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > cap:
                            break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as exc:
            # Nothing took the connection, or the proxy would not reach the door: the request never left.
            raise DoorGone(type(exc).__name__, unsent=True) from exc
        except (httpx.HTTPError, httpx.InvalidURL, OSError, TimeoutError, ValueError) as exc:
            raise DoorGone(type(exc).__name__) from exc
        try:
            retry: float | None = float(response.headers.get("retry-after", ""))
        except ValueError:
            retry = None
        return _Answered(response.status_code, retry), raw


class AgentTalks:
    def __init__(self, app: Application | Any, dialer: Dialer | None = None) -> None:
        self.app = app
        web = app.config.tools.web if getattr(app, "config", None) is not None else None
        self.dialer = dialer or Dialer(private=bool(getattr(app.settings, "agent_talk_private_addresses", False)), proxy=getattr(web, "proxy", "") or "")
        self._followers: dict[str, asyncio.Task[None]] = {}
        self._wakers: dict[str, asyncio.Task[None]] = {}
        self._news: dict[str, asyncio.Event] = {}
        self._readers: dict[str, int] = {}
        self._wakes: dict[str, str] = {}
        """Run id to the conversation it answers."""
        self._told: dict[tuple[str, str], float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._retriers: dict[str, asyncio.Task[None]] = {}
        self._retry_news: dict[str, asyncio.Event] = {}
        self._announcers: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()

    @property
    def manager(self) -> Any:
        return self.app.manager

    @property
    def db(self) -> Any:
        return self.app.db

    # -- rows ---------------------------------------------------------------------------

    async def talk(self, talk_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM agent_talks WHERE id = ?", (talk_id,))

    async def talks_of(self, session_id: str) -> list[Any]:
        return list(await self.db.fetchall("SELECT * FROM agent_talks WHERE session_id = ? ORDER BY created_at DESC", (session_id,)))

    async def messages(self, talk_id: str) -> list[Any]:
        return list(await self.db.fetchall("SELECT * FROM agent_talk_messages WHERE talk_id = ? ORDER BY id", (talk_id,)))

    async def _update(self, talk_id: str, **fields: Any) -> None:
        names = ", ".join(f"{name} = ?" for name in fields)
        await self.db.execute(f"UPDATE agent_talks SET {names} WHERE id = ?", (*fields.values(), talk_id))

    async def _unread(self, talk_id: str) -> list[Any]:
        return list(await self.db.fetchall("SELECT * FROM agent_talk_messages WHERE talk_id = ? AND state = ? ORDER BY id", (talk_id, UNREAD)))

    async def runs_of(self, talk: Any) -> list[str]:
        """The runs that took part, newest last: what a turn opened by a reply reads, and what the spend adds up."""
        rows = await self.db.fetchall("SELECT run_id, max(id) AS last FROM agent_talk_messages WHERE talk_id = ? AND run_id != '' GROUP BY run_id ORDER BY last", (talk["id"],))
        return [run for run in dict.fromkeys([str(talk["opened_run"]), *(str(row["run_id"]) for row in rows)]) if run]

    async def spent(self, talk: Any) -> float:
        runs = await self.runs_of(talk)
        if not runs:
            return 0.0
        marks = ",".join("?" * len(runs))
        row = await self.db.fetchone(f"SELECT coalesce(sum(cost_usd), 0) AS usd FROM usage_events WHERE run_id IN ({marks})", tuple(runs))
        return float(row["usd"] or 0.0) if row is not None else 0.0

    async def describe(self, talk: Any, *, messages: bool = False) -> dict[str, Any]:
        """A conversation for the app. Named field by field so that nothing added to the row reaches it by accident:
        the token and the invite's hash are not fields of this."""
        rows = await self.messages(talk["id"])
        out: dict[str, Any] = {
            "id": talk["id"], "session_id": talk["session_id"], "host": talk["host"], "topic": talk["topic"], "mode": talk["mode"], "state": talk["state"], "reason": talk["reason"],
            "waiting_for": talk["waiting_for"], "quiet": bool(talk["quiet"]), "review": talk["review"], "sends": talk["sends"], "sends_limit": talk["sends_limit"], "wake_turns": talk["wake_turns"],
            "wake_usd": round(float(talk["wake_usd"]), 6), "created_at": talk["created_at"], "last_at": talk["last_at"], "closed_at": talk["closed_at"], "spent_usd": round(await self.spent(talk), 6),
            "replies": sum(1 for row in rows if row["role"] == THEM), "unread": sum(1 for row in rows if row["state"] == UNREAD), "held": sum(1 for row in rows if row["state"] == HELD),
            "file_mb": float(talk["file_mb"]), "day_mb": float(talk["day_mb"]),
            "tasks": [task_view(row) for row in await self.db.fetchall("SELECT * FROM link_tasks WHERE side = ? AND owner = ? ORDER BY id", (SIDE, talk["id"]))],
            "fingerprint": link_identity.fingerprint(str(talk["peer_key"] or "")),
        }
        contact = await self.db.fetchone("SELECT id, name, state FROM link_contacts WHERE talk_id = ? ORDER BY created_at DESC", (talk["id"],))
        out["contact"] = {"id": contact["id"], "name": contact["name"], "state": contact["state"]} if contact is not None else None
        if messages:
            files = [file_view(row) for row in await self.db.fetchall("SELECT * FROM link_files WHERE side = ? AND owner = ? ORDER BY id", (SIDE, talk["id"]))]
            out["files"] = files
            out["messages"] = [
                {"id": row["id"], "role": row["role"], "text": row["text"], "state": row["state"], "note": row["note"], "at": row["created_at"], "kind": row["kind"], "task": row["task"], "task_state": row["task_state"],
                 "next_at": row["next_at"] if row["state"] == WAITING else "", "files": [file["id"] for file in files if file["message_id"] == row["id"]], "author": row["author"], "signed": row["signed"],
                 "card": _card_of(row)}
                for row in rows
            ]
            flows = flows_module.flows_of(self.app)
            out["cards"] = await flows.cards_of(SIDE, talk["id"]) if flows is not None else []
            out["actions"] = await flows.actions_of(SIDE, talk["id"]) if flows is not None else []
        return out

    # -- the tool -----------------------------------------------------------------------

    async def tool(
        self, session_id: str, action: str, *, link: str = "", conversation: str = "", text: str = "", wait_seconds: int | None = None, files: list[str] | None = None,
        task: str = "", contact: str = "", extra: dict[str, Any] | None = None,
    ) -> Outcome:
        state = self.manager.live_state(session_id)
        if state is None or not self.manager.ordinary_chat(state):
            return Outcome("Agent links belong to an ordinary chat.", error=True)
        turn = state.guest_turn
        if turn is not None and turn.kind != "reply":
            # A guest's turn talks to its guest and to nobody else: the door's side of the same protocol.
            bridge = bridge_of(self.app)
            if bridge is None:
                return Outcome("Agent links are not available here.", error=True)
            said, failed = await bridge.turn_action(state, action, text=text, files=list(files or []), extra=extra or {})
            return Outcome(said, failed)
        if turn is not None and turn.link_id.startswith(flows_module.POLL_PREFIX):
            return Outcome("This turn compares the answers to one question for the operator: it sends nothing. Write the comparison.", error=True)
        if turn is None and state.run_origin not in ORIGINS:
            return Outcome("Talking to another agent is for the operator's own turns, with somebody there to see it.", error=True)
        wait = 0 if turn is not None else max(0, min(WAIT_DEFAULT if wait_seconds is None else int(wait_seconds), WAIT_MAX))
        if turn is not None and (files or action == "task"):
            return Outcome("A turn started by a reply sends no files and sets no tasks: tell the operator what you would send.", error=True)
        contacts = contacts_of(self.app)
        if action == "contacts":
            return Outcome(await contacts.listed() if contacts is not None else "Contacts are not available here.")
        if contact:
            # A contact is the installation's, not one chat's: any chat of the operator's may write to it, and its replies stay in its own chat.
            if turn is not None:
                return Outcome("A turn started by a reply writes only to its own conversation.", error=True)
            found = await contacts.by_name(contact) if contacts is not None else None
            if found is None or not found["talk_id"]:
                return Outcome(f"There is no live contact called {contact!r}. action='contacts' lists them.", error=True)
            conversation = str(found["talk_id"])
        try:
            if action == "open":
                if turn is not None:
                    return Outcome("A turn started by a reply opens nothing.", error=True)
                opened = await self._open(state, link)
                if opened.error or not (text.strip() or files):
                    return opened
                # Both real models handed their first message to ``open`` and took it as said, so it is.
                sent = await self._send(state, await self.talk(opened.talk), text, wait, files=files or [])
                return Outcome(f"{opened.text}\n{sent.text}", sent.error, opened.talk)
            flows = flows_module.flows_of(self.app)
            if action in flows_module.FLOW_ACTIONS and (turn is not None or flows is None):
                return Outcome("That is for a turn of the operator's." if turn is not None else "That is not available here.", error=True)
            if action == "ask" and flows is not None:
                said, failed = await flows.ask_many(state, list((extra or {}).get("conversations") or []), text, (extra or {}).get("deadline_minutes"))
                return Outcome(said, failed)
            if action not in ("send", "read", "end", "task", "cancel", *flows_module.FLOW_ACTIONS):
                return Outcome("action is one of open, send, task, cancel, read, end, contacts, ask, summary, slots, meet, apply_patch.", error=True)
            if turn is None and action == "send" and not contact and (guest := await self._guest_addressed(session_id, conversation)) is not None:
                if isinstance(guest, Outcome):
                    return guest
                said, failed = await bridge_of(self.app).write_to_guest(state, guest, text, list(files or []))  # type: ignore[union-attr]
                return Outcome(said, failed, guest["conversation"])
            talk = await self._chosen(session_id, turn.link_id if turn is not None else conversation, contact=bool(contact))
            if isinstance(talk, Outcome):
                return talk
            if action in flows_module.FLOW_ACTIONS and flows is not None:
                said, failed = await flows.talk_action(state, action, talk, text=text, files=list(files or []), extra=extra or {})
                return Outcome(said, failed, talk["id"])
            if action in ("send", "task"):
                return await self._send(state, talk, text, wait, files=files or [], kind=link_protocol.TASK if action == "task" else link_protocol.TEXT, task=task if action == "send" else "")
            if action == "cancel":
                return await self.cancel(talk["id"], task)
            if action == "read":
                return await self._read(state, talk, wait)
            await self.end(talk["id"], reason="ended")
            return Outcome(f"The conversation with {talk['host']} is ended.", talk=talk["id"])
        except NotReachable as exc:
            return Outcome(str(exc), error=True)

    async def _guest_addressed(self, session_id: str, conversation: str) -> dict[str, str] | Outcome | None:
        """The guest at this chat's own door a send of the operator's turn is for, or ``None`` when it is for a conversation
        at somebody else's: the one named, or with none named and none of those open, the one guest there is."""
        bridge = bridge_of(self.app)
        if bridge is None:
            return None
        if conversation and await self.talk(conversation) is not None:
            return None
        guests = await bridge.open_guests(session_id)
        if conversation:
            return next((guest for guest in guests if guest["conversation"] == conversation), None)
        if any(talk["state"] != CLOSED for talk in await self.talks_of(session_id)) or not guests:
            return None
        if len(guests) == 1:
            return guests[0]
        return Outcome("Several guests are in conversation with this chat; say which with conversation: " + ", ".join(f"{g['conversation']} (link {g['label']!r})" for g in guests), error=True)

    async def _chosen(self, session_id: str, conversation: str, *, contact: bool = False) -> Any:
        if contact:
            found = await self.talk(conversation)
            return found if found is not None else Outcome("That contact's conversation is gone.", error=True)
        talks = await self.talks_of(session_id)
        if conversation:
            found = next((talk for talk in talks if talk["id"] == conversation), None)
            return found if found is not None else Outcome("This chat has no such conversation.", error=True)
        live = [talk for talk in talks if talk["state"] != CLOSED]
        if len(live) == 1:
            return live[0]
        if not live:
            return Outcome("This chat has no open conversation. Open one with the link the operator gave you.", error=True)
        return Outcome("Several conversations are open; say which: " + ", ".join(f"{talk['id']} ({talk['host']})" for talk in live), error=True)

    async def _in_operators_words(self, session_id: str, door: agent_talk.DoorLink) -> bool:
        """Whether the operator wrote this very address in this chat, in their own words.

        Asked of the stored transcript and of nothing a model could have put there. The whole address, parsed out of
        what they wrote: testing for the code alone let a stranger name their own host with any long string from the
        operator's message as the code, and the GET that followed carried that string out, on a free tool.
        """
        rows = await self.db.fetchall(
            "SELECT message FROM transcript WHERE session_id = ? AND json_extract(message, '$.role') = 'user' "
            "AND json_extract(message, '$.metadata.\"daedalus.origin\"') = 'operator' AND instr(message, ?) > 0",
            (session_id, door.invite),
        )
        for row in rows:
            blocks = jsonlib.loads(row["message"]).get("content_blocks") or []
            said = without_host_notes("".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("kind") == "text"))
            if door in agent_talk.find_links(said):
                return True
        return False

    async def _open(self, state: SessionState, link: str) -> Outcome:
        session_id = state.session.id
        found = agent_talk.find_links(link)
        if len(found) != 1:
            return Outcome("That is not an agent link: an address ending in /a/ and a long code.", error=True)
        door = found[0]
        if not await self._in_operators_words(session_id, door):
            return Outcome(
                "That link did not come from the operator in this chat, so it stays closed: a link from a reply, a page or a file is not theirs to have given. Show it to them; if they write it here you can open it.",
                error=True,
            )
        invite_hash = _hash(door.invite)
        row = await self.db.fetchone("SELECT * FROM agent_talks WHERE session_id = ? AND invite_hash = ?", (session_id, invite_hash))
        if row is not None:
            return Outcome(self._state_line(row, "This chat has already opened this link." if row["state"] != CLOSED else ""), talk=row["id"], error=row["state"] == CLOSED)
        return await self._redeem(session_id, door, state.run_id or "")

    async def open_contact(self, session_id: str, address: str, key: str) -> str:
        """Redeem a contact's link, which their host sent signed: the operator decided by taking the offer up, so no
        agent is asked and no operator's words are looked for. Its card must prove the key the offer was signed with."""
        from daedalus.extensions.agent_contacts import ContactError  # Lazy: contacts import this module.

        found = agent_talk.find_links(address)
        if len(found) != 1:
            raise ContactError("the other side sent no link that could be opened")
        opened = await self._redeem(session_id, found[0], "", contact_key=key)
        if opened.error:
            raise ContactError(opened.text)
        return opened.talk

    async def _redeem(self, session_id: str, door: agent_talk.DoorLink, run_id: str, *, contact_key: str = "") -> Outcome:
        """The card, then the redeem, signed with this installation's key. ``contact_key`` is the key the door's card must
        prove its owner holds: a contact's link at a door that does not is refused, loudly."""
        invite_hash = _hash(door.invite)
        url = f"{door.base}/a/{door.invite}"
        try:
            card = await self.dialer.call("GET", url)
        except DoorGone:
            return Outcome(f"{door.host} did not answer. Nothing was spent; try again later.", error=True)
        if card.body.get("door") != agent_talk.DOOR_MARK:
            return Outcome(f"{door.host} does not answer as an agent link. Nothing was spent.", error=True)
        if card.status == 410 or card.body.get("status") != "waiting":
            words = "has been used already" if card.body.get("status") == "used" else "is closed"
            return Outcome(f"That link {words}. Ask the operator for a new one.", error=True)
        topic = " ".join(inert(str(card.body.get("topic") or "")).split())[:MAX_TOPIC]
        mode = card.body.get("mode") if card.body.get("mode") in ("stand_in", "direct") else ""
        retention = _seconds(card.body.get("retention_days"))
        limit = card.body.get("max_text")
        peer = str(card.body.get("key") or "")
        # The card's key counts only where its proof over this very invite holds: a key copied from another door's card does not.
        peer = peer if link_identity.verify(peer, str(card.body.get("proof") or ""), "door", {"invite": invite_hash}) else ""
        if contact_key and peer != contact_key:
            return Outcome(f"The door at {door.host} did not prove the key this contact was offered with, so its link was not opened. Nothing was spent.", error=True)
        talk_id = secrets.token_hex(6)
        now = _now()
        await self.db.execute(
            "INSERT INTO agent_talks(id, session_id, base_url, host, invite_hash, state, topic, mode, approval, retention_days, max_text, opened_run, created_at, last_at, peer_key, sends_limit, review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (talk_id, session_id, door.base, door.host, invite_hash, OPENING, topic, mode, int(card.body.get("approval") is True), retention or 0.0,
             min(limit, MAX_TEXT) if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else MAX_TEXT, run_id, now, now, peer, 0 if contact_key else MAX_SENDS,
             # A contact's first message is not a first contact: both operators met it in the offer. What looks like a
             # secret, or follows a look around after a reply, still waits; the operator can ask for more in the tab.
             "off" if contact_key else "first"),
        )
        identity = identity_of(self.app)
        signing = {"x-agent-key": identity.public, "x-agent-proof": identity.redeem_proof(door.invite)} if identity is not None else {}
        try:
            answer = await self.dialer.call("POST", f"{url}/redeem", extra={"x-agent-client": CLIENT_KIND, **signing})
        except DoorGone:
            await self.db.execute("DELETE FROM agent_talks WHERE id = ?", (talk_id,))
            return Outcome(f"{door.host} did not answer the redeem. The link may or may not be spent: opening it again will show which.", error=True)
        token = answer.body.get("token")
        if answer.status != 201 or not isinstance(token, str) or not token or len(token) > 512 or not (token.isascii() and token.isprintable()):
            await self.db.execute("DELETE FROM agent_talks WHERE id = ?", (talk_id,))
            return Outcome("The door would not open this link. Ask the operator for a new one.", error=True)
        conversation_state = PENDING if answer.body.get("status") == PENDING else OPEN
        await self._update(talk_id, token=token, state=conversation_state, waiting_for="")
        talk = await self.talk(talk_id)
        parts = [self._state_line(talk, f"Opened a conversation with the agent at {door.host}, as {talk_id}.")]
        if mode:
            parts.append("Its door says this is a stand-in that knows only what its owner wrote down." if mode == "stand_in" else "Its door says this is its owner's own agent.")
        if conversation_state == PENDING:
            parts.append("Its owner admits a first contact by hand, so the first answer may take a while.")
        if topic and run_id:
            parts.append(agent_talk.fence_topic(topic, door.host))
            self.manager.touch_run(session_id)
            parts.append(agent_talk.TOUCHED_NOTE)
        return Outcome("\n".join(parts), talk=talk_id)

    def _state_line(self, talk: Any, lead: str = "") -> str:
        state = talk["state"]
        if state == CLOSED:
            said = CLOSED_WORDS.get(str(talk["reason"]), CLOSED_WORDS["ended"])
        elif talk["waiting_for"] == "owner":
            said = (
                "Its owner has not admitted this conversation yet." if state == PENDING
                else "The other agent waits for its owner to decide on what you asked: the answer comes later, and you are told, so do not wait for it in this turn."
            )
        elif talk["waiting_for"] == OFFLINE:
            # Not "a reply is owed": an agent told that waited out its turn, or gave up, on a side that was only away.
            said = "The other side is offline: no reply comes until it is back, so do not wait for one in this turn."
        elif talk["waiting_for"] == "reply":
            said = "The door says it is no use waiting for now: tell the operator, and read again later." if talk["quiet"] else "A reply is owed."
        else:
            said = "Nothing is owed."
        sent = f"{talk['sends']} of {talk['sends_limit']} messages sent." if talk["sends_limit"] else f"{talk['sends']} messages sent."
        return " ".join(part for part in (lead, said, sent, f"[conversation {talk['id']}]") if part)

    async def _send(
        self, state: SessionState, talk: Any, text: str, wait: int, *, files: list[str] | None = None, kind: str = link_protocol.TEXT, task: str = "", card: dict[str, Any] | None = None,
    ) -> Outcome:
        session_id = state.session.id
        if talk["state"] in (CLOSED, OPENING):
            return Outcome(self._state_line(talk), error=True, talk=talk["id"])
        if talk["sends_limit"] and talk["sends"] + await self._waiting_count(talk["id"]) >= talk["sends_limit"]:
            # Those waiting for the door count now: each is counted once more when it goes, which it will by itself.
            return Outcome(f"This conversation has used its {talk['sends_limit']} messages. Tell the operator: they can lift the limit, or talking on needs a new link.", error=True, talk=talk["id"])
        run_id = state.run_id or ""
        turn = state.guest_turn
        if turn is not None and await self.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND role = ? AND run_id = ?", (talk["id"], US, run_id)) is not None:
            return Outcome("A turn started by a reply sends once. End the turn; the next reply starts another.", error=True, talk=talk["id"])
        clean = self.manager.redactor.conceal_named(text).strip()
        if not clean and not files:
            return Outcome("text is the message to send." if kind != link_protocol.TASK else "text says what the task is.", error=True, talk=talk["id"])
        if kind == link_protocol.TASK and not clean:
            return Outcome("text says what the task is.", error=True, talk=talk["id"])
        if len(clean) > talk["max_text"]:
            return Outcome(f"A message may be at most {talk['max_text']} characters.", error=True, talk=talk["id"])
        if task and not await self.db.fetchone("SELECT 1 FROM link_tasks WHERE side = ? AND owner = ? AND task = ?", (SIDE, talk["id"], task)):
            return Outcome(f"This conversation has no task {task}.", error=True, talk=talk["id"])
        if await self.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = ?", (talk["id"], HELD)) is not None:
            return Outcome("A message of this conversation is already waiting for the operator. Wait for them or end your turn.", error=True, talk=talk["id"])
        staged: list[Staged] = []
        outside = False
        if files:
            prepared = await self._stage(state, talk, files)
            if isinstance(prepared, Outcome):
                return prepared
            staged, outside = prepared
        if kind == link_protocol.TEXT and staged:
            kind = link_protocol.FILE
        hold = "review" if state.review_holds_send else "outside" if outside else self._why_hold(talk, clean, session_id, staged)
        state.review_holds_send = False
        message_id = await self._insert_message(talk["id"], US, clean, HELD if hold else SENDING, run_id, hold, kind=kind, task=task, card=card)
        for item in staged:
            await self._file_row(talk, "out", item.name, item.size, item.sha256, str(item.path), "held" if hold else "sending", message_id=message_id, task=task, note=item.source)
        if hold:
            shown = clean if len(clean) <= CARD_CHARS else f"{clean[:CARD_CHARS]}… [{len(clean)} characters in all: read the whole message in the chat's conversation before you allow it]"
            listed = "".join(f"\n· {item.name} ({item.size} bytes)" for item in staged)
            what = "A task for" if kind == link_protocol.TASK else "To"
            self.manager.ask_operator(
                session_id, key=ask_key(message_id), tool=agent_talk.TOOL, text=f"{what} {talk['host']}: {shown}{listed}", on_answer=ASK_HOOK, extra={"talk": talk["id"], "message": message_id},
            )
            settled = await self._await_decision(talk["id"], message_id, wait)
            if settled == HELD:
                return Outcome(f"Not sent: {HELD_WORDS[hold]}, so it is with the operator. End your turn unless you have other work; nothing goes until they let it.", talk=talk["id"])
            if settled == REFUSED:
                return Outcome("The operator did not let this message go. Nothing was sent.", talk=talk["id"])
        else:
            await self._post(talk["id"], message_id, clean)
            settled = (await self._message(message_id))["state"]
        fresh = await self.talk(talk["id"])
        message = await self._message(message_id)
        if settled == WAITING:
            return Outcome(self._state_line(fresh, KEPT_WORDS), talk=talk["id"])
        if settled != SENT:
            return Outcome(self._send_failure(fresh, message), error=True, talk=talk["id"])
        return await self._reply_or_status(state, fresh, wait, OWNER_AWAY_WORDS if message["note"] == "owner_offline" else "Sent.", newer_than=message_id)

    async def _stage(self, state: SessionState, talk: Any, paths: list[str]) -> tuple[list[Staged], bool] | Outcome:
        """The files a message is to carry, made ready to go and checked against the conversation's limits, and whether
        any of them comes from outside the chat's folder."""
        if len(paths) > link_protocol.MAX_FILES:
            return Outcome(f"At most {link_protocol.MAX_FILES} files in one message.", error=True, talk=talk["id"])
        fs = state.services.fs if state.services is not None else None
        if fs is not None and getattr(fs, "remote", False):
            return Outcome("Files go only from a chat whose folder is on this machine.", error=True, talk=talk["id"])
        root = Path(os.path.realpath(state.services.workspace_dir if state.services is not None else state.workspace))
        chosen: list[Path] = []
        outside = False
        for path in paths:
            given = Path(str(path)).expanduser()
            real = Path(os.path.realpath(given if given.is_absolute() else root / given))
            if not real.exists():
                return Outcome(f"There is no {path}.", error=True, talk=talk["id"])
            if state.services is not None and state.services.is_protected(real):
                return Outcome(f"{path} is part of this installation, and does not leave it.", error=True, talk=talk["id"])
            outside = outside or not real.is_relative_to(root)
            chosen.append(real)
        try:
            staged = await asyncio.to_thread(link_protocol.stage, chosen, self.app.settings.state_dir / link_protocol.STAGING)
        except (LinkFileError, OSError) as exc:
            return Outcome(f"Could not get the files ready: {exc}", error=True, talk=talk["id"])
        why = link_protocol.check_limits((item.size for item in staged), file_mb=float(talk["file_mb"]), day_mb=float(talk["day_mb"]), used_today=await self._bytes_today(talk["id"]))
        if why:
            link_protocol.unstage(staged)
            return Outcome(f"Not sent: {why}. The operator can change the limits on the conversation.", error=True, talk=talk["id"])
        return staged, outside

    async def _bytes_today(self, talk_id: str, direction: str = "out") -> int:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = await self.db.fetchone(
            "SELECT coalesce(sum(size), 0) AS n FROM link_files WHERE side = ? AND owner = ? AND direction = ? AND created_at >= ? AND state NOT IN ('refused', 'dropped', 'failed')",
            (SIDE, talk_id, direction, midnight),
        )
        return int(row["n"]) if row is not None else 0

    async def _file_row(
        self, talk: Any, direction: str, name: str, size: int, sha256: str, path: str, state: str, *, message_id: int | None = None, task: str = "", remote_id: str = "",
        note: str = "",
    ) -> int:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO link_files(side, owner, session_id, direction, name, size, sha256, mime, path, remote_id, task, message_id, state, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (SIDE, talk["id"], talk["session_id"], direction, link_protocol.safe_name(name), int(size), sha256, link_protocol.mime_of(name), path, remote_id, task, message_id,
                 state, note[:500], _now()),
            )
            row_id = int(cursor.lastrowid or 0)
            await cursor.close()
        return row_id

    async def _files_of(self, message_id: int, *states: str) -> list[Any]:
        marks = ",".join("?" * len(states))
        return list(await self.db.fetchall(f"SELECT * FROM link_files WHERE side = ? AND message_id = ? AND state IN ({marks}) ORDER BY id", (SIDE, message_id, *states)))

    def _why_hold(self, talk: Any, text: str, session_id: str, staged: list[Staged] | None = None) -> str:
        if self.manager.redactor.contains_secret(text) or any(self._holds_secret(item) for item in staged or ()):
            return "secret"
        touch = self.manager.touched(session_id)
        if touch is not None and touch.get("looked"):
            return "looked"
        if talk["review"] == "all":
            return "all"
        return "first" if talk["review"] == "first" and talk["sends"] == 0 else ""

    def _holds_secret(self, item: Staged) -> bool:
        """Whether a small text file going out holds a value this installation knows to be secret, or looks like one."""
        if item.size > SECRET_SCAN_BYTES:
            return False
        try:
            text = item.path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return self.manager.redactor.contains_secret(text)

    async def _insert_message(
        self, talk_id: str, role: str, text: str, state: str, run_id: str, note: str = "", *, kind: str = link_protocol.TEXT, task: str = "", author: str = "",
        card: dict[str, Any] | None = None,
    ) -> int:
        now = _now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO agent_talk_messages(talk_id, role, text, state, run_id, note, created_at, kind, task, author, card) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (talk_id, role, text, state, run_id, note, now, kind, task, author, jsonlib.dumps(card, ensure_ascii=False) if card is not None else ""),
            )
            message_id = int(cursor.lastrowid or 0)
            await cursor.close()
        return message_id

    async def _message(self, message_id: int) -> Any:
        return await self.db.fetchone("SELECT * FROM agent_talk_messages WHERE id = ?", (message_id,))

    async def _waiting_count(self, talk_id: str) -> int:
        row = await self.db.fetchone("SELECT count(*) AS n FROM agent_talk_messages WHERE talk_id = ? AND state = ?", (talk_id, WAITING))
        return int(row["n"]) if row is not None else 0

    def _send_failure(self, talk: Any, message: Any) -> str:
        note = str(message["note"])
        if talk["state"] == CLOSED:
            return self._state_line(talk)
        if message["state"] == UNSURE:
            return "The door did not answer, so this message may or may not have arrived. Read before you send it again."
        if note.startswith("limit:"):
            return f"Not sent. {LIMIT_WORDS.get(note[6:], 'A limit of that link was reached.')} Tell the operator."
        if note.startswith("files:"):
            return f"Not sent: {FILE_WORDS.get(note[6:], 'the door would not take a file')}. Tell the operator."
        if note == "busy":
            return "Not sent: earlier messages are still waiting for an answer. Read first."
        if note == "owner":
            return "Not sent: the first message is still with the other side's owner, and nothing more is taken until they answer."
        if note == "gave_up":
            return f"Not sent: the other side stayed offline for {int(GIVE_UP_SECONDS // 3600)} hours, so the message was given up."
        if note == "behind":
            return "Not sent: it waited behind an earlier message that did not go, and was not sent out of order."
        return "Not sent: the door would not take this message."

    def signature(self, talk: Any, text: str, *, kind: str = link_protocol.TEXT, task: str = "", by: str = "", card: dict[str, Any] | None = None) -> dict[str, Any]:
        """The ``signed`` field of a message to this conversation's door, or nothing where this side has no key.

        Signed as the door will keep it, which cleans the text as it stores it, and for the key of the door's owner where
        its card proved one. Whatever sends to a door outside :meth:`_post` signs through here too: a contact refuses
        an unsigned message, and tells its operator of it as of a stranger's."""
        identity = identity_of(self.app)
        if identity is None:
            return {}
        seen = link_flows.card_view(card) if card is not None else None
        return {"signed": identity.signed_message(text=inert(text), kind=kind, task=task, by=by, to=str(talk["peer_key"] or ""), card=seen)}

    async def _upload(self, talk: Any, row: Any) -> str:
        """One file to the other door, announced and then sent a piece at a time from wherever that door has got to.
        The door's id, or :class:`LinkFileError` with the door's refusal as one of this code's words."""
        base, token = talk["base_url"], talk["token"]
        file_id = str(row["remote_id"] or "")
        received = 0
        if file_id:
            # Announced before a drop: the door says how far it got, and the file goes on from there.
            known = _present(await self.dialer.call("GET", f"{base}/a/files/{file_id}", token=token))
            received = _count(known.body.get("received")) if known.status == 200 and known.body.get("state") == "uploading" else -1
            if known.status == 200 and known.body.get("state") == "ready":
                return file_id
        if not file_id or received < 0:
            made = _present(await self.dialer.call("POST", f"{base}/a/files", token=token, body={"name": row["name"], "size": row["size"], "sha256": row["sha256"]}))
            file_id = str(made.body.get("id") or "")
            if made.status not in (200, 201) or not re.fullmatch(r"f_[0-9a-f]{16}", file_id):
                code = made.body.get("code")
                raise LinkFileError(code if isinstance(code, str) and code in FILE_WORDS else "refused")
            await self.db.execute("UPDATE link_files SET remote_id = ? WHERE id = ?", (file_id, row["id"]))
            received = 0

        async def put(offset: int, piece: bytes) -> int:
            answer = _present(await self.dialer.call("PUT", f"{base}/a/files/{file_id}?offset={offset}", token=token, content=piece, timeout=PIECE_SECONDS))
            if answer.status in (200, 409) and isinstance(answer.body.get("received"), int):
                return _count(answer.body.get("received"))
            raise LinkFileError("refused")

        await link_protocol.send_pieces(Path(str(row["path"])), int(row["size"]), received, put)
        return file_id

    async def _post(self, talk_id: str, message_id: int, text: str) -> None:
        """Send one message and record what became of it. The only caller of ``POST /a/messages``: its files go first,
        each to the end, and the message names them once they are all there.

        A door that is not there leaves the message :data:`WAITING`, to go by itself when it is back, and so does an
        earlier message of the conversation that waits: the other side reads them in the order they were written."""
        async with self._locks.setdefault(talk_id, asyncio.Lock()):
            talk = await self.talk(talk_id)
            if talk is None or talk["state"] == CLOSED or not talk["token"]:
                await self._mark(message_id, FAILED)
                return
            if await self.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = ? AND id < ?", (talk_id, WAITING, message_id)) is not None:
                now = _now()
                await self.db.execute("UPDATE agent_talk_messages SET state = ?, next_at = ?, waiting_since = ? WHERE id = ?", (WAITING, now, now, message_id))
                return
            await self._mark(message_id, SENDING)
            message = await self._message(message_id)
            body: dict[str, Any] = {"text": text}
            if message["kind"] != link_protocol.TEXT:
                body["kind"] = message["kind"]
            if message["task"]:
                body["task"] = message["task"]
            by = "owner" if message["author"] == "owner" else ""
            if by:
                body["from"] = by
            card = jsonlib.loads(message["card"]) if message["card"] else None
            if card is not None:
                body["card"] = card
            body.update(self.signature(talk, text, kind=str(message["kind"]), task=str(message["task"] or ""), by=by, card=card))
            files = await self._files_of(message_id, "sending")
            posting = False
            try:
                ids = [await self._upload(talk, row) for row in files]
                if ids:
                    body["files"] = ids
                posting = True
                answer = await self.dialer.call("POST", f"{talk['base_url']}/a/messages", token=talk["token"], body=body)
            except LinkFileError as exc:
                await self._mark(message_id, FAILED, note=f"files:{exc}")
                for row in files:
                    await self.db.execute("UPDATE link_files SET state = 'failed' WHERE id = ?", (row["id"],))
                link_protocol.unstage(Staged(str(row["name"]), Path(str(row["path"])), 0, "", "") for row in files)
                return
            except DoorGone as exc:
                if exc.unsent or not posting:
                    # Before the message itself went, a file cut off goes on later from where the door has it.
                    await self._keep_for_door(talk_id, message_id)
                    return
                await self._mark(message_id, UNSURE)
                await self._update(talk_id, waiting_for="reply", quiet=0, last_at=_now())
                self.follow(talk_id)
                return
            except NotReachable:
                await self._mark(message_id, UNSURE)
                await self._update(talk_id, waiting_for="reply", quiet=0, last_at=_now())
                self.follow(talk_id)
                return
            if door_absent(answer):
                await self._keep_for_door(talk_id, message_id)
                return
            word = answer.body.get("status")
            if answer.status == 202 and "id" not in answer.body:
                # The first message is with an owner who has not answered, and the door took nothing more.
                await self._mark(message_id, FAILED, note="owner")
            elif answer.status == 202:
                away = answer.body.get("owner_offline") is True
                await self._mark(message_id, SENT, note="owner_offline" if away else None)
                for row in files:
                    await self.db.execute("UPDATE link_files SET state = 'sent', path = '' WHERE id = ?", (row["id"],))
                link_protocol.unstage(Staged(str(row["name"]), Path(str(row["path"])), 0, "", "") for row in files)
                # Kept at the door with its owner away: a reply is owed but will not come soon, so it is read slowly.
                await self.db.execute(
                    "UPDATE agent_talks SET sends = sends + 1, waiting_for = ?, quiet = ?, last_at = ? WHERE id = ?",
                    (OFFLINE if away else "owner" if word == PENDING else "reply", int(away), _now(), talk_id),
                )
                task = _task_id(answer.body.get("task"))
                if message["kind"] == link_protocol.TASK and task:
                    await self._task_set(talk, task, str(message["text"]))
                    await self.db.execute("UPDATE agent_talk_messages SET task = ? WHERE id = ?", (task, message_id))
                self.follow(talk_id)
            elif answer.status in (401, 410):
                await self._mark(message_id, FAILED)
                await self._close(talk_id, "token" if answer.status == 401 else self._reason(answer.body))
            elif word == "limit":
                limit = answer.body.get("limit")
                await self._mark(message_id, FAILED, note=f"limit:{limit}" if isinstance(limit, str) and limit in LIMIT_WORDS else "limit:")
            else:
                await self._mark(message_id, FAILED, note="busy" if word == "busy" else "refused")

    async def say_as_owner(self, talk_id: str, text: str) -> None:
        """The operator's own words to the other side: sent as theirs, with no review, because they are the reviewer."""
        talk = await self.talk(talk_id)
        if talk is None or talk["state"] in (CLOSED, OPENING) or not talk["token"]:
            raise ValueError("this conversation is not open")
        words = self.manager.redactor.conceal_named(text).strip()
        if not words:
            raise ValueError("the message is empty")
        if len(words) > talk["max_text"]:
            raise ValueError(f"a message may be at most {talk['max_text']} characters")
        message_id = await self._insert_message(talk_id, US, words, SENDING, "", author="owner")
        await self._post(talk_id, message_id, words)
        # Kept for a door that is not there goes by itself when it is back, as the agent's own words do.
        if (await self._message(message_id))["state"] not in (SENT, WAITING):
            raise ValueError(self._send_failure(await self.talk(talk_id), await self._message(message_id)))

    async def post_contact(self, talk_id: str, payload: dict[str, Any], text: str) -> None:
        """An offer of a contact, or the answer to one, over this conversation: the operator's decision, sent as it is."""
        from daedalus.extensions.agent_contacts import ContactError  # Lazy: contacts import this module.

        talk = await self.talk(talk_id)
        if talk is None or talk["state"] == CLOSED or not talk["token"]:
            raise ContactError("that conversation is not open")
        message_id = await self._insert_message(talk_id, US, text, SENDING, "", kind="contact", author="owner")
        async with self._locks.setdefault(talk_id, asyncio.Lock()):
            try:
                answer = await self.dialer.call("POST", f"{talk['base_url']}/a/messages", token=talk["token"], body={"kind": "contact", "contact": payload, "text": text, "from": "owner"})
            except (DoorGone, NotReachable) as exc:
                await self._mark(message_id, FAILED)
                raise ContactError(f"{talk['host']} did not answer") from exc
        if answer.status != 202:
            await self._mark(message_id, FAILED)
            raise ContactError(f"{talk['host']} did not take it ({answer.status})")
        await self._mark(message_id, SENT)
        await self.db.execute("UPDATE agent_talks SET sends = sends + 1, last_at = ? WHERE id = ?", (_now(), talk_id))

    async def _keep_for_door(self, talk_id: str, message_id: int) -> None:
        """The door was not there for this message: it waits, and is tried again on :data:`RETRY_SECONDS`.

        It used to be failed, or marked as perhaps arrived, and the agent gave up on a conversation whose other side
        was only a desktop app closed for the night."""
        row = await self._message(message_id)
        attempts = int(row["attempts"]) + 1
        now = datetime.now(UTC)
        later = now + timedelta(seconds=RETRY_SECONDS[min(attempts, len(RETRY_SECONDS)) - 1])
        await self.db.execute(
            "UPDATE agent_talk_messages SET state = ?, attempts = ?, next_at = ?, waiting_since = CASE WHEN waiting_since = '' THEN ? ELSE waiting_since END WHERE id = ?",
            (WAITING, attempts, later.isoformat(), now.isoformat(), message_id),
        )
        await self._update(talk_id, waiting_for=OFFLINE, quiet=1)
        self._wake_readers(talk_id)
        # The reader looks at the door every few minutes, the hourly retry far less often: whichever sees it back first
        # sends what waits.
        self.follow(talk_id)
        self._retry_later(talk_id)

    def _retry_later(self, talk_id: str) -> None:
        """Send what waits for this conversation's door, in order, once. Nothing waiting ends the task at once."""
        running = self._retriers.get(talk_id)
        if running is None or running.done():
            self._retriers[talk_id] = self._spawn(self._retry(talk_id), f"agent-talk-retry:{talk_id}")

    def _door_back(self, talk_id: str) -> None:
        """The other side answered again: what waits for it goes now rather than at its next hour."""
        news = self._retry_news.get(talk_id)
        if news is not None:
            news.set()

    async def _retry(self, talk_id: str) -> None:
        """Try the oldest waiting message of a conversation when it is due, and the next once it went. A message that
        cannot go for another reason stops the line, and the ones behind it are not sent out of order."""
        while True:
            talk = await self.talk(talk_id)
            head = await self.db.fetchone("SELECT * FROM agent_talk_messages WHERE talk_id = ? AND state = ? ORDER BY id LIMIT 1", (talk_id, WAITING))
            if talk is None or head is None or talk["state"] == CLOSED:
                break
            now = datetime.now(UTC)
            since = _instant(head["waiting_since"]) or now
            if (now - since).total_seconds() >= GIVE_UP_SECONDS:
                await self._settle_waiting(talk_id, "gave_up")
                break
            left = ((_instant(head["next_at"]) or now) - now).total_seconds()
            if left > 0:
                news = self._retry_news[talk_id] = asyncio.Event()
                with suppress(TimeoutError):
                    await asyncio.wait_for(news.wait(), min(left, GIVE_UP_SECONDS - (now - since).total_seconds()))
                if news.is_set():
                    await self.db.execute("UPDATE agent_talk_messages SET next_at = ? WHERE talk_id = ? AND state = ?", (_now(), talk_id, WAITING))
                continue
            await self._post(talk_id, int(head["id"]), str(head["text"]))
            after = await self._message(int(head["id"]))
            if after is None or after["state"] == WAITING:
                continue
            await self.db.execute("UPDATE agent_talk_messages SET wake_state = 'owed' WHERE id = ?", (head["id"],))
            if after["state"] != SENT:
                await self._settle_waiting(talk_id, "behind")
                break
        self._retry_news.pop(talk_id, None)
        self.announce(talk_id)

    async def _settle_waiting(self, talk_id: str, note: str) -> None:
        """Fail every message still waiting for this door, with ``note`` for why, and owe the chat word of it."""
        for row in await self.db.fetchall("SELECT id FROM agent_talk_messages WHERE talk_id = ? AND state = ?", (talk_id, WAITING)):
            await self.db.execute("UPDATE agent_talk_messages SET state = ?, note = ?, wake_state = 'owed' WHERE id = ? AND state = ?", (FAILED, note, row["id"], WAITING))
            for file in await self._files_of(int(row["id"]), "sending"):
                await self.db.execute("UPDATE link_files SET state = 'failed' WHERE id = ?", (file["id"],))
                link_protocol.unstage([Staged(str(file["name"]), Path(str(file["path"])), 0, "", "")])

    def announce(self, talk_id: str) -> None:
        running = self._announcers.get(talk_id)
        if running is None or running.done():
            self._announcers[talk_id] = self._spawn(self._announce(talk_id), f"agent-talk-announce:{talk_id}")

    async def _announce(self, talk_id: str) -> None:
        """Open a turn to say what became of the messages that waited for the door: delivered, or given up.

        Once a message, whether or not the turn could be had: the row is claimed before the turn is asked for, so a
        restart in between costs the word and never says it twice. Bounded as a turn a task opens is, at
        :data:`TURN_USD` and the tools of a reply's turn, and opened whether or not waking is on: the agent sent
        those words and was told it would hear what became of them."""
        deadline = time.monotonic() + CHAT_FREE_SECONDS
        while True:
            talk = await self.talk(talk_id)
            rows = list(await self.db.fetchall("SELECT * FROM agent_talk_messages WHERE talk_id = ? AND wake_state = 'owed' ORDER BY id", (talk_id,)))
            if talk is None or not rows:
                return
            if await self._waiting_count(talk_id):
                return  # the rest of the line is still going: one turn for all of it, when it is through
            if talk["state"] == CLOSED or time.monotonic() >= deadline:
                await self._told_of(rows)
                return
            if self._readers.get(talk_id):
                await asyncio.sleep(1.0)
                continue
            session_id = str(talk["session_id"])
            manager = self.manager
            key = "delivered" if all(row["state"] == SENT for row in rows) else "gave_up"
            why = await manager.guest_ready(session_id, "none")
            price = await manager.guest_price(session_id)
            body = self._outcome_note(talk, rows)
            runs = (await self.runs_of(talk))[-RUNS_KEPT:]
            usd_left = TURN_USD if price is not None and any(price) else None
            if why or price is None or (usd_left is not None and await manager.guest_first_call_usd(session_id, price, runs=runs, extra_chars=len(body)) > usd_left):
                logger.info("what became of a waiting message could not wake session %s: %s", session_id, why or "no turn within its budget")
                await self._told_of(rows)
                await self._tell(talk, key)
                return
            await self._told_of(rows)
            turn = GuestTurn(talk_id, str(talk["host"]), "none", usd_left, "", "reply", tuple(runs), notice=True)
            try:
                await manager.submit(session_id, body, as_answer=False, origin=turn.origin, via="agent_talk", guest=turn)
            except GuestTurnBusy:
                await self.db.execute(f"UPDATE agent_talk_messages SET wake_state = 'owed' WHERE id IN ({','.join('?' * len(rows))})", tuple(row["id"] for row in rows))
                await asyncio.sleep(1.0)
                continue
            except (RuntimeError, KeyError) as exc:
                logger.warning("what became of a waiting message could not open a turn in session %s: %s", session_id, exc)
                await self._tell(talk, key)
                return
            # The turn reads this conversation's runs, which hold the stranger's replies.
            manager.touch_run(session_id)
            if key == "gave_up":
                await self._tell(talk, key)
            return

    async def _told_of(self, rows: list[Any]) -> None:
        await self.db.execute(f"UPDATE agent_talk_messages SET wake_state = 'done' WHERE id IN ({','.join('?' * len(rows))})", tuple(row["id"] for row in rows))

    @staticmethod
    def _outcome_note(talk: Any, rows: list[Any]) -> str:
        """What the turn opened by :meth:`_announce` reads: this code's words and the agent's own, none of the stranger's."""
        lines = [f"What became of the messages that waited while the agent at {talk['host']} was offline (conversation {talk['id']}):"]
        for row in rows:
            text = str(row["text"])
            quoted = text if len(text) <= CARD_CHARS else f"{text[:CARD_CHARS]}…"
            if row["state"] == SENT:
                outcome = "delivered"
            elif row["note"] == "gave_up":
                outcome = f"not sent: the other side stayed offline for {int(GIVE_UP_SECONDS // 3600)} hours"
            elif row["note"] == "behind":
                outcome = "not sent: it waited behind a message that did not go"
            elif row["state"] == UNSURE:
                outcome = "may or may not have arrived: the door stopped answering while it was being sent"
            else:
                outcome = "not sent: the door would not take it"
            lines.append(f"- {outcome}: {quoted!r}")
        return "\n".join(lines)

    async def _mark(self, message_id: int, state: str, *, note: str | None = None) -> None:
        if note is None:
            await self.db.execute("UPDATE agent_talk_messages SET state = ? WHERE id = ?", (state, message_id))
        else:
            await self.db.execute("UPDATE agent_talk_messages SET state = ?, note = ? WHERE id = ?", (state, note, message_id))

    async def _await_decision(self, talk_id: str, message_id: int, wait: int) -> str:
        deadline = time.monotonic() + wait
        while True:
            news = self._news.setdefault(talk_id, asyncio.Event())
            row = await self._message(message_id)
            state = str(row["state"]) if row is not None else REFUSED
            left = deadline - time.monotonic()
            if state != HELD or left <= 0:
                return state
            with suppress(TimeoutError):
                await asyncio.wait_for(news.wait(), left)

    async def _read(self, state: SessionState, talk: Any, wait: int) -> Outcome:
        return await self._reply_or_status(state, talk, wait, "")

    async def _reply_or_status(self, state: SessionState, talk: Any, wait: int, lead: str, *, newer_than: int = 0) -> Outcome:
        """What came, or where the conversation stands. ``newer_than``, for a send, is its own message: what was unread
        from before it is handed over too, but is not the answer the send waits for. Without it a send returned at once
        with the tail of the last exchange (the agent's own answer that followed a card of free times), and the agent
        took that for the answer to its new question."""
        talk_id = talk["id"]
        self._readers[talk_id] = self._readers.get(talk_id, 0) + 1
        try:
            deadline = time.monotonic() + wait
            self.follow(talk_id)
            while True:
                news = self._news.setdefault(talk_id, asyncio.Event())
                unread = await self._unread(talk_id)
                talk = await self.talk(talk_id)
                left = deadline - time.monotonic()
                answered = bool(unread) and (not newer_than or any(int(row["id"]) > newer_than for row in unread))
                if answered or talk is None or talk["state"] == CLOSED or left <= 0 or talk["quiet"] or not talk["waiting_for"]:
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(news.wait(), left)
        finally:
            self._readers[talk_id] -= 1
            if not self._readers[talk_id]:
                del self._readers[talk_id]
        if talk is None:
            return Outcome("This conversation is gone.", error=True)
        if not unread:
            return Outcome(self._state_line(talk, lead), talk=talk_id)
        taken = self._portion(unread)
        more = [f"{len(unread) - len(taken)} more replies are unread: read again."] if len(taken) < len(unread) else []
        return Outcome("\n".join([self._state_line(talk, lead), *await self._fenced(taken, talk, state.run_id or "", state.session.id), *more, agent_talk.TOUCHED_NOTE]), talk=talk_id)

    @staticmethod
    def _portion(rows: list[Any]) -> list[Any]:
        """As many of ``rows`` as fit in :data:`MAX_READ_CHARS`, always at least one."""
        taken: list[Any] = []
        size = 0
        for row in rows:
            size += len(row["text"])
            if taken and size > MAX_READ_CHARS:
                break
            taken.append(row)
        return taken

    async def _fenced(self, rows: list[Any], talk: Any, run_id: str, reader: str) -> list[str]:
        """Hand unread replies to a run: fenced, marked read by that run, and the run touched. In that order of
        thought and the reverse in code: the run is touched first, so nothing can go wrong between its reading
        a stranger and its being treated as having done so."""
        # The chat whose run reads them, which for a contact written to from another chat is not the conversation's own.
        self.manager.touch_run(reader)
        rules = getattr(self.manager, "action_rules", None)
        out: list[str] = []
        for row in rows:
            signs: list[str] = []
            if rules is not None and hasattr(rules, "injection_check"):
                with suppress(Exception):  # a check that cannot run must not cost the reply: the fence is what holds
                    signs = await rules.injection_check(f"the door at {talk['host']}", row["text"])
            out.append(agent_talk.fence_reply(await self._as_read(row, talk), talk["host"], signs=signs))
            await self.db.execute("UPDATE agent_talk_messages SET state = ?, run_id = ? WHERE id = ?", (READ, run_id, row["id"]))
        return out

    async def _as_read(self, row: Any, talk: Any) -> str:
        """A reply as the model reads it: its words, its task, and where each of its files is, all inside the fence,
        because a door's kinds and names are as much a stranger's as its words."""
        lines = [str(row["text"])]
        if row["author"] == "owner":
            lines.insert(0, "[written by the other side's owner themselves, a person, and not by their agent]")
        if row["task"] and row["kind"] != link_protocol.TASK:
            lines.append(f"[about task {row['task']}]")
        state = await self.manager.get_state(str(talk["session_id"]))
        root = self.manager.work_dir(state) if state is not None else None
        for file in await self.db.fetchall("SELECT * FROM link_files WHERE side = ? AND message_id = ? ORDER BY id", (SIDE, row["id"])):
            path = Path(str(file["path"])) if file["path"] else None
            where = (path.relative_to(root).as_posix() if root is not None and path.is_relative_to(root) else str(path)) if path is not None else ""
            status = {"received": f"saved as {where}", "pending": "still arriving: read again in a moment", "refused": f"not taken: {file['note']}", "failed": "could not be fetched"}
            lines.append(f"[file {file['name']} ({file['size']} bytes): {status.get(str(file['state']), str(file['state']))}]")
        return "\n".join(line for line in lines if line)

    # -- following a door ---------------------------------------------------------------

    def _spawn(self, work: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(work, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def follow(self, talk_id: str) -> None:
        """Hold the door's read for this conversation, once. Nothing to follow ends the task at once."""
        running = self._followers.get(talk_id)
        if running is None or running.done():
            self._followers[talk_id] = self._spawn(self._follow(talk_id), f"agent-talk:{talk_id}")

    async def _follow(self, talk_id: str) -> None:
        failures = 0
        while True:
            talk = await self.talk(talk_id)
            if talk is None or talk["state"] == CLOSED or not talk["token"] or not talk["waiting_for"]:
                return
            started = time.monotonic()
            try:
                answer = await self.dialer.call(
                    "GET", f"{talk['base_url']}/a/messages?after={talk['cursor']}&wait={DOOR_WAIT_SECONDS}", token=talk["token"], timeout=DOOR_WAIT_SECONDS + CALL_SECONDS,
                )
                if door_absent(answer):
                    raise DoorGone(f"answered {answer.status} in place of the door")
            except (DoorGone, NotReachable) as exc:
                failures += 1
                logger.debug("the door of conversation %s did not answer: %s", talk_id, exc)
                if failures >= OFFLINE_AFTER and talk["waiting_for"] != OFFLINE:
                    await self._update(talk_id, waiting_for=OFFLINE, quiet=1)
                    self._wake_readers(talk_id)
                await asyncio.sleep(min(FAILURE_BACKOFF_MAX, MIN_CYCLE_SECONDS * 2**failures))
                continue
            failures = 0
            if talk["waiting_for"] == OFFLINE:
                self._door_back(talk_id)
            try:
                pause = await self._took(talk, answer)
            except Exception:  # noqa: BLE001 — nothing a door sends may stop what follows it silently
                logger.exception("could not read what the door of conversation %s sent", talk_id)
                pause = QUIET_POLL_SECONDS
            if pause is None:
                return
            await asyncio.sleep(max(pause, MIN_CYCLE_SECONDS - (time.monotonic() - started)))

    async def _glance(self, talk_id: str) -> None:
        """One look at a quiet conversation's door, without holding it open: what the other side wrote unasked.

        The door says something with nobody's message before it: the other owner's own words, files their operator let
        go after the turn had ended, the answer their owner gave later, a subscription's news. Followed only while a
        reply was owed, all of that sat at the door until this side happened to send again."""
        talk = await self.talk(talk_id)
        if talk is None or talk["state"] != OPEN or not talk["token"] or talk["waiting_for"]:
            return
        try:
            answer = await self.dialer.call("GET", f"{talk['base_url']}/a/messages?after={talk['cursor']}&wait=0", token=talk["token"], timeout=CALL_SECONDS)
            if door_absent(answer):
                return
            if await self._took(talk, answer) is not None:
                self.follow(talk_id)
        except (DoorGone, NotReachable) as exc:
            logger.debug("a look at conversation %s found no door: %s", talk_id, exc)

    async def _took(self, talk: Any, answer: DoorAnswer) -> float | None:
        """Take in one read. How long to wait before the next, or ``None`` when there is nothing more to look for."""
        talk_id = talk["id"]
        if answer.status == 401:
            await self._close(talk_id, "token")
            return None
        if answer.status == 410:
            await self._close(talk_id, self._reason(answer.body))
            return None
        if answer.status == 429:
            return answer.retry_after if answer.retry_after is not None else 15.0
        if answer.status not in (200, 202):
            return QUIET_POLL_SECONDS
        taken = await self._store(talk, answer.body.get("messages"), answer.body.get("cursor"))
        word = answer.body.get("status")
        if word == CLOSED:
            await self._close(talk_id, self._reason(answer.body))
            return None
        if word in (OPEN, PENDING) and word != talk["state"]:
            await self._update(talk_id, state=word)
        stop = answer.body.get("stop_polling") is True
        waiting = answer.body.get("waiting_for")
        if stop and (taken or waiting == "nothing"):
            if not await self._settled_by_read(talk, waiting_for="", quiet=0):
                return 0.0
            return None
        # The door is up and has kept what was sent, but its owner's agent has not been seen: offline, read slowly.
        owed = "owner" if waiting == "owner" else OFFLINE if waiting == "owner_online" else "reply"
        # Waiting on a person, theirs: admitting a first contact, or deciding on files or a question. No use holding a turn
        # for it; a first message to an owner who had not yet admitted it kept the agent's tool waiting its two minutes out.
        if stop or owed in (OFFLINE, "owner"):
            if not await self._settled_by_read(talk, waiting_for=owed, quiet=1):
                return 0.0
            self._wake_readers(talk_id)
            return max(QUIET_POLL_SECONDS, answer.retry_after or 0.0)
        await self._settled_by_read(talk, waiting_for=owed, quiet=0)
        return 0.0

    async def _settled_by_read(self, talk: Any, **fields: Any) -> bool:
        """Write what a read found the conversation waiting for, unless a message went or is going since the read left.

        One statement, not a look and then a write: what a door said of the state before a send is stale once it has
        gone. A read out before a send came back with "nothing owed" after the send had set its wait, cleared it, and
        the follower the send started found nothing to follow: the agent was told nothing was owed, its answer unread."""
        names = ", ".join(f"{name} = ?" for name in fields)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE agent_talks SET {names} WHERE id = ? AND sends = ? AND NOT EXISTS (SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state = ?)",  # noqa: S608 — names are this code's
                (*fields.values(), talk["id"], talk["sends"], talk["id"], SENDING),
            )
            done = cursor.rowcount > 0
            await cursor.close()
        return done

    def _reason(self, body: dict[str, Any]) -> str:
        reason = body.get("reason")
        return reason if reason in REASONS else "ended"

    async def _store(self, talk: Any, items: Any, cursor: Any) -> int:
        """Keep what a read brought, each message once, with the new cursor in the same breath. A message's kind, its
        task and its files are fields like any other a door sends: each is checked, and one that makes no sense is left out."""
        if not isinstance(items, list):
            return 0
        talk_id = talk["id"]
        last = int(talk["cursor"])
        kept: list[tuple[str, str]] = []
        moved = False
        files_came = False
        room = MAX_MESSAGES - int((await self.db.fetchone("SELECT count(*) AS n FROM agent_talk_messages WHERE talk_id = ?", (talk_id,)))["n"])
        contacts = contacts_of(self.app)
        contact = await contacts.of_talk(talk_id) if contacts is not None else None
        offers: list[tuple[Any, str]] = []
        mine = {str(row["task"]): str(row["state"]) for row in await self.db.fetchall("SELECT task, state FROM link_tasks WHERE side = ? AND owner = ? AND direction = 'mine'", (SIDE, talk_id))}
        # Each signature is checked before the transaction below, which holds the one connection: the check writes too.
        verdicts: dict[int, str] = {}
        for item in items[:MAX_PER_READ] if contacts is not None else []:
            if isinstance(item, dict) and item.get("from") in ("agent", "owner") and item.get("kind") != "contact" and isinstance(item.get("id"), int) and isinstance(item.get("text"), str):
                if item["id"] > last:
                    by = "owner" if item.get("from") == "owner" else ""
                    kind = item.get("kind") if item.get("kind") in link_protocol.KINDS else link_protocol.TEXT
                    seen = link_flows.card_view(item.get("card")) if item.get("from") == "agent" else None
                    verdicts[item["id"]] = await contacts.check_reply(talk, item, text=item["text"], kind=kind, task=_task_id(item.get("task")), by=by, card=seen)
        cards: list[dict[str, Any]] = []
        async with self.db.transaction() as conn:
            for item in items[:MAX_PER_READ]:
                if not isinstance(item, dict) or room <= 0:
                    continue
                door_id, sender, text = item.get("id"), item.get("from"), item.get("text")
                if isinstance(door_id, bool) or not isinstance(door_id, int) or door_id <= last or sender not in ("agent", "door", "owner") or not isinstance(text, str):
                    continue
                kind = item.get("kind") if item.get("kind") in (*link_protocol.KINDS, "contact") else link_protocol.TEXT
                task = _task_id(item.get("task"))
                state = item.get("state") if item.get("state") in link_protocol.TASK_STATES else ""
                files = _door_files(item.get("files"))
                clean = inert(text)[:MAX_TEXT]
                author = "owner" if sender == "owner" else ""
                if kind == "contact":
                    # An offer is the operator's to decide, not the agent's to read: kept as read, and handed to the contacts.
                    offers.append((item.get("contact"), clean))
                card = link_flows.card_view(item.get("card")) if sender == "agent" else None
                if kind == link_protocol.STATUS and task and state and not clean:
                    clean = f"Task {task}: {state.replace('_', ' ')}."
                if card is not None and not clean:
                    clean = link_flows.card_words(card)
                if not clean and not files and kind != "contact":
                    continue
                role = DOOR if sender == "door" else THEM
                signed = verdicts.get(door_id, "")
                refused = signed == "bad" and contact is not None
                if refused:
                    card = None
                cursor_row = await conn.execute(
                    "INSERT OR IGNORE INTO agent_talk_messages(talk_id, role, text, state, door_id, created_at, kind, task, task_state, author, signed, note, card)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (talk_id, role, clean, REFUSED if refused else READ if kind == "contact" else UNREAD, door_id, _now(), kind, task, state, author, signed, "key" if refused else "",
                     jsonlib.dumps(card, ensure_ascii=False) if card is not None else ""),
                )
                message_id = int(cursor_row.lastrowid or 0) if cursor_row.rowcount else 0
                await cursor_row.close()
                if refused:
                    # Under another key, or none: nothing of it reaches a model, a task or a file fetch.
                    last = door_id
                    room -= 1
                    continue
                for file in files if message_id else []:
                    await conn.execute(
                        "INSERT INTO link_files(side, owner, session_id, direction, name, size, sha256, mime, remote_id, task, message_id, state, created_at)"
                        " VALUES (?, ?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (SIDE, talk_id, talk["session_id"], file["name"], file["size"], file["sha256"], link_protocol.mime_of(file["name"]), file["id"], task, message_id, _now()),
                    )
                    files_came = True
                if task in mine and state and mine[task] in link_protocol.OPEN_TASK_STATES and state != mine[task]:
                    result = clean if state in (link_protocol.DONE, link_protocol.REFUSED, link_protocol.NEEDS_INPUT) else ""
                    await conn.execute(
                        "UPDATE link_tasks SET state = ?, result = CASE WHEN ? != '' THEN ? ELSE result END, updated_at = ? WHERE side = ? AND owner = ? AND task = ?",
                        (state, result, result[:4000], _now(), SIDE, talk_id, task),
                    )
                    mine[task] = state
                    moved = moved or state in link_protocol.WAKING_STATES
                last = door_id
                room -= 1
                if kind != "contact":
                    # A summary or a service shown asks nothing back: kept and shown, and no turn wakes for it.
                    kept.append(("card" if card is not None and card["type"] in QUIET_CARDS else role, clean))
                if card is not None and message_id:
                    cards.append(card)
            await conn.execute("UPDATE agent_talks SET cursor = ?, last_at = ? WHERE id = ?", (last, _now() if kept else talk["last_at"], talk_id))
        for offer, words in offers if contacts is not None else []:
            await contacts.on_talk_contact(talk, offer, words)
        if room <= 0:
            await self._close(talk_id, "too_long")
        if files_came:
            self._spawn(self._fetch_files(talk_id), f"agent-talk-files:{talk_id}")
        flows = flows_module.flows_of(self.app)
        for card in cards if flows is not None else []:
            await flows.keep_card(SIDE, talk_id, str(talk["session_id"]), "theirs", card, text=link_flows.card_words(card))
        if kept:
            await self._arrived(talk, kept, task_moved=moved)
        return len(kept)

    async def _fetch_files(self, talk_id: str) -> None:
        """Bring what the other side sent into ``guest-files/<conversation>/`` in the chat's folder, within this
        conversation's limits; what is over them stays at the other door and is marked so."""
        async with self._locks.setdefault(f"files:{talk_id}", asyncio.Lock()):
            talk = await self.talk(talk_id)
            if talk is None or not talk["token"]:
                return
            state = await self.manager.get_state(str(talk["session_id"]))
            if state is None:
                return
            fs = state.services.fs if state.services is not None else None
            root = self.app.settings.state_dir / link_protocol.STAGING / "in" if fs is not None and getattr(fs, "remote", False) else self.manager.work_dir(state)
            directory = link_protocol.incoming_dir(root, talk_id)
            for row in await self.db.fetchall("SELECT * FROM link_files WHERE side = ? AND owner = ? AND direction = 'in' AND state = 'pending' ORDER BY id", (SIDE, talk_id)):
                why = link_protocol.check_limits([int(row["size"])], file_mb=float(talk["file_mb"]), day_mb=float(talk["day_mb"]), used_today=await self._bytes_today(talk_id, "in") - int(row["size"]))
                if why:
                    await self.db.execute("UPDATE link_files SET state = 'refused', note = ? WHERE id = ?", (why, row["id"]))
                    continue
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                target = link_protocol.free_path(directory, str(row["name"]))
                url = f"{talk['base_url']}/a/files/{row['remote_id']}/content"

                async def piece(offset: int, url: str = url) -> bytes:
                    status, data = await self.dialer.fetch(f"{url}?offset={offset}", token=str(talk["token"]))
                    if status != 200:
                        raise LinkFileError(f"the door answered {status}")
                    return data

                try:
                    await link_protocol.fetch_pieces(target, int(row["size"]), str(row["sha256"]), piece)
                except (LinkFileError, DoorGone, NotReachable, OSError) as exc:
                    await self.db.execute("UPDATE link_files SET state = 'failed', note = ? WHERE id = ?", (str(exc)[:200], row["id"]))
                    continue
                await self.db.execute("UPDATE link_files SET state = 'received', path = ? WHERE id = ?", (str(target), row["id"]))

    async def _task_set(self, talk: Any, task: str, text: str) -> None:
        """A task this side set on the other: recorded, and one more turn its changes may open with waking off."""
        now = _now()
        await self.db.execute(
            "INSERT OR IGNORE INTO link_tasks(side, owner, session_id, task, direction, text, state, created_at, updated_at) VALUES (?, ?, ?, ?, 'mine', ?, ?, ?, ?)",
            (SIDE, talk["id"], talk["session_id"], task, text[:4000], link_protocol.ACCEPTED, now, now),
        )
        await self.db.execute("UPDATE agent_talks SET task_wakes = min(?, task_wakes + 1) WHERE id = ?", (TASK_WAKES_MAX, talk["id"]))

    async def cancel(self, talk_id: str, task: str) -> Outcome:
        """Cancel a task this side set. The other side stops its turn on it; a task that is over stays as it ended."""
        talk = await self.talk(talk_id)
        row = await self.db.fetchone("SELECT * FROM link_tasks WHERE side = ? AND owner = ? AND task = ? AND direction = 'mine'", (SIDE, talk_id, task)) if talk is not None else None
        if talk is None or row is None:
            return Outcome(f"This conversation has no task {task} of yours.", error=True, talk=talk_id)
        if row["state"] not in link_protocol.OPEN_TASK_STATES:
            return Outcome(f"The task {task} is already {row['state']}.", talk=talk_id)
        if talk["state"] != CLOSED and talk["token"]:
            try:
                answer = await self.dialer.call("POST", f"{talk['base_url']}/a/messages", token=talk["token"], body={"kind": link_protocol.STATUS, "task": task, "state": link_protocol.CANCELLED})
            except (DoorGone, NotReachable):
                return Outcome("The door did not answer, so the task may still be running there. Try again.", error=True, talk=talk_id)
            if answer.status not in (200, 409):
                return Outcome("The door would not cancel the task.", error=True, talk=talk_id)
        await self.db.execute("UPDATE link_tasks SET state = ?, updated_at = ? WHERE id = ?", (link_protocol.CANCELLED, _now(), row["id"]))
        return Outcome(f"The task {task} is cancelled.", talk=talk_id)

    def _wake_readers(self, talk_id: str) -> None:
        news = self._news.pop(talk_id, None)
        if news is not None:
            news.set()

    async def _arrived(self, talk: Any, kept: list[tuple[str, str]], *, task_moved: bool = False) -> None:
        talk_id = talk["id"]
        self._wake_readers(talk_id)
        spoken = [text for role, text in kept if role == THEM]
        if spoken:
            await self._count_repeats(talk, spoken)
        flows = flows_module.flows_of(self.app)
        if spoken and flows is not None and await flows.poll_reply(talk_id):
            return
        fresh = await self.talk(talk_id)
        if fresh is None or self._readers.get(talk_id):
            return
        if fresh["wake_turns"] != 0 and (spoken or task_moved):
            self.wake(talk_id)
        elif task_moved and fresh["task_wakes"] > 0:
            # The side that set a task wakes when the task moves, whether or not the operator switched waking on.
            self.wake(talk_id)
        elif spoken:
            await self._tell(fresh, "reply", every=REPLY_NOTICE_SECONDS)

    async def _count_repeats(self, talk: Any, spoken: list[str]) -> None:
        rows = await self.db.fetchall("SELECT text FROM agent_talk_messages WHERE talk_id = ? AND role = ? ORDER BY id DESC LIMIT ?", (talk["id"], THEM, len(spoken) + 1))
        texts = [_same(str(row["text"])) for row in rows]
        repeats = int(talk["repeats"]) + 1 if len(texts) >= 2 and len(set(texts)) == 1 else 0
        await self._update(talk["id"], repeats=repeats)
        if repeats >= REPEATS_ALLOWED and talk["wake_turns"] != 0:
            # The surest sign of two agents politely stuck, and each turn of it is paid for on both sides.
            await self._update(talk["id"], wake_turns=0)
            await self._tell(talk, "repeats")

    async def _tell(self, talk: Any, key: str, *, every: float = 0.0) -> None:
        """A notice in this code's words and the host's name. Never a word of the reply: this text travels to
        Telegram, where a link in a stranger's message would be fetched by a preview."""
        notifications = getattr(self.app, "notifications", None)
        if notifications is None:
            return
        now = time.monotonic()
        mark = (talk["id"], key)
        if every and now - self._told.get(mark, -every) < every:
            return
        self._told[mark] = now
        lang = notifications.language()
        session_id = str(talk["session_id"])
        await notifications.post(Draft(
            "system", render(f"talk.{key}", lang, host=talk["host"]), render(f"talk.{key}.body", lang), kind=f"agent_talk_{key}", session_id=session_id, link=chat_link(session_id),
            source="agent_talk", dedupe_key=f"agent-talk-{key}:{talk['id']}:{int(time.time() // 3600)}",
        ))

    # -- a reply that opens a turn ------------------------------------------------------

    def wake(self, talk_id: str) -> None:
        running = self._wakers.get(talk_id)
        if running is None or running.done():
            self._wakers[talk_id] = self._spawn(self._wake(talk_id), f"agent-talk-wake:{talk_id}")

    async def _wake(self, talk_id: str) -> None:
        """Open a turn on the unread replies once the chat is free. Not at all once something has read them."""
        deadline = time.monotonic() + CHAT_FREE_SECONDS
        while time.monotonic() < deadline:
            talk = await self.talk(talk_id)
            if talk is None or talk["state"] == CLOSED or (talk["wake_turns"] == 0 and talk["task_wakes"] <= 0):
                return
            by_task = talk["wake_turns"] == 0
            unread = await self._unread(talk_id)
            if not unread:
                return
            if self._readers.get(talk_id):
                await asyncio.sleep(1.0)
                continue
            session_id = str(talk["session_id"])
            manager = self.manager
            why = await manager.guest_ready(session_id, "none")
            price = await manager.guest_price(session_id)
            if why or price is None:
                # Told once an hour, and left unread: the operator's next word, or a later sweep, picks it up.
                logger.info("a reply could not wake session %s: %s", session_id, why or "the chat's model has no known price")
                await self._tell(talk, "reply", every=REPLY_NOTICE_SECONDS)
                return
            runs = (await self.runs_of(talk))[-RUNS_KEPT:]
            unread = self._portion(unread)
            said = "\n\n".join(str(row["text"]) for row in unread)
            body = "\n".join([agent_talk.fence_reply(await self._as_read(row, talk), talk["host"]) for row in unread])
            usd_left: float | None = None
            if by_task and any(price):
                usd_left = TURN_USD
                if await manager.guest_first_call_usd(session_id, price, runs=runs, extra_chars=len(body)) > usd_left:
                    await self._update(talk_id, task_wakes=0)
                    await self._tell(talk, "wake_spent")
                    return
            elif any(price) and float(talk["wake_usd"]) != NO_LIMIT:
                usd_left = min(float(talk["wake_usd"]), TURN_USD)
                if usd_left <= 0 or await manager.guest_first_call_usd(session_id, price, runs=runs, extra_chars=len(body)) > usd_left:
                    await self._wake_spent(talk)
                    return
            turn = GuestTurn(talk_id, str(talk["host"]), "none", usd_left, said, "reply", tuple(runs))
            try:
                run_id = await manager.submit(session_id, body, as_answer=False, origin=turn.origin, via="agent_talk", guest=turn)
            except GuestTurnBusy:
                await asyncio.sleep(1.0)
                continue
            except (RuntimeError, KeyError) as exc:
                logger.warning("a reply could not open a turn in session %s: %s", session_id, exc)
                return
            # Whatever follows this turn before the operator writes is a run over a stranger's words too.
            manager.touch_run(session_id)
            for row in unread:
                await self.db.execute("UPDATE agent_talk_messages SET state = ?, run_id = ? WHERE id = ?", (READ, run_id, row["id"]))
            # Before the turn ends and not after: a turn cut off by a restart was still a turn.
            if by_task:
                await self.db.execute("UPDATE agent_talks SET task_wakes = max(0, task_wakes - 1) WHERE id = ?", (talk_id,))
            else:
                await self.db.execute("UPDATE agent_talks SET wake_turns = wake_turns - 1 WHERE id = ? AND wake_turns > 0", (talk_id,))
            self._wakes[run_id] = talk_id
            if not manager.is_running(session_id):
                await self.on_run_finished(session_id, run_id, "completed")
            return

    async def _task_woken(self, talk_id: str) -> bool:
        """Whether this conversation's waking was ever the operator's: one that only tasks woke says nothing of turns spent."""
        talk = await self.talk(talk_id)
        return talk is not None and talk["wake_turns"] == 0 and float(talk["wake_usd"]) == 0 and await self.db.fetchone(
            "SELECT 1 FROM link_tasks WHERE side = ? AND owner = ? AND direction = 'mine'", (SIDE, talk_id),
        ) is not None

    async def _wake_spent(self, talk: Any) -> None:
        await self._update(talk["id"], wake_turns=0)
        await self._tell(talk, "wake_spent")

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if status == "awaiting":
            return
        talk_id = self._wakes.pop(run_id, None)
        if talk_id is None:
            return
        spent, _ = await self.manager.spend(run_id=run_id)
        await self.db.execute("UPDATE agent_talks SET wake_usd = max(0, wake_usd - ?) WHERE id = ? AND wake_usd >= 0", (float(spent), talk_id))
        talk = await self.talk(talk_id)
        if talk is None or talk["state"] == CLOSED:
            return
        price = await self.manager.guest_price(session_id)
        if talk["wake_turns"] == 0 and talk["task_wakes"] > 0:
            # A turn a task opened, with waking off: what is still unread waits for the operator, or the next task's change.
            return
        if talk["wake_turns"] == 0 or (price is not None and any(price) and talk["wake_usd"] == 0):
            if not await self._task_woken(talk_id):
                await self._wake_spent(talk)
        elif await self._unread(talk_id):
            self.wake(talk_id)

    # -- what the operator does ---------------------------------------------------------

    async def decided(self, session_id: str, request: dict[str, Any], allowed: bool) -> None:
        """The answer on a message's card."""
        talk_id, message_id = str(request.get("talk") or ""), request.get("message")
        if isinstance(message_id, int):
            await self.decide(talk_id, message_id, allowed)

    async def decide(self, talk_id: str, message_id: int, allowed: bool, *, via: str = "", text: str | None = None, drop: list[int] | None = None) -> bool:
        """Let a held message go, or not. What goes is what the row holds: no model is asked to say it again.

        ``text`` is the operator's own edit of the message, which replaces the row in the same step that claims
        it, so what is posted and what the conversation shows as sent are one text. A value the operator handed
        over goes out as its placeholder, as it would from the agent. ``ValueError`` for an edit that is empty
        or longer than the other door takes, before anything is claimed."""
        talk = await self.talk(talk_id)
        message = await self._message(message_id)
        if talk is None or message is None or message["talk_id"] != talk_id:
            return False
        edited: str | None = None
        if allowed and text is not None:
            edited = self.manager.redactor.conceal_named(text).strip()
            if not edited:
                raise ValueError("the message is empty")
            if len(edited) > talk["max_text"]:
                raise ValueError(f"a message may be at most {talk['max_text']} characters")
        held_files = await self._files_of(message_id, "held")
        dropped = {int(i) for i in drop or ()} & {int(row["id"]) for row in held_files}
        words = edited if edited is not None else str(message["text"])
        if allowed and held_files and len(dropped) == len(held_files) and not words.strip():
            # Every file held back and no words: there is nothing left to send.
            allowed = False
        if not await self._claim(message_id, SENDING if allowed else REFUSED, text=edited):
            return False
        for row in held_files:
            gone = not allowed or int(row["id"]) in dropped
            await self.db.execute("UPDATE link_files SET state = ? WHERE id = ?", ("dropped" if gone and allowed else "refused" if gone else "sending", row["id"]))
            if gone:
                link_protocol.unstage([Staged(str(row["name"]), Path(str(row["path"])), 0, "", "")])
        if allowed:
            await self._post(talk_id, message_id, words)
        if via:
            await self.manager.settle_ask(talk["session_id"], ask_key(message_id), "allow" if allowed else "deny", via=via)
        self._wake_readers(talk_id)
        return True

    async def _claim(self, message_id: int, state: str, *, text: str | None = None) -> bool:
        """Take a held message out of waiting, once: a card and the sheet answered together must not both act."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("UPDATE agent_talk_messages SET state = ?, text = coalesce(?, text) WHERE id = ? AND state = ?", (state, text, message_id, HELD))
            claimed = cursor.rowcount > 0
            await cursor.close()
        return claimed

    async def configure(
        self, talk_id: str, *, review: str | None = None, wake_turns: int | None = None, wake_usd: float | None = None, sends_limit: int | None = None,
        file_mb: float | None = None, day_mb: float | None = None,
    ) -> None:
        """The operator's settings. :data:`NO_LIMIT` for turns or dollars, and 0 for ``sends_limit`` and the file limits, are none at all."""
        if review is not None:
            await self._update(talk_id, review=review)
        if file_mb is not None:
            await self._update(talk_id, file_mb=file_mb)
        if day_mb is not None:
            await self._update(talk_id, day_mb=day_mb)
        if sends_limit is not None:
            await self._update(talk_id, sends_limit=sends_limit)
        if wake_turns is not None:
            await self._update(talk_id, wake_turns=wake_turns, wake_usd=wake_usd or 0.0)
            if wake_turns != 0:
                self.wake(talk_id)

    async def end(self, talk_id: str, *, reason: str = "ended") -> None:
        """Close a conversation on both sides. The far side's part is a courtesy: this side is closed either way."""
        talk = await self.talk(talk_id)
        if talk is None or talk["state"] == CLOSED:
            return
        flows = flows_module.flows_of(self.app)
        if reason == "ended" and flows is not None and await self._summary_owed(talk_id):
            # The end of a conversation that said something leaves both sides the same summary, as the button does.
            try:
                await flows.summarise_talk(talk_id)
            except Exception:  # noqa: BLE001 — a summary that could not be made must not keep a conversation open
                logger.exception("the summary of conversation %s at its end failed", talk_id)
        # Closed here first: whatever the door does with the call below, the token is gone from this side.
        await self._close(talk_id, reason, quiet=True)
        if talk["token"]:
            with suppress(DoorGone, NotReachable):
                await self.dialer.call("POST", f"{talk['base_url']}/a/end", token=talk["token"])

    async def _summary_owed(self, talk_id: str) -> bool:
        """Whether ending this conversation should leave a summary: the other side said something, and no summary of
        ours went since it last did."""
        last = await self.db.fetchone("SELECT max(id) AS id FROM agent_talk_messages WHERE talk_id = ? AND role = ?", (talk_id, THEM))
        if last is None or last["id"] is None:
            return False
        return await self.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND role = ? AND note = 'summary' AND id > ?", (talk_id, US, last["id"])) is None

    async def _close(self, talk_id: str, reason: str, *, quiet: bool = False) -> None:
        talk = await self.talk(talk_id)
        if talk is None or talk["state"] == CLOSED:
            return
        await self._update(talk_id, state=CLOSED, reason=reason, token="", waiting_for="", quiet=0, wake_turns=0, closed_at=_now())
        await self.db.execute("UPDATE agent_talk_messages SET state = ?, note = 'closed' WHERE talk_id = ? AND state = ?", (FAILED, talk_id, WAITING))
        held = await self.db.fetchall("SELECT id FROM agent_talk_messages WHERE talk_id = ? AND state = ?", (talk_id, HELD))
        for row in held:
            await self._claim(int(row["id"]), REFUSED)
            await self.manager.settle_ask(talk["session_id"], ask_key(int(row["id"])), "deny", via="app")
        for row in await self.db.fetchall("SELECT * FROM link_files WHERE side = ? AND owner = ? AND direction = 'out' AND state IN ('held', 'sending')", (SIDE, talk_id)):
            await self.db.execute("UPDATE link_files SET state = 'refused' WHERE id = ?", (row["id"],))
            link_protocol.unstage([Staged(str(row["name"]), Path(str(row["path"])), 0, "", "")])
        await self.db.execute(
            "UPDATE link_tasks SET state = ?, updated_at = ? WHERE side = ? AND owner = ? AND state IN ('accepted', 'working', 'needs_input')", (link_protocol.CANCELLED, _now(), SIDE, talk_id),
        )
        self._wake_readers(talk_id)
        contacts = contacts_of(self.app)
        if contacts is not None:
            await contacts.on_talk_closed(talk_id, reason)
        if not quiet:
            await self._tell(talk, "closed")

    async def on_session_deleted(self, session_id: str) -> None:
        for talk in await self.talks_of(session_id):
            await self.end(talk["id"])
            follower = self._followers.pop(talk["id"], None)
            if follower is not None:
                follower.cancel()

    # -- what the agent is told at the start of a turn ------------------------------------

    async def turn_notes(self, state: SessionState) -> str:
        """The conversations of this chat, for a turn of the operator's. In the turn context and not the prompt, for
        the prompt's cache, and written here: nothing in it is a word of the other side's but the host's name."""
        if state.run_origin != "operator":
            return ""
        contacts = contacts_of(self.app)
        standing = await contacts.turn_notes() if contacts is not None else ""
        bridge = bridge_of(self.app)
        guests = [
            f"- A guest of this chat's link {guest['label']!r} is in conversation {guest['conversation']}: AgentTalk send with conversation={guest['conversation']} "
            "writes to them as you, text and files, if the operator's message is about them."
            for guest in (await bridge.open_guests(state.session.id) if bridge is not None else [])
        ]
        standing = "\n".join(part for part in (standing, *guests) if part)
        talks = await self.talks_of(state.session.id)
        if not talks:
            return standing
        lines: list[str] = [standing] if standing else []
        for talk in talks:
            if talk["state"] == CLOSED:
                continue
            unread = len(await self._unread(talk["id"]))
            tasks = await self.db.fetchall("SELECT task, state FROM link_tasks WHERE side = ? AND owner = ? AND direction = 'mine' AND state IN ('accepted', 'working', 'needs_input')", (SIDE, talk["id"]))
            open_tasks = f", tasks of yours open there: {', '.join(f'{row['task']} ({row['state']})' for row in tasks)}" if tasks else ""
            lines.append(f"- An open conversation with the agent at {talk['host']} ({talk['id']}): {talk['sends']} sent, {unread} replies unread{open_tasks}. Use AgentTalk for it if the operator's message is about it.")
        if await self.db.fetchone(
            f"SELECT 1 FROM agent_talk_messages WHERE role = ? AND state = ? AND talk_id IN ({','.join('?' * len(talks))}) LIMIT 1",
            (THEM, READ, *(talk["id"] for talk in talks)),
        ) is not None:
            lines.append(agent_talk.LATER_NOTE)
        return "\n".join(lines)

    # -- the long run -------------------------------------------------------------------

    async def _sweep(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(seconds=IDLE_SECONDS)).isoformat()
        for talk in await self.db.fetchall("SELECT * FROM agent_talks WHERE state != ?", (CLOSED,)):
            try:
                standing = await self.db.fetchone("SELECT 1 FROM link_contacts WHERE talk_id = ? AND state IN ('offered', 'accepting', 'active')", (talk["id"],))
                # A contact's conversation waits as long as the contact lasts: that it may be quiet for weeks is the point of one.
                # A message that waits for the operator or for the door is somebody still meaning to talk.
                if standing is None and talk["last_at"] < cutoff and await self.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND state IN (?, ?)", (talk["id"], HELD, WAITING)) is None:
                    await self.end(talk["id"], reason="idle")
                    await self._tell(talk, "closed")
                    continue
                if talk["waiting_for"]:
                    self.follow(talk["id"])
                elif talk["state"] == OPEN and talk["token"]:
                    self._spawn(self._glance(talk["id"]), f"agent-talk-glance:{talk['id']}")
                if await self.db.fetchone("SELECT 1 FROM link_files WHERE side = ? AND owner = ? AND direction = 'in' AND state = 'pending'", (SIDE, talk["id"])):
                    # A fetch a restart or a dropped connection cut short goes on from where its piece file stopped.
                    self._spawn(self._fetch_files(talk["id"]), f"agent-talk-files:{talk['id']}")
                if talk["wake_turns"] != 0:
                    self.wake(talk["id"])
                # Picked up after a restart: what waits for the door, and word owed of what became of it.
                if await self._waiting_count(talk["id"]):
                    self._retry_later(talk["id"])
                elif await self.db.fetchone("SELECT 1 FROM agent_talk_messages WHERE talk_id = ? AND wake_state = 'owed'", (talk["id"],)):
                    self.announce(talk["id"])
            except Exception:  # noqa: BLE001 — one conversation that cannot be swept must not cost the others theirs
                logger.exception("could not sweep conversation %s", talk["id"])

    async def serve(self) -> None:
        """Pick up what a restart left, then keep the slow clock: idle ends, and anything that fell off.

        A message caught on its way out is left as sent-or-not: sending it again could be saying it twice. One that
        waits for the door was never sent, and goes on waiting.
        """
        for row in await self.db.fetchall("SELECT id, talk_id FROM agent_talk_messages WHERE state = ? AND waiting_since != ''", (SENDING,)):
            # A retry the restart cut short may have arrived: the chat hears so, and what waited behind it does not
            # go after a message the other side may never have had.
            await self.db.execute("UPDATE agent_talk_messages SET state = ?, wake_state = 'owed' WHERE id = ?", (UNSURE, row["id"]))
            await self._settle_waiting(str(row["talk_id"]), "behind")
        await self.db.execute("UPDATE agent_talk_messages SET state = ? WHERE state = ?", (UNSURE, SENDING))
        await self.db.execute("DELETE FROM agent_talks WHERE state = ? AND token = ''", (OPENING,))
        try:
            while True:
                try:
                    await self._sweep()
                except Exception:  # noqa: BLE001 — the next sweep may do better; nothing here may end the task
                    logger.exception("the sweep of agent talks failed")
                await asyncio.sleep(SWEEP_SECONDS)
        finally:
            for task in list(self._background):
                task.cancel()


def _present(answer: DoorAnswer) -> DoorAnswer:
    """``answer``, or :class:`DoorGone` with nothing sent when a front answered in the door's place."""
    if door_absent(answer):
        raise DoorGone(f"answered {answer.status} in place of the door", unsent=True)
    return answer


QUIET_CARDS = frozenset({link_flows.SUMMARY, link_flows.SERVICE})


def _card_of(row: Any) -> dict[str, Any] | None:
    """A message's card as the app draws it: ours as we sent it, theirs checked again, as everything of theirs is."""
    if not row["card"]:
        return None
    card = link_flows.card_view(jsonlib.loads(row["card"]))
    return link_flows.as_seen(card, "mine" if row["role"] == US else "theirs") if card is not None else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _task_id(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"t_[0-9a-f]{12}", value) else ""


def _door_files(value: Any) -> list[dict[str, Any]]:
    """The files a reply names, each field checked; one that makes no sense is left out, and so is anything past a message's worth."""
    out: list[dict[str, Any]] = []
    for item in value[: link_protocol.MAX_FILES] if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        file_id, size, sha = item.get("id"), item.get("size"), str(item.get("sha256") or "")
        if isinstance(file_id, str) and re.fullmatch(r"f_[0-9a-f]{16}", file_id) and _count(size) > 0 and link_protocol.SHA256.fullmatch(sha):
            out.append({"id": file_id, "name": link_protocol.safe_name(item.get("name")), "size": int(size), "sha256": sha})
    return out


def contacts_of(app: Application | Any) -> Any:
    from daedalus.extensions.agent_contacts import contacts_of as found  # Lazy: contacts import this module.

    return found(app)


def talks_of(app: Application | Any) -> AgentTalks | None:
    return (getattr(app, "extensions", None) or {}).get(EXTENSION_KEY)


_filter_installed: list[bool] = []


def install_log_filter() -> None:
    if not _filter_installed:
        logging.getLogger("httpx").addFilter(HideDoorPaths())
        _filter_installed.append(True)


async def install(app: Application) -> list[asyncio.Task[None]]:
    talks = AgentTalks(app)
    app.extensions[EXTENSION_KEY] = talks
    install_log_filter()
    manager = app.manager
    if manager is None:
        return []
    manager.service_hooks["agent_talk"] = talks.tool
    manager.service_hooks["agent_talk_notes"] = talks.turn_notes
    manager.answer_hooks[ASK_HOOK] = talks.decided
    manager.on_finished(talks.on_run_finished)
    manager.delete_hooks.append(talks.on_session_deleted)
    return [asyncio.create_task(talks.serve(), name="agent-talks")]


__all__ = ["ASK_HOOK", "EXTENSION_KEY", "MAX_SENDS", "NO_LIMIT", "SIDE", "TASK_WAKES_MAX", "REVIEWS", "WAKE_TURNS_MAX", "WAKE_USD_MAX", "AgentTalks", "Dialer", "DoorAnswer", "DoorGone", "HideDoorPaths", "NotReachable", "Outcome", "ask_key", "door_absent", "install", "public_addresses", "talks_of"]
