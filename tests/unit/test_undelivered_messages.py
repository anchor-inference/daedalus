"""A message to a member either reaches it or fails where its sender sees it: never left ``queued``
for good because the session it waited for ended, the member was suspended under it, or the CLI it
was handed to on the command line died before reading it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from daedalus.config import Settings
from daedalus.host.events import AppEvent, EventFilter
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


async def failures(manager: Any, member: Any) -> dict[str, dict[str, Any]]:
    events = await manager.bus.latest(EventFilter(types=("staff.message",), staff_id=member.id), limit=50)
    return {e.payload["message_id"]: e.payload for e in events if e.payload.get("state") == "failed"}


async def test_a_message_still_queued_when_the_session_ends_fails_to_its_sender(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The orchestrator's "stop the render now" sat ``queued`` for two weeks after it released the
    member; nobody was ever told it had not arrived."""
    manager, team, runtime, member, live = await at_work(settings, db, tmp_path)
    try:
        await team.ingress.status(live, "working")
        runtime.receipt = Receipt("queued")
        told = await team.tell(member, "Stop the render now.", by="orchestrator")
        assert told["state"] == "queued"
        await team._end(live, "released by the orchestrator", stop=True, by="orchestrator")
        stored = await manager.staff.message(told["message_id"])
        assert stored.state == "failed"
        assert stored.error == "the session ended before the message reached it (released by the orchestrator)"
        payload = (await failures(manager, member))[told["message_id"]]
        assert payload["origin"] == "orchestrator"
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_session_closed_behind_the_teams_back_still_fails_its_messages(settings: Settings, db: Database, tmp_path: Path) -> None:
    """An exit the daemon observed, or a recovery, closes the row without the team's own ending; the
    tick finds what that left on its way."""
    manager, team, runtime, member, live = await at_work(settings, db, tmp_path)
    try:
        runtime.receipt = Receipt("queued")
        told = await team.tell(member, "One more thing.", by="orchestrator")
        await db.execute("UPDATE staff_sessions SET ended_at = ?, status = 'exited', end_reason = 'its terminal exited' WHERE id = ?",
                         (datetime.now(UTC).isoformat(), live.id))
        assert await team.settle_undelivered() == 1
        stored = await manager.staff.message(told["message_id"])
        assert stored.state == "failed" and "its terminal exited" in stored.error
        assert await team.settle_undelivered() == 0  # settled once
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_message_left_queued_under_a_suspended_member_is_failed_not_kept(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, member, live = await at_work(settings, db, tmp_path)
    try:
        runtime.receipt = Receipt("queued")
        told = await team.tell(member, "One more thing.", by="orchestrator")
        await db.execute("UPDATE staff_sessions SET status = 'suspended' WHERE id = ?", (live.id,))
        assert await team.settle_undelivered() == 0  # a moment ago: the sweep leaves it to the tell under way
        assert await team.settle_undelivered(datetime.now(UTC) + timedelta(minutes=5)) == 1
        stored = await manager.staff.message(told["message_id"])
        assert stored.state == "failed" and "suspended" in stored.error
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_first_prompt_whose_cli_died_while_starting_fails(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A CLI given its task on the command line that exits before it is ready never read it; its
    receipt said ``submitted`` for good, six times for one member whose launch kept crashing."""
    manager, team, runtime, member, live = await at_work(settings, db, tmp_path)
    try:
        [first] = await manager.staff.messages(member.id)
        assert first.state == "submitted" and live.session.status == "starting"
        await team.ingress.ended(live, "Claude Code exited with code 1")
        stored = await manager.staff.message(first.id)
        assert stored.state == "failed" and "exited with code 1" in stored.error
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_session_that_ended_after_its_start_keeps_what_it_was_given(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, member, live = await at_work(settings, db, tmp_path)
    try:
        await team.ingress.status(live, "working")
        live = await team.live_of(member)
        [first] = await manager.staff.messages(member.id)
        await team.ingress.ended(live, "its task was done")
        assert (await manager.staff.message(first.id)).state == "submitted"
    finally:
        await close_team(manager)
        await manager.close()


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


async def test_the_orchestrator_is_woken_by_its_own_message_that_failed(settings: Settings, db: Database, tmp_path: Path) -> None:
    """Tell answers "queued"; what became of the message arrived as an event no wake-up listened to,
    so an orchestrator went on counting on an order its member never got."""
    from tests.unit.test_orchestrator import rig
    from tests.unit.test_orchestrator_team import fake, office, working

    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.orch.stop_queue(r.project.id)
        member, live = await working(r)
        await r.team.ingress.status(live, "working")
        runtime.receipt = Receipt("queued")
        said = await r.call(sid, "tell", staff="Ada", text="Stop the render now.")
        message_id = said.split()[1]
        await r.team._end(await r.team.live_of(member), "its task was done", stop=False)
        events = await r.manager.bus.latest(EventFilter(types=("staff.message",), staff_id=member.id), limit=10)
        [failed] = [e for e in events if e.payload.get("state") == "failed"]
        wake = await r.orch.classify(r.project.id, failed)
        assert wake is not None and wake.key == f"message:{message_id}"
        line = await r.orch.line(r.project, failed)
        assert f"your message {message_id} to" in line and "Stop the render now." in line
        assert "was not delivered: the session ended before the message reached it (its task was done)" in line

        # The operator's own message is the operator's news, and a weeks-old one found late is history.
        operator = AppEvent(seq=0, at=failed.at, type="staff.message", payload={**failed.payload, "origin": "operator"},
                            project_id=r.project.id, staff_id=member.id)
        assert await r.orch.classify(r.project.id, operator) is None
        await db.execute("UPDATE staff_messages SET created_at = ? WHERE id = ?",
                         ((datetime.now(UTC) - timedelta(days=20)).isoformat(), message_id))
        assert await r.orch.classify(r.project.id, failed) is None
    finally:
        await close_team(r.manager)
        await r.manager.close()
