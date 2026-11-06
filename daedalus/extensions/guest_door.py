"""The host's end of the guest door: working the door, and running a direct link's turns.

The door (``deploy/guestd``) is a process with nothing in it, and this is the only code that talks
to it, always by dialling its unix socket. For a stand-in link nothing passes through here but the
operator's own actions: the stand-in's conversation happens entirely on the other side.

A direct link is the narrow exception, and it is kept narrow on purpose. One thing comes in: a guest's
words, claimed from the door's feed. One thing goes out: the final text of the turn those words
started. What that turn may do is decided here from this database, where the session and the tool
setting of a link were written when the operator made it. The door never held them, so a door that
goes wrong can make up a guest's words but cannot aim them at another chat or widen the turn they start.

The door is treated as what it faces: nothing that comes from it is believed. A feed is checked item by item
and one bad item costs that item. The money a turn is given is clamped to what this side recorded for the link,
and a notification is made of this side's words and label. The token lives in a directory only this side
writes, so nothing in the door's own directory is ever opened from here; the socket there is only dialled,
and the peer has to be the door's user.

Each guest's message is a run of its own, started only when the chat is free. It waits here rather
than in the session's queue, because that queue feeds whatever run comes next, and an operator's run
must never pick up a guest's words as its input.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import os
import re
import secrets
import socket
import stat
import struct
import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpcore
import httpx

from daedalus import public_address
from daedalus.config import is_keyproxy_url, keyproxy_base
from daedalus.extensions.notifications import Action, ActionConflict, ActionOutcome, ActionRefused, ActionRequest, Draft
from daedalus.host import guest_turns
from daedalus.host.guest_turns import GuestTurn, GuestTurnBusy
from daedalus.host.notify_text import render

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSION_KEY = "guest_door"
SOCKET_NAME = "control.sock"
ENDPOINT_NAME = "control.endpoint"
TOKEN_NAME = "control.token"
TOKEN_MAX_BYTES = 256
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GUEST_TEXT = 16_000
SEEN_KEPT = 4096
CONTACTS_PER_HOUR = 12
"""First-contact notices in an hour, across all links, whatever a door claims happened."""
REF = "guest"
"""``guest:<conversation id>`` is the first-contact request a notification answers."""
FEED_KV = "guest_door_feed_cursor"
FEED_WAIT_SECONDS = 25
RECONNECT_SECONDS = 5.0
CALL_SECONDS = 10.0
CHAT_FREE_SECONDS = 900.0
"""How long a guest's message waits for its chat. Past it the guest is told so, rather than a turn
starting an hour later for a guest who has long stopped reading."""
NOTIFY_EVERY_SECONDS = 3600.0
"""At most one notice of each kind per link in this long: a guest must not be able to page the operator."""


def chat_link(session_id: str | None) -> str:
    """Where a notification about a link opens: the chat that owns it, as every other notification names its chat.

    The app has no screen of its own for guests: the sheet is in the chat's menu. A link starting with
    anything but ``/app/`` is ignored by the app, and a push built from an empty one opens the inbox.
    """
    return f"/app/agents/{session_id}" if session_id else ""


class DoorUnavailable(RuntimeError):
    """The door is not running, or was never set up."""


class DoorRefused(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


_OPEN_GUARDED = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
"""Neither name exists on Windows, where the directory is the operator's own and the launcher's to make."""


def ensure_token(directory: Path, door_uid: int) -> str:
    """This side's token for the door, made once in a directory only this side writes.

    Opened as if the directory were hostile anyway: not following a link, not waiting on a pipe, only a
    regular file of a token's size. This runs as root in the agent's container; a mistake about who
    owns a volume must not turn into reading some other file out to a socket, or into a frozen loop.
    """
    path = directory / TOKEN_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | _OPEN_GUARDED | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_GUARDED, 0o640)
        with os.fdopen(descriptor, "w") as file:
            if door_uid >= 0:  # natively there is no other user to hand it to
                with suppress(PermissionError):  # only root can give a group away; a test is not root
                    os.fchown(file.fileno(), -1, door_uid)
            file.write(token)
        return token
    with os.fdopen(descriptor, "rb") as file:
        found = os.fstat(file.fileno())
        if not stat.S_ISREG(found.st_mode) or found.st_size > TOKEN_MAX_BYTES:
            raise DoorUnavailable("the door's token is not a token")
        return file.read(TOKEN_MAX_BYTES).decode("ascii", "replace").strip()


class DoorSocket(httpcore.AnyIOBackend):
    """A connection to the door's socket, refused unless the door is really what answered.

    The directory is the door's, so the name in it could be a link to any other daemon's socket here,
    and this process would connect as root and write to it. Checked on the connection itself, after
    the name has been resolved, where it cannot be swapped.

    Two checks, each on the connection and so past any swap of the name. The peer's user, in a container:
    natively the door is the same user as this process, and ``SO_PEERCRED`` does not exist on a Mac. And
    everywhere, the path the peer bound: the door, behind its wall, can leave a link in its folder to any
    other socket of this user, and this process would hand that daemon the door's token. A socket says what
    it was bound as, and the door cannot bind another daemon's path from where it stands.
    """

    def __init__(self, door_uid: int) -> None:
        self.door_uid = door_uid

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise httpcore.ConnectError("the door's socket is not a socket")
        stream = await super().connect_unix_socket(path, timeout=timeout, socket_options=socket_options)
        sock = stream.get_extra_info("socket")
        same_path = sock.getpeername() == path
        same_user = self.door_uid < 0 or struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1] == self.door_uid
        if not same_path or not same_user:
            await stream.aclose()
            raise httpcore.ConnectError("something other than the door answered on its socket")
        return stream


class DoorClient:
    """HTTP over the door's unix socket, with a token this side owns."""

    def __init__(self, run_dir: Path | None, *, token_dir: Path | None = None, door_uid: int = -1, transport: httpx.AsyncBaseTransport | None = None, token: str = "", listen: str = "unix") -> None:
        self.run_dir = run_dir
        self.listen = listen
        """``tcp`` where the door has a port in place of a socket, as the launcher decided. The door's own
        folder can say which port, and is believed only in that form: otherwise a door taken over behind its
        wall could write a file there and send this process, with the token, to any port on this machine."""
        self.token_dir = token_dir
        self.door_uid = door_uid
        self.transport = transport
        self.token = token
        """Read once: the door cannot change the file, and a read per call is a read on the event loop."""
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return (self.run_dir is not None and self.token_dir is not None) or self.transport is not None

    def _ready(self) -> httpx.AsyncClient:
        if not self.configured:
            raise DoorUnavailable("no guest door is set up on this installation")
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self.transport or self._dial(), base_url=self._address(), timeout=CALL_SECONDS, trust_env=False)
        return self._client

    def _address(self) -> str:
        """The door's own loopback port where it has one in place of a socket (see ``deploy/guestd``)."""
        if self.listen == "tcp" and self.transport is None and self.run_dir is not None:
            with suppress(OSError):
                kind, _, rest = (self.run_dir / ENDPOINT_NAME).read_text().strip().partition(":")
                if kind == "tcp" and rest.startswith("127.0.0.1:"):
                    return f"http://{rest}"
        return "http://guestd"

    def _dial(self) -> httpx.AsyncBaseTransport | None:
        assert self.run_dir is not None
        if self._address() != "http://guestd":
            return None
        path = str(self.run_dir / SOCKET_NAME)
        dialled = httpx.AsyncHTTPTransport(uds=path)
        dialled._pool = httpcore.AsyncConnectionPool(uds=path, network_backend=DoorSocket(self.door_uid))
        return dialled

    async def _token(self) -> str:
        if not self.token:
            assert self.token_dir is not None
            try:
                self.token = await asyncio.to_thread(ensure_token, self.token_dir, self.door_uid)
            except OSError as exc:
                raise DoorUnavailable("the guest door's token cannot be read") from exc
        return self.token

    async def call(self, method: str, path: str, *, json: Any = None, timeout: float = CALL_SECONDS) -> dict[str, Any]:
        client = self._ready()
        headers = {"authorization": f"Bearer {await self._token()}"}
        try:
            async with client.stream(method, path, json=json, headers=headers, timeout=timeout) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DoorRefused(502, "the door's answer is too long")
        except (httpx.HTTPError, OSError) as exc:
            # A door on a loopback port comes back on another one, so the address is read again.
            await self.aclose()
            raise DoorUnavailable("the guest door is not running") from exc
        try:
            body = jsonlib.loads(raw)
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        if response.status_code >= 400:
            raise DoorRefused(response.status_code, str(body.get("detail") or f"the door answered {response.status_code}")[:300])
        return body

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass(slots=True)
class Delivery:
    message_id: int
    link_id: str
    conversation_id: str
    text: str
    allowance_usd: float
    done: asyncio.Future[str] | None = None
    run_id: str = ""


