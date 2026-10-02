"""Telling the operator, in the app, that a newer desktop launcher is out, and letting them act on it.

The desktop launcher checks the project's releases itself (``desktop/release.go``) and reports what it
found in its own status, which this process can already read through the launcher bridge. The launcher
also raises a desktop notification, but that is easy to miss and does nothing on a machine without a
notification daemon; the app is where the operator actually looks. So this reads the launcher's
status now and then and, for a release it has not announced before, posts one entry to the app's own
notification centre, in the operator's language: what is out, and the exact command that installs it
on this machine.

Settings → About reads the same status (:func:`describe`), asks the launcher to look again
(:func:`check_now`) and, in the desktop window, asks it to install (:func:`install_now`). Nothing is
installed unasked. The upgrade asks for a yes, stops the stack, keeps the data from
before it (a whole copy of the data folder on Linux with ext4, a verified backup elsewhere), and puts
the data and the launcher back on failure — it runs in a terminal, with the launcher closed, because
it replaces the launcher's own files and restarts this very process.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft
from daedalus.host import launcher_bridge
from daedalus.host.notify_text import render

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
    language = app.notifications.language()
    installable = body.get("installable") is True
    lines = [render("launcher.upgrade.current", language, version=_version(current))]
    if installable:
        # The desktop window installs it with a button; a terminal command there was a dead end
        # for an operator who has no reason to open one.
        lines.append(render("launcher.upgrade.here", language))
    elif offer.get("package"):
        lines.append(render("launcher.upgrade.package", language))
    else:
        # A launcher run from a terminal, with no window to close and reopen: the terminal it runs
        # in is where the command goes.
        if command:
            lines.append(render("launcher.upgrade.command", language, command=command))
        lines.append(render("launcher.upgrade.promise", language))
    if url := str(offer.get("url") or ""):
        lines.append(render("launcher.upgrade.notes", language, url=url))
    await app.notifications.post(Draft(
        "system",
        render("launcher.upgrade", language, version=_version(to)),
        "\n\n".join(lines),
        kind="launcher_upgrade",
        link="/app/settings/about" if installable else "",
        dedupe_key=f"launcher-upgrade:{to}",
    ))
    _note(state_dir, to)
    return offer


def _offer(body: dict[str, Any]) -> dict[str, str] | None:
    offer = body.get("upgrade")
    if not isinstance(offer, dict) or not str(offer.get("to") or "").startswith("desktop-v"):
        return None
    # The command that installs it from a terminal stays with the launcher: the app installs with a
    # button and never shows one.
    return {key: str(offer.get(key) or "") for key in ("from", "to", "url", "package")}


async def describe(app: Application | Any) -> dict[str, Any]:
    """What the About page shows: whether a launcher is beside this process, its version, the newer
    release it found, whether it can install that from here, and when it last looked.

    ``connected`` is false on a server with no launcher (a source checkout, a plain Docker host):
    the page then has nothing to check and says so, rather than offering a button that cannot work.
    """
    launcher = await asyncio.to_thread(launcher_bridge.read, Path(app.settings.state_dir))
    if launcher is None:
        return {"connected": False}
    try:
        body = await launcher_bridge.status(launcher)
    except launcher_bridge.LauncherUnavailable:
        return {"connected": False}
    offer = _offer(body)
    # A launcher from before the app's update button (0.15.1 and older) reports neither whether it
    # can install from its window nor a download. Its upgrade action installs in one step from the
    # window and refuses with its reason without one, so the app offers that and shows the refusal.
    legacy = "download" not in body
    installable = body.get("installable") is True or (legacy and offer is not None and not offer["package"])
    about = {
        "connected": True,
        # An older launcher does not report its version; the offer it made says it.
        "version": _version(str(body.get("version") or (offer or {}).get("from") or "")),
        "upgrade": offer,
        "installable": installable,
        "checked_at": str(body.get("upgrade_checked") or ""),
        "error": str(body.get("upgrade_error") or ""),
        "download": _download(body),
    }
    if legacy:
        about["direct"] = True
    return about


def _download(body: dict[str, Any]) -> dict[str, Any]:
    download = body.get("download") if isinstance(body.get("download"), dict) else {}
    state = str(download.get("state") or "idle")
    return {
        "state": state if state in {"idle", "running", "done", "failed"} else "idle",
        "done": int(download.get("done") or 0),
        "total": int(download.get("total") or 0),
        "error": str(download.get("error") or ""),
        "ready": download.get("ready") is True,
    }


async def check_now(app: Application | Any) -> dict[str, Any]:
    """Ask the launcher to look for a newer release now, then describe what it found."""
    launcher = await asyncio.to_thread(launcher_bridge.read, Path(app.settings.state_dir))
    if launcher is None:
        return {"connected": False}
    await launcher_bridge.act(launcher, "check-upgrade")
    return await describe(app)


async def download_now(app: Application | Any) -> dict[str, Any]:
    """Start downloading the offered release beside the installation, then describe how it is going.
    Nothing is replaced and nothing stops: that is :func:`install_now`, once the download is ready."""
    launcher = await asyncio.to_thread(launcher_bridge.read, Path(app.settings.state_dir))
    if launcher is None:
        return {"connected": False}
    await launcher_bridge.act(launcher, "download")
    return await describe(app)


async def install_now(app: Application | Any) -> None:
    """Ask the launcher to install the release it offers. It closes the window and reopens it on the
    new version, so the answer is the last thing this process says before the restart."""
    launcher = await asyncio.to_thread(launcher_bridge.read, Path(app.settings.state_dir))
    if launcher is None:
        raise launcher_bridge.LauncherUnavailable("no launcher is running beside this installation")
    await launcher_bridge.act(launcher, "upgrade")


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


__all__ = ["check_now", "check_once", "describe", "download_now", "install", "install_now"]
