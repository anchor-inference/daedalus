"""The tunnel watch: a process that looks like a tunnel, told to the operator once.

The policy refuses a tunnel it can read in a command (:mod:`daedalus.host.tunnels`). It cannot read
one a script raises, one under another name, or one started in a container, and an agent that was
refused once is the agent most likely to try one of those. So the same reading is applied to what is
running, on a timer, and the first sighting of each process goes to the operator's Inbox (and wherever
their notification settings send a system notice), with its command line and how long it has run.

It tells and does not kill: a tunnel the operator started themselves from a terminal looks the same
as one the agent started, and ending the operator's own process on a guess is worse than a notice.
Once per process, so a tunnel that stays up is not an hourly nag; a restart of the host tells again,
because what was said before it is not remembered. Whatever the installation itself raises, once it
raises any, must be kept out of this by the process it starts, or it will be reported as a stranger.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from daedalus.extensions.notifications import Draft
from daedalus.host import tunnels
from daedalus.host.notify_text import render

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
"""A scan reads one small file per process of this user and runs in a thread; half a minute is the
slowest that still tells the operator while the run that raised the tunnel is on."""
FIRST_PASS_DELAY_SECONDS = 60.0
"""The first look waits, so it does not compete with the start itself."""


class TunnelWatch:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.known: set[tuple[int, str]] = set()
        """The processes already told, by id and command line: a process id alone is reused."""

    async def check(self) -> list[tunnels.Running]:
        """Look once, tell about what is new, and return what was found."""
        found = await asyncio.to_thread(tunnels.running)
        if not found:
            self.known = set()  # nothing runs, so the next one is news whatever its id
            return []
        fresh = [r for r in found if (r.pid, r.command) not in self.known]
        self.known = {(r.pid, r.command) for r in found}
        if fresh:
            await self.tell(fresh)
        return found

    async def tell(self, fresh: list[tunnels.Running]) -> None:
        for process in fresh:
            logger.warning("a tunnel is running: %s", process.line)
        notifications = self.app.notifications
        if notifications is None:
            return
        language = notifications.language()
        try:
            await notifications.post(Draft(
                "system", render("tunnel.running", language), render("tunnel.running.body", language, processes="\n".join(f"- {r.line}" for r in fresh)),
                kind="tunnel", tone="warning", link="/app/health",
                dedupe_key="tunnel:" + ",".join(str(r.pid) for r in fresh), source="tunnels",
            ))
        except Exception:  # noqa: BLE001 — a notice that failed is logged; the process stays on the doctor's list
            logger.exception("the tunnel watch could not post its notice")

    async def run(self) -> None:
        await asyncio.sleep(FIRST_PASS_DELAY_SECONDS)
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 — one bad round must not end the watching
                logger.exception("the tunnel watch failed a round")
            await asyncio.sleep(TICK_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    watch = TunnelWatch(app)
    app.extensions["tunnel_watch"] = watch
    return [asyncio.create_task(watch.run(), name="tunnel-watch")]
