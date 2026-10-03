"""Notifications: the one place anything that wants the operator's attention is recorded and routed.

A scheduled run that failed, a question an agent asks, a policy refusal waiting for an approval, a
service that did not come back: each producer posts a :class:`Draft`, and the service does three
things with it, in this order:

1. decides where it goes — the app (a toast), a push, the desktop, a Telegram line — with the pure
   :func:`daedalus.host.notify_routing.decide`, from the operator's preferences, the level, quiet
   hours, mutes and what the operator is looking at;
2. records it as a row of ``notifications`` (or merges it into the open row with the same dedupe
   key), with the reason for each channel in ``delivered``;
3. delivers it and announces it on the bus as ``notify``, which the app and the desktop launcher read.

The router (:class:`NotificationRouter`) turns the bus's own events — a run that finished, a question,
a permission request, a staff member's turn — into drafts, and resolves a notification when its
request is answered anywhere. A request can be answered from the notification itself through
:meth:`NotificationService.act`: the first answer wins, the second is told what the first one was.

An agent reaches the operator itself through the ``Notify`` tool, which posts through the same
service (:class:`AgentNotifier`) under a budget of its own; subagents and staff do not have it.

What the router never does: post into a project's Telegram topic (the orchestrator speaks there), put
a detached session on Telegram at any level, or do anything Telegram-related when no bot is bound.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, cast, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import NotificationsConfig
from daedalus.extensions.watch_egress import WatchEgressDenied, watch_notification_guard
from daedalus.host.engine_factory import TENANT
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.notify_routing import (
    REACHES_TELEGRAM,
    Candidate,
    Channels,
    Decision,
    RateLimiter,
    TelegramFacts,
    decide,
)
from daedalus.host.notify_text import render
from daedalus.host.presence import PresenceSnapshot
from daedalus.transport.telegram.front import TelegramOutbox, is_subagent
from daedalus.transport.telegram.markdown import split_message

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.events import EventBus
    from daedalus.host.presence import Presence
    from daedalus.host.session_runner import SessionManager
    from daedalus.stores.database import Database
    from daedalus.transport.telegram.front import TelegramFront

logger = logging.getLogger(__name__)

Category = Literal[
    "run_finished",
    "question",
    "permission",
    "run_failed",
    "staff_turn",
    "staff_review",
    "orchestrator_report",
    "agent_notify",
    "reminder",
    "spend",
    "system",
]
Level = Literal["quiet", "normal", "urgent"]
Tone = Literal["ok", "info", "warning", "error"]
View = Literal["all", "unseen", "problems", "needs_you"]
ActionStyle = Literal["primary", "default", "danger", "ghost"]

CATEGORIES: tuple[str, ...] = get_args(Category)
LEVELS: tuple[str, ...] = get_args(Level)
TONES: tuple[str, ...] = get_args(Tone)
VIEWS: tuple[str, ...] = get_args(View)

DEFAULT_LEVEL: dict[str, Level] = {"permission": "urgent"}
"""A category's level when the producer does not name one; everything not listed is ``normal``."""

TITLE_MAX = 300
BODY_MAX = 20_000
EVENT_BODY_MAX = 4_000
"""The body a ``notify`` event carries. The full text is one list request away, and a body at its
stored limit, written in a script of four-byte characters, would pass the bus's payload ceiling."""
LIST_MAX = 500
TELEGRAM_BODY_MAX = 1_500
"""A Telegram line is a pointer to the entry, not the entry: the full text is in the app."""

SELF_REPORTING_ORIGINS = ("heartbeat", "schedule", "reminder", "loop")
"""Runs whose failure the scheduler, the heartbeat or the loop already post, with more to say."""
SELF_REPORTING_METADATA = ("unattended", "heartbeat", "staff_id")
"""Sessions the same holds for; a staff member's failure arrives as its ``staff.status``."""
ASK_BUTTONS_MAX = 4


@dataclass(frozen=True)
class Action:
    """A button on a notification. ``quick`` actions may be taken without opening the app."""

    id: str
    label: str
    style: ActionStyle = "default"
    quick: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "style": self.style, "quick": self.quick}


@dataclass(frozen=True)
class Draft:
    """What a producer wants the operator to know.

    ``kind`` is the producer's own sub-kind (the icon, the grouping); it defaults to the category.
    ``handled`` names the channels the producer already delivered through, so nothing sends the
    same thing there twice.
    """

    category: Category
    title: str
    body: str = ""
    kind: str = ""
    level: Level | None = None
    tone: Tone = "info"
    project_id: str | None = None
    session_id: str | None = None
    staff_id: str | None = None
    terminal_id: str | None = None
    run_id: str | None = None
    link: str = ""
    actions: tuple[Action, ...] = ()
    request_ref: str | None = None
    dedupe_key: str | None = None
    source: str = "system"
    handled: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ProjectNotifyPolicy:
    """What a project's orchestrator asks of the router. The default is a project without one."""

    orchestrated: bool = False
    hold_seconds: int = 0
    """How long a staff member's request waits for the orchestrator before the operator hears of it."""
    quick_actions: bool = True
    """Whether its requests may be answered from a notification without opening the app."""
    orchestrator_session_id: str | None = None
    """The orchestrator's own session: what comes from it is never held for itself."""


NO_POLICY = ProjectNotifyPolicy()


class ActionConflict(Exception):
    """The request was already answered elsewhere; ``resolution`` is what that answer was."""

    def __init__(self, resolution: str) -> None:
        super().__init__(f"already answered: {resolution}")
        self.resolution = resolution


class ActionRefused(ValueError):
    """The action does not apply to this notification, or may not be taken this way."""


@dataclass(frozen=True)
class ActionRequest:
    """An answer to a request, handed to the resolver its ``request_ref`` prefix names."""

    request_ref: str
    kind: str
    """The prefix: ``ask``, ``policy``, ``harness``…"""
    scope: str
    """The session (or staff session) the request belongs to."""
    target: str
    """The request's own id within that scope: a tool call id, an approval key."""
    action: str
    value: str | None
    via: str
    notification: NotificationView


@dataclass(frozen=True)
class ActionOutcome:
    resolution: str


Resolver = Callable[[ActionRequest], Awaitable[ActionOutcome]]
ProjectPolicy = Callable[[str], "ProjectNotifyPolicy | Awaitable[ProjectNotifyPolicy]"]


class Channel(Protocol):
    """A way out that is not the app itself, registered by whoever implements it (the push sender)."""

    name: str

    def available(self) -> bool:
        """Whether there is anywhere to send right now (a registered device, say)."""
        ...

    async def deliver(self, view: NotificationView, *, more: int) -> Any:
        """Send it; return a JSON-able outcome for ``delivered`` (``{"sent": 2}``)."""
        ...


class NotificationSummary(TypedDict):
    unseen: int
    needs_you: int


class NotificationView(TypedDict):
    id: int
    at: str
    updated_at: str
    category: str
    kind: str
    level: str
    tone: str
    title: str
    body: str
    link: str
    session_id: str | None
    run_id: str | None
    project_id: str | None
    staff_id: str | None
    terminal_id: str | None
    source: str
    dedupe_key: str | None
    request_ref: str | None
    count: int
    actions: list[dict[str, Any]]
    seen: bool
    resolved: str | None
    needs_you: bool
    delivered: dict[str, Any]


