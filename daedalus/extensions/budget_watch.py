"""The daily cap reopening: what waited for it goes on.

The supervisor writes ``BUDGET_EXCEEDED`` when the day's spend passes the cap and removes it when the
spend is under the cap again — at midnight UTC, or after the operator raises it. Nothing in the bot
used to notice the removal: runs boot recovery parked for the cap stayed parked until some later
restart, and a helper's report its leader could not take was lost. This watch looks every half a
minute and hands each of them on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from daedalus.extensions.subagents import BUDGET_POLL_SECONDS

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


async def tick(app: Application) -> None:
    """One look: resume the parked runs if the cap is open, then let the helpers catch up."""
    manager = app.manager
    if manager is None or manager.recovering or manager.shutting_down:
        return
    if manager.budget_parked and not manager.budget_exceeded():
        resumed = await manager.resume_budget_parked()
        if resumed:
            logger.warning("the daily cap is open again; resumed %d run(s) it had parked", len(resumed))
    subagents = app.extensions.get("subagents")
    if subagents is not None:
        await subagents.budget_tick()


async def watch(app: Application) -> None:
    while True:
        await asyncio.sleep(BUDGET_POLL_SECONDS)
        try:
            await tick(app)
        except Exception:  # noqa: BLE001 — the next look tries again
            logger.exception("budget watch failed")


async def install(app: Application) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(watch(app), name="budget-watch")]
