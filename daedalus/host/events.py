"""The host's event bus: one in-process publish/subscribe with a persisted, resumable ring.

Everything that happens to a session, a terminal, a staff member or a notification is published
here once, and everything that wants to know — the app's stream, the notification router, an
orchestrator's wake queue, the desktop launcher — subscribes here rather than polling a table.

What the bus promises:

- **Order.** A persisted event is numbered by ``seq`` (``AUTOINCREMENT``, so a number is never
  handed out twice, not after a prune and not after a restart), and every subscriber receives the
  persisted events in ``seq`` order. An ephemeral event has ``seq == 0`` and arrives in publish
  order among the live ones.
- **Durability before delivery.** A persisted event is in ``app_events`` before any subscriber sees
  it, so whatever a subscriber has seen it can replay after a restart with ``after=<its cursor>``.
- **A publisher never waits for a subscriber.** A subscriber that falls behind its bounded buffer
  stops receiving live events, reads what it missed from the table, and rejoins; only ephemeral
  events can be lost that way. A cursor older than retention yields one ``bus.gap`` item first.
- **One contract.** Every event type is registered below with a ``TypedDict`` for its payload, and
  ``publish`` refuses an unknown type, a missing required key, or a value JSON cannot carry. Extra
  keys pass, so an owner can add optional keys to its types; nobody renames or removes one.

This module deliberately does not use ``from __future__ import annotations``: with postponed
annotations a ``TypedDict`` sees ``NotRequired[...]`` as a string and reports every key as required,
which would make ``publish`` refuse payloads that are perfectly valid.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NotRequired, TypedDict

import aiosqlite

from daedalus.security import redact
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)


# -- payloads ------------------------------------------------------------------------------------
#
# One ``TypedDict`` per event type. The ids an event concerns (project, session, staff member,
# terminal) are not in the payload: they are columns, so a filter can select on them in SQL.


class RunStarted(TypedDict):
    run_id: str
    origin: str
    title: str


class RunFinished(TypedDict):
    run_id: str
    status: Literal["completed", "failed", "cancelled"]
    duration_s: float
    watched: bool
    """Whether some client was attending the session when the run ended."""
    origin: str
    operator_facing: bool
    telegram: bool
    """Whether the answer was delivered to a Telegram topic."""
    title: str
    summary: NotRequired[str]
    error: NotRequired[str]


class SessionStatus(TypedDict):
    status: Literal["idle", "running", "waiting", "compacting", "failed"]
    run_id: NotRequired[str]


class SessionUnreadResult(TypedDict):
    unread: bool
    run_id: NotRequired[str]


class AskOption(TypedDict):
    label: str
    description: str


class AskQuestion(TypedDict):
    question: str
    options: list[AskOption]
    multi: bool
    custom: bool


class AskPending(TypedDict):
    request_id: str
    request_ref: str
    """``<kind>:<scope>:<id>``, e.g. ``ask:<session>:<tool call id>``; the prefix selects the resolver."""
    run_id: str
    title: str
    questions: list[AskQuestion]
    operator_facing: bool
    telegram: bool


class AskRouted(TypedDict):
    """A request handed to someone else to answer (the orchestrator escalating to the operator)."""

    request_id: str
    request_ref: str
    routed_to: str


class AskAnswered(TypedDict):
    request_id: str
    request_ref: str
    via: str
    """app · telegram · notification · push · timeout · cli · orchestrator"""


class AskBatch(TypedDict):
    batch_id: str
    ask_ids: list[str]
    """The requests of one project the operator answered together, each already resolved and each
    announced by its own ``ask.answered`` / ``permission.resolved``. This event is what wakes the
    project's orchestrator, once for the whole batch instead of once per answer."""
    by: str
    via: str


class PermissionPending(TypedDict):
    request_id: str
    request_ref: str
    kind: str
    """policy · harness · …"""
    title: str
    tool: str
    text: str
    risk: Literal["routine", "elevated"]
    quick: bool
    """Whether it may be answered from a notification without opening the app."""
    telegram: bool
    expires_at: NotRequired[str]


class PermissionResolved(TypedDict):
    request_id: str
    request_ref: str
    decision: Literal["allow", "deny", "expired"]
    via: str
    by: NotRequired[str]


class TerminalCreated(TypedDict):
    env: str
    owner_kind: str
    """session · staff · project · free"""
    owner_id: str | None
    title: str
    cwd: str
    profile: str
    """``shell`` or ``harness:<name>``"""
    sandbox: bool


class TerminalExited(TypedDict):
    exit_code: int | None
    signal: NotRequired[str]
    lost: NotRequired[bool]
    """The terminal's daemon went away with it, so there is no exit status to report."""


class TerminalTitle(TypedDict):
    title: str


class TerminalCwd(TypedDict):
    cwd: str


class TerminalCommand(TypedDict):
    exit_code: int | None
    command: NotRequired[str]
    duration_ms: NotRequired[int]
    mark_seq: NotRequired[int]


