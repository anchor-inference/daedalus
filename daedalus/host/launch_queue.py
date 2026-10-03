"""When a staff member's assignment may start, and when it has to wait — with the reason in words.

Two limits bound how much runs at once. A project runs at most its ``concurrency`` of staff sessions
that are working (starting, working, waiting on a question or a permission, or silent). The machine
runs at most ``running_cap`` terminal sessions across every project and both environments; a
command-line staff member is a terminal session, so its launch waits for a place there as well, while
a Daedalus staff member is not a terminal and is held to its project's limit only. Launches of one
project are spaced ``stagger`` seconds apart, so a queue that frees six slots at once does not start
six processes in the same second.

The queue offers capacity to one claimed durable command at a time. Deferred work remains in the
outbox; a timer or restart cannot launch an assignment without that command's current authority.
Waiting entries carry their reason so the operator can see what prevents admission.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import aiosqlite

from daedalus.stores.control import Principal

logger = logging.getLogger(__name__)

WaitReason = Literal["dependencies", "busy", "project", "terminals", "machine", "stagger", "behind"]
"""Why an entry waits:

- ``dependencies``: the task waits for other tasks to finish;
- ``busy``: the member is still working on something else;
- ``project``: every slot of the project's concurrency is taken;
- ``terminals``: a command-line member needs the terminals service, which is not running here;
- ``machine``: the machine already runs as many terminal sessions as its cap allows;
- ``stagger``: the project launched a moment ago and spaces its launches out;
- ``behind``: another entry of the project starts first.
"""


class MachineCapacity(Protocol):
    """How many terminal sessions run on the machine, and how many it allows.

    The terminals service answers this; the launch queue asks only these three questions, so a test
    can answer them with numbers and the queue never depends on how the service is built.
    """

    async def running(self) -> int:
        """Terminal sessions running now, in every environment."""

    def cap(self) -> int:
        """The machine-wide cap on running terminal sessions."""

    def waiting(self) -> int:
        """Launches already waiting in the terminals service's own line for a place under the cap."""

    def unavailable(self, env: str) -> str | None:
        """Why no terminal can be opened in ``env`` now (its daemon is not running), or ``None``."""


class TerminalsCapacity:
    """:class:`MachineCapacity` read from the terminals service (``app.extensions["terminals"]``)."""

    def __init__(self, terminals: Any) -> None:
        self.terminals = terminals

    async def running(self) -> int:
        return int(await self.terminals.count_running())

    def cap(self) -> int:
        return int(self.terminals.config().running_cap)

    def waiting(self) -> int:
        return len(self.terminals.queue())

    def unavailable(self, env: str) -> str | None:
        for status in self.terminals.environments():
            if status.env == env and not status.available:
                return status.detail or status.reason or "not connected"
        return None


@dataclass
class Entry:
    """One assignment waiting to start."""

    project_id: str
    staff_id: str
    staff_name: str
    task_id: str
    priority: int
    """1 is the most urgent, as on the board."""
    terminal: bool
    """Whether the launch is a terminal session (a command-line member), and so counts against the machine."""
    by: str
    env: str = ""
    resume_from: str | None = None
    order: int = 0
    since: float = field(default_factory=time.time)
    reason: WaitReason | None = None
    detail: str = ""
    error: BaseException | None = None
    started: bool = False
    principal: Principal | None = None
    check_authority: Callable[[], Awaitable[None]] | None = None
    capacity_slot_id: str | None = None
    resources: dict[str, Any] | None = None

    def view(self, position: int) -> dict[str, Any]:
        return {
            "staff_id": self.staff_id,
            "task_id": self.task_id,
            "priority": self.priority,
            "position": position,
            "reason": self.reason,
            "detail": self.detail,
            "since": self.since,
            "by": self.by,
        }


@dataclass(frozen=True, slots=True)
class Admission:
    """What ``request`` did: started the assignment, or queued it at ``position`` for ``reason``."""

    state: Literal["started", "queued"]
    position: int = 0
    reason: WaitReason | None = None
    detail: str = ""


class Check(Protocol):
    async def __call__(self, entry: Entry) -> str | None:
        """``None`` when the entry may go, otherwise the reason in words."""


