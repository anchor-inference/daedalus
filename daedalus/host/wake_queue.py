"""Waking a sleeping agent with batches of bus events: what an orchestrator is woken by.

An agent that runs a team sleeps between turns. The bus tells it what happened, but a turn per event
would be a turn per keystroke of its team, so events are gathered: routine ones wait for a window
after the first of them, the ones that cannot wait go at once, and events about the same thing
replace each other (the latest status of a member, the latest move of a task). Each delivered batch
is one message, and the cursor of the last delivered event is stored, so a restart delivers what was
missed once and nothing twice.

The queue knows nothing of projects or staff. Its owner gives it a filter, a ``classify`` that says
which events are worth waking for and which cannot wait, a ``render`` that turns a batch into text,
and a ``deliver`` that hands the text over. The same class serves any agent woken that way.

Time is injected (``clock``) and the driving loop only sleeps until the next thing is due, so a test
steps it with :meth:`WakeQueue.pump` and a fake clock instead of waiting for real seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from daedalus.host.events import GAP, AppEvent, EventBus, EventFilter

logger = logging.getLogger(__name__)

TargetState = Literal["idle", "running", "waiting", "gone"]
"""What the agent is doing: ``idle`` takes a new turn, ``running`` takes a steer, ``waiting`` (a
question of its own is pending) takes nothing, ``gone`` has no session to deliver to."""

URGENT_DELAY_SECONDS = 1.0
"""An urgent event waits this long. Enough for the burst it arrived in (a question and the status that
announces it) to join the same batch, and for the request row it names to be written; not enough to
be noticed."""
BACKOFF_FIRST_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
STUCK_BACKOFF_FIRST_SECONDS = 60.0
STUCK_BACKOFF_MAX_SECONDS = 1800.0
"""A refusal that will not go away by itself (a folder that is not mounted, no model configured) is
tried again rarely: someone has to act first, and every try is a line in the log. The owner pokes the
queue when something it can see changes."""
HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class Wake:
    """An event worth waking for. ``key`` coalesces: a later wake with the same key replaces the earlier."""

    key: str
    urgent: bool = False


@dataclass(slots=True)
class Pending:
    event: AppEvent
    wake: Wake
    arrived: float


@dataclass(frozen=True, slots=True)
class Batch:
    """What one delivery carries: the events in arrival order and how many more there were."""

    items: tuple[Pending, ...]
    urgent: bool

    @property
    def events(self) -> tuple[AppEvent, ...]:
        return tuple(p.event for p in self.items)


@dataclass(slots=True)
class WakeStats:
    delivered: int = 0
    steered: int = 0
    failures: int = 0
    capped_hours: int = 0
    wakes: deque[float] = field(default_factory=deque)
    """When each wake that started a turn was delivered, for the hourly cap."""


class WakeQueue:
    """One agent's wake-ups: gathered from the bus, delivered as batches, resumable from a cursor."""

    def __init__(
        self,
        *,
        name: str,
        bus: EventBus,
        flt: EventFilter,
        classify: Callable[[AppEvent], Awaitable[Wake | None]],
        render: Callable[[Batch], Awaitable[str]],
        state: Callable[[], Awaitable[TargetState]],
        deliver: Callable[[str, bool], Awaitable[None]],
        load_cursor: Callable[[], Awaitable[int | None]],
        save_cursor: Callable[[int], Awaitable[None]],
        batch_seconds: Callable[[], float],
        max_wakes_per_hour: Callable[[], int],
        on_capped: Callable[[float], Awaitable[None]] | None = None,
        lasting: Callable[[BaseException], bool] | None = None,
        on_stuck: Callable[[str], Awaitable[None]] | None = None,
        on_unstuck: Callable[[], Awaitable[None]] | None = None,
        obsolete: Callable[[Batch], Awaitable[bool]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        urgent_delay: float = URGENT_DELAY_SECONDS,
    ) -> None:
        self.name = name
        self.bus = bus
        self.filter = flt
        self._classify = classify
        self._render = render
        self._state = state
        self._deliver = deliver
        self._load_cursor = load_cursor
        self._save_cursor = save_cursor
        self._batch_seconds = batch_seconds
        self._max_wakes = max_wakes_per_hour
        self._on_capped = on_capped
        self._lasting = lasting
        self._on_stuck = on_stuck
        self._on_unstuck = on_unstuck
        self._obsolete = obsolete
        self.stuck = ""
        """Why deliveries are refused for a reason that will not fix itself; empty while they are not.
        The owner hears of every such refusal (``on_stuck``) and decides what is news; it hears once
        more when a delivery goes through again (``on_unstuck``)."""
        self.clock = clock
        self.urgent_delay = urgent_delay
        self.items: dict[str, Pending] = {}
        self.stats = WakeStats()
        self.last_seen = 0
        """The newest ``seq`` this queue has looked at, worth waking for or not."""
        self.cursor = 0
        """The newest ``seq`` everything up to which is delivered or not worth delivering."""
        self._held_for_run = False
        """Routine events wait for the end of the current turn: a steer is for what cannot wait."""
        self._backoff_until = 0.0
        self._backoff = 0.0
        self._capped_told_at = -HOUR
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self.closed = False

    # -- lifetime --------------------------------------------------------------------------------

    async def start(self) -> None:
        """Resume from the stored cursor (every missed event is read again, once) and follow the bus.

        A queue with no cursor starts at the bus's head: an agent switched on now is not woken by
        what happened to its project before it existed.
        """
        stored = await self._load_cursor()
        if stored is None:
            stored = self.bus.head
            await self._save_cursor(stored)
        self.cursor = self.last_seen = stored
        subscription = self.bus.subscribe(self.filter, after=stored, name=f"wake:{self.name}")

        async def follow() -> None:
            async with subscription:
                async for event in subscription:
                    if event.type == GAP:
                        continue  # retention passed the cursor: what is gone cannot be delivered
                    try:
                        await self.offer(event)
                    except Exception:  # noqa: BLE001 — one event that cannot be read must not end the queue
                        logger.exception("wake queue %s could not take %s", self.name, event.type)

        self._tasks = [asyncio.create_task(follow(), name=f"wake:{self.name}:follow"), asyncio.create_task(self._drive(), name=f"wake:{self.name}:drive")]

    async def close(self) -> None:
        self.closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if not self.items and self.last_seen > self.cursor:
            # Nothing waits: what was looked at need not be looked at again after a restart.
            self.cursor = self.last_seen
            await self._save_cursor(self.cursor)

    async def _drive(self) -> None:
        while not self.closed:
            # Cleared before the question, so an event that arrives while it is being answered still
            # ends the wait below instead of being noticed only at the next timeout.
            self._changed.clear()
            due = self.due_at()
            now = self.clock()
            if due is None or due > now:
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=None if due is None else due - now)
                except TimeoutError:
                    pass
                continue
            try:
                await self.pump()
            except Exception:  # noqa: BLE001
                logger.exception("wake queue %s failed to deliver", self.name)
                self._back_off()
            await asyncio.sleep(0)

    def poke(self) -> None:
        """The agent's turn ended, or whatever kept a batch back may have changed: try again now."""
        self._held_for_run = False
        self._backoff_until = 0.0
        self._changed.set()

    # -- taking events -------------------------------------------------------------------------

    async def offer(self, event: AppEvent) -> bool:
        """Take one event from the bus; whether it is worth waking for."""
        wake = await self._classify(event)
        if event.seq:
            # Classification can await storage while a due batch is being consumed. Until it has
            # decided, this event must not extend the cursor past a wake that has not been queued.
            self.last_seen = max(self.last_seen, event.seq)
        if wake is None:
            if not self.items and event.seq > self.cursor:
                self.cursor = event.seq  # nothing waits before it, so nothing before it is owed
            return False
        now = self.clock()
        previous = self.items.pop(wake.key, None)
        # The latest word about a thing replaces the earlier one, and it keeps its urgency: a routine
        # status that follows an error of the same member must not demote the error.
        urgent = wake.urgent or (previous is not None and previous.wake.urgent)
        self.items[wake.key] = Pending(event, Wake(wake.key, urgent), previous.arrived if previous is not None else now)
        self._changed.set()
        return True

    # -- when ----------------------------------------------------------------------------------

    def _urgent(self) -> list[Pending]:
        return [p for p in self.items.values() if p.wake.urgent]

    def capped_until(self, now: float) -> float | None:
        """While the hourly cap is reached, when the oldest wake of the hour leaves the window."""
        wakes = self.stats.wakes
        while wakes and wakes[0] <= now - HOUR:
            wakes.popleft()
        if len(wakes) < max(1, self._max_wakes()):
            return None
        return wakes[0] + HOUR

    def due_at(self) -> float | None:
        """When the next delivery should be tried; ``None`` while there is nothing to try.

        While the hourly cap holds routine events, the first try is still made when the window closes,
        so the operator hears once that the agent is being held back; after that the next try is when
        the cap frees.
        """
        if not self.items:
            return None
        urgent = self._urgent()
        if urgent:
            due = min(p.arrived for p in urgent) + self.urgent_delay
        else:
            if self._held_for_run:
                return None
            due = min(p.arrived for p in self.items.values()) + self._batch_seconds()
            capped = self.capped_until(self.clock())
            if capped is not None and self._told_capped():
                due = max(due, capped)
        return max(due, self._backoff_until)

    def _told_capped(self) -> bool:
        return self._on_capped is None or self.clock() - self._capped_told_at < HOUR

    # -- delivering ------------------------------------------------------------------------------

    async def pump(self) -> bool:
        """Deliver what is due, if the agent can take it now. Whether anything was delivered."""
        async with self._lock:
            now = self.clock()
            due = self.due_at()
            if due is None or due > now:
                return False
            if self._obsolete is not None:
                batch = Batch(tuple(self.items.values()), urgent=bool(self._urgent()))
                if await self._obsolete(batch):
                    # A later state change can finish the work while its routine news is batched.
                    # Consume that news without spending a turn; events offered meanwhile remain.
                    for pending in batch.items:
                        if self.items.get(pending.wake.key) is pending:
                            del self.items[pending.wake.key]
                    await self._clear_delivery_hold()
                    await self._advance_cursor(batch)
                    return False
            state = await self._state()
            if state in ("gone", "waiting"):
                # Nobody to deliver to, or a question of its own is open and anything submitted now
                # would be taken as its answer. The events stay; the owner pokes when that changes.
                self._back_off()
                return False
            urgent = self._urgent()
            if state == "running":
                if not urgent:
                    self._held_for_run = True
                    return False
                return await self._send(tuple(urgent), steer=True)
            capped = self.capped_until(now)
            if capped is not None and not urgent:
                await self._tell_capped(capped)
                return False
            return await self._send(tuple(self.items.values()), steer=False)

    async def _send(self, items: tuple[Pending, ...], *, steer: bool) -> bool:
        batch = Batch(tuple(sorted(items, key=lambda p: (p.event.seq or 0, p.arrived))), urgent=any(p.wake.urgent for p in items))
        text = await self._render(batch)
        try:
            await self._deliver(text, steer)
        except Exception as exc:  # noqa: BLE001 — the events stay and are tried again later
            self.stats.failures += 1
            if self._lasting is not None and self._lasting(exc):
                await self._got_stuck(str(exc) or type(exc).__name__, len(items))
                return False
            logger.warning("wake queue %s could not deliver %d events: %s", self.name, len(items), exc)
            self._back_off()
            return False
        await self._clear_delivery_hold(clear_run_hold=not steer)
        for pending in items:
            if self.items.get(pending.wake.key) is pending:
                del self.items[pending.wake.key]
        if steer:
            self.stats.steered += 1
        else:
            self.stats.delivered += 1
            self.stats.wakes.append(self.clock())
        await self._advance_cursor(batch)
        return True

    async def _clear_delivery_hold(self, *, clear_run_hold: bool = True) -> None:
        self._backoff = 0.0
        self._backoff_until = 0.0
        if clear_run_hold:
            self._held_for_run = False
        if self.stuck:
            self.stuck = ""
            logger.info("wake queue %s can proceed again", self.name)
            if self._on_unstuck is not None:
                try:
                    await self._on_unstuck()
                except Exception:  # noqa: BLE001 — the delivery stands; the all-clear is a courtesy
                    logger.warning("wake queue %s could not say it delivers again", self.name, exc_info=True)

    async def _advance_cursor(self, batch: Batch) -> None:
        """Up to the newest delivered event, but never past one still waiting: a held routine event
        older than a steered urgent one must be read again after a restart, not skipped."""
        delivered = max((p.event.seq for p in batch.items if p.event.seq), default=self.cursor)
        waiting = [p.event.seq for p in self.items.values() if p.event.seq]
        cursor = min(waiting) - 1 if waiting else max(delivered, self.last_seen)
        if cursor > self.cursor:
            self.cursor = cursor
            await self._save_cursor(cursor)

    def _back_off(self, *, first: float = BACKOFF_FIRST_SECONDS, most: float = BACKOFF_MAX_SECONDS) -> None:
        self._backoff = first if not self._backoff else min(most, max(first, self._backoff * 2))
        self._backoff_until = self.clock() + self._backoff

    async def _got_stuck(self, reason: str, count: int) -> None:
        """A refusal that will not fix itself: logged once per reason, handed to the owner, tried again rarely.

        Repeating it every few seconds told nobody anything — the operator saw an empty chat and a
        dispatch "in progress" while the log filled with the same line. The owner is handed every
        such refusal, not only the first, because what it has to act on can be new (work that arrived
        while the agent was stuck); saying the same thing twice is its to avoid."""
        self._back_off(first=STUCK_BACKOFF_FIRST_SECONDS, most=STUCK_BACKOFF_MAX_SECONDS)
        if reason == self.stuck:
            logger.debug("wake queue %s still cannot deliver: %s", self.name, reason)
        else:
            self.stuck = reason
            logger.warning("wake queue %s cannot deliver %d events until this changes: %s", self.name, count, reason)
        if self._on_stuck is None:
            return
        try:
            await self._on_stuck(reason)
        except Exception:  # noqa: BLE001 — the events stay either way
            logger.warning("wake queue %s could not say why it is stuck", self.name, exc_info=True)

    async def _tell_capped(self, until: float) -> None:
        now = self.clock()
        if self._told_capped() or self._on_capped is None:
            return
        self._capped_told_at = now
        self.stats.capped_hours += 1
        try:
            await self._on_capped(until - now)
        except Exception:  # noqa: BLE001
            logger.warning("wake queue %s could not say it is capped", self.name, exc_info=True)


__all__ = ["Batch", "Pending", "TargetState", "Wake", "WakeQueue"]