class TerminalBell(TypedDict):
    pass


class TerminalNotify(TypedDict):
    title: str
    body: str


class TerminalProgress(TypedDict):
    state: str
    percent: NotRequired[int]


class BrowserOpened(TypedDict):
    """An agent's browser group opened (or came back after its browser had closed): the app's cue to
    offer the Browser tab and the corner preview for its owner."""

    group_id: str
    env: str
    profile: str
    owner_kind: str
    """session · staff"""
    owner_id: str
    url: str
    fresh: bool
    """A throwaway context whose cookies go with it."""


class BrowserNeedsYou(TypedDict):
    """The agent handed the browser to the operator, or the page asked for what only a person gives: a
    password, a one-time code, a CAPTCHA, an HTTP sign-in."""

    group_id: str
    reason: str
    """login · captcha · two_factor · payment · confirm · other · field_forbidden · basic_auth"""
    what: str
    url: str
    title: str
    """Who asks, as a person reads it: the session's title or the staff member's name."""
    by: NotRequired[str]
    """``agent`` (a handoff) or ``daemon`` (a secret field, a CAPTCHA, an HTTP sign-in)."""


class BrowserReturned(TypedDict):
    """The operator gave the browser back; the owner is woken with where it now is."""

    group_id: str
    url: str
    title: str
    tabs: int
    by: str
    note: NotRequired[str]


class BrowserControl(TypedDict):
    """Who holds a browser's controls now: ``agent``, ``human`` or ``paused``. Ephemeral: the app's
    lists re-read on it, and the listing is the truth."""

    group_id: str
    owner: str
    reason: str


class BrowserActivity(TypedDict):
    """The agent acted on its browser: the app's pulsing dot and its corner preview. Ephemeral."""

    group_id: str
    kind: str
    element: str
    at: str


class BrowserWorkflow(TypedDict):
    """A recording of the operator's steps started, stopped, was drafted or discarded: the app reads
    the group's recordings again. Ephemeral."""

    group_id: str
    id: str
    state: str


class BrowserClosed(TypedDict):
    group_id: str
    reason: str
    """closed · idle · crashed · memory · shutdown · lost · owner_gone"""
    by: str


class StaffStatus(TypedDict):
    status: Literal["starting", "working", "turn_done_unseen", "idle", "question", "permission", "error", "exited", "no_signal"]
    previous: str | None
    waiting_for: NotRequired[str]
    detail: NotRequired[str]
    actor: NotRequired[str]


class FileRef(TypedDict):
    """A kept file an event hands on (``daedalus.stores.files``): its handle is ``att:<id>``."""

    id: str
    name: str
    mime: str
    size: int


class StaffReport(TypedDict):
    kind: Literal["checkpoint", "needs_input", "stuck", "done", "turn_done"]
    """``turn_done`` is only ever implicit: a command-line member's turn that ended with no report."""
    text: str
    refs: NotRequired[list[str]]
    actor: NotRequired[str]
    call_id: NotRequired[str]
    """The command-line member's call it came from; a replay of the same call is not reported again."""
    implicit: NotRequired[bool]
    """Made by the host from the turn's last message because the member did not report; ``needs_input``
    when that message ends by asking something."""
    files: NotRequired[list[FileRef]]
    """The artifacts the host took in from the member's folder, kept as the project's files."""
    acknowledged: NotRequired[list[str]]
    """The task's requirements (``R2`` …) the member confirmed it took into its plan."""
    evidence: NotRequired[list[dict[str, str]]]
    """A done report's word on each check it names: ``{item, how, result}``, ``item`` a check or a requirement."""
    unproven: NotRequired[list[str]]
    """The checks and requirements a done report gave no evidence for."""
    operator_steps: NotRequired[dict[str, Any]]
    """Steps the member wrote for the operator (``operator_steps.OperatorSteps.view``) and the file they are kept in."""


class StaffChannel(TypedDict):
    """Whether a command-line member's team tools are connected: its CLI started ``ptyd team-mcp``
    and read the tools. ``missing`` after the wait means its reports and questions cannot arrive,
    though hooks, the screen and messages still work."""

    team_tools: Literal["connected", "missing"]
    detail: NotRequired[str]


class StaffMessage(TypedDict):
    message_id: str
    state: str
    error: NotRequired[str]
    actor: NotRequired[str]


# ``from`` is a keyword, so these two are spelled in the functional form.
TaskChange = TypedDict(
    "TaskChange",
    {
        "task_id": str,
        "title": str,
        "from": NotRequired[str],
        "to": NotRequired[str],
        "assignee_staff_id": NotRequired[str],
        "actor": NotRequired[str],
        "error": NotRequired[str],
    },
)

class AskUpdated(TypedDict):
    """An open question of the orchestrator's reworded in place: the app redraws the same card."""

    request_id: str
    short_id: str
    routed_to: NotRequired[str]


