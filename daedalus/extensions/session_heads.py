"""The branch and the repository of a chat whose folder this process cannot read.

The session detail carries ``branch`` and ``repository`` and the app polls it. For a folder this
process can see they come from the repository's ``HEAD`` file (:mod:`daedalus.host.git_branch`). A
chat working in a host folder from the container has its folder on the other side: only git run
there, through the host's terminal daemon (as the chat's Changes routes run it), can say. That is a
round trip and a process on the host, which a poll must not wait for or pay each time.

So the answer is kept per session for a short while and served from memory: a poll returns what is
known (nothing, until the first answer), and a stale or missing answer is asked again in the
background, at most one question per session at a time, bounded by the daemon's own timeout.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from daedalus.host.git_branch import UNKNOWN, Head
from daedalus.host.gitrun import git_argv
from daedalus.terminals.changes import GIT_ENV

logger = logging.getLogger(__name__)

FRESH_S = 15.0
"""How long an answer is served before it is asked again: a checkout made in a terminal shows within this."""
TIMEOUT_S = 5.0
"""The daemon ends git after this; reading one symbolic ref takes milliseconds."""
KEPT = 512
"""Sessions remembered at once; the least recently asked go first."""
OUTPUT_MAX = 4096
_BRANCH = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


@dataclass(slots=True)
class _Known:
    place: tuple[str, str]
    head: Head
    at: float


class HostHeads:
    """``branch`` and ``repository`` of the sessions whose folder is in another environment, by session."""

    def __init__(self, terminals: Callable[[], Any], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._terminals = terminals
        self._clock = clock
        self._known: dict[str, _Known] = {}
        self._asking: dict[str, asyncio.Task[None]] = {}

    def peek(self, session_id: str, env: str, directory: str) -> Head:
        """What is known of the folder now, never waiting; asks again in the background when the
        answer is missing, stale, or was for another folder (the chat moved)."""
        place = (env, directory)
        known = self._known.get(session_id)
        if known is not None and known.place == place:
            # Most recently asked last, so the oldest is the one let go when the table is full.
            self._known[session_id] = self._known.pop(session_id)
        fresh = known is not None and known.place == place and self._clock() - known.at < FRESH_S
        if not fresh and session_id not in self._asking:
            task = asyncio.get_running_loop().create_task(self._ask(session_id, place), name=f"head:{session_id}")
            self._asking[session_id] = task
        return known.head if known is not None and known.place == place else UNKNOWN

    async def settled(self) -> None:
        """Every question in flight answered: for a shutdown and for a test."""
        while self._asking:
            await asyncio.gather(*list(self._asking.values()), return_exceptions=True)

    async def _ask(self, session_id: str, place: tuple[str, str]) -> None:
        try:
            head = await self.read(*place)
        except Exception:  # noqa: BLE001 — the detail is served whatever the bridge does; it reads "not known"
            logger.debug("could not read the branch of %s", session_id, exc_info=True)
            head = UNKNOWN
        finally:
            self._asking.pop(session_id, None)
        # Kept even when not known: a daemon that is down is asked again after the same while, not on every poll.
        self._known[session_id] = _Known(place, head, self._clock())
        while len(self._known) > KEPT:
            self._known.pop(next(iter(self._known)))

    async def read(self, env: str, directory: str) -> Head:
        """One ``git symbolic-ref`` in ``directory`` through ``env``'s daemon: exit 0 names the branch,
        1 is a detached ``HEAD`` (a repository on no branch), and "not a git repository" is none. Anything
        else (no daemon, a folder that is gone, an owner git refuses) is not known."""
        terminals = self._terminals()
        if terminals is None or not terminals.configured(env):
            return UNKNOWN
        ran = await terminals.exec_run(env, git_argv("-C", directory, "symbolic-ref", "--quiet", "--short", "HEAD"),
                                       env_vars=dict(GIT_ENV), timeout=TIMEOUT_S, max_output=OUTPUT_MAX, actor="system", audit=False)
        if ran.timed_out:
            return UNKNOWN
        if ran.exit_code == 0:
            name = ran.stdout.strip()
            return Head(True, name if _BRANCH.match(name) else None)
        if ran.exit_code == 1 and not ran.stderr.strip():
            return Head(True, None)
        if "not a git repository" in ran.stderr:
            return Head(False, None)
        return UNKNOWN


__all__ = ["HostHeads"]
