"""What a session controls right now: the operator's Chrome, their computer, or a terminal of theirs.

A session's kind (``sessions.kind``) says what it was made for and never changes. That a chat is
working in the operator's Chrome, or holds their computer, is a different fact: live, and over as soon
as the control is. The two used to be one column. The first time an ordinary chat was given the
operator's Chrome or a computer grant it became a Chrome or computer conversation for good, sat in the
Browser tab forever, and the extension announced every later turn of it in Chrome.

The marks live in memory. Whoever knows the control recomputes them (the browser service from the tab
groups in the operator's Chrome, the computer's watcher from the desktop daemon's lease) and hands the
whole set over; a session whose marks changed is announced as ``session.controlling``, which is what
moves its row into and out of the Browser and Computer tabs without waiting for a poll.

The rule is the same for both (:func:`controlling`). A session controls X while it holds it (an open
tab group in the operator's Chrome; the desktop lease) and one of these is true:

- it is waiting for the operator there (a hand-over in their Chrome);
- a run of it is going, or parked on the operator's answer, and it used the control since that run
  started;
- it used the control within the last :data:`IDLE_AFTER`.

Otherwise it is idle, and the mark is gone even though it still holds the control. A turn in the
operator's Chrome closes its tabs when it ends, so there the mark normally goes with the turn; the five
minutes are for a browser left open (tab closing turned off) or a lease granted for an hour, so a chat
that stopped using them does not keep the mark until the grant runs out, and a run that pauses between
two clicks does not flicker out of the tab.

A terminal (``remote``) follows a simpler rule of its own (``daedalus.remote.control``): a session is
marked while a connected terminal client shows it, and the mark goes with the client. A terminal is
either there or not; there is no lease to hold idle.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CONTROLS = ("chrome", "computer", "remote")
"""What a session can control, spelled as the session kinds that are made for it."""
IDLE_AFTER = timedelta(minutes=5)
"""How long after its last use a held control still marks a session that has no run going."""


def controlling(*, used_at: datetime, now: datetime, run_since: datetime | None = None, waits_here: bool = False) -> bool:
    """Whether a session that holds a control counts as controlling it now (the module's rule).

    ``used_at`` is the control's last use (a browser action, a computer action, the grant itself);
    ``run_since`` when the session's current run started, ``None`` when it has none going or parked;
    ``waits_here`` that the operator is asked to act inside the control (a hand-over)."""
    if waits_here:
        return True
    if run_since is not None and used_at >= run_since:
        return True
    return now - used_at < IDLE_AFTER


Announce = Callable[[str, list[str]], Awaitable[None]]
"""``(session id, its controls now)``: called once per session whose marks changed."""


class ControlMarks:
    """The sessions controlling each control now, as their recomputers last said."""

    def __init__(self, announce: Announce) -> None:
        self._announce = announce
        self._held: dict[str, frozenset[str]] = {control: frozenset() for control in CONTROLS}

    def of(self, session_id: str) -> list[str]:
        """The controls this session holds a mark for, in :data:`CONTROLS` order."""
        return [control for control in CONTROLS if session_id in self._held[control]]

    def holders(self, control: str) -> frozenset[str]:
        return self._held[control]

    async def update(self, control: str, session_ids: Iterable[str]) -> None:
        """Replace who controls ``control`` with ``session_ids`` and announce every session that
        gained or lost the mark. The whole set, not a change: a recomputer that missed an event is
        corrected by its next round instead of leaving a mark behind."""
        if control not in CONTROLS:
            raise ValueError(f"a control is one of {', '.join(CONTROLS)}, not {control!r}")
        fresh = frozenset(session_ids)
        changed = sorted(fresh ^ self._held[control])
        # Swapped before anything is awaited, so a recompute that runs meanwhile diffs against this one.
        self._held[control] = fresh
        for session_id in changed:
            try:
                await self._announce(session_id, self.of(session_id))
            except Exception:  # noqa: BLE001 — the mark stands; a front that missed it reads it on its next list
                logger.warning("could not announce the controls of session %s", session_id, exc_info=True)

    def forget(self, session_id: str) -> None:
        """A deleted session holds nothing; dropped without an announcement nobody could act on."""
        for control in CONTROLS:
            if session_id in self._held[control]:
                self._held[control] = self._held[control] - {session_id}


__all__ = ["CONTROLS", "IDLE_AFTER", "ControlMarks", "controlling"]
