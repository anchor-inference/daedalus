"""A finished run reports itself finished at once, and what it still owes happens behind that.

The bug these cover: everything a run did after its last token — the snapshot, the run-finished
callbacks, the queue it drained — used to happen inside the run's own task, so the session read as
running for as long as it took and the app drew a blinking cursor under an answer that was over.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.stores.database import Database
from tests.support.waiting import until
from tests.unit.test_session_runner import ScriptedProvider, _manager

SLOW = 2.0
"""How long the stand-in housekeeping takes. A memory extraction is a model call and takes longer."""


async def _settled_at(manager: Any, session_id: str) -> tuple[list[TurnEvent], list[float]]:
    """Collects the run_settled events and the moment each arrived."""
    events: list[TurnEvent] = []
    at: list[float] = []

    async def sink(sid: str, event: TurnEvent) -> None:
        if sid == session_id and event.type is EventType.RUN_SETTLED:
            events.append(event)
            at.append(time.perf_counter())

    manager.add_sink(sink)
    return events, at


async def test_the_run_reports_itself_over_before_the_housekeeping_runs(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "the answer"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    events, at = await _settled_at(manager, state.session.id)
    stopped: list[float] = []

    async def watch(sid: str, event: TurnEvent) -> None:
        if event.type is EventType.MESSAGE_STOP:
            stopped.append(time.perf_counter())

    manager.add_sink(watch)
    # The housekeeping is held until the first announcement has been checked, instead of sleeping
    # for SLOW: a run that announced itself only after its housekeeping would then never announce
    # itself at all and fail the wait below, and the test no longer sits out both sleeps.
    housekeeping_may_finish = asyncio.Event()

    async def slow_callback(session_id: str, run_id: str, status: str) -> None:
        await housekeeping_may_finish.wait()

    manager.on_finished(slow_callback)
    snapshot_took = asyncio.Event()

    async def slow_snapshot(st: Any, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        # Only the snapshot after the turn is housekeeping; the one before it opens the run, which
        # would otherwise wait for a release that only comes once the run has ended.
        if kind == "after":
            await housekeeping_may_finish.wait()
            snapshot_took.set()
        return "sha"

    manager.checkpoint = slow_snapshot  # type: ignore[method-assign]
    await manager.submit(state.session.id, "hello")
    await until(lambda: bool(events), "the run announced itself settled")
    # The first announcement comes with the final token, not with the work that follows it. The
    # bound is half the housekeeping and not a tight one: what is being told apart is "immediately"
    # from "after two seconds of snapshotting", and a tighter bound only measures the host.
    assert (at[0] - stopped[-1]) < SLOW / 2, f"the run took {at[0] - stopped[-1]:.3f}s to say it was over"
    assert not state.running, "the session still reads as running after it announced the end of the run"
    assert events[0].payload["housekeeping"] is True and events[0].payload["status"] == "completed"
    # …and the second one when there is nothing left to save.
    assert len(events) == 1 and not snapshot_took.is_set()
    housekeeping_may_finish.set()
    await asyncio.wait_for(snapshot_took.wait(), timeout=30)
    await until(lambda: len(events) >= 2, "the second settled event arrived")
    assert events[1].payload["housekeeping"] is False
    await manager.close()


async def test_the_next_run_waits_for_the_history_and_the_snapshot(settings: Settings, db: Database) -> None:
    """The one ordering guarantee the detached housekeeping may not lose.

    A revert of the turn that just ended puts the files back as that turn left them, so the snapshot
    taken after it has to be on disk before anything is allowed to change the workspace again. The
    rest of the housekeeping — the callbacks, the event log's tidying — nobody waits for.
    """
    provider = ScriptedProvider([{"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    order: list[str] = []
    persisted = manager._persist_history

    async def watch_persist(*args: Any, **kwargs: Any) -> None:
        await persisted(*args, **kwargs)
        order.append("persist")

    async def slow_snapshot(st: Any, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        if kind == "after":
            await asyncio.sleep(SLOW)
        order.append(f"snapshot:{kind}")
        return "sha"

    # The callback is held until the end rather than sleeping out twice the snapshot's time: the
    # second run starting before it can then only mean the run never waited for it, never that a
    # loaded host happened to run the snapshot first, and close() no longer sits out the sleep.
    callback_may_finish = asyncio.Event()

    async def slow_callback(session_id: str, run_id: str, status: str) -> None:
        await callback_may_finish.wait()
        order.append("callback")

    manager._persist_history = watch_persist  # type: ignore[method-assign]
    manager.checkpoint = slow_snapshot  # type: ignore[method-assign]
    manager.on_finished(slow_callback)
    events, _ = await _settled_at(manager, state.session.id)
    await manager.submit(state.session.id, "one")
    await until(lambda: bool(events), "the first run announced itself settled")
    assert not state.running
    started = time.perf_counter()
    # Bounded because the callback is held: a run that waited for it would otherwise hang here.
    try:
        await asyncio.wait_for(manager.submit(state.session.id, "two"), timeout=30)
    except TimeoutError:
        raise AssertionError("the next run waited for a run-finished callback it does not depend on") from None
    order.append("submitted")
    # The second run waited for the first one's snapshot and not for its callback.
    assert order.index("persist") < order.index("snapshot:after") < order.index("submitted")
    assert "callback" not in order, "the next run waited for a run-finished callback it does not depend on"
    assert time.perf_counter() - started >= SLOW * 0.5
    callback_may_finish.set()
    await manager.close()


async def test_a_queued_message_still_starts_the_next_run(settings: Settings, db: Database) -> None:
    """The queue the settling run leaves behind is drained from the detached task, exactly once."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 0.4"}}, {"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    manager.config.compaction.auto_ratio = 0.0
    await manager.submit(state.session.id, "start")
    # The message is queued while the run is under way, which is the case this covers — so the test
    # waits for the run to be under way rather than for a length of time a loaded host can spend
    # before it has even started.
    await until(lambda: state.running, "the first run started")
    await manager.live.save_queues(state.session.id, [{"kind": "follow_up", "text": "and then this"}], [])
    await until(
        lambda: not state.running and state.housekeeping is not None and state.housekeeping.done(),
        "the run settled and its housekeeping finished",
    )
    history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    texts = [b.text for m in history for b in m.content_blocks if getattr(b, "text", "")]
    assert any("and then this" in t for t in texts), "the queued message never started a run"
    await manager.close()


async def test_an_undo_during_the_settling_window_lands_after_the_snapshot(settings: Settings, db: Database) -> None:
    """The one operation the settling window exists to protect used to ignore it.

    Between the two ``run_settled`` events the run is over, ``state.running`` is already False and
    the undo button in the session screen is live — while the snapshot of the turn the undo would
    undo is still being written. An undo accepted there restores the workspace to the *before* tree,
    and the housekeeping behind it then writes that reverted tree into the checkpoint labelled
    "after" the run it just undid. So the undo waits for the snapshot instead.
    """
    provider = ScriptedProvider([{"text": "the answer"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    manager.config.compaction.auto_ratio = 0.0
    order: list[str] = []
    entered = asyncio.Event()

    async def slow_snapshot(st: Any, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        if kind == "after":
            entered.set()
            await asyncio.sleep(SLOW)
            order.append("after-snapshot")
        return "sha"

    manager.checkpoint = slow_snapshot  # type: ignore[method-assign]
    events, _ = await _settled_at(manager, state.session.id)
    await manager.submit(state.session.id, "hello")
    await asyncio.wait_for(entered.wait(), timeout=30)
    # Exactly the state the app is in when the undo button is live: not running, first event out.
    assert not state.running
    assert events and events[0].payload["housekeeping"] is True
    assert state.session.id in manager.busy_sessions()
    seq = (await manager.sessions.list_transcript(state.session.id))[0].metadata["daedalus.seq"]
    await manager.revert(state.session.id, int(seq))
    order.append("revert")
    assert order == ["after-snapshot", "revert"], f"the undo overtook the snapshot it invalidates: {order}"
    await manager.close()


async def test_a_shutdown_finishes_the_snapshot_and_the_handover(settings: Settings, db: Database) -> None:
    """A restart in the settling window used to drop both halves of what a finished run still owes.

    Before the housekeeping was a task of its own it ran inside the run, which had already ended by
    the time ``close()`` looked at it. As a task it is something a shutdown can kill — so the
    shutdown gives it the moment it needs first.
    """
    provider = ScriptedProvider([{"text": "the answer"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    manager.config.compaction.auto_ratio = 0.0
    kinds: list[str] = []
    finished: list[str] = []
    entered = asyncio.Event()

    async def slow_snapshot(st: Any, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        if kind == "after":
            entered.set()
            await asyncio.sleep(SLOW)
        kinds.append(kind)
        return "sha"

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        finished.append(status)

    manager.checkpoint = slow_snapshot  # type: ignore[method-assign]
    manager.on_finished(on_finished)
    await manager.submit(state.session.id, "hello")
    await asyncio.wait_for(entered.wait(), timeout=30)
    await manager.close()
    assert "after" in kinds, "the shutdown took the snapshot of the turn that had just ended"
    assert finished == ["completed"], f"the shutdown dropped the handover to the other fronts: {finished}"


async def test_a_shutdown_mid_run_announces_nothing(settings: Settings, db: Database) -> None:
    """A parked run has not settled, so nothing says it ended.

    ``resume_unfinished()`` drives it on after the restart. Announcing the end of it would put every
    front back to idle on a turn that is about to continue, and the run-finished callbacks would
    hand on an answer that is not finished.
    """
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "never reached"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    events, _ = await _settled_at(manager, state.session.id)
    finished: list[str] = []

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        finished.append(status)

    manager.on_finished(on_finished)
    await manager.submit(state.session.id, "start")
    await until(lambda: state.running, "the run started")
    await manager.close()
    assert events == [], f"a parked run announced itself as settled: {[e.payload for e in events]}"
    assert finished == [], f"a parked run handed its unfinished answer on: {finished}"
