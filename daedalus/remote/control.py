"""Which sessions a connected terminal shows now, and the local guard it reports for each.

A session carries the live ``remote`` mark (:mod:`daedalus.host.control_marks`) while a terminal client
shows it: the client reported it in its presence within :data:`SHOWN_FOR`, or the client's relay is up
and says it shows it. The mark goes with the client. Unlike Chrome and the computer there is nothing to
hold idle: a terminal is either there or not, and a client that went quiet without a goodbye (a laptop
lid closed) loses the mark when its last report runs out.

Presence says it per client (one process of the client), and so does the relay: a device holds one
relay connection per process, each carrying the sessions that process shows, so two terminals open on
one laptop do not take each other's place. A session's calls go to the connection of its home device
that carries it, the one that said so last when two do (:meth:`RemoteControl.carrier`). Presence and
relay are kept apart and joined when the marks are recomputed, so one stopping does not take the other's
sessions with it.

When a device's last connection goes down, or a session stops being carried by any, the moment is kept
(:meth:`RemoteControl.disconnected_at`, :meth:`RemoteControl.session_disconnected_at`) and the device's
is announced with ``device.connection``, so every window tells a terminal that dropped a moment ago and
is likely reconnecting from one that has been gone for an hour, not only the window that watched it
drop. It is kept in memory: after a restart of the server a device that has not come back has no such
moment, which the app reads as offline.

The guard (``ask``, ``auto``, ``plan``) is kept per device and session, in memory: it is the client's own
rule and the client says it again whenever it connects, so a restart of the server only forgets it until
then. :meth:`RemoteControl.guard_of` is what the session view shows and what a turn's context reads.

A remote turn whose device is away holds its calls until the device is back; whatever parks it says
so here (:meth:`RemoteControl.set_waiting`), which announces ``remote.waiting`` and is what the session
view reads, so the app and ``/resume`` show the wait without knowing how the parking is done.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.remote.devices import GUARDS

logger = logging.getLogger(__name__)

SHOWN_FOR = 60.0
"""Seconds a client's report of the sessions it shows counts: three of its twenty-second re-sends."""
EVERY = 15.0
"""Seconds between two recomputes, which is how late a client that went quiet loses its mark."""
LINKS_PER_DEVICE = 8
"""Relay connections one device may hold at once, one per process: more is a client stuck in a loop."""


@dataclass(slots=True)
class _Link:
    sessions: frozenset[str]
    order: int
    """Rises with every declaration, so the newest carrier of a session is the one with the highest."""


@dataclass(slots=True)
class _Shown:
    device_id: str
    sessions: frozenset[str]
    at: float