@dataclass(slots=True)
class ChatLine:
    """The guest messages waiting for one chat, and the task that feeds them in."""

    waiting: deque[Delivery] = field(default_factory=deque)
    worker: asyncio.Task[None] | None = None


def _items(value: Any) -> Iterable[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _whole(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class GuestBridge:
    def __init__(self, app: Application | Any, client: DoorClient | None = None) -> None:
        self.app = app
        self.client = client or DoorClient(app.settings.guest_run_dir, token_dir=app.settings.guest_token_dir, door_uid=app.settings.guest_door_uid, listen=app.settings.guest_listen)
        self.lines: dict[str, ChatLine] = {}
        self.turns: dict[str, Delivery] = {}
        """Run id to the delivery it answers, for the turns of this process."""
        self.connected = False
        self._noticed: dict[tuple[str, str], float] = {}
        self._ended_early: dict[str, str] = {}
        """Guest runs that ended before ``submit`` returned their id: a turn that fails at once can."""
        self._seen: OrderedDict[int, None] = OrderedDict()
        """Messages already taken: the door offers one again until it hears it arrived."""
        self._contacts: deque[float] = deque()

    # -- the links the host answers for -----------------------------------------------

    async def remember_link(self, link_id: str, session_id: str, label: str, mode: str, tools: str, usd_limit: float = 0.0) -> None:
        await self.app.db.execute(
            "INSERT INTO guest_links(id, session_id, label, mode, tools, usd_limit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (link_id, session_id, label, mode, tools, usd_limit, datetime.now(UTC).isoformat()),
        )

    async def known_link(self, link_id: str) -> Any:
        return await self.app.db.fetchone("SELECT * FROM guest_links WHERE id = ?", (link_id,))

    async def links_of(self, session_id: str | None) -> set[str]:
        if session_id is None:
            rows = await self.app.db.fetchall("SELECT id FROM guest_links")
        else:
            rows = await self.app.db.fetchall("SELECT id FROM guest_links WHERE session_id = ?", (session_id,))
        return {str(row["id"]) for row in rows}

    async def revoked(self, link_id: str, *, door_told: bool = True) -> None:
        """A revocation, on this side: the turn running stops and what is waiting goes.

        First, and not after the door has answered: with the door out of reach a revoked link must still
        stop here. ``door_told`` is what the next connection reads to tell the door.
        """
        await self.app.db.execute("UPDATE guest_links SET revoked_at = coalesce(revoked_at, ?), door_told = ? WHERE id = ?", (datetime.now(UTC).isoformat(), int(door_told), link_id))
        row = await self.known_link(link_id)
        for line in self.lines.values():
            for delivery in [d for d in line.waiting if d.link_id == link_id and not d.run_id]:
                line.waiting.remove(delivery)
        manager = self.app.manager
        for run_id, delivery in list(self.turns.items()):
            if delivery.link_id == link_id and manager is not None and row is not None:
                await manager.stop_run(str(row["session_id"]), run_id)

    async def tell_door_of_revocations(self) -> None:
        for row in await self.app.db.fetchall("SELECT id FROM guest_links WHERE revoked_at IS NOT NULL AND door_told = 0"):
            with suppress(DoorRefused):
                await self.client.call("POST", f"/control/links/{row['id']}/revoke")
            await self.app.db.execute("UPDATE guest_links SET door_told = 1 WHERE id = ?", (row["id"],))

    # -- the feed ---------------------------------------------------------------------

    async def serve(self) -> None:
        """Follow the door for as long as this process lives; a door that is gone is waited for."""
        cursor = int(await self.app.db.kv_get(FEED_KV, 0) or 0)
        started = False
        while True:
            try:
                if not self.connected:
                    await self.publish_address()
                    await self.publish_routes()
                    await self.tell_door_of_revocations()
                    if not started:
                        # Once per process, not per connection: a door that restarted leaves this
                        # process's turns running, and they still answer.
                        await self.client.call("POST", "/control/deliveries/reset")
                        started = True
                    self.connected = True
                feed = await self.client.call("GET", f"/control/feed?after={cursor}&wait={FEED_WAIT_SECONDS}", timeout=FEED_WAIT_SECONDS + CALL_SECONDS)
            except (DoorUnavailable, DoorRefused) as exc:
                self.connected = False
                logger.debug("the guest door is out of reach: %s", exc)
                await asyncio.sleep(RECONNECT_SECONDS)
                continue
            try:
                await self.absorb(feed)
            except Exception:  # noqa: BLE001 — nothing a door sends may end the only thing following it
                logger.exception("could not take in the guest door's feed")
            # A door whose volume was recreated counts from the start again, and a cursor from before
            # that would wait for ever for an event number it has not reached.
            moved = _whole(feed.get("cursor")) if _whole(feed.get("latest")) >= cursor else _whole(feed.get("latest"))
            if moved != cursor:
                cursor = moved
                await self.app.db.kv_set(FEED_KV, cursor)

    async def publish_address(self) -> None:
        await self.client.call("PUT", "/control/settings", json={"public_base": public_address.guest_base(self.app.settings)})

    async def publish_routes(self) -> None:
        """Tell the door which providers its models are limited to. A call of its own, apart from the address:
        a table the door refuses must not take the connection down with it, and it is only ever one model's
        trouble. A door out of reach hears at its next connection, which this marks as owed. With no agent
        in this process there is nothing to say, and an empty table would lift every limit the door holds.
        """
        if self.app.manager is None or self.app.config is None:
            return
        try:
            await self.client.call("PUT", "/control/settings", json={"model_routes": model_routes(self.app)})
        except DoorUnavailable as exc:
            self.connected = False
            logger.debug("the guest door did not take the model routes: %s", exc)
        except DoorRefused as exc:
            logger.warning("the guest door refused the model routes and keeps its old table: %s", exc)

    async def absorb(self, feed: dict[str, Any]) -> None:
        for event in _items(feed.get("events")):
            try:
                await self.on_event(event)
            except Exception:  # noqa: BLE001 — a notice lost is not a reason to stop following the door
                logger.exception("could not act on a guest door event")
        for item in _items(feed.get("deliveries")):
            try:
                delivery = Delivery(int(item["message_id"]), str(item["link_id"]), str(item["conversation_id"]), str(item["text"])[:MAX_GUEST_TEXT], float(item.get("allowance_usd") or 0.0))
            except (KeyError, TypeError, ValueError):
                logger.warning("the guest door sent a delivery that makes no sense; left out")
                continue
            try:
                await self.take(delivery)
            except Exception:  # noqa: BLE001 — one message that cannot start must not cost the next
                logger.exception("could not take message %s from the guest door", delivery.message_id)

    async def take(self, delivery: Delivery) -> None:
        """Say it arrived, then act on it once: the door offers what it has not heard about again."""
        first = delivery.message_id not in self._seen
        self._seen[delivery.message_id] = None
        while len(self._seen) > SEEN_KEPT:
            self._seen.popitem(last=False)
        with suppress(DoorUnavailable, DoorRefused):
            await self.client.call("POST", f"/control/deliveries/{delivery.message_id}/ack")
        if first:
            await self.on_delivery(delivery)

    async def on_event(self, event: dict[str, Any]) -> None:
        notifications = getattr(self.app, "notifications", None)
        if notifications is None:
            return
        kind, link_id = str(event.get("kind")), str(event.get("link_id"))
        if kind not in ("first_contact", "redeem_refused", "limit", "approval_timeout"):
            return
        row = await self.known_link(link_id)
        if row is None or row["revoked_at"]:
            return  # a link this side never made: an event about it is the door's invention
        if kind == "approval_timeout":
            known = await self.client.call("GET", f"/control/links/{link_id}")
            await self.contact_settled(str(((known.get("link") or {}).get("conversation") or {}).get("id") or ""), render("guest.refused", notifications.language()))
            return
        now = time.monotonic()
        if kind == "first_contact":
            while self._contacts and now - self._contacts[0] > NOTIFY_EVERY_SECONDS:
                self._contacts.popleft()
            if len(self._contacts) >= CONTACTS_PER_HOUR:
                return  # the links stay in the app's list; what stops here is a door paging the operator
            self._contacts.append(now)
        elif now - self._noticed.get((kind, link_id), -NOTIFY_EVERY_SECONDS) < NOTIFY_EVERY_SECONDS:
            return
        self._noticed[(kind, link_id)] = now
        label = str(row["label"]) or link_id
        session_id = str(row["session_id"])
        link = chat_link(session_id)
        # Words of this code and the operator's own label, never the door's or the guest's: this text
        # travels to Telegram, where a link in a stranger's message would be fetched by a preview.
        lang = notifications.language()
        if kind == "first_contact":
            known = await self.client.call("GET", f"/control/links/{link_id}")
            conversation = str(((known.get("link") or {}).get("conversation") or {}).get("id") or "")
            if not re.fullmatch(r"[0-9a-f]{16}", conversation):
                return
            ref = f"{REF}:{conversation}"
            await notifications.post(Draft(
                "permission", render("guest.contact", lang, label=label), render("guest.contact.body", lang), kind="guest_contact", session_id=session_id, link=link,
                actions=(Action("admit", render("guest.admit", lang), "primary"), Action("refuse", render("guest.refuse", lang), "danger")), request_ref=ref, dedupe_key=ref, source="guest",
            ))
        elif kind == "redeem_refused":
            await notifications.post(Draft(
                "system", render("guest.redeem", lang, label=label), render("guest.redeem.body", lang), kind="guest_redeem", tone="warning", session_id=session_id, link=link,
                source="guest", dedupe_key=f"{REF}-redeem:{link_id}:{int(now // NOTIFY_EVERY_SECONDS)}",
            ))
        else:
            await notifications.post(Draft(
                "spend", render("guest.limit", lang, label=label), render("guest.limit.body", lang), kind="guest_limit", session_id=session_id, link=link, source="guest",
                dedupe_key=f"{REF}-limit:{link_id}:{int(now // NOTIFY_EVERY_SECONDS)}",
            ))

    # -- a direct link's turns --------------------------------------------------------

    async def on_delivery(self, delivery: Delivery) -> None:
        row = await self.known_link(delivery.link_id)
        if row is None or row["mode"] != "direct" or row["revoked_at"]:
            # Not a link this side made for a chat, whatever the door believes.
            logger.warning("the guest door offered a message for link %s, which has no live direct link here", delivery.link_id)
            await self._answer(delivery, ok=False, ran=False)
            return
        session_id = str(row["session_id"])
        line = self.lines.setdefault(session_id, ChatLine())
        line.waiting.append(delivery)
        if line.worker is None or line.worker.done():
            line.worker = asyncio.create_task(self._work_line(session_id, line), name=f"guest-line:{session_id}")

    async def _work_line(self, session_id: str, line: ChatLine) -> None:
        while line.waiting:
            delivery = line.waiting[0]
            try:
                await self._run_one(session_id, delivery)
            except Exception:  # noqa: BLE001 — the guest is told; the next message must still be tried
                logger.exception("a guest's turn failed in session %s", session_id)
                await self._answer(delivery, ok=False)
            if line.waiting and line.waiting[0] is delivery:
                line.waiting.popleft()

    async def _run_one(self, session_id: str, delivery: Delivery) -> None:
        row = await self.known_link(delivery.link_id)
        if row is None or row["revoked_at"]:
            await self._answer(delivery, ok=False, ran=False)
            return
        manager = self.app.manager
        tools = row["tools"] if row["tools"] in guest_turns.GUEST_TOOLS else "none"
        why = await manager.guest_ready(session_id, tools)
        price = await manager.guest_price(session_id)
        if why or price is None:
            logger.warning("a guest's message could not start a turn in session %s: %s", session_id, why or "the chat's model has no known price")
            await self._answer(delivery, ok=False, ran=False)
            return
        usd_left: float | None = None
        if any(price):
            # Clamped here: what the door says a turn may spend is only ever a request.
            usd_left = max(0.0, min(delivery.allowance_usd, max(guest_turns.TURN_USD, float(row["usd_limit"]) / 4), float(row["usd_limit"])))
            if await manager.guest_first_call_usd(session_id, price) > usd_left:
                logger.warning("a guest's turn in session %s would pass its limit on its first call", session_id)
                await self._answer(delivery, ok=False, ran=False)
                return
        turn = GuestTurn(delivery.link_id, str(row["label"]), tools, usd_left, delivery.text)
        run_id = await self._start(session_id, delivery, turn)
        if not run_id:
            await self._answer(delivery, ok=False, ran=False)
            return
        assert delivery.done is not None
        status = await delivery.done
        self.turns.pop(run_id, None)
        spent, _ = await manager.spend(run_id=run_id)
        text = await manager.guest_answer(session_id, run_id) if status == "completed" else ""
        # A turn that ended without a word, on its model-call bound for one, has not answered.
        await self._answer(delivery, ok=status == "completed" and bool(text), text=text, cost=spent)

    async def _start(self, session_id: str, delivery: Delivery, turn: GuestTurn) -> str:
        """Open the guest's own run as soon as the chat is free; "" when it never is, or cannot run."""
        manager = self.app.manager
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + CHAT_FREE_SECONDS
        delivery.done = loop.create_future()
        while time.monotonic() < deadline:
            row = await self.known_link(delivery.link_id)
            if row is None or row["revoked_at"]:
                return ""
            try:
                run_id = await manager.submit(session_id, guest_turns.body_for(delivery.text, turn.label), as_answer=False, origin=turn.origin, via="guest", guest=turn)
            except GuestTurnBusy:
                await asyncio.sleep(1.0)
                continue
            except (RuntimeError, KeyError) as exc:
                logger.warning("a guest's message could not start a turn in session %s: %s", session_id, exc)
                return ""
            delivery.run_id = run_id
            self.turns[run_id] = delivery
            row = await self.known_link(delivery.link_id)
            if row is None or row["revoked_at"]:
                # Revoked while submit was running: too late for the check above, and ``revoked`` could
                # not yet find this run to stop it.
                await manager.stop_run(session_id, run_id)
            ended = self._ended_early.pop(run_id, None)
            if ended is not None and not delivery.done.done():
                delivery.done.set_result(ended)
            return run_id
        return ""

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if status == "awaiting":
            return  # parked on an approval card; the same run finishes later
        delivery = self.turns.get(run_id)
        if delivery is None:
            manager = self.app.manager
            state = manager._states.get(session_id) if manager is not None else None
            if state is not None and state.guest_turn is not None and state.run_id == run_id:
                self._ended_early[run_id] = status
            return
        if delivery.done is not None and not delivery.done.done():
            delivery.done.set_result(status)

    async def _answer(self, delivery: Delivery, *, ok: bool, text: str = "", cost: float = 0.0, ran: bool = True) -> None:
        try:
            await self.client.call("POST", f"/control/deliveries/{delivery.message_id}/reply", json={"ok": ok, "text": text, "cost_usd": cost, "ran": ran})
        except DoorRefused as exc:
            logger.info("the door took no reply for message %s: %s", delivery.message_id, exc.detail)
        except DoorUnavailable:
            logger.warning("the door was out of reach for the reply to message %s", delivery.message_id)

    async def on_session_deleted(self, session_id: str) -> None:
        """A chat that is gone cannot answer: its links close with it, and their cards with them."""
        notifications = getattr(self.app, "notifications", None)
        if notifications is not None:
            await notifications.resolve_session(session_id, render("guest.revoked", notifications.language()), via="app", prefixes=(REF,))
        for link_id in await self.links_of(session_id):
            row = await self.known_link(link_id)
            if row is None or row["revoked_at"]:
                continue
            await self.revoked(link_id)
            try:
                await self.client.call("POST", f"/control/links/{link_id}/revoke")
            except (DoorUnavailable, DoorRefused):
                logger.warning("could not close link %s at the door along with its chat", link_id)

    # -- notification buttons ---------------------------------------------------------

    async def contact_settled(self, conversation_id: str, resolution: str) -> None:
        """The card of a first contact goes when the contact is decided by any other way: left open it
        stays urgent in the operator's inbox, offering Admit on a conversation that is already over."""
        notifications = getattr(self.app, "notifications", None)
        if notifications is not None and conversation_id:
            await notifications.resolve(f"{REF}:{conversation_id}", resolution, via="app")

    async def resolve_action(self, request: ActionRequest) -> ActionOutcome:
        """Admit or Refuse on ``guest:<conversation id>``. No "admit all": each request is one decision."""
        if request.action not in ("admit", "refuse"):
            raise ActionRefused(f"this notification offers no {request.action!r}")
        try:
            await self.client.call("POST", f"/control/conversations/{request.scope}/{request.action}")
        except DoorRefused as exc:
            raise ActionConflict("decided") from exc
        except DoorUnavailable as exc:
            raise ActionRefused(str(exc)) from None
        return ActionOutcome(render("guest.admitted" if request.action == "admit" else "guest.refused", self.app.notifications.language()))


LEFT_OUT = {
    "outside_key_proxy": "this endpoint is not behind the key proxy, and the door has no way out but the proxy's socket",
    "no_provider": "the model's provider is missing or could not be set up",
    "not_named": "its upstream is not in KEYPROXY_GUEST_UPSTREAMS",
    "no_price": "a metered model with no known price, so no dollar limit could hold on it",
}
"""Every reason a model the app lists is not offered to a stand-in. Nothing else may keep one out."""


def model_choices(app: Application | Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every model the app lists, as what a stand-in can be given and what cannot be, with the reason.

    ``billing`` is what bounds a link on the model. ``metered``: dollars and turns. ``plan``: a prepaid plan or a
    CLI login, whose calls cost no dollars and whose allowance is spent all the same, so turns alone. ``free``: a
    model at no price, turns alone. A metered model with no price is left out rather than guessed at, because a
    limit in dollars cannot be checked before the spending on a model nobody can price: "no price" reads as "free"
    only where the route is a known plan.
    """
    base = keyproxy_base()
    named = {name.strip().lower() for name in app.settings.guest_model_upstreams.split(",") if name.strip()}
    options: list[dict[str, Any]] = []
    left_out: list[dict[str, str]] = []

    def leave_out(preset_id: str, preset: Any, why: str) -> None:
        left_out.append({"id": preset_id, "label": preset.display(preset_id), "why": why})

    for preset_id, preset in app.config.presets.items():
        provider = app.config.providers.get(preset.provider)
        if provider is None:
            leave_out(preset_id, preset, "no_provider")
            continue
        if not is_keyproxy_url(provider.base_url):
            leave_out(preset_id, preset, "outside_key_proxy")
            continue
        try:
            endpoint = app.manager.providers.get(preset.provider).endpoint
        except KeyError:
            leave_out(preset_id, preset, "no_provider")
            continue
        path = provider.base_url.rstrip("/").removeprefix(base).strip("/")
        if "*" not in named and path.split("/", 1)[0] not in named:
            leave_out(preset_id, preset, "not_named")
            continue
        pricing = endpoint.pricing_for(preset.model)
        plan = bool(endpoint.subscription) or guest_turns.login_route(provider.base_url)
        if plan:
            billing = "plan"
        elif endpoint.kind == "llamacpp" or preset.model.endswith((":free", "-free")):
            billing = "free"
        elif pricing is None or pricing.input is None or pricing.output is None:
            leave_out(preset_id, preset, "no_price")
            continue
        else:
            billing = "metered" if pricing.input or pricing.output else "free"
        metered = billing == "metered"
        options.append({
            "id": preset_id, "label": preset.display(preset_id), "path": path, "model": preset.model, "billing": billing,
            "price_in": float(pricing.input) if metered else 0.0, "price_out": float(pricing.output) if metered else 0.0, "free": billing == "free",  # type: ignore[union-attr]
        })
    # A plan last: the first is the form's default, and what a stranger's questions should cost by default is
    # nothing or next to it, not the operator's own allowance.
    options.sort(key=lambda o: (o["billing"] == "plan", o["price_in"] + o["price_out"] * 4, str(o["label"]).lower()))
    return options, left_out


def model_routes(app: Application | Any) -> list[dict[str, Any]]:
    """The OpenRouter providers each key-proxy model is limited to, with what that makes it cost.

    A table the door holds rather than fields on a link. A link is made once and lives for weeks; had it
    kept the choice of the day it was made, a model limited afterwards would go on being routed anywhere for its
    links, and the price would go on being reserved at the old provider's rate. Every preset is in it,
    offered to new links or not, because a link already made does not stop running when its model stops being offered.
    A model with no price stays in with ``None`` for it: left out, its limit would be lifted for want of a number.
    """
    base = keyproxy_base()
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for preset in app.config.presets.values():
        provider = app.config.providers.get(preset.provider)
        if not preset.openrouter_providers or provider is None or not is_keyproxy_url(provider.base_url):
            continue
        try:
            pricing = app.manager.providers.get(preset.provider).endpoint.pricing_for(preset.model)
        except KeyError:
            pricing = None
        priced = pricing is not None and pricing.input is not None and pricing.output is not None
        path = provider.base_url.rstrip("/").removeprefix(base).strip("/")
        routes[(path, preset.model)] = {
            "path": path, "model": preset.model, "only": list(preset.openrouter_providers),
            "price_in": float(pricing.input) if priced else None, "price_out": float(pricing.output) if priced else None,  # type: ignore[union-attr, arg-type]
        }
    return list(routes.values())


async def routes_changed(app: Application | Any) -> None:
    """A model's providers or their price moved: the door's table follows."""
    bridge = bridge_of(app)
    if bridge is not None and bridge.client.configured:
        await bridge.publish_routes()


def model_options(app: Application | Any) -> list[dict[str, Any]]:
    """The models a stand-in can be given, cheapest first."""
    return model_choices(app)[0]


def bridge_of(app: Application | Any) -> GuestBridge | None:
    return (getattr(app, "extensions", None) or {}).get(EXTENSION_KEY)


async def install(app: Application) -> list[asyncio.Task[None]]:
    bridge = GuestBridge(app)
    app.extensions[EXTENSION_KEY] = bridge
    if app.notifications is not None:
        app.notifications.register_resolver(REF, bridge.resolve_action)
    if app.manager is not None:
        app.manager.on_finished(bridge.on_run_finished)
        app.manager.delete_hooks.append(bridge.on_session_deleted)
    if not bridge.client.configured:
        return []
    return [asyncio.create_task(bridge.serve(), name="guest-door")]


__all__ = ["EXTENSION_KEY", "LEFT_OUT", "DoorClient", "DoorRefused", "DoorSocket", "DoorUnavailable", "GuestBridge", "bridge_of", "ensure_token", "install", "model_choices", "model_options", "model_routes", "routes_changed"]
