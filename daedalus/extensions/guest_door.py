"""The host's end of the guest door: working the door, and running a direct link's turns.

The door (``deploy/guestd``) is a process with nothing in it, and this is the only code that talks
to it, always by dialling its unix socket. For a stand-in link nothing passes through here but the
operator's own actions: the stand-in's conversation happens entirely on the other side.

A direct link is the narrow exception, and it is kept narrow on purpose. One thing comes in: a guest's
words, claimed from the door's feed. One thing goes out: the final text of the turn those words
started. What that turn may do is decided here from this database, where the session and the tool
setting of a link were written when the operator made it. The door never held them, so a door that
goes wrong can make up a guest's words but cannot aim them at another chat or widen the turn they start.

Each guest's message is a run of its own, started only when the chat is free. It waits here rather
than in the session's queue, because that queue feeds whatever run comes next, and an operator's run
must never pick up a guest's words as its input.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
TOKEN_NAME = "control.token"
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


class DoorClient:
    """HTTP over the door's unix socket, with the token from beside it."""

    def __init__(self, run_dir: Path | None, *, transport: httpx.AsyncBaseTransport | None = None, token: str = "") -> None:
        self.run_dir = run_dir
        self.transport = transport
        self.token = token
        """Given by a caller with a transport of its own; otherwise read from the door's run directory."""
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return self.run_dir is not None or self.transport is not None

    def _ready(self) -> httpx.AsyncClient:
        if self.run_dir is None and self.transport is None:
            raise DoorUnavailable("no guest door is set up on this installation")
        if self._client is None:
            transport = self.transport
            if transport is None:
                assert self.run_dir is not None
                transport = httpx.AsyncHTTPTransport(uds=str(self.run_dir / SOCKET_NAME))
            self._client = httpx.AsyncClient(transport=transport, base_url="http://guestd", timeout=CALL_SECONDS)
        return self._client

    def _token(self) -> str:
        if self.run_dir is None:
            return self.token
        try:
            return (self.run_dir / TOKEN_NAME).read_text().strip()
        except OSError as exc:
            raise DoorUnavailable("the guest door is not running") from exc

    async def call(self, method: str, path: str, *, json: Any = None, timeout: float = CALL_SECONDS) -> dict[str, Any]:
        client = self._ready()
        try:
            response = await client.request(method, path, json=json, headers={"authorization": f"Bearer {self._token()}"}, timeout=timeout)
        except (httpx.HTTPError, OSError) as exc:
            raise DoorUnavailable("the guest door is not running") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise DoorRefused(response.status_code, str(body.get("detail") or f"the door answered {response.status_code}"))
        return body if isinstance(body, dict) else {}

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
    label: str
    done: asyncio.Future[str] | None = None
    run_id: str = ""


@dataclass(slots=True)
class ChatLine:
    """The guest messages waiting for one chat, and the task that feeds them in."""

    waiting: deque[Delivery] = field(default_factory=deque)
    worker: asyncio.Task[None] | None = None


