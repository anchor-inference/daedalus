"""A session is loaded once, however many callers ask for it at the same moment."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config


async def test_two_concurrent_loads_share_one_state_and_stop_reaches_it(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        created = await manager.create_session("loaded twice")
        sid = created.session.id
        manager._states.pop(sid)
        # Slow one of the reads between "not held" and "installed", as a busy database does after a
        # restart, so both callers are inside the load at once.
        original = manager.projects.for_session

        async def slow(session_id: str) -> Any:
            await asyncio.sleep(0.05)
            return await original(session_id)

        manager.projects.for_session = slow  # type: ignore[method-assign]
        first, second = await asyncio.gather(manager.get_state(sid), manager.get_state(sid))
        assert first is not None and first is second
        assert manager.live_state(sid) is first

        # The run starts on the object one caller holds; Stop, which reads the map, reaches it.
        stopped: list[bool] = []
        first.engine = SimpleNamespace(stop=lambda: stopped.append(True))  # type: ignore[assignment]
        first.task = asyncio.create_task(asyncio.sleep(10))
        try:
            assert await manager.stop(sid) is True
            assert stopped == [True]
        finally:
            first.task.cancel()
            first.engine = None
    finally:
        await manager.close()


async def test_a_caller_that_gives_up_does_not_cancel_the_load_for_the_others(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        created = await manager.create_session("abandoned")
        sid = created.session.id
        manager._states.pop(sid)
        original = manager.projects.for_session

        async def slow(session_id: str) -> Any:
            await asyncio.sleep(0.05)
            return await original(session_id)

        manager.projects.for_session = slow  # type: ignore[method-assign]
        impatient = asyncio.create_task(manager.get_state(sid))
        patient = asyncio.create_task(manager.get_state(sid))
        await asyncio.sleep(0.01)
        impatient.cancel()
        state = await patient
        assert state is not None and manager.live_state(sid) is state
        assert not manager._loading
    finally:
        await manager.close()
