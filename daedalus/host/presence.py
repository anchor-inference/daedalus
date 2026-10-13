"""What the operator is looking at, as the app's windows report it.

Every window of the app says, every twenty seconds while it is visible and whenever that changes,
whether it is visible and focused and which sessions, terminals and projects it shows. From those
reports the host answers three questions without asking anybody:

- *attending X*: some window is visible, focused, has reported recently, and shows X;
- *present*: some window is visible and focused;
- *away*: neither.

It is decided here rather than in a window because push and desktop notifications are sent by the
host, which needs the answer at the moment it decides whether to send one.

A report lives ``ttl_seconds``. A window that also holds ``/api/events`` is tied to that connection:
when the connection drops, the window counts for ``grace_seconds`` more (a reconnect inside that
keeps it) and is then forgotten, which is far sooner than the report would expire on its own — a
phone that is locked stops talking without saying goodbye. A connection with ``kind=launcher`` is
the desktop launcher listening for notifications to raise; it is a deliverer, never a presence.

Every change is published on the bus as the ephemeral ``presence`` event, never stored and never
streamed, with the ids that became attended, so an in-process subscriber (the unread-result mark,
the notification router) can act on "the operator just opened this".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from daedalus.host.events import EventBus
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

LOCALE_KEY = "operator_locale"
"""Where the last reported language and time zone are kept, so the host can write in the
operator's language after a restart, before any window has reported again."""

MAX_SESSIONS = 4
MAX_TERMINALS = 8
MAX_PROJECTS = 4
MAX_ID_LENGTH = 64
"""What one report may name. A window shows at most a split view of two sessions plus the companion
screen's own; anything larger is not a window describing itself."""


@dataclass(frozen=True, slots=True)
class PresenceReport:
    client: str
    """A per-tab id the app keeps in ``sessionStorage``; the same id it sends to ``/api/events``."""
    kind: str = "browser"
    """browser · pwa · telegram · window"""
    visible: bool = False
    focused: bool = False
    sessions: tuple[str, ...] = ()
    terminals: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    screen: str = ""
    lang: str = ""
    tz: str = ""

    def attends(self) -> bool:
        return self.visible and self.focused


@dataclass(slots=True)
class _Client:
    kind: str
    report: PresenceReport | None = None
    reported_at: float = 0.0
    streams: int = 0
    """Open ``/api/events`` connections that named this client."""
    lost_at: float | None = None
    """When the last of those connections closed; ``None`` while one is open or none ever was."""


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    """A frozen view of presence at one moment, for decisions that must not do I/O."""

    present: bool
    sessions: frozenset[str] = field(default_factory=frozenset)
    terminals: frozenset[str] = field(default_factory=frozenset)
    projects: frozenset[str] = field(default_factory=frozenset)
    desktop: bool = False
    lang: str = ""
    tz: str = ""

    def attending(self, *, session_id: str | None = None, terminal_id: str | None = None, project_id: str | None = None) -> bool:
        return _attends(self.sessions, self.terminals, self.projects, session_id, terminal_id, project_id)


def _attends(
    sessions: Iterable[str], terminals: Iterable[str], projects: Iterable[str],
    session_id: str | None, terminal_id: str | None, project_id: str | None,
) -> bool:
    """Whether any of the named ids is attended. Naming none asks nothing and is answered no."""
    return (
        (session_id is not None and session_id in sessions)
        or (terminal_id is not None and terminal_id in terminals)
        or (project_id is not None and project_id in projects)
    )