class OpenResults(TypedDict):
    """The team's results no decision of the orchestrator has followed: said to it once, when a turn
    of its ended with them still open (see :mod:`daedalus.extensions.orchestrator_loops`)."""

    loops: list[int]
    lines: list[str]


BusGap = TypedDict("BusGap", {"from": int, "to": int})


class ScheduleFired(TypedDict):
    schedule_id: str
    name: str
    kind: str
    note: NotRequired[str]
    """A wake-up's note: what its orchestrator is to look at."""
    set_by: NotRequired[str]
    """A wake-up's author: ``orchestrator`` or ``operator``."""


class WatchFired(TypedDict):
    watch_id: str
    fire_count: NotRequired[int]
    pattern: NotRequired[dict[str, Any]]
    actor: NotRequired[str]
    action: NotRequired[str]
    """``wake`` · ``tell`` · ``notify``: only a ``wake`` is for the orchestrator's queue."""
    note: NotRequired[str]
    detail: NotRequired[str]
    """What happened, in one line: "Max finished a turn", "CI failure in shop/web on main"."""
    error: NotRequired[str]


class WebhookReceived(TypedDict):
    provider: str
    event: str
    delivery_id: NotRequired[str]
    summary: NotRequired[str]
    repo: NotRequired[str]
    action: NotRequired[str]
    conclusion: NotRequired[str]
    """A pull request's outcome (``opened``, ``merged`` …) or a CI run's (``success``, ``failure`` …)."""
    branch: NotRequired[str]
    title: NotRequired[str]


class ProjectChanged(TypedDict):
    change: NotRequired[str]
    actor: NotRequired[str]


class DispatchCreated(TypedDict):
    dispatch_id: str
    seq: int
    title: str
    text: str
    kind: str
    """``work``, or ``setup``: the survey that writes a new project's brief."""
    from_session: NotRequired[str]
    files: NotRequired[list[FileRef]]
    """Files the main orchestrator handed over with it, now the project's."""


class DispatchMessage(TypedDict):
    dispatch_id: str
    author: str
    """``dispatcher`` (a follow-up for the project) · ``orchestrator`` (a progress report) · ``operator`` · ``system``."""
    kind: str
    text: str
    files: NotRequired[list[FileRef]]


class DispatchUpdated(TypedDict):
    dispatch_id: str
    status: str
    change: NotRequired[str]


class DispatchClosed(TypedDict):
    dispatch_id: str
    status: str
    """``done`` · ``blocked`` · ``cancelled``."""
    result: str
    by: NotRequired[str]
    files: NotRequired[list[FileRef]]


class DispatchStalled(TypedDict):
    dispatch_id: str
    minutes: int


class NotifyDeliver(TypedDict):
    push: bool
    desktop: bool
    telegram: bool


class NotifySummary(TypedDict):
    unseen: int
    needs_you: int


class Notify(TypedDict):
    notification: dict[str, Any]
    toast: bool
    deliver: NotifyDeliver
    merged: bool
    summary: NotifySummary


class NotifySeen(TypedDict):
    ids: list[int] | Literal["all"]
    summary: NotifySummary


class NotifyResolved(TypedDict):
    id: int
    request_ref: str
    resolution: str
    via: str
    summary: NotifySummary


class Presence(TypedDict):
    client: str
    visible: bool
    focused: bool
    sessions: list[str]
    terminals: list[str]
    projects: list[str]
    attended: bool
    """Whether this window is visible and focused, so what it lists is being looked at."""
    newly_attended: NotRequired[dict[str, list[str]]]
    """``sessions`` / ``terminals`` / ``projects`` that no window attended before this change and
    one does now, across every window: what a subscriber acts on when the operator opens something."""


class HarnessUpdated(TypedDict):
    env: str
    harness: str
    version: str
    previous: NotRequired[str]
    error: NotRequired[str]


class HarnessCheck(TypedDict):
    env: str
    harness: str
    ok: bool
    error: NotRequired[str]


class HarnessModels(TypedDict):
    env: str
    harness: str
    models: list[str] | None
    """The models now offered; None when every one is."""


@dataclass(frozen=True, slots=True)
class EventSpec:
    payload: type
    """The ``TypedDict`` the payload follows; its required keys are checked at publish."""
    persist: bool = True
    """Written to ``app_events`` and numbered. An ephemeral event is delivered live only."""
    stream: bool = True
    """Sent on ``/api/events`` by default. An in-process-only event is never sent to a client."""


