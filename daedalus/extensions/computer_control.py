"""Which conversation is controlling the operator's computer now (:mod:`daedalus.host.control_marks`).

The desktop daemon holds at most one lease, for one conversation, until it expires, the operator takes
it back, or they stop everything. That conversation controls the computer while the rule of
``control_marks`` holds: a run of it has used the lease since it started, or the lease was granted or
used within the last few minutes. A grant used to make a plain chat a computer one for good; now the
chat shows the mark while it controls the computer and is a chat again once it does not.

The daemon says how many actions the lease has taken but not when, so the watcher notes the moment it
sees the count move (and the grant's own time for a lease it has not seen before) as the last use.

Read every :data:`EVERY` seconds while this installation has a desktop daemon, at once when the app
grants, revokes or stops (``api_computer``), and when a run starts or ends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.host.control_marks import controlling
from daedalus.host.events import EventFilter

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EVERY = 15.0
"""Seconds between two reads of the lease: how late a lease that ran out leaves the Computer tab."""
EVENTS = ("run.started", "run.finished", "session.status")


def _stamp(text: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


class ComputerControl:
    def __init__(self, manager: Any, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.manager = manager
        self.now = now
        self._seen: tuple[str, int] | None = None
        self._used_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def current(self) -> set[str]:
        """The conversation controlling the computer as of now, or nobody."""
        lease = await self.manager.computer_holder()
        now = self.now()
        if lease is None:
            self._seen = self._used_at = None
            return set()
        seen = (str(lease.get("id") or ""), int(lease.get("actions") or 0))
        if seen != self._seen:
            # A lease not seen before was last used when it was granted; one whose count moved, now.
            fresh = self._seen is None or seen[0] != self._seen[0]
            self._used_at = (_stamp(lease.get("granted_at")) or now) if fresh else now
            self._seen = seen
        session_id = str(lease.get("session") or "")
        used = self._used_at or now
        return {session_id} if controlling(used_at=used, now=now, run_since=self.manager.run_since(session_id)) else set()

    async def refresh(self) -> None:
        async with self._lock:
            await self.manager.control_marks.update("computer", await self.current())

    async def on_event(self, _event: Any) -> None:
        await self.refresh()

    async def watch(self, every: float = EVERY) -> None:
        """Runs for the life of the host."""
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 — the next round tries again
                logger.exception("recomputing which conversation controls the computer failed")
            await asyncio.sleep(every)


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    if manager is None or not manager.capabilities.computer.configured:
        # No desktop daemon on this installation (a server): nothing can hold the computer.
        return []
    control = ComputerControl(manager)
    app.extensions["computer_control"] = control
    return [
        manager.bus.on(EventFilter(types=EVENTS), control.on_event, name="computer-control"),
        asyncio.create_task(control.watch(), name="computer-control-idle"),
    ]


__all__ = ["EVENTS", "EVERY", "ComputerControl", "install"]