class LaunchQueue:
    """Every project's waiting assignments, started in priority order as the limits allow.

    The callables are the host's answers to the questions the queue asks; injected so the queue can
    be driven by a test with numbers and a clock. ``launch`` starts one assignment and is awaited
    under the queue's lock: two pumps cannot both see one free slot, because the first has claimed
    the member's session (counted as active) before the second looks.
    """

    def __init__(
        self,
        *,
        concurrency: Callable[[str], Awaitable[int]],
        active: Callable[[str], Awaitable[int]],
        ready: Check,
        free: Check,
        launch: Callable[[Entry], Awaitable[None]],
        capacity: Callable[[], MachineCapacity | None],
        stagger: Callable[[], float],
        clock: Callable[[], float] = time.monotonic,
        on_failure: Callable[[Entry, BaseException], Awaitable[None]] | None = None,
        reuses: Callable[[Entry], Awaitable[bool]] | None = None,
        check_reserved: Callable[[Entry], Awaitable[bool]] | None = None,
        active_in: Callable[[aiosqlite.Connection, str], Awaitable[int]] | None = None,
        concurrency_in: Callable[[aiosqlite.Connection, str], Awaitable[int]] | None = None,
    ) -> None:
        self._concurrency = concurrency
        self._active = active
        self._ready = ready
        self._free = free
        self._launch = launch
        self._capacity = capacity
        self._stagger = stagger
        self._clock = clock
        self._on_failure = on_failure
        self._reuses = reuses
        self._check_reserved = check_reserved
        self._active_in = active_in
        self._concurrency_in = concurrency_in
        """Whether an entry goes to its member's live session as a message rather than a launch: it
        then opens no terminal and starts no process, so neither the machine's cap nor the spacing
        of launches applies to it. Without this, a member idle at its prompt waited behind other
        members' launches for a place it would never take."""
        self._entries: dict[str, list[Entry]] = {}
        self._last_launch: dict[str, float] = {}
        self._terminal_launches = 0
        """Command-line launches under way whose terminal may not be counted by the service yet."""
        self._lock = asyncio.Lock()
        self._guard_owner: asyncio.Task[Any] | None = None
        self._order = itertools.count()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # -- reading ---------------------------------------------------------------------------------

    def entries(self, project_id: str) -> list[Entry]:
        return sorted(self._entries.get(project_id, []), key=lambda e: (e.priority, e.order))

    def queue(self, project_id: str) -> list[dict[str, Any]]:
        """The project's waiting assignments in the order they would start, each with why it waits."""
        return [e.view(i) for i, e in enumerate(self.entries(project_id), start=1)]

    def waiting_for(self, staff_id: str) -> list[dict[str, Any]]:
        """A member's waiting assignments, for the staff card."""
        out = []
        for project_id in self._entries:
            out.extend(v for v in self.queue(project_id) if v["staff_id"] == staff_id)
        return out

    def projects(self) -> list[str]:
        return [p for p, items in self._entries.items() if items]

    @asynccontextmanager
    async def admission_guard(self):
        """Hold queue admission while a caller commits a bounded pair of capacity claims."""
        async with self._lock:
            self._guard_owner = asyncio.current_task()
            try:
                yield
            finally:
                self._guard_owner = None

    async def check_pair_capacity(self, project_id: str, *, count: int = 2) -> bool:
        """Check project slots under admission_guard before both reservations are committed."""
        if self._guard_owner is not asyncio.current_task() or count != 2:
            raise ValueError("pair capacity needs the admission guard and exactly two slots")
        return await self._active(project_id) + count <= await self._concurrency(project_id)

    async def check_pair_capacity_in(self, conn: aiosqlite.Connection, project_id: str,
                                     *, count: int = 2) -> bool:
        """Check capacity on the command's connection so pair reservation cannot deadlock."""
        if self._guard_owner is not asyncio.current_task() or count != 2:
            raise ValueError("pair capacity needs the admission guard and exactly two slots")
        if self._active_in is None or self._concurrency_in is None:
            raise RuntimeError("transactional pair capacity readers are not installed")
        return await self._active_in(conn, project_id) + count <= await self._concurrency_in(conn, project_id)

    # -- changing ----------------------------------------------------------------------------------

    async def request(self, entry: Entry) -> Admission:
        """Queue an assignment and start whatever may start now; says what became of this one.

        A task already waiting is replaced, not doubled: assigning it again (to the same member or
        another) is a change of mind. A launch that fails is raised to the caller that asked for it.
        """
        entry.order = next(self._order)
        items = self._entries.setdefault(entry.project_id, [])
        items[:] = [e for e in items if (e.task_id, e.capacity_slot_id) !=
                    (entry.task_id, entry.capacity_slot_id)]
        items.append(entry)
        await self.pump(entry.project_id)
        if entry.error is not None:
            raise entry.error
        if entry.started:
            return Admission("started")
        position = next((i for i, e in enumerate(self.entries(entry.project_id), start=1) if e is entry), 0)
        return Admission("queued", position, entry.reason, entry.detail)

    async def offer(self, entry: Entry) -> Admission:
        """Attempt admission; durable commands retain waiting work outside this memory queue."""
        try:
            return await self.request(entry)
        finally:
            # A pending outbox command is retried only after another capacity observation. Leaving
            # its entry here would let an unclaimed command start from a later timer callback.
            items = self._entries.get(entry.project_id, [])
            items[:] = [queued for queued in items if queued is not entry]

    def add(self, entry: Entry) -> None:
        """Queue an assignment without starting anything now: what a restart restores from the board."""
        entry.order = next(self._order)
        items = self._entries.setdefault(entry.project_id, [])
        items[:] = [e for e in items if (e.task_id, e.capacity_slot_id) !=
                    (entry.task_id, entry.capacity_slot_id)]
        items.append(entry)

    def withdraw(self, project_id: str, *, task_id: str | None = None, staff_id: str | None = None) -> int:
        """Take entries off the queue: a task no longer assigned, or a member dismissed. Returns how many."""
        items = self._entries.get(project_id, [])
        keep = [e for e in items if not ((task_id is not None and e.task_id == task_id) or (staff_id is not None and e.staff_id == staff_id))]
        self._entries[project_id] = keep
        return len(items) - len(keep)

    def pump_soon(self, project_id: str | None = None) -> None:
        """Re-evaluate from synchronous code or an event handler, without waiting for the outcome."""
        task = asyncio.get_running_loop().create_task(self.pump(project_id), name=f"launch-queue:{project_id or 'all'}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def pump(self, project_id: str | None = None) -> None:
        """Start what may start, in priority order, and write down why the rest waits."""
        async with self._lock:
            for pid in [project_id] if project_id is not None else self.projects():
                try:
                    await self._pump(pid)
                except Exception:  # noqa: BLE001 — one project's trouble must not stall the others' queues
                    logger.exception("launch queue of project %s failed", pid)

    async def _pump(self, project_id: str) -> None:
        concurrency = await self._concurrency(project_id)
        active = await self._active(project_id)
        stagger = max(0.0, float(self._stagger()))
        launched_here = False
        for entry in self.entries(project_id):
            entry.reason, entry.detail = None, ""
        for entry in self.entries(project_id):
            waits = await self._ready(entry)
            if waits is not None:
                self._wait(entry, "dependencies", waits)
                continue
            waits = await self._free(entry)
            if waits is not None:
                self._wait(entry, "busy", waits)
                continue
            reserved = entry.capacity_slot_id is not None
            if reserved and (self._check_reserved is None or not await self._check_reserved(entry)):
                self._entries.get(project_id, []).remove(entry)
                entry.error = ValueError("the comparison capacity reservation is no longer current")
                continue
            if active - int(reserved) >= concurrency:
                self._wait(entry, "project", f"{active} of the project's {concurrency} staff slots are working")
                continue
            reuse = entry.principal is None and self._reuses is not None and await self._reuses(entry)
            if entry.terminal and not reuse:
                machine = await self._machine(entry.env)
                if machine is not None:
                    self._wait(entry, *machine)
                    continue
            since = self._clock() - self._last_launch.get(project_id, float("-inf"))
            if not reuse and (launched_here or since < stagger):
                delay = stagger if launched_here else stagger - since
                self._wait(entry, "stagger", f"starts in about {max(1, round(delay))} s; launches of a project are spaced {stagger:g} s apart")
                self._wake_in(project_id, delay)
                break
            await self._start(entry, reuse=reuse)
            launched_here = launched_here or (entry.started and not reuse)
            if entry.started and not reserved:
                active += 1
        # Whatever the loop did not reach waits behind the entry that stopped it.
        for entry in self.entries(project_id):
            if entry.reason is None:
                self._wait(entry, "behind", "another assignment of the project starts first")

    async def _machine(self, env: str) -> tuple[WaitReason, str] | None:
        capacity = self._capacity()
        if capacity is None:
            return "terminals", "the terminals service is not running here; command-line staff start once it is"
        down = capacity.unavailable(env)
        if down is not None:
            return "terminals", f"no terminal can be opened {'on the host' if env == 'host' else 'in the container'} yet ({down}); command-line staff start once it can"
        running = await capacity.running()
        cap = capacity.cap()
        # The service's own line is counted too: those launches were promised places before this one.
        taken = running + self._terminal_launches + capacity.waiting()
        if taken >= cap:
            return "machine", f"{running} of the machine's {cap} terminal sessions are running"
        return None

    def _wait(self, entry: Entry, reason: WaitReason, detail: str) -> None:
        entry.reason, entry.detail = reason, detail

    async def _start(self, entry: Entry, *, reuse: bool = False) -> None:
        items = self._entries.get(entry.project_id, [])
        if entry in items:
            items.remove(entry)
        launches = entry.terminal and not reuse
        if not reuse:
            self._last_launch[entry.project_id] = self._clock()
        if launches:
            self._terminal_launches += 1
        try:
            await self._launch(entry)
            entry.started = True
            entry.reason, entry.detail = None, ""
        except Exception as exc:  # noqa: BLE001 — the failure is the requester's to hear, not the queue's to die of
            entry.error = exc
            logger.warning("launch of %s for task %s failed: %s", entry.staff_name, entry.task_id, exc)
            if self._on_failure is not None:
                try:
                    await self._on_failure(entry, exc)
                except Exception:  # noqa: BLE001
                    logger.exception("reporting a failed launch failed")
        finally:
            if launches:
                self._terminal_launches -= 1

    def _wake_in(self, project_id: str, delay: float) -> None:
        if project_id in self._timers:
            return
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._timers.pop(project_id, None)
            self.pump_soon(project_id)

        self._timers[project_id] = loop.call_later(max(0.05, delay), fire)

    def close(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for task in list(self._tasks):
            task.cancel()


__all__ = ["Admission", "Entry", "LaunchQueue", "MachineCapacity", "TerminalsCapacity", "WaitReason"]
