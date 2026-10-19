"""A message to a member either reaches it or fails where its sender sees it: never left ``queued``
for good because the session it waited for ended, the member was suspended under it, or the CLI it
was handed to on the command line died before reading it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from daedalus.config import Settings
from daedalus.staff_runtime import LiveSession, OutgoingMessage, Receipt
from daedalus.stores.database import Database
from tests.support.authorized_launch import operator_assignment
from tests.unit.test_staff_runtime import board_task, close_team, fake_team


async def at_work(settings: Settings, db: Database, tmp_path: Path, name: str = "Ada") -> tuple[Any, Any, Any, Any, LiveSession]:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    member = await manager.staff.hire(project.id, name=name, isolation="shared")
    task_id = await board_task(manager, project, "Menu")
    await operator_assignment(team, member, task_id)
    live = await team.live_of(member)
    assert live is not None
    return manager, team, runtime, member, live


async def test_a_message_is_handed_over_under_the_suspend_lock(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The idle sweep skips a member whose suspend lock is held. A message handed over outside it could
    go into the delivery queue of a CLI the sweep was stopping, and wait there for good."""
    manager, team, runtime, member, live = await at_work(settings, db, tmp_path)
    try:
        held: list[bool] = []
        plain = runtime.send

        async def send(live: LiveSession, msg: OutgoingMessage) -> Receipt:
            held.append(team.suspend_lock(member.id).locked())
            return await plain(live, msg)

        runtime.send = send  # type: ignore[method-assign]
        await team.ingress.status(live, "working")
        await asyncio.wait_for(team.tell(member, "One more thing.", by="orchestrator"), 5)
        assert held == [True]
        assert not team.suspend_lock(member.id).locked()
    finally:
        await close_team(manager)
        await manager.close()
