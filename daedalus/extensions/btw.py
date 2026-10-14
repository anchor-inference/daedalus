"""A side question's place in the life of its chat: it goes when the chat goes (:mod:`daedalus.host.btw`)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from daedalus.host import btw

if TYPE_CHECKING:
    from daedalus.app import Application


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None

    async def session_deleted(session_id: str) -> None:
        # Before the chat's rows go: one left behind would keep the chat's scratch project and its directory
        # alive, in a session no list shows and nobody could reach to delete.
        await btw.close_btw(manager, session_id)

    manager.delete_hooks.append(session_deleted)
    return []
