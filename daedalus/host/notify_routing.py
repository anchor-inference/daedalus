"""Where one notification goes, decided once, by a function that does no I/O.

Everything the decision depends on arrives as a value: the notification (a :class:`Candidate`),
the operator's preferences, what the operator is looking at (a presence snapshot), what Telegram
can do for it (:class:`TelegramFacts`) and the time. That makes the matrix, the levels, quiet hours,
mutes, presence and the Telegram rules one table of tests rather than a behaviour only a running
host shows. Each channel's reason lands in the notification's ``delivered``, which is how "why did
my phone not buzz" gets an answer.

The rules, in the order they apply:

1. ``force`` (the test button) reaches every channel that exists, whatever else holds.
2. The operator is looking at the thing: an answer is not recorded at all, anything else is
   recorded as seen, and a request waiting for them is a toast (the dock beside it shows it).
3. A quiet notification is a record, nothing more.
4. The matrix: a channel whose cell is off stays off; an ``urgent`` cell only carries urgent ones.
   The app's cell off means the entry is recorded as seen.
5. A muted project says nothing below urgent.
6. A window in front of the operator makes push and the desktop redundant.
7. Quiet hours hold back push, the desktop and Telegram below urgent.
7a. A conversation the operator follows in their Chrome sounds there instead: what would have been
   pushed or raised on the desktop is shown by the extension, once, and the two are skipped. The
   extension used to announce every turn's end by itself, beside the app's own push or desktop
   notification of the same end, so each one was heard twice. The rules below hold for it as they
   hold for the two it replaces.
8. Telegram: what the front or the producer already delivered is ``handled``; with no bot there is
   no Telegram at all; an orchestrated project's items are its orchestrator's to post, and the
   router never posts into a project's topic; a session detached from Telegram (a site session)
   stays off it at every level; a session otherwise gets its topic, and a notice with no session and
   no project goes to General.
9. What went to Telegram is not pushed, raised on the desktop or shown in Chrome as well.
10. A repeat of an entry the operator has not seen yet makes no second sound unless its level rose.
11. A staff member's request in an orchestrated project is held for the orchestrator first.
12. Rate limits: per session, and one ceiling for everything.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daedalus.config import NotificationsConfig, parse_quiet_hours
from daedalus.host.presence import PresenceSnapshot

TelegramRoute = Literal["off", "handled", "none", "orchestrator", "project", "detached", "session", "general"]
"""``session`` and ``general`` are the two the router sends itself; ``handled`` went out already."""

SENDS_TO_TELEGRAM: frozenset[str] = frozenset({"session", "general"})
REACHES_TELEGRAM: frozenset[str] = frozenset({"handled", "session", "general"})


@dataclass(frozen=True, slots=True)
class Candidate:
    """A notification about to be recorded, reduced to what the decision reads."""

    category: str
    level: str
    actionable: bool = False
    """It carries a request the operator can answer (``request_ref``)."""
    session_id: str | None = None
    terminal_id: str | None = None
    project_id: str | None = None
    staff_id: str | None = None
    handled: frozenset[str] = frozenset()
    chrome: bool = False
    """The conversation is one the operator follows in their Chrome, and that Chrome is connected."""
    merged: bool = False
    """It repeats an entry with the same dedupe key that the operator has not seen yet."""
    level_rose: bool = False
    """The repeat raised the entry's level, which is worth a sound again."""
    force: bool = False


@dataclass(frozen=True, slots=True)
class TelegramFacts:
    front: bool = False
    """A bot is bound. Telegram is optional; without one nothing here mentions it."""
    detached: bool = False
    """The session is kept off Telegram (a site session), which no level overrides."""
    orchestrated: bool = False
    """The notification's project has an orchestrator switched on."""
    from_orchestrator: bool = False
    """The notification comes from that orchestrator itself, not from its staff."""


@dataclass(frozen=True, slots=True)
class Channels:
    """What exists to deliver through, independent of this notification."""

    push: bool = False
    """A push channel is registered and has somewhere to send."""


@dataclass(frozen=True, slots=True)
class Decision:
    record: bool = True
    seen_now: bool = False
    toast: bool = False
    push: bool = False
    desktop: bool = False
    chrome: bool = False
    telegram: TelegramRoute = "off"
    hold_until: datetime | None = None
    more: int = 0
    """Notifications the rate limit swallowed for this scope since the last one that sounded."""
    reasons: dict[str, str] = field(default_factory=dict)


