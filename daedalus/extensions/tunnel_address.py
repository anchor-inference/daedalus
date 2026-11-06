"""The public address of a native installation, as the launcher reports it.

The operator opens a tunnel in the app's settings; the launcher keeps it (``desktop/tunnel.go``) and only
it knows the address, which on some providers changes at every start. This follows the launcher and
publishes what it hears to :mod:`daedalus.public_address`, so that shared services and agent links are
handed out on the address that is really there, and tells the guest door, which writes that address into
its pages. A tunnel going away takes the address away with it, and links go back to this machine's own.

While it is reconnecting the address is kept: it comes back, and a link made in that moment should carry
it, rather than a loopback address nobody outside can use.

The agent has no way in here. There is no tool for this, the app's API port is sealed from it and the
launcher's token is in a file it may not read, and a tunnel it raises itself is
refused and reported.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import public_address
from daedalus.host import launcher_bridge

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSION_KEY = "tunnel_address"
TICK_SECONDS = 5.0
"""How long a new address can take to reach a link. One small read on loopback."""
HOLDS_ADDRESS = frozenset({"up", "reconnecting"})


class TunnelAddress:
    def __init__(self, app: Application) -> None:
        self.app = app
        settings = app.settings
        # What the operator configured by hand, which is what is left when a tunnel goes away.
        self.configured = (settings.services_public_url, settings.guest_public_url)
        self.published = ""
        self.door_told = ""
        self.own_pids: frozenset[int] = frozenset()
        """The tunnel's own processes, as the launcher names them, for the watch and the doctor."""

    async def read(self) -> dict[str, Any]:
        """The launcher's view, or the reason there is none: ``unavailable`` in a sentence, ``reason`` as a key."""
        launcher = await asyncio.to_thread(launcher_bridge.read, Path(self.app.settings.state_dir))
        if launcher is None:
            return {"available": False, "reason": "no_launcher", "unavailable": "no launcher is running beside this installation"}
        try:
            return {"available": True, **await launcher_bridge.tunnel(launcher)}
        except launcher_bridge.LauncherTooOld as exc:
            return {"available": False, "reason": "old_launcher", "unavailable": str(exc)}
        except launcher_bridge.LauncherUnavailable as exc:
            return {"available": False, "reason": "no_launcher", "unavailable": str(exc)}

    async def refresh(self) -> dict[str, Any]:
        view = await self.read()
        self.own_pids = frozenset(pid for pid in view.get("pids") or () if isinstance(pid, int) and pid > 1)
        await self.publish(view)
        return view

    async def publish(self, view: dict[str, Any]) -> None:
        held = view.get("enabled") and view.get("state") in HOLDS_ADDRESS
        address = str(view.get("address") or "") if held else ""
        if address != self.published:
            services, guest = self.configured
            public_address.publish(self.app.settings, services=address or services, guest=address or guest)
            self.published = address
            logger.info("the public address is now %s", address or "off")
        door = self.app.extensions.get("guest_door")
        if door is None or address == self.door_told or not door.client.configured:
            return
        try:
            await door.publish_address()
            self.door_told = address
        except Exception:  # noqa: BLE001 — a door that is down is told on the next tick, and on its next connection
            logger.info("the guest door could not be told the address yet")

    async def change(self, body: dict[str, Any]) -> dict[str, Any]:
        launcher = await asyncio.to_thread(launcher_bridge.read, Path(self.app.settings.state_dir))
        if launcher is None:
            raise launcher_bridge.LauncherUnavailable("no launcher is running beside this installation")
        await launcher_bridge.tunnel(launcher, body)
        return await self.refresh()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 — one bad round must not end the following
                logger.exception("could not read the public address")
            await asyncio.sleep(TICK_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    address = TunnelAddress(app)
    app.extensions[EXTENSION_KEY] = address
    return [asyncio.create_task(address.run(), name="tunnel-address")] if app.settings.native else []


__all__ = ["EXTENSION_KEY", "TunnelAddress", "install"]