class GuestBridge:
    def __init__(self, app: Application | Any, client: DoorClient | None = None) -> None:
        self.app = app
        self.client = client or DoorClient(app.settings.guest_run_dir)
        self.lines: dict[str, ChatLine] = {}
        self.turns: dict[str, Delivery] = {}
        """Run id to the delivery it answers, for the turns of this process."""
        self.connected = False
        self._noticed: dict[tuple[str, str], float] = {}
        self._ended_early: dict[str, str] = {}
        """Guest runs that ended before ``submit`` returned their id: a turn that fails at once can."""

    # -- the links the host answers for -----------------------------------------------

    async def remember_link(self, link_id: str, session_id: str, label: str, mode: str, tools: str) -> None:
        await self.app.db.execute(
            "INSERT INTO guest_links(id, session_id, label, mode, tools, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (link_id, session_id, label, mode, tools, datetime.now(UTC).isoformat()),
        )

    async def known_link(self, link_id: str) -> Any:
        return await self.app.db.fetchone("SELECT * FROM guest_links WHERE id = ?", (link_id,))

    async def links_of(self, session_id: str | None) -> set[str]:
        if session_id is None:
            rows = await self.app.db.fetchall("SELECT id FROM guest_links")
        else:
            rows = await self.app.db.fetchall("SELECT id FROM guest_links WHERE session_id = ?", (session_id,))
        return {str(row["id"]) for row in rows}

    async def revoked(self, link_id: str) -> None:
        """A revocation, on this side: the turn running stops and what is waiting goes."""
        await self.app.db.execute("UPDATE guest_links SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (datetime.now(UTC).isoformat(), link_id))
        row = await self.known_link(link_id)
        for line in self.lines.values():
            for delivery in [d for d in line.waiting if d.link_id == link_id and not d.run_id]:
                line.waiting.remove(delivery)
        manager = self.app.manager
        for run_id, delivery in list(self.turns.items()):
            if delivery.link_id == link_id and manager is not None and row is not None:
                await manager.stop_run(str(row["session_id"]), run_id)

    # -- the feed ---------------------------------------------------------------------

    async def serve(self) -> None:
        """Follow the door for as long as this process lives; a door that is gone is waited for."""
        cursor = int(await self.app.db.kv_get(FEED_KV, 0) or 0)
        started = False
        while True:
            try:
                if not self.connected:
                    await self.publish_address()
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
            await self.absorb(feed)
            # A door whose volume was recreated counts from the start again, and a cursor from before
            # that would wait for ever for an event number it has not reached.
            moved = int(feed.get("cursor") or 0) if int(feed.get("latest") or 0) >= cursor else int(feed.get("latest") or 0)
            if moved != cursor:
                cursor = moved
                await self.app.db.kv_set(FEED_KV, cursor)

    async def publish_address(self) -> None:
        await self.client.call("PUT", "/control/settings", json={"public_base": public_address.guest_base(self.app.settings)})

    async def absorb(self, feed: dict[str, Any]) -> None:
        for event in feed.get("events") or ():
            try:
                await self.on_event(event)
            except Exception:  # noqa: BLE001 — a notice lost is not a reason to stop following the door
                logger.exception("could not act on a guest door event")
        for item in feed.get("deliveries") or ():
            await self.on_delivery(Delivery(int(item["message_id"]), str(item["link_id"]), str(item["conversation_id"]), str(item["text"]), float(item.get("allowance_usd") or 0.0), str(item.get("label") or "")))

    async def on_event(self, event: dict[str, Any]) -> None:
        notifications = getattr(self.app, "notifications", None)
        if notifications is None:
            return
        kind, link_id = str(event.get("kind")), str(event.get("link_id"))
        if kind not in ("first_contact", "redeem_refused", "limit"):
            return
        now = time.monotonic()
        if kind != "first_contact" and now - self._noticed.get((kind, link_id), -NOTIFY_EVERY_SECONDS) < NOTIFY_EVERY_SECONDS:
            return
        self._noticed[(kind, link_id)] = now
        known = await self.client.call("GET", f"/control/links/{link_id}")
        link = known.get("link") or {}
        label = str(link.get("label") or link_id)
        row = await self.known_link(link_id)
        session_id = str(row["session_id"]) if row is not None else None
        # Words of this code and the operator's own label, never the guest's: this text travels to
        # Telegram, where a link in a stranger's message would be fetched by a preview.
        lang = notifications.language()
        if kind == "first_contact":
            conversation = link.get("conversation") or {}
            ref = f"{REF}:{conversation.get('id')}"
            await notifications.post(Draft(
                "permission", render("guest.contact", lang, label=label), render("guest.contact.body", lang), kind="guest_contact", session_id=session_id, link=chat_link(session_id),
                actions=(Action("admit", render("guest.admit", lang), "primary"), Action("refuse", render("guest.refuse", lang), "danger")), request_ref=ref, dedupe_key=ref, source="guest",
            ))
        elif kind == "redeem_refused":
            await notifications.post(Draft(
                "system", render("guest.redeem", lang, label=label), render("guest.redeem.body", lang), kind="guest_redeem", tone="warning", session_id=session_id, link=chat_link(session_id),
                source="guest", dedupe_key=f"{REF}-redeem:{link_id}:{int(now // NOTIFY_EVERY_SECONDS)}",
            ))
        else:
            await notifications.post(Draft(
                "spend", render("guest.limit", lang, label=label), str(event.get("detail") or ""), kind="guest_limit", session_id=session_id, link=chat_link(session_id), source="guest",
                dedupe_key=f"{REF}-limit:{link_id}:{int(now // NOTIFY_EVERY_SECONDS)}",
            ))

    # -- a direct link's turns --------------------------------------------------------

    async def on_delivery(self, delivery: Delivery) -> None:
        row = await self.known_link(delivery.link_id)
        if row is None or row["mode"] != "direct" or row["revoked_at"]:
            # Not a link this side made for a chat, whatever the door believes.
            logger.warning("the guest door offered a message for link %s, which has no live direct link here", delivery.link_id)
            await self._answer(delivery, ok=False)
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
            await self._answer(delivery, ok=False)
            return
        turn = GuestTurn(delivery.link_id, str(row["label"]), row["tools"] if row["tools"] in guest_turns.GUEST_TOOLS else "none", delivery.allowance_usd, delivery.text)
        run_id = await self._start(session_id, delivery, turn)
        if not run_id:
            await self._answer(delivery, ok=False)
            return
        assert delivery.done is not None
        status = await delivery.done
        self.turns.pop(run_id, None)
        manager = self.app.manager
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

    async def _answer(self, delivery: Delivery, *, ok: bool, text: str = "", cost: float = 0.0) -> None:
        try:
            await self.client.call("POST", f"/control/deliveries/{delivery.message_id}/reply", json={"ok": ok, "text": text, "cost_usd": cost})
        except DoorRefused as exc:
            logger.info("the door took no reply for message %s: %s", delivery.message_id, exc.detail)
        except DoorUnavailable:
            logger.warning("the door was out of reach for the reply to message %s", delivery.message_id)

    async def on_session_deleted(self, session_id: str) -> None:
        """A chat that is gone cannot answer: its links close with it."""
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


def model_options(app: Application | Any) -> list[dict[str, Any]]:
    """The models the door can reach: those served through the key proxy, cheapest first.

    Priced, or costing nothing by their plan. One with no known price is left out rather than guessed
    at, because a limit in dollars cannot be checked before the spending on a model nobody can price.
    """
    base = keyproxy_base()
    options: list[dict[str, Any]] = []
    for preset_id, preset in app.config.presets.items():
        provider = app.config.providers.get(preset.provider)
        if provider is None or not is_keyproxy_url(provider.base_url):
            continue
        manager = app.manager
        try:
            endpoint = manager.providers.get(preset.provider).endpoint
        except KeyError:
            continue
        pricing = endpoint.pricing_for(preset.model)
        free = bool(endpoint.subscription) or endpoint.kind == "llamacpp" or preset.model.endswith((":free", "-free"))
        if not free and (pricing is None or pricing.input is None or pricing.output is None):
            continue
        free = free or not (pricing.input or pricing.output)  # type: ignore[union-attr]
        options.append({
            "id": preset_id, "label": preset.display(preset_id), "path": provider.base_url.rstrip("/").removeprefix(base).strip("/"), "model": preset.model,
            "price_in": 0.0 if free else float(pricing.input), "price_out": 0.0 if free else float(pricing.output), "free": free,  # type: ignore[union-attr]
        })
    options.sort(key=lambda o: (o["price_in"] + o["price_out"] * 4, str(o["label"]).lower()))
    return options


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


__all__ = ["EXTENSION_KEY", "DoorClient", "DoorRefused", "DoorUnavailable", "GuestBridge", "bridge_of", "install", "model_options"]