def in_quiet_hours(spec: str, zone: str, now: datetime) -> bool:
    """Whether ``now`` (aware) falls inside ``spec`` read in ``zone``; an unknown zone reads as UTC.

    The comparison is made on the wall clock of the zone, so a window keeps its local meaning across
    a daylight-saving change: 23:00–07:00 is still eleven at night to seven in the morning.
    """
    window = parse_quiet_hours(spec) if spec else None
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    try:
        tz = ZoneInfo(zone) if zone else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    minute = local.hour * 60 + local.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def muted(preferences: NotificationsConfig, project_id: str | None, now: datetime) -> bool:
    if not project_id or project_id not in preferences.muted_projects:
        return False
    until = preferences.muted_projects[project_id]
    if not until:
        return True
    try:
        end = datetime.fromisoformat(until)
    except ValueError:
        return True  # a mute that cannot be read is kept rather than silently lifted
    if end.tzinfo is None:
        end = end.replace(tzinfo=now.tzinfo)
    return now < end


class RateLimiter:
    """Push and desktop budgets: ``per_scope`` within ``window`` for one session, ``per_hour`` for all.

    What the budget refuses is counted, and the next notification that sounds for that scope carries
    the count ("+3 more"), so a burst is one sound and a number rather than silence.
    """

    def __init__(self, per_scope: int, window: timedelta, per_hour: int) -> None:
        self.per_scope = per_scope
        self.window = window
        self.per_hour = per_hour
        self._scopes: dict[str, deque[datetime]] = {}
        self._all: deque[datetime] = deque()
        self._swallowed: dict[str, int] = {}

    def configure(self, per_scope: int, window: timedelta, per_hour: int) -> None:
        self.per_scope, self.window, self.per_hour = per_scope, window, per_hour

    def take(self, scope: str, now: datetime) -> tuple[bool, int]:
        """Spend one unit of ``scope``'s budget. Returns whether it may sound, and the count it carries."""
        times = self._scopes.setdefault(scope, deque())
        while times and now - times[0] >= self.window:
            times.popleft()
        while self._all and now - self._all[0] >= timedelta(hours=1):
            self._all.popleft()
        if len(times) >= self.per_scope or len(self._all) >= self.per_hour:
            self._swallowed[scope] = self._swallowed.get(scope, 0) + 1
            return False, 0
        times.append(now)
        self._all.append(now)
        return True, self._swallowed.pop(scope, 0)


def _scope_attended(presence: PresenceSnapshot, c: Candidate) -> bool:
    # The most specific thing the notification is about: a session or a terminal when it names one,
    # the project only when it names neither — a project view open on the desk is not the operator
    # reading one particular staff member's question.
    if c.session_id or c.terminal_id:
        return presence.attending(session_id=c.session_id, terminal_id=c.terminal_id)
    return presence.attending(project_id=c.project_id)


