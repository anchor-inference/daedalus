"""The operator hears once that Exec's sandbox is down, and once that it is back.

One entry per outage, kept by :mod:`daedalus.tools.sandbox_health`, rather than a failed command in red in
every chat: while the sandbox cannot be made every command of every session fails the same way, and the
fault is the machine's. The entry offers two answers. Check drops the probe's answer and asks again (and
in the agent's container puts a foreign ``/proc`` right on the way). Switching the sandbox off runs commands
without it until the agent restarts, never for a guest's turn, which runs nothing outside its own sandbox.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from daedalus.extensions.notifications import Action, ActionOutcome, ActionRefused, ActionRequest, Draft
from daedalus.host.notify_text import render
from daedalus.tools import sandbox_health
from daedalus.tools.shell import bwrap_status, invalidate_bwrap

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

REF = "sandbox"
LINK = "/app/health"


def ref_of(outage: sandbox_health.Outage) -> str:
    return f"{REF}:host:{outage.number}"


def listener(app: Application) -> sandbox_health.Listener:
    async def tell(event: str, outage: sandbox_health.Outage) -> None:
        notifications = app.notifications
        if notifications is None:
            return
        lang = notifications.language()
        if event == "up":
            await notifications.resolve(ref_of(outage), render("sandbox.up", lang), via="sandbox")
            return
        await notifications.post(Draft(
            "system", render("sandbox.down", lang), render("sandbox.down.body", lang, reason=outage.reason),
            kind="sandbox", tone="error", link=LINK,
            actions=(Action("recheck", render("sandbox.recheck", lang), "primary"), Action("off", render("sandbox.off", lang), "danger")),
            request_ref=ref_of(outage), dedupe_key=ref_of(outage), source="sandbox",
        ))

    return tell


def resolver(app: Application):  # type: ignore[no-untyped-def]
    async def resolve(request: ActionRequest) -> ActionOutcome:
        lang = app.notifications.language() if app.notifications is not None else "en"
        if request.action == "off":
            sandbox_health.suspend()
            return ActionOutcome(render("sandbox.suspended", lang))
        if request.action != "recheck":
            raise ActionRefused(f"the sandbox notice is not answered with {request.action!r}")
        invalidate_bwrap()
        status = await asyncio.to_thread(bwrap_status)
        if status != "ok":
            raise ActionRefused(render("sandbox.still", lang, reason=status))
        await sandbox_health.report_up()
        return ActionOutcome(render("sandbox.up", lang))

    return resolve


async def install(app: Application) -> list[asyncio.Task[None]]:
    if app.notifications is None:
        return []
    sandbox_health.listen("notice", listener(app))
    app.notifications.register_resolver(REF, resolver(app))
    return []


__all__ = ["install", "listener", "ref_of", "resolver"]
