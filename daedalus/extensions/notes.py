"""The notes store shared by the app's routes and the agent's ``Notes`` tool, and the cleanup of the
bytes a deleted chat or project leaves behind.

Note rows go with their chat or project by cascade, inside the deletion's own transaction; the blob
bytes cannot, since the blob store is files on disk. Once the deletion is announced, the prune frees
every note blob no row names any more.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.host.events import EventFilter
from daedalus.stores.notes import HOOK, store_for

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    store = store_for(manager)
    app.extensions[HOOK] = store

    async def prune(event: Any = None) -> None:
        if event is not None and event.type == "project.changed" and event.payload.get("change") != "removed":
            return
        freed = await store.prune_blobs()
        if freed:
            logger.info("freed %d note files left by a deleted chat or project", freed)

    tasks = [asyncio.create_task(prune(), name="notes-prune")]
    tasks.append(manager.bus.on(EventFilter(types=("session.deleted", "project.changed")), prune, name="notes-prune"))
    return tasks


__all__ = ["install"]
