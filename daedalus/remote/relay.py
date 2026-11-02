"""The relay desk: the one door a paired terminal client's executor comes in by, and the links it holds.

``wss /api/cli/relay`` (``daedalus.extensions.api_remote_relay``) hands each accepted socket to
:meth:`DeviceHub.serve`, which authenticates the device from the ``Authorization: Device <token>``
of the upgrade request, reads its ``device.hello``, answers ``device.welcome`` with the
conversations whose hands are on it, and carries the link until either side ends it. One link per
device: a second connection of the same device replaces the first (``4409``), which is what a laptop
that woke from sleep with a dead socket does.

Who a token belongs to is not this module's business: :class:`DeviceDirectory` answers it, and the
pairing's store (``daedalus.remote.devices.Devices``) implements it. What a client reports about the
conversations it shows — that its relay is up for them, the local guard of each — goes to a
:class:`ControlBook` (``daedalus.remote.control.RemoteControl``), which keeps the ``remote`` marks and
the guards for the session view. :class:`MemoryControl` is the same interface kept here, for an
installation without the pairing's side and for the tests. The hub itself keeps only the links.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from daedalus.remote.link import (
    CLOSE_GOING_AWAY,
    CLOSE_PROTOCOL,
    CLOSE_REPLACED,
    CLOSE_UNAUTHORIZED,
    CONTRACT,
    GUARDS,
    HELLO_SECONDS,
    MAX_MESSAGE,
    PROTOCOL,
    DeviceLink,
    Hello,
    RelaySocket,
)

logger = logging.getLogger(__name__)

Bound = Callable[[str], Awaitable[list[dict[str, str]]]]
"""The conversations whose hands are on a device, as ``[{session_id, cwd}]``."""
Announce = Callable[[str, dict[str, Any], str | None], Awaitable[None]]
"""Publish one event on the bus: its type, its payload, and the session it is about, if any."""


@dataclass(frozen=True, slots=True)
class RemoteDevice:
    """A paired device as the relay needs it: who it is and what to call it."""

    id: str
    name: str


class DeviceDirectory(Protocol):
    """Who a device token belongs to and what a device may reach: the pairing's store.

    The relay asks it once per connection; the token is never kept here. ``in_scope`` is the
    device's scope (``cli_device_sessions``, its subagents included): the sessions whose guard a
    client may report and whose events it may follow."""

    async def check(self, token: str, *, address: str = "") -> Any: ...

    async def in_scope(self, device_id: str, session_id: str) -> bool: ...


class ControlBook(Protocol):
    """What the clients report about the conversations they show, kept for the marks and the session view."""

    async def linked(self, device_id: str, sessions: Iterable[str]) -> None: ...

    async def unlinked(self, device_id: str) -> None: ...

    async def set_guard(self, device_id: str, session_id: str, guard: str) -> bool: ...

    def guard_of(self, device_id: str, session_id: str) -> str | None: ...

    async def set_waiting(self, session_id: str, device_id: str, calls: list[dict[str, Any]] | None, *, since: str = "") -> None: ...


class MemoryControl:
    """:class:`ControlBook` in memory, announcing ``remote.guard`` and ``remote.waiting`` itself."""

    def __init__(self, announce: Announce | None = None) -> None:
        self._announce = announce
        self._linked: dict[str, frozenset[str]] = {}
        self._guards: dict[tuple[str, str], str] = {}
        self._waiting: dict[str, dict[str, Any]] = {}

    async def linked(self, device_id: str, sessions: Iterable[str]) -> None:
        self._linked[device_id] = frozenset(sessions)

    async def unlinked(self, device_id: str) -> None:
        self._linked.pop(device_id, None)

    async def set_guard(self, device_id: str, session_id: str, guard: str) -> bool:
        if guard not in GUARDS:
            raise ValueError(f"a guard is one of {', '.join(GUARDS)}, not {guard!r}")
        if self._guards.get((device_id, session_id)) == guard:
            return False
        self._guards[(device_id, session_id)] = guard
        await self._say("remote.guard", {"device_id": device_id, "guard": guard}, session_id)
        return True

    def guard_of(self, device_id: str, session_id: str) -> str | None:
        return self._guards.get((device_id, session_id))

    async def set_waiting(self, session_id: str, device_id: str, calls: list[dict[str, Any]] | None, *, since: str = "") -> None:
        if calls is None:
            if self._waiting.pop(session_id, None) is None:
                return
            await self._say("remote.waiting", {"waiting": False, "device_id": device_id}, session_id)
            return
        entry = {"since": since or (self._waiting.get(session_id) or {}).get("since") or "", "calls": [{"tool_call_id": str(c.get("tool_call_id") or ""), "tool": str(c.get("tool") or "")} for c in calls]}
        if self._waiting.get(session_id) == entry:
            return
        self._waiting[session_id] = entry
        await self._say("remote.waiting", {"waiting": True, "device_id": device_id, **entry}, session_id)

    def waiting_of(self, session_id: str) -> dict[str, Any] | None:
        found = self._waiting.get(session_id)
        return {"since": found["since"], "calls": list(found["calls"])} if found is not None else None

    async def _say(self, event_type: str, payload: dict[str, Any], session_id: str) -> None:
        if self._announce is None:
            return
        try:
            await self._announce(event_type, payload, session_id)
        except Exception:  # noqa: BLE001 — recorded either way; the session view reads it
            logger.warning("could not announce %s", event_type, exc_info=True)




def device_token(header: str) -> str:
    """The token of an ``Authorization: Device <token>`` header, or ``""``."""
    scheme, _, value = (header or "").strip().partition(" ")
    return value.strip() if scheme.lower() == "device" else ""


class DeviceHub:
    def __init__(
        self,
        directory: Callable[[], DeviceDirectory | None],
        *,
        bound: Bound,
        control: ControlBook | None = None,
        announce: Announce | None = None,
        server_version: str = "",
    ) -> None:
        self._directory = directory
        self._bound = bound
        self._announce = announce
        self.control: ControlBook = control if control is not None else MemoryControl(announce)
        self.server_version = server_version
        self.links: dict[str, DeviceLink] = {}
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition()
        self.on_connect: list[Callable[[DeviceLink], Awaitable[None]]] = []
        """Called once a link is up and welcomed: the parked turns of its conversations go on from here."""
        self.on_disconnect: list[Callable[[str], Awaitable[None]]] = []

    # -- what the rest of the host asks ----------------------------------------------------------

    def link_of(self, device_id: str) -> DeviceLink | None:
        link = self.links.get(device_id)
        return link if link is not None and link.connected else None

    def connected(self, device_id: str) -> bool:
        return self.link_of(device_id) is not None

    def guard(self, device_id: str, session_id: str) -> str:
        """The local guard the device last reported for the session; ``auto`` until it reports one."""
        return self.control.guard_of(device_id, session_id) or "auto"

    async def _set_guard(self, device_id: str, session_id: str, guard: str) -> None:
        if guard in GUARDS:
            await self.control.set_guard(device_id, session_id, guard)

    async def wait_connected(self, device_id: str, timeout: float) -> bool:
        """Whether the device is connected, waiting up to ``timeout`` seconds for it to be."""
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self.connected(device_id)), timeout)
            except TimeoutError:
                return False
        return True

    async def revoked(self, device_id: str) -> None:
        """The device was revoked: its live relay ends with ``4401`` and it may not come back."""
        link = self.links.get(device_id)
        if link is not None:
            await link.close(CLOSE_UNAUTHORIZED, "this terminal was revoked; pair it again")

    async def close_all(self) -> None:
        for link in list(self.links.values()):
            await link.close(CLOSE_GOING_AWAY, "the server is going away")

    # -- the door --------------------------------------------------------------------------------

    async def serve(self, socket: RelaySocket, *, token: str = "", address: str = "", device: RemoteDevice | None = None) -> None:
        """Carry one device's relay until either side ends it. ``device`` when the door already checked
        the credential; otherwise ``token`` is checked here."""
        if device is None:
            directory = self._directory()
            found = await directory.check(token, address=address) if directory is not None and token else None
            device = RemoteDevice(str(found.id), str(getattr(found, "name", "") or "")) if found is not None else None
        if device is None:
            await socket.close(CLOSE_UNAUTHORIZED, "the device token is missing, unknown or revoked; run daedalus-cli login again")
            return
        hello = await self._hello(socket)
        if hello is None:
            return
        link = DeviceLink(device.id, device.name, socket, hello, on_notify=lambda method, params: self._notified(link, method, params))
        async with self._lock:
            old = self.links.get(device.id)
            self.links[device.id] = link
        if old is not None and old.connected:
            await old.close(CLOSE_REPLACED, "this terminal connected again")
        bound = await self._bound(device.id)
        try:
            await link.notify("device.welcome", {"device_id": device.id, "contract": CONTRACT, "server_version": self.server_version, "bound": bound})
            shown = [session_id for session_id in hello.sessions if await self._may_show(device.id, session_id, bound)]
            for session_id in shown:
                await self._set_guard(device.id, session_id, hello.sessions[session_id])
            await self._linked(device.id, [*shown, *(item["session_id"] for item in bound)])
            async with self._changed:
                self._changed.notify_all()
            await self._safe_announce("device.connection", {"device_id": device.id, "connected": True}, None)
            logger.info("terminal %s (%s) connected from %s, %d conversation(s) bound", device.id, device.name, address or "?", len(bound))
            carrying = asyncio.create_task(link.carry(), name=f"relay:{device.id}")
            for callback in list(self.on_connect):
                try:
                    await callback(link)
                except Exception:  # noqa: BLE001 — a turn that cannot go on must not cost the link
                    logger.exception("terminal %s: a connect callback failed", device.id)
            await carrying
        finally:
            await link.close(CLOSE_GOING_AWAY, link.reason or "relay ended")
            async with self._lock:
                if self.links.get(device.id) is link:
                    self.links.pop(device.id, None)
            async with self._changed:
                self._changed.notify_all()
            if self.links.get(device.id) is None:
                with contextlib.suppress(Exception):
                    await self.control.unlinked(device.id)
                await self._safe_announce("device.connection", {"device_id": device.id, "connected": False}, None)
                for callback in list(self.on_disconnect):
                    with contextlib.suppress(Exception):
                        await callback(device.id)
            logger.info("terminal %s disconnected: %s", device.id, link.reason)

    async def _hello(self, socket: RelaySocket) -> Hello | None:
        """The client's ``device.hello``, or ``None`` with the socket closed as the contract says."""
        try:
            first = await asyncio.wait_for(socket.receive(), HELLO_SECONDS)
        except TimeoutError:
            await socket.close(CLOSE_PROTOCOL, "no device.hello")
            return None
        if first is None:
            return None
        text = first.decode("utf-8", "replace") if isinstance(first, bytes) else first
        try:
            message = json.loads(text) if len(text.encode()) <= MAX_MESSAGE else None
        except ValueError:
            message = None
        if not isinstance(message, dict) or message.get("method") != "device.hello" or "id" in message:
            await socket.close(CLOSE_PROTOCOL, "the first message must be the device.hello notification")
            return None
        try:
            hello = Hello.parse(message.get("params"))
        except (TypeError, ValueError) as exc:
            await socket.close(CLOSE_PROTOCOL, f"device.hello: {exc}"[:120])
            return None
        if hello.protocol != PROTOCOL:
            await socket.close(CLOSE_PROTOCOL, f"this server speaks relay protocol {PROTOCOL}, not {hello.protocol}; update daedalus-cli")
            return None
        return hello

    async def _may_show(self, device_id: str, session_id: str, bound: list[dict[str, str]] | None = None) -> bool:
        if bound is not None and any(item.get("session_id") == session_id for item in bound):
            return True
        directory = self._directory()
        return bool(directory is not None and await directory.in_scope(device_id, session_id))

    async def _linked(self, device_id: str, sessions: list[str]) -> None:
        try:
            await self.control.linked(device_id, list(dict.fromkeys(sessions)))
        except Exception:  # noqa: BLE001 — the marks are a courtesy; the link is the work
            logger.warning("terminal %s: could not mark its conversations", device_id, exc_info=True)

    async def _notified(self, link: DeviceLink, method: str, params: dict[str, Any]) -> None:
        if method == "device.guard":
            session_id = str(params.get("session_id") or "")
            if session_id and await self._may_show(link.device_id, session_id, await self._bound(link.device_id)):
                await self._set_guard(link.device_id, session_id, str(params.get("guard") or ""))
        elif method == "device.sessions":
            # Only conversations in the device's scope: a client cannot mark or guard another one.
            bound = await self._bound(link.device_id)
            shown: list[str] = []
            for item in params.get("sessions") or []:
                if isinstance(item, dict) and isinstance(item.get("session_id"), str) and await self._may_show(link.device_id, item["session_id"], bound):
                    shown.append(item["session_id"])
                    await self._set_guard(link.device_id, item["session_id"], str(item.get("guard") or "auto"))
            await self._linked(link.device_id, [*shown, *(entry["session_id"] for entry in bound)])

    async def _safe_announce(self, event_type: str, payload: dict[str, Any], session_id: str | None) -> None:
        if self._announce is None:
            return
        try:
            await self._announce(event_type, payload, session_id)
        except Exception:  # noqa: BLE001 — an event is a courtesy to the app; the link is the work
            logger.warning("could not announce %s", event_type, exc_info=True)


__all__ = ["ControlBook", "DeviceDirectory", "DeviceHub", "MemoryControl", "RemoteDevice", "device_token"]
