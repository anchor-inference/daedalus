"""Waiting out an endpoint that cannot be reached, before any byte of the request has left.

A deploy recreates the key proxy, and for a few seconds every route behind it refuses connections. The
core's in-place retry gave such a failure two more attempts, one and two seconds apart, and so spent its
whole allowance in about three seconds: runs dozens of steps deep stopped on
``All connection attempts failed`` while the proxy was still starting. A refused connection is the one
failure where sending the request again cannot do anything twice, because nothing was sent, so the
adapter waits it out itself, for a bounded stretch, and only then hands the failure to the core.

What counts is narrow on purpose: a refused, unreachable or unresolvable endpoint, and a connect that
timed out, all raised before the request was written; and the key proxy's own report that it could not
connect to its upstream. Anything after a byte went out (a reset mid-body, a read timeout, a stream cut
half-way) may have reached the model, and stays with the core's existing recovery.
"""

from __future__ import annotations

import random
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

PATIENCE_SECONDS = 120.0
"""How long an unreachable endpoint is waited for before the failure goes to the core. A container
recreated by a deploy answers again in seconds, a host reboot in about a minute; past two minutes the
endpoint is down rather than restarting, and the run's fallback chain or the host's outage recovery,
which waits minutes between whole turns, is the better answer."""

FIRST_WAIT_SECONDS = 1.0
LONGEST_WAIT_SECONDS = 15.0
"""The longest single pause. Well under the core's 90 s idle watchdog, which the adapter feeds with a
progress delta before every pause, and short enough that an endpoint back after 20 s is not left
unused for another half minute."""

_PROXY_UNREACHABLE = ("upstream unreachable: ConnectError", "upstream unreachable: ConnectTimeout")
"""The key proxy's words when it could not open a connection to its upstream (``deploy/keyproxy``):
the upstream never saw the request, exactly as if the adapter's own connect had failed."""


def unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` is a connect that failed before any byte of the request was written.

    A certificate failure is raised as a ``ConnectError`` too, and is excluded: it is a configuration
    fault that two minutes of waiting does not fix.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return True
    if not isinstance(exc, httpx.ConnectError):
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return False
        current = current.__cause__ or current.__context__
    return True


def proxy_unreachable(status: int, text: str) -> bool:
    """Whether a response is the key proxy saying it could not connect to its upstream."""
    return status == 502 and '"proxy_error"' in text and any(marker in text for marker in _PROXY_UNREACHABLE)


@dataclass(slots=True)
class Patience:
    """The waits one request is allowed against an unreachable endpoint.

    The pause grows from ``first`` towards ``longest`` with full jitter, so the runs a deploy cut off
    together do not come back in one burst, and the total is bounded by wall clock from the first failure
    rather than by a count: a connect that takes 30 s to time out spends the budget as fast as it should.
    """

    budget: float = PATIENCE_SECONDS
    first: float = FIRST_WAIT_SECONDS
    longest: float = LONGEST_WAIT_SECONDS
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    began: float | None = None
    attempts: int = 0
    waited: list[float] = field(default_factory=list)

    def next_wait(self) -> float | None:
        """The pause before the next attempt, or ``None`` once the budget is spent."""
        now = self.clock()
        if self.began is None:
            self.began = now
        left = self.budget - (now - self.began)
        if left <= 0:
            return None
        self.attempts += 1
        ceiling = min(self.longest, self.first * 2 ** (self.attempts - 1))
        # Never below half the ceiling: full jitter down to zero turns some waits into busy retries.
        pause = min(ceiling * (0.5 + 0.5 * self.jitter()), left)
        self.waited.append(pause)
        return pause

    @property
    def elapsed(self) -> float:
        return 0.0 if self.began is None else self.clock() - self.began


__all__ = ["PATIENCE_SECONDS", "Patience", "proxy_unreachable", "unreachable"]
