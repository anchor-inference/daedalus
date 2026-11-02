"""The relay of the terminal client's executor, and remote conversations' turns in the session manager.

Installs :class:`daedalus.remote.relay.DeviceHub` (the links), :class:`daedalus.remote.journal.Journal`
(the audit and delivery record of every request) and :class:`daedalus.remote.turns.RemoteTurns` (whose
hands a session has, and turns that wait for them) before the API, which serves ``wss
/api/cli/relay`` from them (``api_remote_relay``). The pairing's store and the control book come from
the devices' extension when it is installed (``remote_devices``, ``remote_control``); without it the
relay refuses every device, and the guards and waits are kept in memory here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus import __version__
from daedalus.remote.journal import Journal
from daedalus.remote.relay import DeviceHub
from daedalus.remote.turns import RemoteTurns, subscribe

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    bus = manager.bus

    async def announce(name: str, payload: dict[str, Any], session_id: str | None) -> None:
        await bus.publish(name, payload, session_id=session_id)

    devices = app.extensions.get("remote_devices")
    turns: RemoteTurns | None = None

    async def bound(device_id: str) -> list[dict[str, str]]:
        return await turns.bound_to(device_id) if turns is not None else []

    hub = DeviceHub(lambda: app.extensions.get("remote_devices"), bound=bound, control=app.extensions.get("remote_control"), announce=announce, server_version=__version__)
    journal = Journal(app.db)

    async def homes() -> dict[str, tuple[str, str]]:
        if devices is None:
            return {}
        return {session_id: (binding.device_id, binding.cwd) for session_id, binding in (await devices.homes()).items()}

    turns = RemoteTurns(manager, hub, journal, homes=homes)
    await turns.load()
    subscribe(manager, turns)
    if devices is not None and hasattr(devices, "revoked_hooks"):
        devices.revoked_hooks.append(hub.revoked)
    app.extensions["remote_relay"] = hub
    app.extensions["remote_journal"] = journal
    app.extensions["remote_turns"] = turns
    pruned = await journal.prune()
    if pruned:
        logger.info("forgot %d remote call(s) older than the audit keeps", pruned)
    return []


__all__ = ["install"]