class RemoteControl:
    """The ``remote`` marks, from what the clients report, and their guards."""

    def __init__(
        self,
        marks: Any,
        *,
        publish: Callable[[str, dict[str, Any], str | None], Awaitable[Any]] | None = None,
        drop_presence: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.marks = marks
        """The session manager's ``control_marks``."""
        self.publish = publish
        """``(type, payload, session id or None)``: how a guard, a wait and a connection are announced."""
        self.drop_presence = drop_presence
        self.clock = clock
        self.now = now
        self._lost_at: dict[str, str] = {}
        self._session_lost: dict[str, str] = {}
        self._shown: dict[str, _Shown] = {}
        self._links: dict[tuple[str, str], _Link] = {}
        self._order = 0
        self._guards: dict[tuple[str, str], str] = {}
        self._waiting: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # -- what the clients say ----------------------------------------------------------------------

    async def reported(self, device_id: str, client: str, sessions: Iterable[str]) -> None:
        """A client's presence report, its sessions already narrowed to the device's scope."""
        fresh = frozenset(sessions)
        before = self._shown.get(client)
        self._shown[client] = _Shown(device_id, fresh, self.clock())
        if before is None or before.sessions != fresh:
            await self.refresh()

    def links(self, device_id: str) -> list[str]:
        """The processes (their client ids) whose relay connection of this device is up."""
        return [client for (device, client) in self._links if device == device_id]

    def may_link(self, device_id: str, client: str) -> bool:
        """Whether one more connection of this device is allowed: its own process reconnecting always
        is, a new one while :data:`LINKS_PER_DEVICE` are up is not."""
        return (device_id, client) in self._links or len(self.links(device_id)) < LINKS_PER_DEVICE

    def _carried(self, device_id: str) -> set[str]:
        out: set[str] = set()
        for (device, _client), link in self._links.items():
            if device == device_id:
                out |= link.sessions
        return out

    async def _changed(self, device_id: str, before: set[str], had_link: bool) -> None:
        """After a connection of the device came, went or changed what it carries: the moments sessions
        stopped being carried, the device's own, and the announcement when the device came or went."""
        stamp = self.now().isoformat()
        after = self._carried(device_id)
        for session_id in before - after:
            self._session_lost[session_id] = stamp
        for session_id in after:
            self._session_lost.pop(session_id, None)
        has_link = bool(self.links(device_id))
        if has_link and not had_link:
            self._lost_at.pop(device_id, None)
            await self._announce("device.connection", {"device_id": device_id, "connected": True, "disconnected_at": None}, None)
        elif had_link and not has_link:
            self._lost_at[device_id] = stamp
            await self._announce("device.connection", {"device_id": device_id, "connected": False, "disconnected_at": stamp}, None)
        await self.refresh()

    async def linked(self, device_id: str, sessions: Iterable[str], *, client: str) -> None:
        """A relay connection of the device, the process ``client``, is up and carries these sessions (its
        ``device.hello`` or ``device.sessions``). The device's first connection announces it."""
        before, had_link = self._carried(device_id), bool(self.links(device_id))
        self._order += 1
        self._links[(device_id, client)] = _Link(frozenset(sessions), self._order)
        await self._changed(device_id, before, had_link)

    async def unlinked(self, device_id: str, *, client: str) -> None:
        """That connection went down; when it was the device's last, the moment is kept and announced."""
        before, had_link = self._carried(device_id), bool(self.links(device_id))
        if self._links.pop((device_id, client), None) is None:
            return
        await self._changed(device_id, before, had_link)

    def connected(self, device_id: str) -> bool:
        """Whether any relay connection of the device is up."""
        return bool(self.links(device_id))

    def carrier(self, device_id: str, session_id: str) -> str | None:
        """The connection (its client id) of the device that the session's calls go to: of those that
        carry it, the one that said so last; None when none does, and the turn waits."""
        found = [(link.order, client) for (device, client), link in self._links.items() if device == device_id and session_id in link.sessions]
        return max(found)[1] if found else None

    def disconnected_at(self, device_id: str) -> str | None:
        """When the device's last connection went down, while none is up; None while one is, and for a
        device that has not connected since this server started."""
        return None if self.connected(device_id) else self._lost_at.get(device_id)

    def session_disconnected_at(self, device_id: str, session_id: str) -> str | None:
        """When the session stopped being carried by any connection of its home device, while none
        carries it; the device's own moment when it went with the device; None while it is carried."""
        if self.carrier(device_id, session_id) is not None:
            return None
        return self._session_lost.get(session_id) or self.disconnected_at(device_id)

    async def _announce(self, name: str, payload: dict[str, Any], session_id: str | None) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(name, payload, session_id)
        except Exception:  # noqa: BLE001 — the state is kept; a front that missed it reads it on its next list
            logger.warning("could not announce %s", name, exc_info=True)

    async def forget_device(self, device_id: str) -> None:
        """A revoked device: what its clients reported, its relay's sessions and its guards go at once."""
        clients = [client for client, shown in self._shown.items() if shown.device_id == device_id]
        for client in clients:
            del self._shown[client]
            if self.drop_presence is not None:
                try:
                    await self.drop_presence(client)
                except Exception:  # noqa: BLE001 — the report runs out on its own within a minute
                    logger.warning("could not drop the presence of client %s", client, exc_info=True)
        for key in [key for key in self._links if key[0] == device_id]:
            del self._links[key]
        self._lost_at.pop(device_id, None)
        for key in [key for key in self._guards if key[0] == device_id]:
            del self._guards[key]
        await self.refresh()

    # -- the guard ----------------------------------------------------------------------------------

    async def set_guard(self, device_id: str, session_id: str, guard: str) -> bool:
        """The guard the device's client has for this session; announced when it changed. Whether it did."""
        if guard not in GUARDS:
            raise ValueError(f"a guard is one of {', '.join(GUARDS)}, not {guard!r}")
        key = (device_id, session_id)
        if self._guards.get(key) == guard:
            return False
        self._guards[key] = guard
        await self._announce("remote.guard", {"device_id": device_id, "guard": guard}, session_id)
        return True

    def guard_of(self, device_id: str, session_id: str) -> str | None:
        """The guard this device reported for the session, or None before it reported one. What matters
        for a session's tools is its home device's (``Devices.home``)."""
        return self._guards.get((device_id, session_id))

    # -- a turn waiting for its device -----------------------------------------------------------

    async def set_waiting(self, session_id: str, device_id: str, calls: list[dict[str, Any]] | None, *, since: str = "") -> None:
        """A remote session's turn holds calls for its device until it is back (``calls``), or stopped
        holding them (``None``); announced as ``remote.waiting`` when that changed. Called by whatever
        parks the turn; the session view reads :meth:`waiting_of`."""
        before = self._waiting.get(session_id)
        if calls is None:
            if self._waiting.pop(session_id, None) is None:
                return
            payload: dict[str, Any] = {"waiting": False, "device_id": device_id}
        else:
            entry = {"since": since or (before or {}).get("since") or "", "calls": [{"tool_call_id": str(c.get("tool_call_id") or ""), "tool": str(c.get("tool") or "")} for c in calls]}
            if before == entry:
                return
            self._waiting[session_id] = entry
            payload = {"waiting": True, "device_id": device_id, **entry}
        await self._announce("remote.waiting", payload, session_id)

    def waiting_of(self, session_id: str) -> dict[str, Any] | None:
        """``{since, calls}`` while the session's turn waits for its device, else None."""
        found = self._waiting.get(session_id)
        return {"since": found["since"], "calls": list(found["calls"])} if found is not None else None

    # -- the marks ----------------------------------------------------------------------------------

    def shown(self) -> set[str]:
        """The sessions a connected terminal shows now."""
        now = self.clock()
        out: set[str] = set()
        for shown in self._shown.values():
            if now - shown.at <= SHOWN_FOR:
                out |= shown.sessions
        for link in self._links.values():
            out |= link.sessions
        return out

    async def refresh(self) -> None:
        async with self._lock:
            now = self.clock()
            for client in [client for client, shown in self._shown.items() if now - shown.at > SHOWN_FOR]:
                del self._shown[client]
            await self.marks.update("remote", self.shown())

    async def watch(self, every: float = EVERY) -> None:
        """Runs for the life of the host: a client that stopped reporting loses its mark."""
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 — the next round tries again
                logger.exception("recomputing which sessions a terminal shows failed")
            await asyncio.sleep(every)


__all__ = ["EVERY", "SHOWN_FOR", "RemoteControl"]