class Presence:
    """The host's view of the operator's windows. Built by the session manager as ``presence``."""

    def __init__(
        self,
        bus: EventBus,
        db: Database,
        *,
        ttl_seconds: float = 60.0,
        grace_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.db = db
        self.ttl_seconds = ttl_seconds
        self.grace_seconds = grace_seconds
        self._clock = clock
        self._clients: dict[str, _Client] = {}
        self._desktops = 0
        self._locale: tuple[str, str] = ("", "")
        self._timers: set[asyncio.Task[None]] = set()

    async def load(self) -> None:
        """Read the persisted locale; called once at start."""
        stored = await self.db.kv_get(LOCALE_KEY, None)
        if isinstance(stored, dict):
            self._locale = (str(stored.get("lang") or ""), str(stored.get("tz") or ""))

    async def close(self) -> None:
        for task in list(self._timers):
            task.cancel()
        if self._timers:
            await asyncio.gather(*self._timers, return_exceptions=True)

    # -- what the windows say --------------------------------------------------------------

    async def report(self, report: PresenceReport) -> None:
        """One window's account of itself. Publishes ``presence`` when anything changed."""
        now = self._clock()
        before = self._attended(now)
        client = self._clients.get(report.client)
        previous = client.report if client is not None and self._fresh(client, now) else None
        if client is None:
            client = self._clients[report.client] = _Client(kind=report.kind)
        client.kind = report.kind
        client.report = report
        client.reported_at = now
        # A window that is still reporting is alive, whatever happened to its event stream.
        client.lost_at = None
        await self._remember_locale(report.lang, report.tz)
        if previous is not None and _same_view(previous, report):
            return  # the periodic re-send: it only keeps the report fresh
        await self._publish(report, before, now)

    async def stream_opened(self, client: str, kind: str) -> None:
        """An ``/api/events`` connection began. A launcher's is a desktop deliverer."""
        if kind == "launcher":
            self._desktops += 1
            return
        if not client:
            return
        entry = self._clients.get(client)
        if entry is None:
            entry = self._clients[client] = _Client(kind=kind)
        entry.streams += 1
        entry.lost_at = None

    async def stream_closed(self, client: str, kind: str) -> None:
        """That connection ended. The window counts for the grace period, then is forgotten."""
        if kind == "launcher":
            self._desktops = max(0, self._desktops - 1)
            return
        entry = self._clients.get(client) if client else None
        if entry is None:
            return
        entry.streams = max(0, entry.streams - 1)
        if entry.streams:
            return
        entry.lost_at = self._clock()
        task = asyncio.create_task(self._forget_after_grace(client, entry.lost_at), name=f"presence-grace:{client}")
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _forget_after_grace(self, client: str, lost_at: float) -> None:
        await asyncio.sleep(self.grace_seconds)
        entry = self._clients.get(client)
        if entry is None or entry.streams or entry.lost_at != lost_at:
            return  # it reconnected, or reported, inside the grace
        now = self._clock()
        # Measured with the client still counted, so the event says what this window stopped showing.
        was = entry.report if self._fresh(entry, lost_at) else None
        before = self._attended(now)
        del self._clients[client]
        if was is not None and was.attends():
            await self._publish(PresenceReport(client=client, kind=entry.kind), before, now)

    # -- the answers -------------------------------------------------------------------------

    def attending(self, *, session_id: str | None = None, terminal_id: str | None = None, project_id: str | None = None) -> bool:
        """Some window is visible, focused, fresh, and shows one of the named things."""
        sessions, terminals, projects = self._attended(self._clock())
        return _attends(sessions, terminals, projects, session_id, terminal_id, project_id)

    def present(self) -> bool:
        """Some window is visible and focused, whatever it shows."""
        now = self._clock()
        return any(c.report is not None and c.report.attends() and self._fresh(c, now) for c in self._clients.values())

    def desktop_connected(self) -> bool:
        """A desktop launcher holds ``/api/events`` and will raise what is marked for the desktop."""
        return self._desktops > 0

    def locale(self) -> tuple[str, str]:
        """``(lang, tz)`` as last reported by any window, persisted across restarts; empty when never."""
        return self._locale

    def snapshot(self) -> PresenceSnapshot:
        now = self._clock()
        sessions, terminals, projects = self._attended(now)
        return PresenceSnapshot(
            present=self.present(),
            sessions=frozenset(sessions),
            terminals=frozenset(terminals),
            projects=frozenset(projects),
            desktop=self.desktop_connected(),
            lang=self._locale[0],
            tz=self._locale[1],
        )

    # -- inside --------------------------------------------------------------------------------

    def _fresh(self, client: _Client, now: float) -> bool:
        if client.report is None or now - client.reported_at > self.ttl_seconds:
            return False
        return not (client.streams == 0 and client.lost_at is not None and now - client.lost_at > self.grace_seconds)

    def _attended(self, now: float) -> tuple[set[str], set[str], set[str]]:
        sessions: set[str] = set()
        terminals: set[str] = set()
        projects: set[str] = set()
        for client in self._clients.values():
            report = client.report
            if report is None or not report.attends() or not self._fresh(client, now):
                continue
            sessions.update(report.sessions)
            terminals.update(report.terminals)
            projects.update(report.projects)
        return sessions, terminals, projects

    async def _publish(self, report: PresenceReport, before: tuple[set[str], set[str], set[str]], now: float) -> None:
        after = self._attended(now)
        payload: dict[str, Any] = {
            "client": report.client,
            "visible": report.visible,
            "focused": report.focused,
            "sessions": list(report.sessions),
            "terminals": list(report.terminals),
            "projects": list(report.projects),
            "attended": report.attends(),
            "newly_attended": {
                "sessions": sorted(after[0] - before[0]),
                "terminals": sorted(after[1] - before[1]),
                "projects": sorted(after[2] - before[2]),
            },
        }
        try:
            await self.bus.publish("presence", payload)
        except Exception:  # noqa: BLE001 — a window's report must never fail because of a subscriber
            logger.warning("could not publish presence", exc_info=True)

    async def _remember_locale(self, lang: str, tz: str) -> None:
        # A field the window left empty keeps what an earlier report said.
        updated = (lang or self._locale[0], tz or self._locale[1])
        if updated == self._locale:
            return
        self._locale = updated
        try:
            await self.db.kv_set(LOCALE_KEY, {"lang": updated[0], "tz": updated[1]})
        except Exception:  # noqa: BLE001 — the language survives in memory; the next change writes it again
            logger.warning("could not store the operator's locale", exc_info=True)


def _same_view(a: PresenceReport, b: PresenceReport) -> bool:
    return (a.visible, a.focused, a.sessions, a.terminals, a.projects) == (b.visible, b.focused, b.sessions, b.terminals, b.projects)


__all__ = ["LOCALE_KEY", "MAX_ID_LENGTH", "MAX_PROJECTS", "MAX_SESSIONS", "MAX_TERMINALS", "Presence", "PresenceReport", "PresenceSnapshot"]
