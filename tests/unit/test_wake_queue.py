"""The wake queue on a real bus with a fake clock: windows, urgency, coalescing, runs, the hourly cap and restarts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from daedalus.host.events import AppEvent, EventBus, EventFilter
from daedalus.host.wake_queue import Batch, TargetState, Wake, WakeQueue
from daedalus.stores.database import Database
from tests.support.waiting import until


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Agent:
    """What the queue delivers to: a state it reports and the batches it received."""

    def __init__(self) -> None:
        self.state: TargetState = "idle"
        self.turns: list[str] = []
        self.steers: list[str] = []
        self.refuse: str | None = None
        self.capped: list[float] = []
        self.cursor: int | None = None

    async def deliver(self, text: str, steer: bool) -> None:
        if self.refuse:
            raise RuntimeError(self.refuse)
        (self.steers if steer else self.turns).append(text)

    async def get_state(self) -> TargetState:
        return self.state

    async def load(self) -> int | None:
        return self.cursor

    async def save(self, seq: int) -> None:
        self.cursor = seq

    async def on_capped(self, wait: float) -> None:
        self.capped.append(wait)


async def classify(event: AppEvent) -> Wake | None:
    """Staff statuses coalesce per member; errors and questions cannot wait; anything else is ignored."""
    p = event.payload
    if event.type == "staff.status":
        return Wake(f"staff:{event.staff_id}", urgent=p.get("status") == "error")
    if event.type == "ask.pending":
        return Wake(f"ask:{p['request_id']}", urgent=True)
    return None


async def render(batch: Batch) -> str:
    return "\n".join(f"{e.type}:{e.staff_id or ''}:{e.payload.get('status') or e.payload.get('request_id')}" for e in batch.events)


@pytest.fixture
async def bus(db: Database) -> AsyncIterator[EventBus]:
    bus = EventBus(db)
    await bus.start()
    yield bus
    await bus.close()


def make(bus: EventBus, agent: Agent, clock: Clock, *, per_hour: int = 30, obsolete: Any = None, classify_event: Any = classify) -> WakeQueue:
    return WakeQueue(
        name="test",
        bus=bus,
        flt=EventFilter(types=("staff.status", "ask.pending"), project_id="p1"),
        classify=classify_event,
        render=render,
        state=agent.get_state,
        deliver=agent.deliver,
        load_cursor=agent.load,
        save_cursor=agent.save,
        batch_seconds=lambda: 20,
        max_wakes_per_hour=lambda: per_hour,
        on_capped=agent.on_capped,
        obsolete=obsolete,
        clock=clock,
    )


async def status(bus: EventBus, staff: str, value: str) -> AppEvent:
    return await bus.publish("staff.status", {"status": value, "previous": None}, project_id="p1", staff_id=staff)


async def question(bus: EventBus, request_id: str) -> AppEvent:
    payload: dict[str, Any] = {"request_id": request_id, "request_ref": f"staff:x:{request_id}", "run_id": "", "title": "t", "questions": [], "operator_facing": False, "telegram": False}
    return await bus.publish("ask.pending", payload, project_id="p1", staff_id="s1")


async def test_routine_events_wait_for_the_window_and_coalesce_per_member(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    queue = make(bus, agent, clock)
    for event in (await status(bus, "ira", "turn_done_unseen"), await status(bus, "max", "exited"), await status(bus, "ira", "no_signal")):
        await queue.offer(event)
    clock.advance(19)
    assert not await queue.pump() and agent.turns == []
    clock.advance(1)
    assert await queue.pump()
    [turn] = agent.turns
    # One line per member, the latest word about each, in the order the events happened.
    assert turn.splitlines() == ["staff.status:max:exited", "staff.status:ira:no_signal"]
    assert not queue.items and not await queue.pump()


@pytest.mark.parametrize("same_key", [False, True])
async def test_obsolete_batch_keeps_new_event_offered_during_check(bus: EventBus, same_key: bool) -> None:
    agent, clock = Agent(), Clock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def obsolete(batch: Batch) -> bool:
        if batch.events[0].payload.get("status") != "exited":
            return False
        entered.set()
        await release.wait()
        return True

    queue = make(bus, agent, clock, obsolete=obsolete)
    old = await status(bus, "ira", "exited")
    await queue.offer(old)
    queue.stuck = "old refusal"
    queue._backoff_until = clock.now + 20
    clock.advance(20)
    pumping = asyncio.create_task(queue.pump())
    await entered.wait()
    queue._held_for_run = True
    fresh = await status(bus, "ira", "error") if same_key else await question(bus, "new")
    await queue.offer(fresh)
    release.set()
    assert not await pumping
    assert list(queue.items.values())[0].event is fresh
    assert queue.cursor < fresh.seq and agent.cursor is not None and agent.cursor < fresh.seq
    assert queue.stuck == "" and queue._backoff_until == 0 and not queue._held_for_run
    clock.advance(1)
    assert await queue.pump()
    assert len(agent.turns) == 1 and agent.cursor == fresh.seq


async def test_obsolete_batch_leaves_no_timer_or_stuck_state(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()

    async def obsolete(_batch: Batch) -> bool:
        queue._held_for_run = True
        return True

    queue = make(bus, agent, clock, obsolete=obsolete)
    await queue.offer(await status(bus, "ira", "exited"))
    queue.stuck = "old refusal"
    queue._backoff_until = clock.now + 20
    clock.advance(20)
    assert not await queue.pump()
    assert not queue.items and queue.due_at() is None
    assert queue.stuck == "" and queue._backoff_until == 0 and not queue._held_for_run
    assert agent.turns == [] and queue.stats.delivered == 0


async def test_obsolete_batch_does_not_advance_past_event_still_being_classified(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    checking, finish_check = asyncio.Event(), asyncio.Event()
    classifying, finish_classifying = asyncio.Event(), asyncio.Event()

    async def obsolete(batch: Batch) -> bool:
        if batch.events[0].payload.get("status") != "exited":
            return False
        checking.set()
        await finish_check.wait()
        return True

    async def classify_event(event: AppEvent) -> Wake | None:
        if event.payload.get("status") == "error":
            classifying.set()
            await finish_classifying.wait()
        return await classify(event)

    queue = make(bus, agent, clock, obsolete=obsolete, classify_event=classify_event)
    await queue.offer(await status(bus, "ira", "exited"))
    clock.advance(20)
    pumping = asyncio.create_task(queue.pump())
    await checking.wait()
    fresh = await status(bus, "ira", "error")
    offering = asyncio.create_task(queue.offer(fresh))
    await classifying.wait()
    finish_check.set()
    assert not await pumping
    assert queue.cursor < fresh.seq and agent.cursor is not None and agent.cursor < fresh.seq
    finish_classifying.set()
    assert await offering
    assert list(queue.items.values())[0].event is fresh
    clock.advance(1)
    assert await queue.pump()
    assert agent.cursor == fresh.seq


async def test_an_urgent_event_goes_at_once_and_takes_the_waiting_ones_with_it(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    queue = make(bus, agent, clock)
    await queue.offer(await status(bus, "ira", "turn_done_unseen"))
    clock.advance(2)
    await queue.offer(await question(bus, "q1"))
    assert not await queue.pump(), "the urgent one waits a moment for its burst"
    clock.advance(queue.urgent_delay)
    assert await queue.pump()
    assert agent.turns == ["staff.status:ira:turn_done_unseen\nask.pending:s1:q1"]
    # A routine status that follows an error of the same member keeps it urgent.
    await queue.offer(await status(bus, "max", "error"))
    await queue.offer(await status(bus, "max", "exited"))
    clock.advance(queue.urgent_delay)
    assert await queue.pump() and agent.turns[-1] == "staff.status:max:exited"


async def test_while_the_agent_runs_urgent_ones_steer_and_routine_ones_wait_for_the_end(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    agent.state = "running"
    queue = make(bus, agent, clock)
    routine = await status(bus, "ira", "turn_done_unseen")
    await queue.offer(routine)
    await queue.offer(await question(bus, "q1"))
    clock.advance(1)
    assert await queue.pump()
    assert agent.steers == ["ask.pending:s1:q1"] and agent.turns == []
    # The cursor stops before the routine event still waiting, so a restart now would deliver it.
    assert queue.cursor == routine.seq - 1
    clock.advance(30)
    assert not await queue.pump(), "routine events wait for the end of the turn"
    assert queue.due_at() is None
    agent.state = "idle"
    queue.poke()
    assert await queue.pump()
    assert agent.turns == ["staff.status:ira:turn_done_unseen"] and queue.cursor == agent.cursor == bus.head


async def test_a_pending_question_or_a_missing_session_holds_everything(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    queue = make(bus, agent, clock)
    await queue.offer(await question(bus, "q1"))
    for state in ("waiting", "gone"):
        agent.state = state  # type: ignore[assignment]
        clock.advance(400)
        assert not await queue.pump()
    assert agent.turns == agent.steers == [] and queue.items
    agent.state = "idle"
    queue.poke()
    assert await queue.pump() and agent.turns == ["ask.pending:s1:q1"]


async def test_a_refused_delivery_keeps_the_events_and_backs_off(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    queue = make(bus, agent, clock)
    agent.refuse = "daily budget exceeded"
    await queue.offer(await question(bus, "q1"))
    clock.advance(1)
    assert not await queue.pump() and queue.stats.failures == 1
    first = queue.due_at()
    assert first is not None and first > clock.now
    agent.refuse = None
    clock.advance(4)
    assert not await queue.pump(), "still backing off"
    clock.advance(1)
    assert await queue.pump() and agent.turns == ["ask.pending:s1:q1"]


async def test_past_the_hourly_cap_routine_batches_wait_and_urgent_ones_still_go(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    queue = make(bus, agent, clock, per_hour=2)
    for member in ("a", "b"):
        await queue.offer(await status(bus, member, "turn_done_unseen"))
        clock.advance(20)
        assert await queue.pump()
    await queue.offer(await status(bus, "c", "turn_done_unseen"))
    clock.advance(20)
    assert not await queue.pump() and len(agent.turns) == 2
    assert len(agent.capped) == 1 and 3000 < agent.capped[0] <= 3600, "the operator is told once, with how long it waits"
    clock.advance(60)
    assert not await queue.pump() and len(agent.capped) == 1
    await queue.offer(await question(bus, "q9"))
    clock.advance(1)
    assert await queue.pump(), "a question is never held by the cap"
    assert agent.turns[-1].endswith("ask.pending:s1:q9")
    # An hour after the first wake the window has room again.
    clock.now = 1000 + 20 + 3600
    await queue.offer(await status(bus, "d", "turn_done_unseen"))
    clock.advance(20)
    assert await queue.pump()


async def test_a_restart_delivers_what_was_missed_once_and_nothing_twice(db: Database, bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    first = make(bus, agent, clock)
    await first.start()
    assert agent.cursor == bus.head, "a new queue starts from now"
    await status(bus, "ira", "turn_done_unseen")
    await until(lambda: first.items, "the event reached the queue")
    await first.close()  # the host stops before the window closes
    assert agent.turns == []
    await status(bus, "max", "exited")  # and this happens while it is down

    second = make(bus, agent, clock)
    await second.start()
    await until(lambda: len(second.items) == 2, "both missed events were read again")
    clock.advance(20)
    assert await second.pump()
    assert agent.turns == ["staff.status:ira:turn_done_unseen\nstaff.status:max:exited"]
    await second.close()

    third = make(bus, agent, clock)
    await third.start()
    await bus.publish("session.status", {"status": "idle"}, project_id="p1")
    await status(bus, "lev", "idle")
    await until(lambda: third.items, "the new event arrived")
    assert list(third.items) == ["staff:lev"], "nothing already delivered came back"
    await third.close()


async def test_events_not_worth_waking_for_move_the_cursor_when_nothing_waits(bus: EventBus) -> None:
    agent, clock = Agent(), Clock()
    queue = make(bus, agent, clock)
    ignored = await bus.publish("staff.report", {"kind": "checkpoint", "text": "x"}, project_id="p1")
    assert not await queue.offer(ignored)
    assert queue.cursor == ignored.seq
