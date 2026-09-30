"""Telling the operator, in the app, that a newer desktop launcher is out.

The desktop launcher checks the project's releases itself (``desktop/release.go``) and reports what it
found in its own status, which this process can already read through the launcher bridge. The launcher
also raises a desktop notification, but that is easy to miss and does nothing on a machine without a
notification daemon; the app is where the operator actually looks. So this reads the launcher's
status now and then and, for a release it has not announced before, posts one entry to the app's own
notification centre: what is out, and the exact command that installs it on this machine.

Nothing is installed from here. The upgrade asks for a yes, stops the stack, backs up and checks the
data and the launcher, and rolls back on failure — it runs in a terminal, with the launcher closed,
because it replaces the launcher's own files and restarts this very process.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft
from daedalus.host import launcher_bridge

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

FIRST_CHECK_SECONDS = 90
"""The launcher's own first check runs 30 s after it starts; this reads its answer a minute later."""
CHECK_SECONDS = 30 * 60
NOTED_FILE = "launcher-upgrade-announced"
"""The last release announced, in the state directory, so a restart does not announce it again."""


def _noted(state_dir: Path) -> str:
    try:
        return (state_dir / NOTED_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _note(state_dir: Path, tag: str) -> None:
    try:
        (state_dir / NOTED_FILE).write_text(tag + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not record that %s was announced", tag)


def _version(tag: str) -> str:
    return tag.removeprefix("desktop-v")


async def check_once(app: Application | Any) -> dict[str, Any] | None:
    """One look at the launcher. Returns the offer it announced, or ``None``."""
    state_dir = Path(app.settings.state_dir)
    launcher = await asyncio.to_thread(launcher_bridge.read, state_dir)
    if launcher is None:
        return None
    try:
        body = await launcher_bridge.status(launcher)
    except launcher_bridge.LauncherUnavailable:
        return None
    offer = body.get("upgrade")
    if not isinstance(offer, dict):
        return None
    to, current, command = str(offer.get("to") or ""), str(offer.get("from") or ""), str(offer.get("command") or "")
    if not to.startswith("desktop-v") or _noted(state_dir) == to:
        return None
    lines = [f"This installation's launcher is {_version(current)}. Nothing is installed by itself."]
    if command:
        lines.append(f"Close the launcher and run in a terminal:\n{command}")
    lines.append("It asks first, backs up your data and the launcher and checks the backup, and puts everything back if the new version does not come up.")
    if url := str(offer.get("url") or ""):
        lines.append(f"Release notes: {url}")
    await app.notifications.post(Draft(
        "system",
        f"Daedalus {_version(to)} is available",
        "\n\n".join(lines),
        kind="launcher_upgrade",
        dedupe_key=f"launcher-upgrade:{to}",
    ))
    _note(state_dir, to)
    return offer


async def _watch(app: Application) -> None:
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    while True:
        try:
            await check_once(app)
        except Exception:  # noqa: BLE001 — a failed look is tried again later, never a crash
            logger.exception("could not check the launcher for a newer release")
        await asyncio.sleep(CHECK_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    if app.notifications is None:
        return []
    return [asyncio.create_task(_watch(app), name="launcher-updates")]


__all__ = ["check_once", "install"]