def decide(
    c: Candidate,
    preferences: NotificationsConfig,
    presence: PresenceSnapshot,
    telegram: TelegramFacts,
    channels: Channels,
    now: datetime,
    *,
    limiter: RateLimiter | None = None,
    hold_seconds: int = 0,
) -> Decision:
    reasons: dict[str, str] = {}
    urgent = c.level == "urgent"
    handled = "telegram" in c.handled

    if c.force:
        route: TelegramRoute = "general" if telegram.front else "none"
        return Decision(
            toast=True, push=channels.push, desktop=presence.desktop, telegram=route,
            reasons={"in_app": "toast", "push": "test" if channels.push else "skipped: no device",
                     "desktop": "test" if presence.desktop else "skipped: no launcher",
                     "telegram": "general" if telegram.front else "skipped: no bot"},
        )

    def kept(route: TelegramRoute) -> TelegramRoute:
        return "handled" if handled else route

    if _scope_attended(presence, c):
        if c.actionable:
            return Decision(toast=True, telegram=kept("off"), reasons={"in_app": "toast", "push": "skipped: attending", "desktop": "skipped: attending", "telegram": "handled" if handled else "skipped: attending"})
        if c.category == "run_finished":
            return Decision(record=False, reasons={"in_app": "skipped: attending"})
        return Decision(seen_now=True, telegram=kept("off"), reasons={"in_app": "seen: attending", "push": "skipped: attending", "desktop": "skipped: attending", "telegram": "handled" if handled else "skipped: attending"})

    if c.level == "quiet":
        return Decision(telegram=kept("off"), reasons={"in_app": "recorded: quiet", "push": "skipped: quiet", "desktop": "skipped: quiet", "telegram": "handled" if handled else "skipped: quiet"})

    cells = preferences.cells(c.category)

    def on(cell: str) -> bool:
        return cell == "on" or (cell == "urgent" and urgent)

    toast = on(cells.in_app)
    seen_now = not toast
    reasons["in_app"] = "toast" if toast else "seen: off"
    push = on(cells.push)
    reasons["push"] = "" if push else "skipped: off"
    desktop = on(cells.desktop)
    reasons["desktop"] = "" if desktop else "skipped: off"
    wants_telegram = on(cells.telegram)
    telegram_block = "" if wants_telegram else "skipped: off"

    if not urgent and muted(preferences, c.project_id, now):
        toast = False
        reasons["in_app"] = "recorded: muted"
        for name in ("push", "desktop"):
            reasons[name] = reasons[name] or "skipped: muted"
        push = desktop = False
        telegram_block = telegram_block or "skipped: muted"

    if presence.present:
        for name, wanted in (("push", push), ("desktop", desktop)):
            if wanted:
                reasons[name] = "skipped: present"
        push = desktop = False

    if not urgent and in_quiet_hours(preferences.quiet_hours, presence.tz, now):
        for name, wanted in (("push", push), ("desktop", desktop)):
            if wanted:
                reasons[name] = "skipped: quiet hours"
        push = desktop = False
        telegram_block = telegram_block or "skipped: quiet hours"

    chrome = c.chrome and (push or desktop)
    if chrome:
        for name, wanted in (("push", push), ("desktop", desktop)):
            if wanted:
                reasons[name] = "skipped: shown in Chrome"
        push = desktop = False
        reasons["chrome"] = ""
    elif c.chrome:
        reasons["chrome"] = reasons.get("desktop") or reasons.get("push") or "skipped: off"

    if desktop and not presence.desktop:
        desktop, reasons["desktop"] = False, "skipped: no launcher"
    if push and not channels.push:
        push, reasons["push"] = False, "skipped: no device"

    if handled:
        route, reasons["telegram"] = "handled", "handled"
    elif not telegram.front:
        route, reasons["telegram"] = "none", "skipped: no bot"
    elif telegram_block:
        route, reasons["telegram"] = "off", telegram_block
    elif telegram.orchestrated:
        route, reasons["telegram"] = "orchestrator", "skipped: the orchestrator's topic"
    elif c.session_id and telegram.detached:
        route, reasons["telegram"] = "detached", "skipped: kept off Telegram"
    elif c.session_id:
        route, reasons["telegram"] = "session", "session"
    elif c.project_id:
        route, reasons["telegram"] = "project", "skipped: a project's topic"
    else:
        route, reasons["telegram"] = "general", "general"

    if preferences.telegram_covers_push and route in REACHES_TELEGRAM:
        for name, wanted in (("push", push), ("desktop", desktop), ("chrome", chrome)):
            if wanted:
                reasons[name] = "skipped: sent to Telegram"
        push = desktop = chrome = False

    if c.merged and not c.level_rose:
        for name, wanted in (("push", push), ("desktop", desktop), ("chrome", chrome)):
            if wanted:
                reasons[name] = "skipped: repeat"
        push = desktop = chrome = False
        if route in SENDS_TO_TELEGRAM:
            route, reasons["telegram"] = "off", "skipped: repeat"

    hold_until = None
    if c.actionable and c.staff_id and telegram.orchestrated and not telegram.from_orchestrator and hold_seconds > 0:
        hold_until = now + timedelta(seconds=hold_seconds)

    more = 0
    if (push or desktop or chrome) and limiter is not None and hold_until is None:
        allowed, more = limiter.take(c.session_id or c.terminal_id or c.project_id or "", now)
        if not allowed:
            for name, wanted in (("push", push), ("desktop", desktop), ("chrome", chrome)):
                if wanted:
                    reasons[name] = "skipped: rate limit"
            push = desktop = chrome = False

    for name, wanted in (("push", push), ("desktop", desktop), ("chrome", chrome)):
        if wanted:
            reasons[name] = "sent"
    return Decision(record=True, seen_now=seen_now, toast=toast, push=push, desktop=desktop, chrome=chrome, telegram=route, hold_until=hold_until, more=more, reasons=reasons)


__all__ = [
    "REACHES_TELEGRAM",
    "SENDS_TO_TELEGRAM",
    "Candidate",
    "Channels",
    "Decision",
    "RateLimiter",
    "TelegramFacts",
    "TelegramRoute",
    "decide",
    "in_quiet_hours",
    "muted",
]
