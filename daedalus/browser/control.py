"""Which conversations are controlling the operator's Chrome now (:mod:`daedalus.host.control_marks`).

A conversation controls it while its tab group in one of their Chromes is open, that Chrome is connected, the
conversation is still given that Chrome, and the rule of ``control_marks`` holds: it waits for the
operator there (a hand-over), a run of it has used the group since it started, or the group was used
within the last few minutes. The group's last use is ``browser_groups.last_activity_at``, which every
call through it moves along.

Recomputed whenever something that decides it is announced — a group opened, closed or lost, a run
started or ended, a question asked or answered, the Chrome connecting or going away — and every
:data:`EVERY` seconds besides, since going idle is announced by nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.browser.model import USER, chrome_of_profile
from daedalus.host.control_marks import controlling

if TYPE_CHECKING:
    from daedalus.browser.service import Browsers

logger = logging.getLogger(__name__)

EVERY = 30.0
"""Seconds between two rounds when nothing was announced: how late an idle mark may go."""
EVENTS = ("browser.", "run.started", "run.finished", "session.status", "ask.", "permission.")
"""The bus events after which the marks are recomputed at once."""
NEVER = datetime.min.replace(tzinfo=UTC)


def _stamp(text: Any) -> datetime:
    try:
        at = datetime.fromisoformat(str(text or ""))
    except ValueError:
        return NEVER
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


class ChromeControl:
    def __init__(self, service: Browsers, manager: Any, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.service = service
        self.manager = manager
        self.now = now
        self._lock = asyncio.Lock()

    async def current(self) -> set[str]:
        """The conversations controlling the operator's Chrome as of now."""
        if not self.service.user_offered() or not self.service.user_connection()[0]:
            return set()
        connected = set(self.service.connected_chromes())
        given = set(await self.service.choices.using_user())
        if not given:
            return set()
        rows = await self.service.db.fetchall(
            "SELECT session_id, profile, last_activity_at, needs_json FROM browser_groups"
            " WHERE status = 'open' AND env = ? AND owner_kind = 'session' AND session_id IS NOT NULL",
            (USER,),
        )
        now = self.now()
        return {
            str(row["session_id"])
            for row in rows
            if str(row["session_id"]) in given
            and chrome_of_profile(str(row["profile"])) in connected
            and controlling(used_at=_stamp(row["last_activity_at"]), now=now, run_since=self.manager.run_since(str(row["session_id"])), waits_here=bool(row["needs_json"]))
        }

    async def refresh(self) -> None:
        # One round at a time: two interleaved rounds could hand over their sets in the wrong order.
        async with self._lock:
            await self.manager.control_marks.update("chrome", await self.current())

    async def on_event(self, _event: Any) -> None:
        await self.refresh()

    async def watch(self, every: float = EVERY) -> None:
        """Runs for the life of the host."""
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 — the next round tries again
                logger.exception("recomputing which conversations control the operator's Chrome failed")
            await asyncio.sleep(every)


__all__ = ["EVENTS", "EVERY", "ChromeControl"]