class NotificationPage(TypedDict):
    entries: list[NotificationView]
    next_before: int | None
    summary: NotificationSummary


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _view(row: Any, now: str) -> NotificationView:
    held = row["held_until"] is not None and row["held_until"] > now
    return NotificationView(
        id=int(row["id"]),
        at=row["at"],
        updated_at=row["updated_at"],
        category=row["category"],
        kind=row["kind"],
        level=row["level"],
        tone=row["tone"],
        title=row["title"],
        body=row["body"],
        link=row["link"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        project_id=row["project_id"],
        staff_id=row["staff_id"],
        terminal_id=row["terminal_id"],
        source=row["source"],
        dedupe_key=row["dedupe_key"],
        request_ref=row["request_ref"],
        count=int(row["count"]),
        actions=_json(row["actions_json"], []),
        seen=row["seen_at"] is not None,
        resolved=row["resolution"] if row["resolved_at"] is not None else None,
        needs_you=row["request_ref"] is not None and row["resolved_at"] is None and not held,
        delivered=_json(row["delivered_json"], {}),
    )


def split_ref(request_ref: str) -> tuple[str, str, str]:
    """``ask:<session>:<tool call id>`` → ``("ask", session, tool call id)``."""
    kind, _, rest = request_ref.partition(":")
    scope, _, target = rest.partition(":")
    return kind, scope, target


# A row counts as unseen when nobody has looked at it and it is worth looking at: a quiet row is a
# record ("the heartbeat checked, nothing new") and never badges. A held row is waiting for the
# orchestrator to answer it first, so the operator has not been told about it yet. Each clause takes
# the current time as its parameters, one per ``?``.
_NOT_HELD = "(held_until IS NULL OR held_until <= ?)"
_UNSEEN = f"seen_at IS NULL AND level != 'quiet' AND {_NOT_HELD}"
_NEEDS_YOU = f"request_ref IS NOT NULL AND resolved_at IS NULL AND {_NOT_HELD}"
_VIEW_WHERE: dict[str, str] = {
    "all": _NOT_HELD,
    "unseen": _UNSEEN,
    "problems": f"tone IN ('warning', 'error') AND {_NOT_HELD}",
    "needs_you": _NEEDS_YOU,
}


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class NotificationService:
    """The store, the routing and the announcements. Every write is one transaction followed by one bus event.

    Everything it asks of the rest of the host arrives as a callable, so a test builds one from a
    database and a bus alone: ``front`` (the Telegram front, or ``None``), ``presence``,
    ``preferences`` (the ``[notifications]`` section as it is now), ``session_metadata`` (to learn a
    session is detached or belongs to a staff member) and ``language`` (for the router's own words).
    """

    def __init__(
        self,
        db: Database,
        bus: EventBus | None = None,
        *,
        front: Callable[[], TelegramFront | None] | None = None,
        presence: Presence | None = None,
        preferences: Callable[[], NotificationsConfig] | None = None,
        session_metadata: Callable[[str], Awaitable[Mapping[str, Any] | None]] | None = None,
    ) -> None:
        self.db = db
        self.bus = bus
        self._front = front or (lambda: None)
        self.presence = presence
        self._preferences = preferences or NotificationsConfig
        self._session_metadata = session_metadata
        self._project_policy: ProjectPolicy | None = None
        self._resolvers: dict[str, Resolver] = {}
        self._links: list[Callable[[str], Awaitable[str | None]]] = []
        self._channels: dict[str, Channel] = {}
        prefs = self._preferences()
        self._limiter = RateLimiter(prefs.push_per_session, timedelta(minutes=prefs.push_window_minutes), prefs.push_per_hour)
        self._answering: dict[str, asyncio.Lock] = {}
        self._hold_wake = asyncio.Event()

    # -- what others plug in ---------------------------------------------------------------

    def register_resolver(self, prefix: str, resolver: Resolver) -> None:
        """Who answers a request whose ``request_ref`` starts with ``<prefix>:``."""
        self._resolvers[prefix] = resolver

    def register_link(self, link: Callable[[str], Awaitable[str | None]]) -> None:
        """``request_ref -> path`` asked before a request's notification is written: the first that
        answers is where tapping it goes. A request shown in the main orchestrator's chat opens there."""
        self._links.append(link)

    async def link_for(self, request_ref: str, default: str) -> str:
        for link in self._links:
            try:
                found = await link(request_ref)
            except Exception:  # noqa: BLE001 — a link is a courtesy; the session's own is always there
                logger.warning("a notification link hook failed for %s", request_ref, exc_info=True)
                continue
            if found:
                return found
        return default

    def register_channel(self, channel: Channel) -> None:
        self._channels[channel.name] = channel

    def set_project_policy(self, policy: ProjectPolicy | None) -> None:
        """``(project_id) -> ProjectNotifyPolicy``, sync or async; the orchestrator installs it."""
        self._project_policy = policy

    def preferences(self) -> NotificationsConfig:
        return self._preferences()

    def language(self) -> str:
        return self.presence.locale()[0] if self.presence is not None else ""

    # -- writing -----------------------------------------------------------------------

    async def post(self, draft: Draft, *, force: bool = False) -> NotificationView | None:
        """Record a notification (or merge it into the open one with the same ``dedupe_key``) and send it on.

        A merge counts the repeat, replaces the text and makes the entry unseen again: the operator
        who dismissed "the service is down" must hear that it is down once more, but in one row, and
        without a second sound unless the level rose. Returns ``None`` when there was nothing to
        record: an answer arriving in the session the operator is reading.
        """
        if draft.category not in CATEGORIES:
            raise ValueError(f"unknown notification category {draft.category!r}")
        level = draft.level or DEFAULT_LEVEL.get(draft.category, "normal")
        if level not in LEVELS:
            raise ValueError(f"unknown notification level {level!r}")
        tone = draft.tone if draft.tone in TONES else "info"
        title, body = draft.title.strip()[:TITLE_MAX] or (draft.kind or draft.category), draft.body[:BODY_MAX]
        prefs = self._preferences()
        self._limiter.configure(prefs.push_per_session, timedelta(minutes=prefs.push_window_minutes), prefs.push_per_hour)
        facts, policy, staff_id = await self._facts(draft.session_id, draft.project_id, draft.staff_id, draft.source)
        # A request is answerable from a lock screen only when the producer, the operator and the project all allow it.
        quick_allowed = prefs.quick_actions and policy.quick_actions
        actions = json.dumps([replace(a, quick=a.quick and quick_allowed).as_dict() for a in draft.actions], ensure_ascii=False)
        presence = self._snapshot()
        now_moment = datetime.now(UTC)
        now = now_moment.isoformat()
        merged = False
        still_held = False
        async with self.db.transaction() as conn:
            row_id = 0
            existing = None
            if draft.dedupe_key:
                cursor = await conn.execute(
                    "SELECT id, level, held_until, event_seq, seen_at FROM notifications WHERE dedupe_key = ? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                    (draft.dedupe_key,),
                )
                existing = await cursor.fetchone()
                await cursor.close()
            rose = existing is not None and LEVELS.index(level) > LEVELS.index(existing["level"])
            candidate = Candidate(
                category=draft.category, level=level, actionable=draft.request_ref is not None, session_id=draft.session_id,
                terminal_id=draft.terminal_id, project_id=draft.project_id, staff_id=staff_id, handled=draft.handled,
                # Silent only while the last occurrence is still unseen: that is a burst. Once the operator
                # has looked, the next one is news again (another answer in the same session, the service
                # down once more), and it sounds like the first did.
                merged=existing is not None and existing["seen_at"] is None, level_rose=rose, force=force,
            )
            decision = decide(
                candidate, prefs, presence, facts, self._available(), now_moment,
                limiter=self._limiter, hold_seconds=policy.hold_seconds if policy.orchestrated else 0,
            )
            if not decision.record:
                return None
            seen_at = now if decision.seen_now else None
            if existing is not None:
                row_id, merged = int(existing["id"]), True
                still_held = existing["held_until"] is not None and existing["held_until"] > now and existing["event_seq"] is None
                await conn.execute(
                    "UPDATE notifications SET updated_at = ?, title = ?, body = ?, count = count + 1, seen_at = ?,"
                    " level = CASE WHEN ? = 'urgent' THEN 'urgent' WHEN level = 'quiet' THEN ? ELSE level END,"
                    " tone = ?, actions_json = ?, run_id = COALESCE(?, run_id) WHERE id = ?",
                    (now, title, body, seen_at, level, level, tone, actions, draft.run_id, row_id),
                )
            else:
                held_until = decision.hold_until.isoformat() if decision.hold_until is not None else None
                cursor = await conn.execute(
                    "INSERT INTO notifications(at, updated_at, kind, category, level, tone, title, body, link, session_id, run_id,"
                    " project_id, staff_id, terminal_id, source, dedupe_key, actions_json, request_ref, delivered_json, held_until, seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now, now, draft.kind or draft.category, draft.category, level, tone, title, body, draft.link,
                        draft.session_id, draft.run_id, draft.project_id, staff_id, draft.terminal_id,
                        draft.source, draft.dedupe_key, actions, draft.request_ref,
                        json.dumps({"held": "for the orchestrator"} if held_until else {}), held_until, seen_at,
                    ),
                )
                row_id = int(cursor.lastrowid or 0)
                await cursor.close()
        if (decision.hold_until is not None and not merged) or still_held:
            # Not announced: the orchestrator may answer it before the operator is disturbed at all.
            self._hold_wake.set()
            return await self.get(row_id)
        return await self._deliver(row_id, decision, merged=merged)

    async def _facts(self, session_id: str | None, project_id: str | None, staff_id: str | None, source: str) -> tuple[TelegramFacts, ProjectNotifyPolicy, str | None]:
        metadata: Mapping[str, Any] = {}
        if session_id and self._session_metadata is not None:
            try:
                metadata = await self._session_metadata(session_id) or {}
            except Exception:  # noqa: BLE001 — an unreadable session routes like one with no special marks
                logger.warning("could not read the metadata of session %s", session_id, exc_info=True)
        staff_id = staff_id or (str(metadata["staff_id"]) if metadata.get("staff_id") else None)
        policy = NO_POLICY
        if project_id and self._project_policy is not None:
            try:
                policy = await _maybe(self._project_policy(project_id)) or NO_POLICY
            except Exception:  # noqa: BLE001 — without the orchestrator's answer the project is treated as plain
                logger.warning("could not read the notification policy of project %s", project_id, exc_info=True)
        from_orchestrator = source.startswith("orchestrator") or (session_id is not None and session_id == policy.orchestrator_session_id)
        facts = TelegramFacts(
            front=self._front() is not None,
            # A subagent is off Telegram whatever its leader does (the front refuses it an outbox),
            # so the router must not count on Telegram having shown it.
            detached=bool(metadata.get("telegram_detached")) or is_subagent(dict(metadata)),
            orchestrated=policy.orchestrated,
            from_orchestrator=from_orchestrator,
        )
        return facts, policy, staff_id

    def _snapshot(self) -> PresenceSnapshot:
        return self.presence.snapshot() if self.presence is not None else PresenceSnapshot(present=False)

    def _available(self) -> Channels:
        push = self._channels.get("push")
        try:
            return Channels(push=push is not None and push.available())
        except Exception:  # noqa: BLE001
            logger.warning("the push channel could not say whether it is available", exc_info=True)
            return Channels()

    async def _deliver(self, row_id: int, decision: Decision, *, merged: bool) -> NotificationView:
        """Send what the decision says through each channel, write down how it went, and announce it."""
        view = await self.get(row_id)
        assert view is not None  # written just before; nobody deletes it in between
        outcomes: dict[str, Any] = {name: reason for name, reason in decision.reasons.items() if reason}
        telegram = decision.telegram in REACHES_TELEGRAM
        if decision.telegram in ("session", "general"):
            outcomes["telegram"] = await self._send_telegram(decision.telegram, view)
            telegram = outcomes["telegram"] == decision.telegram
        pushed = False
        channel = self._channels.get("push")
        if decision.push and channel is not None:
            try:
                outcomes["push"] = await channel.deliver(view, more=decision.more)
                pushed = True
            except Exception as exc:  # noqa: BLE001 — the record stands whether or not a device took it
                logger.warning("notification %s could not be pushed", row_id, exc_info=True)
                outcomes["push"] = f"failed: {type(exc).__name__}"
        delivered = {**view["delivered"], **outcomes}
        delivered.pop("held", None)
        await self.db.execute("UPDATE notifications SET delivered_json = ? WHERE id = ?", (json.dumps(delivered, ensure_ascii=False), row_id))
        view["delivered"] = delivered
        await self._announce(view, merged=merged, toast=decision.toast, push=pushed, desktop=decision.desktop, telegram=telegram, more=decision.more)
        return view

    async def _send_telegram(self, route: str, view: NotificationView) -> str:
        front = self._front()
        if front is None:
            return "skipped: no bot"
        head = f"{render('telegram.prefix', self.language())} {view['title']}"
        body = view["body"].strip()
        text = head + (f"\n\n{body[:TELEGRAM_BODY_MAX]}" if body else "")
        try:
            if route == "general":
                await front.notify(text, markdown=False)
                return "general"
            outbox = await front.outbox_for_session(view["session_id"] or "")
            if outbox is None:
                return "skipped: no topic"
            for chunk in split_message(text):
                await outbox.send_text(chunk, markdown=False)
            return "session"
        except Exception as exc:  # noqa: BLE001 — the record stands whether or not the chat took the line
            logger.warning("notification %s could not be sent to Telegram", view["id"], exc_info=True)
            return f"failed: {type(exc).__name__}"

    async def _announce(self, view: NotificationView, *, merged: bool, toast: bool, push: bool, desktop: bool, telegram: bool, more: int = 0) -> None:
        if self.bus is None:
            return
        shown = dict(view)
        if len(view["body"]) > EVENT_BODY_MAX:
            shown["body"] = view["body"][:EVENT_BODY_MAX]
            shown["body_truncated"] = True
        deliver: dict[str, Any] = {"push": push, "desktop": desktop, "telegram": telegram}
        if more:
            deliver["more"] = more
            deliver["more_text"] = render("burst", self.language(), count=more)
        payload = {
            "notification": shown,
            "toast": toast,
            "deliver": deliver,
            "merged": merged,
            "summary": await self.summary(),
        }
        try:
            event = await self.bus.publish(
                "notify", payload,
                project_id=view["project_id"], session_id=view["session_id"], staff_id=view["staff_id"], terminal_id=view["terminal_id"],
            )
        except Exception:  # noqa: BLE001 — the row is the record; a client that missed the event sees it on its next list
            logger.warning("notification %s was stored but not announced", view["id"], exc_info=True)
            return
        if event.seq:
            await self.db.execute("UPDATE notifications SET event_seq = ? WHERE id = ?", (event.seq, view["id"]))

    # -- holding for the orchestrator ------------------------------------------------------

    async def release(self, request_ref: str) -> int:
        """End the hold on a request now (the orchestrator escalates it); returns how many rows were released."""
        rows = await self.db.fetchall(
            "SELECT id FROM notifications WHERE request_ref = ? AND held_until IS NOT NULL AND resolved_at IS NULL", (request_ref,),
        )
        for row in rows:
            await self._release_row(int(row["id"]))
        return len(rows)

    async def _release_row(self, row_id: int) -> None:
        """Announce a held row, routed as if it had just arrived: the operator may have sat down since."""
        try:
            async with watch_notification_guard(self.db, row_id):
                await self._release_row_fenced(row_id)
        except WatchEgressDenied as exc:
            # A held watch record was never shown. Preserve its history but never release its
            # channels after the exact standing grant or condition was withdrawn.
            async with self.db.transaction() as conn:
                cursor = await conn.execute(
                    "UPDATE notifications SET held_until = NULL,resolved_at = ?,resolution = ?,updated_at = ?"
                    " WHERE id = ? AND held_until IS NOT NULL AND event_seq IS NULL AND resolved_at IS NULL",
                    (_now(), "watch approval withdrawn", _now(), row_id),
                )
                suppressed = cursor.rowcount > 0
                await cursor.close()
                if suppressed and exc.delivery_id:
                    await conn.execute(
                        "UPDATE watch_deliveries SET status = 'failed',last_error = ?,updated_at = ?"
                        " WHERE id = ? AND status IN ('reconciling','delivered')",
                        ("watch approval withdrawn before notification release", _now(), exc.delivery_id),
                    )

    async def _release_row_fenced(self, row_id: int) -> None:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE notifications SET held_until = NULL WHERE id = ? AND held_until IS NOT NULL AND resolved_at IS NULL", (row_id,),
            )
            claimed = cursor.rowcount > 0
            await cursor.close()
        if not claimed:
            return  # released or answered meanwhile
        view = await self.get(row_id)
        assert view is not None
        prefs = self._preferences()
        facts, _policy, _staff = await self._facts(view["session_id"], view["project_id"], view["staff_id"], view["source"])
        candidate = Candidate(
            category=view["category"], level=view["level"], actionable=view["request_ref"] is not None,
            session_id=view["session_id"], terminal_id=view["terminal_id"], project_id=view["project_id"], staff_id=view["staff_id"],
        )
        decision = decide(candidate, prefs, self._snapshot(), facts, self._available(), datetime.now(UTC), limiter=self._limiter)
        if decision.seen_now:
            await self.db.execute("UPDATE notifications SET seen_at = ? WHERE id = ?", (_now(), row_id))
        await self._deliver(row_id, decision, merged=False)

    async def release_due(self) -> int:
        """Announce every held row whose time has come; the keeper calls it, and so does a start after downtime."""
        rows = await self.db.fetchall(
            "SELECT id FROM notifications WHERE held_until IS NOT NULL AND held_until <= ? AND resolved_at IS NULL ORDER BY id", (_now(),),
        )
        for row in rows:
            try:
                await self._release_row(int(row["id"]))
            except Exception:  # noqa: BLE001 — one row that cannot be delivered must not keep the rest held
                logger.exception("could not release held notification %s", row["id"])
        return len(rows)

    async def keep_holds(self) -> None:
        """Release held rows as they come due, for as long as the host runs. The table is the schedule,
        so a restart loses nothing: rows that came due while the host was down go out on the first pass."""
        while True:
            self._hold_wake.clear()
            await self.release_due()
            row = await self.db.fetchone("SELECT min(held_until) AS due FROM notifications WHERE held_until IS NOT NULL AND resolved_at IS NULL")
            timeout: float | None = None
            if row is not None and row["due"]:
                timeout = max(0.0, (datetime.fromisoformat(row["due"]) - datetime.now(UTC)).total_seconds()) + 0.01
            with suppress(TimeoutError):
                await asyncio.wait_for(self._hold_wake.wait(), timeout)

    # -- answering -------------------------------------------------------------------------

    async def resolve(self, request_ref: str, resolution: str, *, via: str) -> int:
        """The request is answered (or gone): close every open row for it and tell the clients.

        A row still held for the orchestrator is closed silently — the operator never heard of it.
        Idempotent: the second resolution of the same request changes nothing, so the answer that
        arrived first is the one the entry shows.
        """
        now = _now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT id, event_seq FROM notifications WHERE request_ref = ? AND resolved_at IS NULL", (request_ref,),
            )
            rows = list(await cursor.fetchall())
            await cursor.close()
            if rows:
                await conn.execute(
                    "UPDATE notifications SET resolved_at = ?, resolution = ?, held_until = NULL, updated_at = ?"
                    " WHERE request_ref = ? AND resolved_at IS NULL",
                    (now, resolution[:200], now, request_ref),
                )
        if not rows:
            return 0
        self._hold_wake.set()
        for row in rows:
            if row["event_seq"] is None or self.bus is None:
                continue
            view = await self.get(int(row["id"]))
            try:
                await self.bus.publish(
                    "notify.resolved",
                    {"id": int(row["id"]), "request_ref": request_ref, "resolution": resolution, "via": via, "summary": await self.summary()},
                    project_id=view["project_id"] if view else None, session_id=view["session_id"] if view else None,
                    staff_id=view["staff_id"] if view else None, terminal_id=view["terminal_id"] if view else None,
                )
            except Exception:  # noqa: BLE001
                logger.warning("notify.resolved for %s was not announced", request_ref, exc_info=True)
        return len(rows)

    async def resolve_session(self, session_id: str, resolution: str, *, via: str, prefixes: Iterable[str] = ("ask", "policy")) -> int:
        """Close a session's open requests of the given kinds (its run ended, it was deleted)."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT request_ref FROM notifications WHERE session_id = ? AND request_ref IS NOT NULL AND resolved_at IS NULL", (session_id,),
        )
        wanted = tuple(prefixes)
        closed = 0
        for row in rows:
            ref = str(row["request_ref"])
            if split_ref(ref)[0] in wanted:
                closed += await self.resolve(ref, resolution, via=via)
        return closed

    async def act(self, entry_id: int, action: str, value: str | None = None, *, via: str = "notification", quick_only: bool = False) -> tuple[str | None, NotificationView]:
        """Take one of a notification's actions. Returns the resolution and the entry as it now stands.

        Raises ``LookupError`` for no such entry, :class:`ActionRefused` for an action the entry does
        not offer (or, with ``quick_only``, one that must be taken in the app), and
        :class:`ActionConflict` when the request was already answered: the first answer wins.
        """
        view = await self.get(entry_id)
        if view is None:
            raise LookupError(entry_id)
        offered = {a["id"]: a for a in view["actions"]}
        # Checked first, "open" included: a lock-screen token answers the quick actions and does
        # nothing else, not even marking the entry seen or reading it back.
        if quick_only and not (action in offered and offered[action].get("quick")):
            raise ActionRefused("this request is answered in the app")
        if action == "open":
            await self.mark_seen([entry_id])
            return view["resolved"], await self.get(entry_id) or view
        ref = view["request_ref"]
        if not ref:
            raise ActionRefused("this notification has nothing to answer")
        custom = action == "answer" and bool((value or "").strip()) and split_ref(ref)[0] == "ask"
        if action not in offered and not custom:
            raise ActionRefused(f"this notification offers no {action!r}")
        # One answer at a time per request, so two taps racing each other cannot both reach the
        # resolver: the second waits, then finds the first one's resolution and is told so.
        async with self._answering.setdefault(ref, asyncio.Lock()):
            current = await self.get(entry_id)
            if current is not None and current["resolved"] is not None:
                raise ActionConflict(current["resolved"])
            kind, scope, target = split_ref(ref)
            resolver = self._resolvers.get(kind)
            if resolver is None:
                raise ActionRefused(f"nothing answers {kind!r} requests here")
            try:
                outcome = await resolver(ActionRequest(ref, kind, scope, target, action, value, via, view))
            except ActionConflict as exc:
                # Answered where this host did not hear of it (a restart between the two): the
                # entry learns it now, so it stops asking.
                await self.resolve(ref, exc.resolution, via=via)
                raise
            await self.resolve(ref, outcome.resolution, via=via)
        await self.mark_seen([entry_id])
        return outcome.resolution, await self.get(entry_id) or view

    async def test(self) -> dict[str, Any]:
        """The Settings test button: one notification through every channel there is; returns each one's outcome."""
        lang = self.language()
        view = await self.post(Draft("system", render("test", lang), render("test.body", lang), kind="test", source="test"), force=True)
        return view["delivered"] if view is not None else {}

    # -- the rest of the store --------------------------------------------------------------

    async def mark_seen(self, ids: Sequence[int] | None = None, *, everything: bool = False, session_id: str | None = None) -> int:
        """Mark entries seen: the listed ids, every unseen one, or every one of a session. Returns how many changed."""
        now = _now()
        params: list[str | int]
        announced: list[int] | Literal["all"] | None
        if everything:
            where, params, announced = "seen_at IS NULL", [], "all"
        elif session_id is not None:
            where, params, announced = "seen_at IS NULL AND session_id = ?", [session_id], None
        else:
            wanted = sorted({int(i) for i in ids or ()})
            if not wanted:
                return 0
            where, params, announced = f"seen_at IS NULL AND id IN ({','.join('?' for _ in wanted)})", list(wanted), None
        async with self.db.transaction() as conn:
            cursor = await conn.execute(f"SELECT id FROM notifications WHERE {where}", params)
            changed = [int(r["id"]) for r in await cursor.fetchall()]
            await cursor.close()
            if changed:
                await conn.execute(f"UPDATE notifications SET seen_at = ? WHERE {where}", [now, *params])
        if changed and self.bus is not None:
            try:
                await self.bus.publish("notify.seen", {"ids": announced or changed, "summary": await self.summary()}, session_id=session_id)
            except Exception:  # noqa: BLE001
                logger.warning("notify.seen was not announced", exc_info=True)
        return len(changed)

    async def delete(self, entry_id: int) -> bool:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM notifications WHERE id = ?", (entry_id,))
            removed = cursor.rowcount > 0
            await cursor.close()
        return removed

    async def prune(self, keep_days: int) -> int:
        """Drop old entries that are finished with: seen, or quiet, and not waiting for an answer.

        Quiet rows go unseen or not: they never badge, so an operator who never opens the list
        would otherwise keep every hourly "the heartbeat found nothing" forever.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM notifications WHERE updated_at < ? AND (seen_at IS NOT NULL OR level = 'quiet')"
                " AND (request_ref IS NULL OR resolved_at IS NOT NULL)",
                (cutoff,),
            )
            removed = cursor.rowcount
            await cursor.close()
        return max(removed, 0)

    # -- reading -----------------------------------------------------------------------

    async def get(self, entry_id: int) -> NotificationView | None:
        row = await self.db.fetchone("SELECT * FROM notifications WHERE id = ?", (entry_id,))
        return _view(row, _now()) if row is not None else None

    async def list(self, view: View = "all", *, project_id: str | None = None, before: int | None = None, limit: int = 100) -> NotificationPage:
        """A page of entries, newest first. ``next_before`` is the cursor of the next page, or None at the end."""
        if view not in VIEWS:
            raise ValueError(f"unknown view {view!r}")
        limit = max(1, min(int(limit), LIST_MAX))
        now = _now()
        where = _VIEW_WHERE[view]
        clauses = [where]
        params: list[str | int] = [now] * where.count("?")
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        # Newest occurrence first: a repeat merges into its earlier row and moves its `updated_at`, so
        # ordering by id left a notification that fired a minute ago under hours-old ones. The cursor
        # stays the last entry's id; the page continues below that entry's (updated_at, id).
        if before is not None:
            anchor = await self.db.fetchone("SELECT updated_at FROM notifications WHERE id = ?", (int(before),))
            if anchor is None:
                clauses.append("id < ?")
                params.append(int(before))
            else:
                clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
                params.extend([anchor["updated_at"], anchor["updated_at"], int(before)])
        rows = await self.db.fetchall(f"SELECT * FROM notifications WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC LIMIT ?", [*params, limit + 1])
        entries = [_view(r, now) for r in rows[:limit]]
        return NotificationPage(
            entries=entries,
            next_before=entries[-1]["id"] if len(rows) > limit else None,
            summary=await self.summary(),
        )

    async def summary(self) -> NotificationSummary:
        row = await self.db.fetchone(
            f"SELECT count(*) FILTER (WHERE {_UNSEEN}) AS unseen, count(*) FILTER (WHERE {_NEEDS_YOU}) AS needs_you"
            " FROM notifications WHERE seen_at IS NULL OR request_ref IS NOT NULL",
            [_now()] * (_UNSEEN.count("?") + _NEEDS_YOU.count("?")),
        )
        return NotificationSummary(unseen=int(row["unseen"] or 0) if row else 0, needs_you=int(row["needs_you"] or 0) if row else 0)

    # -- producers wired here ------------------------------------------------------------

    async def on_event(self, session_id: str, event: TurnEvent) -> None:
        """Run-level outcomes that deserve an entry even when the operator is watching."""
        if event.type is EventType.ERROR and event.payload.get("kind") == "run_cap":
            await self.post(Draft(
                "run_failed", "A run hit its spend cap", str(event.payload.get("message") or ""),
                kind="run_cap", tone="warning", session_id=session_id, run_id=event.run_id,
            ))


ROUTED_EVENTS = ("run.finished", "ask.", "permission.", "staff.status", "task.moved", "terminal.notify", "presence", "browser.needs_you", "browser.returned", "browser.closed")


class NotificationRouter:
    """Turns the bus's events into notifications, and answers into resolutions."""

    def __init__(self, service: NotificationService, manager: SessionManager) -> None:
        self.service = service
        self.manager = manager

    def _lang(self) -> str:
        return self.service.language()

    async def handle(self, event: AppEvent) -> None:
        handler = {
            "run.finished": self._run_finished,
            "ask.pending": self._ask_pending,
            "ask.answered": self._answered,
            "permission.pending": self._permission_pending,
            "permission.resolved": self._permission_resolved,
            "staff.status": self._staff_status,
            "task.moved": self._task_moved,
            "terminal.notify": self._terminal_notify,
            "presence": self._presence,
            "browser.needs_you": self._browser_needs_you,
            "browser.returned": self._browser_settled,
            "browser.closed": self._browser_settled,
        }.get(event.type)
        if handler is not None:
            await handler(event)

    @staticmethod
    def _session_link(session_id: str | None) -> str:
        return f"/app/agents/{session_id}" if session_id else ""

    def _telegram_handled(self, payload: Mapping[str, Any]) -> frozenset[str]:
        # The front shows the answer, the question keyboard, the approval buttons and the failure of
        # every session it delivers; the event says whether this one is such a session.
        return frozenset({"telegram"}) if payload.get("telegram") else frozenset()

    async def _run_finished(self, event: AppEvent) -> None:
        p = event.payload
        sid = event.session_id
        if sid:
            # A question cannot outlive the run that asked it. A policy refusal can: its grant is
            # used by the agent's next run, and the dock and the Telegram buttons keep offering it.
            await self.service.resolve_session(sid, "expired", via="run", prefixes=("ask",))
        status = p.get("status")
        title = str(p.get("title") or sid or "")
        # A run that tells news (the main orchestrator relaying a project's report) is worth a line however
        # short it was: the threshold exists for runs the operator started and waited on.
        if status == "completed" and p.get("operator_facing") and (p.get("news") or float(p.get("duration_s") or 0) >= self.service.preferences().finished_min_seconds):
            await self.service.post(Draft(
                "run_finished", render("run.finished", self._lang(), title=title), str(p.get("summary") or ""), kind="run_finished",
                tone="ok", session_id=sid, project_id=event.project_id, run_id=str(p.get("run_id") or "") or None,
                link=str(p.get("link") or "") or self._session_link(sid), dedupe_key=f"run:{sid}", source="run", handled=self._telegram_handled(p),
            ))
        elif status == "failed" and p.get("origin") not in SELF_REPORTING_ORIGINS and not await self._reports_itself(sid):
            body = str(p.get("error") or "").strip() or render("run.failed.body", self._lang())
            await self.service.post(Draft(
                "run_failed", render("run.failed", self._lang(), title=title), body, kind="run_failed", tone="error",
                session_id=sid, project_id=event.project_id, run_id=str(p.get("run_id") or "") or None,
                link=self._session_link(sid), dedupe_key=f"fail:{sid}", source="run", handled=self._telegram_handled(p),
            ))

    async def _presence(self, event: AppEvent) -> None:
        """Opening a session is seeing what it announced. Without this an entry nobody opened in the
        list stays unseen, and every later repeat of it would count as part of the same burst."""
        for session_id in (event.payload.get("newly_attended") or {}).get("sessions") or ():
            await self.service.mark_seen(session_id=str(session_id))

    async def _reports_itself(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        state = self.manager.live_state(session_id)
        metadata = state.metadata if state is not None else {}
        return any(metadata.get(key) for key in SELF_REPORTING_METADATA)

    async def _ask_pending(self, event: AppEvent) -> None:
        p = event.payload
        questions = list(p.get("questions") or [])
        lines = []
        for q in questions:
            lines.append(str(q.get("question") or ""))
            lines.extend(f"· {o.get('label')}" for o in q.get("options") or [])
        actions: list[Action] = []
        only = questions[0] if len(questions) == 1 else None
        if only is not None and not only.get("multi") and 0 < len(only.get("options") or []) <= ASK_BUTTONS_MAX:
            actions = [Action(f"answer:{i}", str(o.get("label") or "")[:60], "primary" if i == 0 else "default", quick=True) for i, o in enumerate(only["options"])]
        actions.append(Action("open", render("open", self._lang()), "ghost"))
        ref = str(p["request_ref"])
        await self.service.post(Draft(
            "question", render("ask", self._lang(), title=str(p.get("title") or "")), "\n".join(line for line in lines if line), kind="ask",
            tone="warning", session_id=event.session_id, project_id=event.project_id, staff_id=event.staff_id,
            run_id=str(p.get("run_id") or "") or None, link=await self.service.link_for(ref, self._session_link(event.session_id)), actions=tuple(actions),
            request_ref=ref, dedupe_key=ref, source="ask", handled=self._telegram_handled(p),
        ))

    async def _answered(self, event: AppEvent) -> None:
        await self.service.resolve(str(event.payload["request_ref"]), "answered", via=str(event.payload.get("via") or ""))

    async def _permission_pending(self, event: AppEvent) -> None:
        p = event.payload
        quick = bool(p.get("quick"))
        lang = self._lang()
        text = str(p.get("text") or "")
        tool = str(p.get("tool") or "")
        actions = (
            Action("allow", render("allow", lang), "primary", quick=quick),
            Action("deny", render("deny", lang), "default", quick=quick),
            Action("open", render("open", lang), "ghost"),
        )
        ref = str(p["request_ref"])
        await self.service.post(Draft(
            "permission", render("permission", lang, title=str(p.get("title") or "")), f"{tool}: {text}" if tool else text,
            kind=str(p.get("kind") or "permission"), tone="warning", level="urgent",
            session_id=event.session_id, project_id=event.project_id, staff_id=event.staff_id, terminal_id=event.terminal_id,
            link=await self.service.link_for(ref, self._session_link(event.session_id)), actions=actions, request_ref=ref, dedupe_key=ref,
            source=str(p.get("kind") or "policy"), handled=self._telegram_handled(p),
        ))

    async def _permission_resolved(self, event: AppEvent) -> None:
        await self.service.resolve(str(event.payload["request_ref"]), str(event.payload.get("decision") or "answered"), via=str(event.payload.get("via") or ""))

    async def _browser_needs_you(self, event: AppEvent) -> None:
        """The agent handed its browser over, or a page asked for what only a person gives. Urgent, and
        answered in the app: the link opens the owner's chat on its Browser tab."""
        p = event.payload
        group = str(p.get("group_id") or "")
        body = "\n".join(part for part in (str(p.get("what") or "").strip(), str(p.get("url") or "").strip()) if part)
        if event.staff_id and event.project_id and not event.session_id:
            link = f"/app/project/{event.project_id}/staff/{event.staff_id}?panel=browser"
        else:
            link = f"{self._session_link(event.session_id)}?panel=browser" if event.session_id else ""
        # A link that opens the Mini App on the owner's Browser tab: from a chat, a lock screen or a
        # phone the operator is not at the app, and "needs you" is a thing to open at once.
        front = self.service._front()
        bot = str(getattr(front, "username", "") or "")
        public = str(getattr(getattr(self.manager, "settings", None), "miniapp_public_url", "") or "").rstrip("/")
        if event.session_id and bot:
            body += f"\n{render('browser.open', self._lang())}: https://t.me/{bot}?startapp=browser_{event.session_id}"
        elif public and link:
            base = public[: -len("/app")] if public.endswith("/app") else public
            body += f"\n{render('browser.open', self._lang())}: {base}{link}"
        ref = f"browser:{group}"
        await self.service.post(Draft(
            "question", render("browser.needs_you", self._lang(), title=str(p.get("title") or "")), body, kind="browser_needs_you", tone="warning", level="urgent",
            session_id=event.session_id, project_id=event.project_id, staff_id=event.staff_id, link=link,
            actions=(Action("open", render("open", self._lang()), "primary"),), request_ref=ref, dedupe_key=ref, source="browser",
        ))

    async def _browser_settled(self, event: AppEvent) -> None:
        """Given back or closed: whatever the browser asked of the operator is over."""
        await self.service.resolve(f"browser:{event.payload.get('group_id') or ''}", "answered", via=str(event.payload.get("by") or ""))

    async def _staff_name(self, staff_id: str | None) -> str:
        if not staff_id:
            return ""
        try:
            row = await self.service.db.fetchone("SELECT name FROM staff WHERE id = ?", (staff_id,))
        except Exception:  # noqa: BLE001 — a name is a nicety; the id still says who
            return staff_id
        return str(row["name"]) if row is not None else staff_id

    async def _staff_status(self, event: AppEvent) -> None:
        status = event.payload.get("status")
        if status not in ("turn_done_unseen", "error"):
            return
        name = await self._staff_name(event.staff_id)
        detail = str(event.payload.get("detail") or event.payload.get("waiting_for") or "")
        common: dict[str, Any] = {
            "session_id": event.session_id, "project_id": event.project_id, "staff_id": event.staff_id,
            "terminal_id": event.terminal_id, "link": self._session_link(event.session_id), "source": f"staff:{event.staff_id}",
        }
        if status == "turn_done_unseen":
            await self.service.post(Draft("staff_turn", render("staff.turn", self._lang(), name=name), detail, kind="staff_turn", tone="ok", dedupe_key=f"staff-turn:{event.staff_id}", **common))
        else:
            await self.service.post(Draft("run_failed", render("staff.failed", self._lang(), name=name), detail, kind="staff_error", tone="error", dedupe_key=f"staff-fail:{event.staff_id}", **common))

    async def _task_moved(self, event: AppEvent) -> None:
        p = event.payload
        if p.get("to") != "review":
            return
        task_id = str(p.get("task_id") or "")
        await self.service.post(Draft(
            "staff_review", render("staff.review", self._lang(), title=str(p.get("title") or task_id)), "", kind="task_review", tone="info",
            project_id=event.project_id, staff_id=event.staff_id, link=f"/app/board/{task_id}" if task_id else "",
            dedupe_key=f"task-review:{task_id}", source="board",
        ))

    async def _terminal_notify(self, event: AppEvent) -> None:
        p = event.payload
        await self.service.post(Draft(
            "agent_notify", str(p.get("title") or "")[:TITLE_MAX], str(p.get("body") or ""), kind="terminal", tone="info",
            terminal_id=event.terminal_id, session_id=event.session_id, project_id=event.project_id, staff_id=event.staff_id,
            source=f"terminal:{event.terminal_id}",
        ))


def _ask_resolver(manager: SessionManager) -> Resolver:
    async def resolve(req: ActionRequest) -> ActionOutcome:
        state = await manager.get_state(req.scope)
        pending = state.pending if state is not None else None
        if pending is None or pending.tool_call_id != req.target:
            raise ActionConflict("answered")
        questions = [q for q in pending.payload.get("questions") or [] if isinstance(q, dict)]
        if req.action.startswith("answer:"):
            if len(questions) != 1:
                raise ActionRefused("this question has more than one part; answer it in the app")
            options = questions[0].get("options") or []
            try:
                index = int(req.action.split(":", 1)[1])
                label = str(options[index].get("label") or "")
            except (ValueError, IndexError, AttributeError) as exc:
                raise ActionRefused(f"no option {req.action!r}") from exc
            answers = [{"question": questions[0].get("question") or "", "selected": [label]}]
        elif req.action == "answer" and (req.value or "").strip():
            first = questions[0].get("question") if questions else ""
            answers = [{"question": first or "", "selected": [], "custom": req.value.strip()}]  # type: ignore[union-attr]
        else:
            raise ActionRefused(f"a question is not answered with {req.action!r}")
        try:
            await manager.answer(req.scope, answers, via=req.via)
        except RuntimeError as exc:
            # Answered a moment ago elsewhere, or the run is still settling: either way, not by this.
            raise ActionConflict("answered") from exc
        return ActionOutcome("answered")

    return resolve


def _policy_resolver(manager: SessionManager) -> Resolver:
    async def resolve(req: ActionRequest) -> ActionOutcome:
        if req.action not in ("allow", "deny"):
            raise ActionRefused(f"a permission is not answered with {req.action!r}")
        state = await manager.get_state(req.scope)
        if state is None:
            raise ActionConflict("withdrawn")
        if req.target not in (state.metadata.get("policy_pending") or {}):
            granted = req.target in (state.metadata.get("policy_grants") or {})
            raise ActionConflict("allow" if granted else "answered")
        if req.action == "allow":
            await manager.grant(req.scope, req.target, via=req.via)
        else:
            await manager.refuse(req.scope, req.target, via=req.via)
        return ActionOutcome(req.action)

    return resolve


NOTIFY_TITLE_MAX = 120
NOTIFY_BODY_MAX = 1_000
NOTIFY_KEY_MAX = 80
NOTIFY_LEVELS: tuple[str, ...] = LEVELS
_NOTIFY_KEY = re.compile(r"^[A-Za-z0-9._:/-]+$")

LEADER_ONLY_METADATA = ("subagent_of", "staff_session_id", "staff_id")
"""Sessions that reach the operator through someone else: a subagent through its leader, a staff
member through its orchestrator. The tool is withheld from them; the hook refuses them as well."""

HELD_BACK_REASONS: dict[str, str] = {
    "present": "the operator has the app open",
    "quiet hours": "quiet hours",
    "muted": "the project is muted",
    "rate limit": "the push limit",
    "repeat": "a repeat of one not yet seen",
    "sent to Telegram": "it went to Telegram",
}
"""Why a channel stayed silent, for the reasons the agent can learn from, in its words. "off" and
"no device" are the operator's arrangement, not something the agent should try to work around."""


class NotifyRefused(ValueError):
    """``Notify`` was not sent: the arguments are wrong, the session may not use it, or its budget is spent."""


@dataclass(frozen=True)
class NotifyFacts:
    """What the ``Notify`` hook needs to know about the calling session."""

    metadata: Mapping[str, Any]
    project_id: str | None = None


def check_notify(title: str, body: str, level: str, link: str, key: str) -> tuple[str, str, Level, str, str]:
    """Validate the tool's arguments; returns them cleaned, or raises :class:`NotifyRefused` naming what is accepted."""
    title, body, link, key = title.strip(), body.strip(), link.strip(), key.strip()
    if not title:
        raise NotifyRefused("title is required: one line saying what happened")
    if len(title) > NOTIFY_TITLE_MAX:
        raise NotifyRefused(f"title is {len(title)} characters; at most {NOTIFY_TITLE_MAX} (put the rest in body)")
    if len(body) > NOTIFY_BODY_MAX:
        raise NotifyRefused(f"body is {len(body)} characters; at most {NOTIFY_BODY_MAX} (point to a file for the rest)")
    if level not in NOTIFY_LEVELS:
        raise NotifyRefused(f"level {level!r} is not one of: {', '.join(NOTIFY_LEVELS)}")
    # A link is opened by a click on the operator's device, so it is either a page of the app or a
    # secure address: never javascript:, a file path, or plain http that a network could rewrite.
    if (link and not (link.startswith("/app/") or link.startswith("https://"))) or any(c.isspace() for c in link):
        raise NotifyRefused("link must be empty (this session), an app path starting with /app/, or an https:// URL")
    if len(key) > NOTIFY_KEY_MAX or (key and not _NOTIFY_KEY.match(key)):
        raise NotifyRefused(f"key must be at most {NOTIFY_KEY_MAX} characters of letters, digits and . _ : / -")
    return title, body, cast(Level, level), link, key


class NotifyBudget:
    """How often one session may call ``Notify``: ``per_session`` within ``window``, and ``urgent_per_hour``.

    Kept in memory: a restart gives every session a fresh budget, which costs at most one more burst
    and saves a table for a limit that exists only against a loop. A call the budget refuses costs
    nothing, and neither does one that failed to post.
    """

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._sent: dict[str, deque[datetime]] = {}
        self._urgent: dict[str, deque[datetime]] = {}

    @staticmethod
    def _trim(times: deque[datetime], now: datetime, window: timedelta) -> deque[datetime]:
        while times and now - times[0] >= window:
            times.popleft()
        return times

    def take(self, session_id: str, urgent: bool, prefs: NotificationsConfig) -> tuple[datetime | None, str, bool]:
        """Spend one call, or return the moment the next is possible, the limit that said no, and
        whether only the urgent limit did (a normal notification would still go)."""
        now = self._clock()
        window = timedelta(minutes=prefs.notify_tool_window_minutes)
        sent = self._trim(self._sent.setdefault(session_id, deque()), now, window)
        hour = timedelta(hours=1)
        urgent_sent = self._trim(self._urgent.setdefault(session_id, deque()), now, hour)
        if len(sent) >= prefs.notify_tool_per_session:
            return sent[len(sent) - prefs.notify_tool_per_session] + window, (
                f"{prefs.notify_tool_per_session} notifications in {prefs.notify_tool_window_minutes} minutes is the limit"
            ), False
        if urgent and len(urgent_sent) >= prefs.notify_tool_urgent_per_hour:
            if prefs.notify_tool_urgent_per_hour == 0:
                return None, "urgent notifications from agents are switched off", True
            return urgent_sent[len(urgent_sent) - prefs.notify_tool_urgent_per_hour] + hour, (
                f"{prefs.notify_tool_urgent_per_hour} urgent notifications an hour is the limit"
            ), True
        sent.append(now)
        if urgent:
            urgent_sent.append(now)
        return None, "", False

    def refund(self, session_id: str, urgent: bool) -> None:
        with suppress(KeyError, IndexError):
            self._sent[session_id].pop()
        if urgent:
            with suppress(KeyError, IndexError):
                self._urgent[session_id].pop()


def _clock_text(moment: datetime, zone: str) -> str:
    try:
        tz = ZoneInfo(zone) if zone else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = moment.astimezone(tz)
    return local.strftime("%H:%M") + ("" if zone else " UTC")


def notify_outcome(view: NotificationView | None) -> str:
    """What happened to a notification, in words the agent that sent it can act on."""
    if view is None:
        return "Not recorded: the operator is reading this session right now."
    delivered = view["delivered"]
    in_app = str(delivered.get("in_app") or "")
    if in_app == "seen: attending":
        return "Recorded: the operator is looking at this session, so nothing was sent; they have seen it."
    if in_app == "recorded: quiet":
        return "Recorded quietly in the notifications: no sound, no badge."
    reached: list[str] = []
    if in_app == "toast":
        reached.append("in the app")
    push = delivered.get("push")
    # The push channel answers ``{"queued": n}`` at once and settles later: a slow push service must
    # not hold the router, so "pushed to n devices" is as much as is known when the tool returns.
    count = int(push.get("sent") or push.get("queued") or 0) if isinstance(push, Mapping) else 0
    if count > 0:
        reached.append(f"pushed to {count} device{'s' if count != 1 else ''}")
    if delivered.get("desktop") == "sent":
        reached.append("on the desktop")
    if delivered.get("telegram") in ("session", "general"):
        reached.append("to Telegram")
    verb = "Updated" if view["count"] > 1 else "Sent"
    if reached:
        head = f"{verb}: {reached[0] if len(reached) == 1 else ', '.join(reached[:-1]) + ' and ' + reached[-1]}."
    else:
        head = f"{verb} in the notifications without a sound."
    held: dict[str, list[str]] = {}
    for channel in ("push", "desktop", "telegram"):
        reason = delivered.get(channel)
        if isinstance(reason, str) and reason.removeprefix("skipped: ") in HELD_BACK_REASONS:
            held.setdefault(HELD_BACK_REASONS[reason.removeprefix("skipped: ")], []).append(channel)
    return head + "".join(f" Not {_listed(channels)}: {why}." for why, channels in held.items())


_CHANNEL_WORDS = {"push": "pushed", "desktop": "on the desktop", "telegram": "to Telegram"}


def _listed(channels: Sequence[str]) -> str:
    words = [_CHANNEL_WORDS[c] for c in channels]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


class AgentNotifier:
    """The service behind the ``Notify`` tool: who may call it, how often, and what the call became.

    Everything else — the matrix, quiet hours, presence, Telegram — is the router's, exactly as for
    any other notification: the tool posts a draft and reports the outcome, it never publishes on its own.
    """

    def __init__(
        self,
        service: NotificationService,
        facts: Callable[[str], Awaitable[NotifyFacts | None]],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service = service
        self._facts = facts
        self.budget = NotifyBudget(clock)

    async def notify(self, *, session_id: str, title: str, body: str = "", level: str = "normal", link: str = "", key: str = "") -> str:
        """Post the agent's notification; returns the outcome in words, or raises :class:`NotifyRefused`."""
        title, body, checked_level, link, key = check_notify(title, body, level, link, key)
        facts = await self._facts(session_id)
        if facts is None:
            raise NotifyRefused("this session is not running here")
        if any(facts.metadata.get(name) for name in LEADER_ONLY_METADATA):
            raise NotifyRefused("Notify is not for this session: your work reaches the operator through the agent that gave it to you")
        urgent = checked_level == "urgent"
        prefs = self.service.preferences()
        next_at, limit, urgent_only = self.budget.take(session_id, urgent, prefs)
        if limit:
            zone = self.service.presence.locale()[1] if self.service.presence is not None else ""
            text = f"Not sent: {limit}" + (f"; the next is possible at {_clock_text(next_at, zone)}." if next_at is not None else ".")
            raise NotifyRefused(text + (" The same with level 'normal' can go now." if urgent_only else ""))
        try:
            view = await self.service.post(Draft(
                "agent_notify", title, body, kind="agent", level=checked_level, tone="info",
                session_id=session_id, project_id=facts.project_id,
                link=link or f"/app/agents/{session_id}",
                dedupe_key=f"agent:{session_id}:{key}" if key else None,
                source=f"agent:{session_id}",
            ))
        except BaseException:
            self.budget.refund(session_id, urgent)
            raise
        return notify_outcome(view)


def format_entries(entries: Iterable[NotificationView]) -> str:
    """The ``/inbox`` digest: one line an entry, newest first."""
    icons = {"ok": "✓", "info": "·", "warning": "⚠️", "error": "❌"}
    lines = []
    for e in entries:
        when = e["at"][5:16].replace("T", " ")
        body = (e.get("body") or "").strip().replace("\n", " ")
        repeat = f" ×{e['count']}" if e["count"] > 1 else ""
        lines.append(f"{icons.get(e['tone'], '·')} {when} **{e['title']}**{repeat}" + (f" — {body[:200]}" if body else ""))
    return "\n".join(lines)


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None
    manager = app.manager

    async def session_metadata(session_id: str) -> Mapping[str, Any] | None:
        state = manager.live_state(session_id)
        if state is not None:
            return state.metadata
        try:
            return (await manager.sessions.get(session_id, TENANT)).metadata
        except Exception:  # noqa: BLE001 — a session that is gone has no marks to route by
            return None

    service = NotificationService(
        app.db, manager.bus, front=lambda: app.front, presence=manager.presence,
        preferences=lambda: app.config.notifications, session_metadata=session_metadata,
    )
    app.notifications = service
    app.extensions["notifications"] = service
    manager.add_sink(service.on_event)
    service.register_resolver("ask", _ask_resolver(manager))
    service.register_resolver("policy", _policy_resolver(manager))
    router = NotificationRouter(service, manager)

    async def on_delete(session_id: str) -> None:
        await service.resolve_session(session_id, "withdrawn", via="deleted", prefixes=tuple(service._resolvers))

    manager.delete_hooks.append(on_delete)

    async def notify_facts(session_id: str) -> NotifyFacts | None:
        state = await manager.get_state(session_id)
        if state is None:
            return None
        return NotifyFacts(metadata=state.metadata, project_id=state.project.id if state.project is not None else None)

    notifier = AgentNotifier(service, notify_facts)
    manager.service_hooks["notify"] = notifier.notify
    tasks = [
        manager.bus.on(EventFilter(types=ROUTED_EVENTS), router.handle, name="notifications"),
        asyncio.create_task(service.keep_holds(), name="notification-holds"),
    ]
    front = app.front
    if front is not None:

        async def cmd_inbox(message, command) -> None:  # type: ignore[no-untyped-def]
            arg = (command.args or "").strip().lower()
            if arg == "clear":
                n = await service.mark_seen(everything=True)
                await message.answer(f"marked {n} entr{'y' if n == 1 else 'ies'} as read")
                return
            page = await service.list("all" if arg == "all" else "unseen", limit=15)
            entries = page["entries"]
            if not entries:
                await message.answer("Inbox: nothing unread." if arg != "all" else "Inbox is empty.")
                return
            text = f"📥 **Inbox** — {page['summary']['unseen']} unread\n\n" + format_entries(entries)
            if arg != "all":
                text += "\n\n_(shown entries are now marked read; /inbox all shows everything)_"
            outbox = TelegramOutbox(front.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)
            for chunk in split_message(text):
                await outbox.send_text(chunk)
            if arg != "all":
                await service.mark_seen([e["id"] for e in entries])  # only after the digest went out

        front.command_hooks["inbox"] = cmd_inbox
    return tasks


__all__ = [
    "CATEGORIES",
    "NOTIFY_BODY_MAX",
    "NOTIFY_TITLE_MAX",
    "LEVELS",
    "TONES",
    "VIEWS",
    "Action",
    "ActionConflict",
    "ActionOutcome",
    "ActionRefused",
    "ActionRequest",
    "AgentNotifier",
    "Category",
    "Channel",
    "Draft",
    "Level",
    "NotificationPage",
    "NotificationRouter",
    "NotificationService",
    "NotificationSummary",
    "NotificationView",
    "NotifyBudget",
    "NotifyFacts",
    "NotifyRefused",
    "ProjectNotifyPolicy",
    "Tone",
    "View",
    "check_notify",
    "format_entries",
    "install",
    "notify_outcome",
    "split_ref",
]
