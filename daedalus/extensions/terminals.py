"""Terminals: the terminal daemons of this installation, reached through ``daedalus.terminals``.

Installs the service, ties terminals to the lives of their owners — a session or a project that is
deleted ends its terminals — retells what the daemons report on the event bus, hands a session's
agent read access to its own ones, and offers the host bridge (``app.extensions["host_bridge"]``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from daedalus.terminals.bridge import HostBridge
from daedalus.terminals.bus import BusBridge
from daedalus.terminals.owners import ManagerOwners
from daedalus.terminals.service import Terminals
from daedalus.terminals.update import DaemonUpdate

if TYPE_CHECKING:
    from daedalus.app import Application


def build(app: Application) -> Terminals:
    assert app.manager is not None
    settings = app.settings
    return Terminals(
        app.db,
        run_dirs={"container": settings.terminals_container_dir, "host": settings.terminals_host_dir},
        config=lambda: app.config.terminals,
        owners=ManagerOwners(app.manager),
        bus=app.manager.bus,
        public_host=settings.services_public_host.strip(),
        # A server in a container terminal is published on the terminals service's own range. One in
        # a host terminal is on the machine itself, on whatever port it picked.
        port_ranges={"container": settings.terminals_port_range, "host": ""},
        # Only a compose install runs the container's daemon from this image; natively there is no
        # image, and the launcher updates its own daemon.
        daemon_update=DaemonUpdate(settings.rebuild_trigger_dir) if settings.terminals_container_dir is not None and not settings.native else None,
    )


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    terminals = build(app)
    app.extensions["terminals"] = terminals
    # The host as a Docker installation reaches it: a folder checked or made there, git run there.
    app.extensions["host_bridge"] = HostBridge(lambda: terminals)
    # And a Daedalus session working in a host folder: its commands and file tools run there.
    manager.host_bridge = app.extensions["host_bridge"]

    async def session_deleted(session_id: str) -> None:
        await terminals.close_owned("session", session_id)

    async def project_deleted(project_id: str) -> None:
        await terminals.close_owned("project", project_id)

    manager.delete_hooks.append(session_deleted)
    manager.project_delete_hooks.append(project_deleted)
    manager.service_hooks["terminals"] = terminals.agent_service
    # Before the connections start, so the events replayed on the first one are retold as well.
    terminals.subscribe(BusBridge(terminals))
    tasks = await terminals.start()
    update = terminals.daemon_update
    if update is not None:

        async def probe() -> None:
            await update.probe()

        # In the background: it runs a program, and nothing about the start waits for its answer.
        tasks.append(asyncio.create_task(probe(), name="terminals-image-version"))

    async def closer() -> None:
        # Cancelled at shutdown like every background task; the connections go with it, which leaves
        # the daemons and their terminals running for the next start to find.
        try:
            await asyncio.Event().wait()
        finally:
            await terminals.close()

    return [*tasks, asyncio.create_task(closer(), name="terminals-close")]