REGISTRY: dict[str, EventSpec] = {
    "run.started": EventSpec(RunStarted),
    "run.finished": EventSpec(RunFinished),
    "session.status": EventSpec(SessionStatus),
    "session.unread_result": EventSpec(SessionUnreadResult),
    "ask.pending": EventSpec(AskPending),
    "ask.answered": EventSpec(AskAnswered),
    "ask.routed": EventSpec(AskRouted),
    "ask.batch": EventSpec(AskBatch),
    "ask.updated": EventSpec(AskUpdated),
    "permission.pending": EventSpec(PermissionPending),
    "permission.resolved": EventSpec(PermissionResolved),
    "terminal.created": EventSpec(TerminalCreated),
    "terminal.exited": EventSpec(TerminalExited),
    "terminal.title": EventSpec(TerminalTitle),
    "terminal.cwd": EventSpec(TerminalCwd),
    "terminal.command": EventSpec(TerminalCommand),
    "terminal.bell": EventSpec(TerminalBell),
    "terminal.notify": EventSpec(TerminalNotify),
    # Progress changes many times a second while a bar moves; only the latest value is worth anything.
    "terminal.progress": EventSpec(TerminalProgress, persist=False),
    "browser.opened": EventSpec(BrowserOpened),
    "browser.needs_you": EventSpec(BrowserNeedsYou),
    "browser.returned": EventSpec(BrowserReturned),
    "browser.closed": EventSpec(BrowserClosed),
    # Many a minute while an agent works, and worth nothing once read: delivered live, never stored.
    "browser.control": EventSpec(BrowserControl, persist=False),
    "browser.activity": EventSpec(BrowserActivity, persist=False),
    "browser.workflow": EventSpec(BrowserWorkflow, persist=False),
    "staff.status": EventSpec(StaffStatus),
    "staff.report": EventSpec(StaffReport),
    "staff.message": EventSpec(StaffMessage),
    "staff.channel": EventSpec(StaffChannel),
    "task.created": EventSpec(TaskChange),
    "task.changed": EventSpec(TaskChange),
    "task.moved": EventSpec(TaskChange),
    "task.assigned": EventSpec(TaskChange),
    "task.accepted": EventSpec(TaskChange),
    "task.merge_failed": EventSpec(TaskChange),
    "orchestrator.open_results": EventSpec(OpenResults),
    "schedule.fired": EventSpec(ScheduleFired),
    "watch.fired": EventSpec(WatchFired),
    "webhook.received": EventSpec(WebhookReceived),
    "project.changed": EventSpec(ProjectChanged),
    "dispatch.created": EventSpec(DispatchCreated),
    "dispatch.message": EventSpec(DispatchMessage),
    "dispatch.updated": EventSpec(DispatchUpdated),
    "dispatch.closed": EventSpec(DispatchClosed),
    "dispatch.stalled": EventSpec(DispatchStalled),
    "notify": EventSpec(Notify),
    "notify.seen": EventSpec(NotifySeen),
    "notify.resolved": EventSpec(NotifyResolved),
    # What each window shows is the host's business alone: it decides whether to notify, and a
    # client has no use for another client's focus.
    "presence": EventSpec(Presence, persist=False, stream=False),
    "harness.updated": EventSpec(HarnessUpdated),
    "harness.check": EventSpec(HarnessCheck),
    "harness.models": EventSpec(HarnessModels),
}

GAP = "bus.gap"
"""The synthetic item a subscription yields when its cursor is older than what retention kept.
It is never published and never stored; its payload is :class:`BusGap`."""

MAX_PAYLOAD_BYTES = 64 * 1024
"""Events are lifecycle-sized. A payload past this is a producer shipping content through the bus
(a transcript, a screen) that belongs in its own store, and every subscriber and every client
connection would carry it."""


class UnknownEventType(ValueError):
    """The type is not in :data:`REGISTRY`."""


class EventPayloadError(ValueError):
    """The payload misses a required key, is not a JSON object, or is too large."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class AppEvent:
    seq: int
    """0 for an ephemeral event."""
    at: str
    """ISO-8601 in UTC."""
    type: str
    payload: Mapping[str, Any]
    project_id: str | None = None
    session_id: str | None = None
    staff_id: str | None = None
    terminal_id: str | None = None

    def wire(self) -> dict[str, Any]:
        """The JSON object the stream and a replay hand out."""
        return {
            "seq": self.seq,
            "at": self.at,
            "type": self.type,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "staff_id": self.staff_id,
            "terminal_id": self.terminal_id,
            "payload": dict(self.payload),
        }


ID_COLUMNS = ("project_id", "session_id", "staff_id", "terminal_id")


@dataclass(frozen=True, slots=True)
class EventFilter:
    types: tuple[str, ...] = ()
    """Exact names, or prefixes ending in ``.`` (``"terminal."``); empty means every type."""
    project_id: str | None = None
    session_id: str | None = None
    staff_id: str | None = None
    terminal_id: str | None = None

    def matches(self, event: AppEvent) -> bool:
        if self.types and not any(event.type.startswith(t) if t.endswith(".") else event.type == t for t in self.types):
            return False
        return all(getattr(self, column) is None or getattr(self, column) == getattr(event, column) for column in ID_COLUMNS)

    def sql(self) -> tuple[str, list[Any]]:
        """The same test as :meth:`matches`, as a WHERE fragment and its parameters.

        A prefix is compared with ``substr`` rather than ``LIKE``: an underscore in a type name
        (``unread_result``) is a ``LIKE`` wildcard, and escaping it everywhere is a trap.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if self.types:
            parts: list[str] = []
            for name in self.types:
                if name.endswith("."):
                    parts.append("substr(type, 1, ?) = ?")
                    params.extend((len(name), name))
                else:
                    parts.append("type = ?")
                    params.append(name)
            clauses.append("(" + " OR ".join(parts) + ")")
        for column in ID_COLUMNS:
            value = getattr(self, column)
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        return (" AND ".join(clauses) or "1"), params


