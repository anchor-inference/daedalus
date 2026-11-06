"""How often an address or a conversation may knock.

In memory, on purpose: a restart forgets them, and what they bound is a burst, not a total. The totals
that matter (money, turns) are in the database.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class Failures:
    """Failed attempts per address and overall, like the app's own public doors.

    Past either bound every attempt is refused until the window moves on, the right one included.
    Only failures count, so a guest with a good token is never held by another guest's guessing.
    """

    def __init__(self, *, per_address: int = 10, overall: int = 200, window: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_address = per_address
        self.overall = overall
        self.window = window
        self.clock = clock
        self._all: deque[float] = deque()
        self._by: dict[str, deque[float]] = {}

    def _trim(self, queue: deque[float], now: float) -> None:
        while queue and now - queue[0] > self.window:
            queue.popleft()

    def blocked(self, address: str) -> bool:
        now = self.clock()
        self._trim(self._all, now)
        mine = self._by.get(address)
        if mine is not None:
            self._trim(mine, now)
            if not mine:
                self._by.pop(address, None)
        return len(self._all) >= self.overall or (mine is not None and len(mine) >= self.per_address)

    def failed(self, address: str) -> None:
        now = self.clock()
        self._all.append(now)
        self._by.setdefault(address, deque()).append(now)
        if len(self._by) > 4096:
            # A flood from many addresses: the overall bound holds it; this table must not grow for ever.
            for key in list(self._by)[:2048]:
                self._by.pop(key, None)


class Rate:
    """At most ``limit`` calls in ``window`` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._by: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        queue = self._by.setdefault(key, deque())
        while queue and now - queue[0] > self.window:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        if len(self._by) > 4096:
            for stale in list(self._by)[:2048]:
                self._by.pop(stale, None)
        return True


__all__ = ["Failures", "Rate"]
