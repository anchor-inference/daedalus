"""The companion's goals and its activity log, over the tables of :mod:`daedalus.stores.companion_schema`.

A cadence is written the way a person says it — ``every 2h``, ``30m``, ``daily`` — or as a five-field
cron expression in UTC. The store keeps the two apart (``cron`` or ``interval_minutes``) because they
answer "when next" differently: an interval counts from the last check, so a check that ran late does
not leave the next one due at once; a cron names wall-clock times, so "every weekday at nine" stays at
nine however late the last one ran.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter

from daedalus.stores.database import Database

GOAL_STATUSES = ("active", "paused", "done", "dropped")
TITLE_MAX = 120
BRIEF_MAX = 4000
REPORT_MAX = 4000
SESSIONS_MAX = 20
"""Agent sessions remembered per goal. A goal with more than this has had its oldest agents finish
long ago; the newest are the ones a check looks at."""
ACTIVITY_KEPT = 1000
"""Lines the activity log keeps. The tab shows the latest hundred; the rest is history for a while."""

INTERVAL_RE = re.compile(r"^(?:every\s+)?(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$", re.I)
NAMED_INTERVALS = {"hourly": 60, "daily": 24 * 60, "weekly": 7 * 24 * 60}


class GoalError(ValueError):
    """A goal that cannot be written as asked; the message says what to change."""


@dataclass(frozen=True)
class Cadence:
    """When a goal is checked: a cron expression or an interval, or neither (``Cadence()``)."""

    cron: str | None = None
    interval_minutes: int | None = None

    @property
    def text(self) -> str:
        if self.cron:
            return self.cron
        if self.interval_minutes:
            minutes = self.interval_minutes
            if minutes % (24 * 60) == 0:
                return f"every {minutes // (24 * 60)}d"
            if minutes % 60 == 0:
                return f"every {minutes // 60}h"
            return f"every {minutes}m"
        return ""

    def next_after(self, moment: datetime) -> datetime | None:
        if self.cron:
            return croniter(self.cron, moment).get_next(datetime).astimezone(UTC)
        if self.interval_minutes:
            return moment + timedelta(minutes=self.interval_minutes)
        return None


def parse_cadence(text: str | None, *, min_minutes: int) -> Cadence:
    """Read a cadence as the companion writes it; empty means none. Raises :class:`GoalError`."""
    value = " ".join(str(text or "").split()).lower()
    if not value or value in ("none", "never", "off"):
        return Cadence()
    if value in NAMED_INTERVALS:
        minutes = NAMED_INTERVALS[value]
    elif match := INTERVAL_RE.match(value):
        number, unit = int(match.group(1)), match.group(2)[0]
        minutes = number * {"m": 1, "h": 60, "d": 24 * 60}[unit]
    elif len(value.split()) == 5 and croniter.is_valid(value):
        # A cron that fires more often than the floor is refused like a short interval is: the two
        # gaps between its next three times are the cheapest honest reading of how often it fires.
        start = datetime.now(UTC)
        times = croniter(value, start)
        first, second, third = (times.get_next(datetime) for _ in range(3))
        if min(second - first, third - second) < timedelta(minutes=min_minutes):
            raise GoalError(f"a goal is checked at most every {min_minutes} minutes; {value!r} fires more often")
        return Cadence(cron=value)
    else:
        raise GoalError(f"{text!r} is not a cadence; write 'every 2h', '30m', 'daily' or a five-field cron in UTC")
    if minutes < min_minutes:
        raise GoalError(f"a goal is checked at most every {min_minutes} minutes")
    return Cadence(interval_minutes=minutes)


@dataclass
class Goal:
    id: str
    title: str
    brief: str
    status: str
    cadence: Cadence
    next_check_at: str | None
    last_check_at: str | None
    last_report: str
    last_report_at: str | None
    sessions: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "brief": self.brief,
            "status": self.status,
            "cadence": self.cadence.text,
            "next_check_at": self.next_check_at,
            "last_check_at": self.last_check_at,
            "last_report": self.last_report,
            "last_report_at": self.last_report_at,
            "sessions": list(self.sessions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _goal(row: Any) -> Goal:
    try:
        sessions = [str(s) for s in json.loads(row["sessions_json"] or "[]")]
    except ValueError:
        sessions = []
    return Goal(
        id=row["id"],
        title=row["title"],
        brief=row["brief"],
        status=row["status"],
        cadence=Cadence(cron=row["cron"], interval_minutes=row["interval_minutes"]),
        next_check_at=row["next_check_at"],
        last_check_at=row["last_check_at"],
        last_report=row["last_report"],
        last_report_at=row["last_report_at"],
        sessions=sessions,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _title(text: str) -> str:
    title = " ".join(str(text or "").split())
    if not title:
        raise GoalError("a goal needs a title")
    return title[:TITLE_MAX]


class CompanionStore:
    """Goals and the activity log. Every method is one statement or one transaction."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- goals ---------------------------------------------------------------------------

    async def create_goal(self, *, title: str, brief: str = "", cadence: Cadence | None = None, first_check_at: datetime | None = None) -> Goal:
        now = _now()
        cadence = cadence or Cadence()
        goal_id = "g" + uuid.uuid4().hex[:10]
        # The first check of a goal with a cadence is one cadence from now, not now: the companion
        # that created it is in the middle of a turn about it, and a check due at once would arrive
        # as the very next turn and ask it what it has just said.
        due = first_check_at or cadence.next_after(now)
        await self.db.execute(
            "INSERT INTO companion_goals(id, title, brief, status, cron, interval_minutes, next_check_at, created_at, updated_at)"
            " VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (goal_id, _title(title), str(brief or "").strip()[:BRIEF_MAX], cadence.cron, cadence.interval_minutes, due.isoformat() if due else None, now.isoformat(), now.isoformat()),
        )
        goal = await self.goal(goal_id)
        assert goal is not None
        return goal

    async def goal(self, goal_id: str) -> Goal | None:
        row = await self.db.fetchone("SELECT * FROM companion_goals WHERE id = ?", (goal_id,))
        return _goal(row) if row is not None else None

    async def goals(self, *, statuses: tuple[str, ...] = GOAL_STATUSES) -> list[Goal]:
        marks = ",".join("?" for _ in statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM companion_goals WHERE status IN ({marks})"
            " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, updated_at DESC",
            tuple(statuses),
        )
        return [_goal(row) for row in rows]

    async def update_goal(
        self,
        goal_id: str,
        *,
        title: str | None = None,
        brief: str | None = None,
        status: str | None = None,
        cadence: Cadence | None = None,
        link_session: str | None = None,
    ) -> Goal:
        goal = await self.goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        now = _now()
        if title is not None:
            goal.title = _title(title)
        if brief is not None:
            goal.brief = str(brief).strip()[:BRIEF_MAX]
        if status is not None:
            if status not in GOAL_STATUSES:
                raise GoalError(f"status is one of {', '.join(GOAL_STATUSES)}, not {status!r}")
            goal.status = status
        if cadence is not None:
            goal.cadence = cadence
            next_at = cadence.next_after(now)
            goal.next_check_at = next_at.isoformat() if next_at else None
        if status == "active" and goal.next_check_at is None and goal.cadence.next_after(now) is not None:
            # A goal taken off pause starts its cadence again from now rather than from before the pause.
            goal.next_check_at = goal.cadence.next_after(now).isoformat()  # type: ignore[union-attr]
        if link_session and link_session not in goal.sessions:
            goal.sessions = [*goal.sessions, link_session][-SESSIONS_MAX:]
        await self.db.execute(
            "UPDATE companion_goals SET title = ?, brief = ?, status = ?, cron = ?, interval_minutes = ?, next_check_at = ?, sessions_json = ?, updated_at = ? WHERE id = ?",
            (goal.title, goal.brief, goal.status, goal.cadence.cron, goal.cadence.interval_minutes, goal.next_check_at, json.dumps(goal.sessions), now.isoformat(), goal_id),
        )
        updated = await self.goal(goal_id)
        assert updated is not None
        return updated

    async def delete_goal(self, goal_id: str) -> bool:
        before = await self.goal(goal_id)
        await self.db.execute("DELETE FROM companion_goals WHERE id = ?", (goal_id,))
        return before is not None

    async def report(self, goal_id: str, text: str) -> Goal:
        now = _now().isoformat()
        body = str(text or "").strip()[:REPORT_MAX]
        if not body:
            raise GoalError("a report needs words: what was found, what was done, what is next")
        if await self.goal(goal_id) is None:
            raise KeyError(goal_id)
        await self.db.execute(
            "UPDATE companion_goals SET last_report = ?, last_report_at = ?, last_check_at = ?, updated_at = ? WHERE id = ?",
            (body, now, now, now, goal_id),
        )
        goal = await self.goal(goal_id)
        assert goal is not None
        return goal

    async def due(self, moment: datetime | None = None) -> list[Goal]:
        """Active goals whose next check has come, the longest overdue first."""
        at = (moment or _now()).isoformat()
        rows = await self.db.fetchall(
            "SELECT * FROM companion_goals WHERE status = 'active' AND next_check_at IS NOT NULL AND next_check_at <= ? ORDER BY next_check_at",
            (at,),
        )
        return [_goal(row) for row in rows]

    async def checked(self, goal_id: str, moment: datetime | None = None) -> Goal | None:
        """Mark a check as begun and move the next one on, before the turn runs.

        Moved first, so a turn that fails or a process that restarts mid-turn does not leave the goal
        due again a minute later, checking it in a loop.
        """
        goal = await self.goal(goal_id)
        if goal is None:
            return None
        now = moment or _now()
        next_at = goal.cadence.next_after(now)
        await self.db.execute(
            "UPDATE companion_goals SET last_check_at = ?, next_check_at = ? WHERE id = ?",
            (now.isoformat(), next_at.isoformat() if next_at else None, goal_id),
        )
        return await self.goal(goal_id)

    # -- activity ------------------------------------------------------------------------

    async def log(self, kind: str, title: str, body: str = "", *, session_id: str | None = None, goal_id: str | None = None) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO companion_activity(at, kind, title, body, session_id, goal_id) VALUES (?, ?, ?, ?, ?, ?)",
                (_now().isoformat(), kind, " ".join(title.split())[:200], body.strip()[:2000], session_id, goal_id),
            )
            await conn.execute(
                "DELETE FROM companion_activity WHERE id <= (SELECT max(id) FROM companion_activity) - ?",
                (ACTIVITY_KEPT,),
            )

    async def activity(self, *, limit: int = 100, before: int | None = None) -> list[dict[str, Any]]:
        if before is not None:
            rows = await self.db.fetchall("SELECT * FROM companion_activity WHERE id < ? ORDER BY id DESC LIMIT ?", (before, limit))
        else:
            rows = await self.db.fetchall("SELECT * FROM companion_activity ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]


__all__ = ["GOAL_STATUSES", "Cadence", "CompanionStore", "Goal", "GoalError", "parse_cadence"]