def _row_event(row: Any) -> AppEvent:
    return AppEvent(
        seq=int(row["seq"]),
        at=str(row["at"]),
        type=str(row["type"]),
        payload=json.loads(row["payload_json"]),
        project_id=row["project_id"],
        session_id=row["session_id"],
        staff_id=row["staff_id"],
        terminal_id=row["terminal_id"],
    )


CATCH_UP_PAGE = 500
"""Rows a lagging subscription reads from the table per query while it catches up."""


class Subscription:
    """An async iterator of :class:`AppEvent` for one subscriber; also an async context manager.

    Registered with the bus the moment it is created, so nothing published between ``subscribe()``
    and the first ``anext`` is missed. ``last_seq`` is the cursor to persist to resume after a
    restart; ``lagging`` says it is reading from the table instead of the live feed; ``dropped``
    counts the ephemeral events it lost while it was.
    """

    def __init__(self, bus: "EventBus", flt: EventFilter, *, after: int | None, name: str, queue_size: int) -> None:
        self.bus = bus
        self.filter = flt
        self.name = name
        self.queue_size = max(1, queue_size)
        self.last_seq = bus.head if after is None else max(0, after)
        # A cursor is served exactly like a subscriber that fell behind: from the table until it
        # has caught up, then from the live feed. One path, so the seam is tested once.
        self.lagging = after is not None
        self.dropped = 0
        self.closed = False
        self._buffer: deque[AppEvent] = deque()
        self._pending: deque[AppEvent] = deque()
        self._wake = asyncio.Event()

    def _offer(self, event: AppEvent) -> None:
        """Called by the bus under its ordering lock. Never blocks and never raises."""
        if self.closed or not self.filter.matches(event):
            return
        if self.lagging:
            # A persisted event will be read from the table; an ephemeral one is lost.
            if not event.seq:
                self.dropped += 1
            return
        if len(self._buffer) >= self.queue_size:
            if event.seq:
                self.lagging = True
                logger.warning("event subscriber %s fell %d events behind; it reads the rest from the table", self.name, len(self._buffer))
            else:
                self.dropped += 1
            self._wake.set()
            return
        self._buffer.append(event)
        self._wake.set()

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> AppEvent:
        while True:
            if self.closed:
                raise StopAsyncIteration
            if self._pending:
                event = self._pending.popleft()
                if event.type != GAP:
                    self.last_seq = event.seq
                return event
            if self._buffer:
                event = self._buffer.popleft()
                if event.seq:
                    if event.seq <= self.last_seq:
                        continue
                    self.last_seq = event.seq
                return event
            if self.lagging:
                await self._catch_up()
                continue
            self._wake.clear()
            await self._wake.wait()

    async def _catch_up(self) -> None:
        """Read the next page of what was missed; rejoin the live feed once the table has no more.

        The last read happens under the bus's ordering lock, so no event can be published between
        "the table has nothing past my cursor" and "deliver me the live ones": no gap and no
        duplicate at the seam. Earlier pages are read without it so a long catch-up never holds
        publishers back.
        """
        oldest = self.bus.oldest
        if self.last_seq < oldest - 1:
            self._pending.append(AppEvent(seq=0, at=_now(), type=GAP, payload={"from": self.last_seq, "to": oldest - 1}))
            self.last_seq = oldest - 1
            return
        page = await self.bus.replay(self.last_seq, self.filter, limit=CATCH_UP_PAGE)
        if len(page) == CATCH_UP_PAGE:
            self._pending.extend(page)
            return
        async with self.bus._order:
            if self.closed:
                return
            page = await self.bus.replay(self.last_seq, self.filter, limit=CATCH_UP_PAGE)
            self._pending.extend(page)
            if len(page) < CATCH_UP_PAGE:
                self.lagging = False
                if not page:
                    # Nothing past the cursor matched. Moving it to the head (safe only here, under
                    # the lock, with the buffer empty) keeps a later catch-up from scanning the
                    # same stretch again and a later prune from mistaking it for a gap.
                    self.last_seq = max(self.last_seq, self.bus.head)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.bus._subscriptions.discard(self)
        self._buffer.clear()
        self._pending.clear()
        self._wake.set()

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()


Handler = Callable[[AppEvent], Awaitable[None]]


@dataclass(slots=True)
class _Outgoing:
    type: str
    payload: Mapping[str, Any]
    ids: dict[str, str | None] = field(default_factory=dict)


