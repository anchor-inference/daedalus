"""Optional subsystems attached to the running application.

Each extension exposes ``async def install(app) -> list[asyncio.Task]``; the tasks
are background loops the application cancels on shutdown.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING

from daedalus.extensions.notifications import Draft

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSIONS = (
    "daedalus.extensions.notifications",
    "daedalus.extensions.effects",
    "daedalus.extensions.push",
    "daedalus.extensions.selfdev",
    "daedalus.extensions.scheduler",
    "daedalus.extensions.heartbeat",
    "daedalus.extensions.learning",
    "daedalus.extensions.inbound",
    "daedalus.extensions.board",
    "daedalus.extensions.peers",
    "daedalus.extensions.subagents",
    "daedalus.extensions.jobs",
    "daedalus.extensions.disk_guard",
    "daedalus.extensions.staff",
    "daedalus.extensions.orchestrator",
    "daedalus.extensions.project_topics",
    "daedalus.extensions.dispatches",
    "daedalus.extensions.dispatcher",
    "daedalus.extensions.dispatcher_projects",
    "daedalus.extensions.loops",
    "daedalus.extensions.services",
    "daedalus.extensions.dialog_share",
    "daedalus.extensions.balance",
    "daedalus.extensions.voice",
    "daedalus.extensions.terminals",
    "daedalus.extensions.harness",
    "daedalus.extensions.browser",
    "daedalus.extensions.watches",
    "daedalus.extensions.plugins",
    "daedalus.extensions.skill_quality",
    "daedalus.extensions.board_workflows",
    "daedalus.extensions.lifecycle",
    "daedalus.extensions.phase_expiry",
    "daedalus.extensions.issue_sync",
    "daedalus.extensions.launcher_updates",
    "daedalus.extensions.calendar_reminders",
    "daedalus.extensions.api",
)


def enabled(app: Application) -> tuple[str, ...]:
    """The extensions this installation actually runs.

    Self-development is the one subsystem an installation may not have at all: with the mode off
    there is no worktree to open, no proposal to decide and no rebuild to ask for, so the extension
    is not installed and nothing it would have registered exists.
    """
    mode = app.manager.capabilities.selfdev.mode if app.manager is not None else "off"
    browser = app.manager.capabilities.browser.configured if app.manager is not None else False
    # A browser likewise: without a browser daemon told of, there is no browser, no tools and no routes.
    skipped = {name for name, off in (("daedalus.extensions.selfdev", mode == "off"), ("daedalus.extensions.browser", not browser)) if off}
    return tuple(name for name in EXTENSIONS if name not in skipped)


FATAL = ("daedalus.extensions.notifications",)
"""The extensions a start may not do without, and why.

Everything else is isolated: an extension that raises while installing is logged, reported to
the operator and skipped, and the rest of the chain still installs. The reason is what a
failure costs on each side. A bot that refuses to start says nothing to anyone — no chat, no
Mini App, no notifications — and under a supervisor it says nothing repeatedly, so the one channel the
operator has for finding out what is wrong is the channel the failure closed. A bot that starts
without its board or its scheduler is diminished and says so, and the operator can decide.

``notifications`` is the exception, and it is fatal deliberately: it is the channel every other
failure is reported through. Starting without it would produce exactly the silent degradation
the isolation exists to avoid — a bot running with subsystems missing and no way to say which.
It is installed first for the same reason, so the failures below it have somewhere to go.

An extension that fails partway may already have registered hooks; those stay registered. The
alternative — unwinding a half-installed extension — needs an uninstall path per extension that
nothing else would ever call, and a hook whose subsystem is missing fails at its own call site,
where it is one tool's error rather than the whole start.
"""


async def install_all(app: Application) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    failures: dict[str, str] = {}
    for name in enabled(app):
        short = name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(name)
            tasks.extend(await module.install(app))
        except Exception as exc:  # noqa: BLE001 — see FATAL: one subsystem must not cost the whole start
            if name in FATAL:
                raise
            logger.exception("extension %s failed to install", name)
            failures[short] = f"{type(exc).__name__}: {exc}"
    app.extension_failures = failures
    if app.notifications is not None:
        for short, reason in failures.items():
            await app.notifications.post(Draft(
                "system",
                f"The {short} subsystem did not start",
                f"{reason}\n\nThe rest of the bot is running without it. Its tools and commands fail until the next restart fixes it.",
                kind="extension",
                tone="error",
            ))
    return tasks


__all__ = ["EXTENSIONS", "FATAL", "enabled", "install_all"]