class EventBus:
    """The bus. Built by the session manager; ``start`` before the first publish, ``close`` last."""

    def __init__(self, db: Database, *, redactor: redact.Redactor | None = None, queue_size: int = 1024, replay_max: int = 5000) -> None:
        self.db = db
        self.redactor = redactor or redact.shared()
        self.queue_size = queue_size
        """Live events one subscriber may have unread before it falls back to reading the table."""
        self.replay_max = replay_max
        """Events a reconnecting client may be behind and still be caught up, rather than told to resync."""
        self._order = asyncio.Lock()
        self._subscriptions: set[Subscription] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._outbox: asyncio.Queue[_Outgoing | None] = asyncio.Queue()
        self._outbox_task: asyncio.Task[None] | None = None
        self._head = 0
        self._oldest = 1
        self.closed = False

    @property
    def head(self) -> int:
        """The newest ``seq`` handed out (0 before the first persisted event)."""
        return self._head

    @property
    def oldest(self) -> int:
        """The oldest ``seq`` still stored; ``head + 1`` when retention has emptied the table."""
        return self._oldest

    async def start(self) -> None:
        await self._read_bounds()
        if self._outbox_task is None:
            self._outbox_task = asyncio.create_task(self._drain_outbox(), name="event-outbox")

    async def _read_bounds(self) -> None:
        # The table may be empty after a prune while the sequence has moved on: the head is what
        # AUTOINCREMENT remembers, not what happens to be left.
        row = await self.db.fetchone("SELECT seq FROM sqlite_sequence WHERE name = 'app_events'")
        self._head = int(row["seq"]) if row else 0
        row = await self.db.fetchone("SELECT min(seq) AS oldest FROM app_events")
        self._oldest = int(row["oldest"]) if row and row["oldest"] is not None else self._head + 1

    @asynccontextmanager
    async def transaction_guard(self) -> AsyncIterator[None]:
        """Hold event sequence ordering across a caller-owned domain transaction and its announcement."""
        async with self._order:
            yield

    async def persist_in(self, conn: aiosqlite.Connection, event_type: str,
                         payload: Mapping[str, Any], *, project_id: str | None = None,
                         session_id: str | None = None, staff_id: str | None = None,
                         terminal_id: str | None = None) -> AppEvent:
        """Insert a durable event in the caller's transaction, with no subscriber side effect yet."""
        ids = {"project_id": project_id, "session_id": session_id,
               "staff_id": staff_id, "terminal_id": terminal_id}
        spec, cleaned, encoded = self._validated(event_type, payload, ids)
        if not spec.persist:
            raise EventPayloadError("a transactional event must be durable")
        at = _now()
        cursor = await conn.execute(
            "INSERT INTO app_events(at,type,project_id,session_id,staff_id,terminal_id,payload_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (at, event_type, project_id, session_id, staff_id, terminal_id, encoded))
        seq = int(cursor.lastrowid or 0)
        await cursor.close()
        return AppEvent(seq=seq, at=at, type=event_type, payload=cleaned, **ids)

    def announce_committed(self, event: AppEvent) -> None:
        """Fan out an already committed event while its transaction guard still holds ordering."""
        if not self._order.locked():
            raise RuntimeError("an event transaction guard is required")
        self._head = max(self._head, event.seq)
        if self._oldest > event.seq:
            self._oldest = event.seq
        if not self.closed:
            for subscription in list(self._subscriptions):
                subscription._offer(event)

    # -- publishing ------------------------------------------------------------------------

    def _validated(self, event_type: str, payload: Mapping[str, Any], ids: Mapping[str, str | None]) -> tuple[EventSpec, dict[str, Any], str]:
        spec = REGISTRY.get(event_type)
        if spec is None:
            raise UnknownEventType(f"{event_type!r} is not a registered event type")
        if not isinstance(payload, Mapping):
            raise EventPayloadError(f"{event_type}: the payload must be a mapping, not {type(payload).__name__}")
        missing = sorted(spec.payload.__required_keys__ - payload.keys())  # type: ignore[attr-defined]
        if missing:
            raise EventPayloadError(f"{event_type}: missing {', '.join(missing)}")
        for column, value in ids.items():
            if value is not None and not isinstance(value, str):
                raise EventPayloadError(f"{event_type}: {column} must be a string, not {type(value).__name__}")
        cleaned = self.redactor.redact_any(dict(payload))
        try:
            text = json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise EventPayloadError(f"{event_type}: {exc}") from exc
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise EventPayloadError(f"{event_type}: the payload is {len(text)} characters; events carry references, not content")
        return spec, cleaned, text

    async def publish(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        staff_id: str | None = None,
        terminal_id: str | None = None,
    ) -> AppEvent:
        """Store the event (unless its type is ephemeral), then hand it to every matching subscriber.

        Raises :class:`UnknownEventType` or :class:`EventPayloadError`; never raises because of a
        subscriber. After :meth:`close` the event is logged and dropped: a publisher must not fail
        because the host is on its way down, and nobody is left to receive it.
        """
        ids = {"project_id": project_id, "session_id": session_id, "staff_id": staff_id, "terminal_id": terminal_id}
        spec, cleaned, text = self._validated(event_type, payload, ids)
        at = _now()
        if self.closed:
            logger.info("event %s published after the bus closed; dropped", event_type)
            return AppEvent(seq=0, at=at, type=event_type, payload=cleaned, **ids)
        async with self._order:
            seq = 0
            if spec.persist:
                async with self.db.transaction() as conn:
                    cursor = await conn.execute(
                        "INSERT INTO app_events(at, type, project_id, session_id, staff_id, terminal_id, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (at, event_type, project_id, session_id, staff_id, terminal_id, text),
                    )
                    seq = int(cursor.lastrowid or 0)
                    await cursor.close()
            event = AppEvent(seq=seq, at=at, type=event_type, payload=cleaned, **ids)
            # No await from here to the end of the fan-out: a subscription created meanwhile either
            # reads the old head and receives this event, or reads the new one and does not.
            if seq:
                self._head = seq
                if self._oldest > seq:
                    self._oldest = seq
            for subscription in list(self._subscriptions):
                subscription._offer(event)
        return event

    def publish_soon(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        staff_id: str | None = None,
        terminal_id: str | None = None,
    ) -> None:
        """Publish from synchronous code (the policy gate): queued, published in call order.

        The type and payload are checked here, so a caller that gets them wrong hears about it at its
        own call site; a failure to store the event later is logged.
        """
        ids = {"project_id": project_id, "session_id": session_id, "staff_id": staff_id, "terminal_id": terminal_id}
        self._validated(event_type, payload, ids)
        if self.closed:
            logger.info("event %s published after the bus closed; dropped", event_type)
            return
        self._outbox.put_nowait(_Outgoing(event_type, payload, ids))
        if self._outbox_task is None:
            self._outbox_task = asyncio.create_task(self._drain_outbox(), name="event-outbox")

    async def _drain_outbox(self) -> None:
        while True:
            item = await self._outbox.get()
            if item is None:
                return
            try:
                await self.publish(item.type, item.payload, **item.ids)
            except Exception:  # noqa: BLE001 — one bad event must not stop the ones queued behind it
                logger.exception("publishing %s from the outbox failed", item.type)

    # -- subscribing -----------------------------------------------------------------------

    def subscribe(self, flt: EventFilter | None = None, *, after: int | None = None, name: str = "") -> Subscription:
        """``after=None`` is live from now; ``after=N`` replays every matching ``seq > N`` first."""
        subscription = Subscription(self, flt or EventFilter(), after=after, name=name or "anonymous", queue_size=self.queue_size)
        if self.closed:
            subscription.closed = True
            return subscription
        self._subscriptions.add(subscription)
        return subscription

    def on(self, flt: EventFilter | None, handler: Handler, *, name: str, after: int | None = None) -> asyncio.Task[None]:
        """Run ``handler`` for every matching event on a task of its own. A handler that raises is
        logged and the loop goes on; the task returns when the bus closes."""

        async def loop() -> None:
            async with self.subscribe(flt, after=after, name=name) as subscription:
                async for event in subscription:
                    try:
                        await handler(event)
                    except Exception:  # noqa: BLE001 — one failed event must not unsubscribe the handler
                        logger.exception("event handler %s failed on %s", name, event.type)

        task = asyncio.create_task(loop(), name=f"events:{name}")
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)
        return task

    async def replay(self, after: int, flt: EventFilter | None = None, *, limit: int = 500) -> list[AppEvent]:
        """Stored events with ``seq > after`` that match, oldest first, at most ``limit``."""
        where, params = (flt or EventFilter()).sql()
        rows = await self.db.fetchall(
            f"SELECT * FROM app_events WHERE seq > ? AND {where} ORDER BY seq LIMIT ?",
            (after, *params, max(1, limit)),
        )
        return [_row_event(row) for row in rows]

    async def latest(self, flt: EventFilter | None = None, *, limit: int = 10) -> list[AppEvent]:
        """The newest stored events that match, at most ``limit``, oldest first among them."""
        where, params = (flt or EventFilter()).sql()
        rows = await self.db.fetchall(
            f"SELECT * FROM (SELECT * FROM app_events WHERE {where} ORDER BY seq DESC LIMIT ?) ORDER BY seq",
            (*params, max(1, limit)),
        )
        return [_row_event(row) for row in rows]

    async def count_after(self, after: int, flt: EventFilter | None = None, *, cap: int) -> int:
        """How many stored events match past ``after``, counting no further than ``cap``."""
        where, params = (flt or EventFilter()).sql()
        row = await self.db.fetchone(
            f"SELECT count(*) AS n FROM (SELECT 1 FROM app_events WHERE seq > ? AND {where} LIMIT ?)",
            (after, *params, cap),
        )
        return int(row["n"]) if row else 0

    # -- retention and lifetime ------------------------------------------------------------

    async def prune(self, *, keep_days: int, max_rows: int, now: datetime | None = None) -> int:
        """Drop events older than ``keep_days`` and then all but the newest ``max_rows``; returns how many."""
        cutoff = _stamp((now or datetime.now(UTC)) - timedelta(days=keep_days))
        before = await self.db.fetchone("SELECT count(*) AS n FROM app_events")
        await self.db.execute("DELETE FROM app_events WHERE at < ?", (cutoff,))
        await self.db.execute(
            "DELETE FROM app_events WHERE seq <= (SELECT seq FROM app_events ORDER BY seq DESC LIMIT 1 OFFSET ?)",
            (max(0, max_rows),),
        )
        after = await self.db.fetchone("SELECT count(*) AS n FROM app_events")
        await self._read_bounds()
        return (int(before["n"]) if before else 0) - (int(after["n"]) if after else 0)

    async def close(self) -> None:
        """Publish what is still queued, then end every subscription and handler."""
        if self.closed:
            return
        if self._outbox_task is not None:
            self._outbox.put_nowait(None)
            with suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await asyncio.gather(self._outbox_task, return_exceptions=True)
            if not self._outbox_task.done():
                self._outbox_task.cancel()
        self.closed = True
        for subscription in list(self._subscriptions):
            subscription.close()
        handlers = list(self._handlers)
        if handlers:
            # A handler in the middle of its own work gets a moment to finish it; one that does not
            # return is cancelled, because a shutdown that waits on a subscriber never ends.
            _done, stuck = await asyncio.wait(handlers, timeout=5)
            for task in stuck:
                task.cancel()
            if stuck:
                await asyncio.gather(*stuck, return_exceptions=True)


# -- the SSE stream ------------------------------------------------------------------------------

KEEPALIVE_SECONDS = 15.0


def _frame(event: str, data: Mapping[str, Any], *, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def streamed_types() -> tuple[str, ...]:
    """What ``/api/events`` sends when the client names no types."""
    return tuple(name for name, spec in REGISTRY.items() if spec.stream)


async def event_stream(
    bus: EventBus,
    flt: EventFilter,
    *,
    after: int | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    client: str = "",
    kind: str = "browser",
    on_open: Callable[[], Awaitable[None]] | None = None,
    on_close: Callable[[], Awaitable[None]] | None = None,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    """The text of ``/api/events``: ``hello``, then the events, with a comment line as keepalive.

    With no cursor the stream is live from the head. With one it replays what matches past it and
    goes live without a seam. A cursor older than retention, ahead of the head, or further behind
    than ``bus.replay_max`` gets one ``resync`` frame and continues live from the head; the client
    re-reads its lists. A type whose spec says ``stream=False`` is never sent, whatever was asked.

    It lives here and not beside the route so a test can drive it with a fake ``is_disconnected``:
    a test client never disconnects a stream it holds open.
    """
    resync: str | None = None
    if after is not None:
        if after > bus.head:
            resync = "cursor_ahead"
        elif after < bus.oldest - 1:
            resync = "expired"
        elif await bus.count_after(after, flt, cap=bus.replay_max + 1) > bus.replay_max:
            resync = "too_far"
    subscription = bus.subscribe(flt, after=None if resync else after, name=f"stream:{kind}:{client or 'anonymous'}")
    pending: asyncio.Future[AppEvent] | None = None
    opened = False
    try:
        hello_head = subscription.last_seq if after is None or resync else bus.head
        yield _frame("hello", {"head": hello_head, "oldest": bus.oldest, "server_time": _now(), "client": client})
        if resync:
            yield _frame("resync", {"reason": resync, "head": subscription.last_seq})
        if on_open is not None:
            await on_open()
        opened = True
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(subscription))
            done, _ = await asyncio.wait({pending}, timeout=keepalive)
            if not done:
                if await is_disconnected():
                    return
                yield ": keepalive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return  # the bus closed: the host is stopping
            finally:
                pending = None
            if event.type == GAP:
                # Retention overtook the cursor between the check above and the replay.
                yield _frame("resync", {"reason": "expired", "head": bus.head})
                continue
            spec = REGISTRY.get(event.type)
            if spec is None or not spec.stream:
                continue
            yield _frame(event.type, event.wire(), event_id=event.seq or None)
            if await is_disconnected():
                return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        subscription.close()
        # Only a stream that was opened is closed: a client gone before its first frame was read would
        # otherwise take away a presence connection it never added.
        if on_close is not None and opened:
            await on_close()


__all__ = [
    "GAP",
    "MAX_PAYLOAD_BYTES",
    "REGISTRY",
    "AppEvent",
    "EventBus",
    "EventFilter",
    "EventPayloadError",
    "EventSpec",
    "Subscription",
    "UnknownEventType",
    "event_stream",
    "streamed_types",
]
