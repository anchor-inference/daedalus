"""Why a staff member's attempt is where it is: one condition per step of its start, each with a reason.

The phase clocks (``stores/phase_clocks.py``) already know when each wait began, when it must end and
how it ended, and the phase sweep stops an attempt whose wait runs out. What nobody showed was that
record: a member stopped at its first-output deadline turned up on the team page as plainly
"exited", and the operator had to read the database to learn that the host had stopped it, at which
step, and that its terminal had been chattering right up to the deadline without producing a word of
output. These conditions are that record read aloud. They are derived on every read from the clocks,
the attempt and its report, so they hold no state of their own and cannot drift from what the sweep
acts on.

Each condition is ``true``, ``false`` or ``unknown``. A ``false`` with a ``deadline`` is a wait still
running (the clock says when the host will give up); a ``false`` without one is a wait that failed.
``unknown`` is a step the attempt has not reached, or never will because an earlier one stopped it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

Status = Literal["true", "false", "unknown"]

# The steps in the order an attempt goes through them, each with the clock phase that times it. The
# login step appears only when the CLI asked for one: for most launches it never happens, and a row
# that always reads "not needed" is a row the operator learns to skip.
STEPS: tuple[tuple[str, str], ...] = (
    ("prepared", "prepare"),
    ("spawned", "spawn"),
    ("authenticated", "auth"),
    ("ready", "ready"),
    ("first_output", "first_output"),
)
ACTIVE_ATTEMPT = frozenset({"queued", "starting", "running", "waiting"})
TURN_ENDED = frozenset({"turn_done_unseen", "idle"})
SUSPENSION_KEY = "staff_suspension:"
"""The kv record of a suspended session (``extensions/staff_suspend.py``): since when, after how long."""

_WAITING_WORDS = {
    "prepared": "preparing the launch",
    "spawned": "starting the runtime",
    "authenticated": "waiting for a login",
    "ready": "waiting for the CLI to be ready",
    "first_output": "no output yet",
}


@dataclass(frozen=True, slots=True)
class Condition:
    name: str
    status: Status
    reason: str
    """A code the app has words for: ``done``, ``waiting``, ``timed_out``, ``pending``,
    ``not_reached``, ``reported``, ``turn_ended``, ``ended``, ``not_yet``, ``suspended``."""
    message: str
    """The same in one English sentence, for the orchestrator and the log."""
    since: str | None = None
    deadline: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "reason": self.reason, "message": self.message,
                "since": self.since, "deadline": self.deadline, "params": self.params}


class Database(Protocol):
    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Any: ...
    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> Any: ...


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds(start: str | None, end: str | None) -> int | None:
    a, b = _parse(start), _parse(end)
    return max(0, round((b - a).total_seconds())) if a is not None and b is not None else None


def _clock_time(stamp: str | None) -> str:
    at = _parse(stamp)
    return at.astimezone(UTC).strftime("%H:%M:%S UTC") if at is not None else "an unknown time"


def derive(
    *,
    clocks: Sequence[Mapping[str, Any]],
    attempt: Mapping[str, Any] | None,
    report: Mapping[str, Any] | None,
    session_status: str,
    session_status_at: str | None = None,
    end_reason: str = "",
    now: datetime | None = None,
) -> list[Condition]:
    """The conditions of one attempt, in step order; empty when there is no attempt to describe.

    ``clocks`` are the attempt's ``attempt_phase_clocks`` rows, ``report`` its newest result receipt.
    A member chatting with the operator outside any task has no attempt, and nothing here applies.
    """
    if attempt is None:
        return []
    now = now or datetime.now(UTC)
    by_phase = {str(row["phase"]): row for row in clocks}
    ordered = sorted(clocks, key=lambda row: str(row["started_at"]))
    live = str(attempt["state"]) in ACTIVE_ATTEMPT
    out: list[Condition] = []
    # Once a step has failed or is still waiting, every later one is out of reach for now.
    stopped = False
    for name, phase in STEPS:
        row = by_phase.get(phase)
        if row is None:
            if phase == "auth":
                continue
            if stopped or not live:
                out.append(Condition(name, "unknown", "not_reached", f"{name}: not reached"))
            else:
                out.append(Condition(name, "unknown", "pending", f"{name}: not yet"))
            continue
        outcome = str(row["outcome"])
        allowed = _seconds(row["started_at"], row["deadline_at"])
        signal = row["last_signal_at"]
        if outcome == "completed":
            # A clock keeps no end time: the next step's start is when this one ended, and the
            # first output is the moment it was seen.
            later = [r for r in ordered if str(r["started_at"]) > str(row["started_at"])]
            ended = row["last_progress_at"] or (later[0]["started_at"] if later else None) or row["started_at"]
            took = _seconds(row["started_at"], ended)
            out.append(Condition(name, "true", "done", f"{name}: done" + (f" in {took} s" if took is not None else ""),
                                 since=ended, params={"took_s": took}))
            continue
        if outcome == "timed_out":
            stopped = True
            params = {"allowed_s": allowed, "last_signal_at": signal}
            heard = (f"; the last signal was at {_clock_time(signal)}, which is not output"
                     if signal and phase == "first_output" else "")
            out.append(Condition(
                name, "false", "timed_out",
                f"{name}: {_WAITING_WORDS[name].removesuffix(' yet')} within {allowed} s; the deadline passed at"
                f" {_clock_time(row['deadline_at'])} and the host stopped the attempt{heard}",
                since=row["deadline_at"], params=params,
            ))
            continue
        # The clock is still running.
        if not live:
            # A wait left open by an attempt that ended some other way: the step was never settled.
            stopped = True
            out.append(Condition(name, "unknown", "not_reached", f"{name}: not settled before the attempt ended",
                                 since=row["started_at"]))
            continue
        stopped = True
        elapsed = _seconds(row["started_at"], now.isoformat())
        out.append(Condition(
            name, "false", "waiting",
            f"{name}: {_WAITING_WORDS[name]} for {elapsed} s, deadline {_clock_time(row['deadline_at'])}",
            since=row["started_at"], deadline=row["deadline_at"],
            params={"elapsed_s": elapsed, "allowed_s": allowed, "last_signal_at": signal},
        ))
    out.append(_reported(attempt, report, live=live, session_status=session_status,
                         session_status_at=session_status_at, end_reason=end_reason))
    return out


def _reported(attempt: Mapping[str, Any], report: Mapping[str, Any] | None, *, live: bool, session_status: str,
              session_status_at: str | None, end_reason: str) -> Condition:
    if report is not None:
        outcome = str(report["outcome"])
        return Condition("reported", "true", "reported", f"reported: {outcome}", since=report["created_at"],
                         params={"outcome": outcome})
    if live and session_status in TURN_ENDED:
        # The member stopped talking without handing anything in: the orchestrator has nothing to
        # read and will not be woken by a report that is never coming.
        return Condition("reported", "false", "turn_ended", "reported: its turn ended without a report",
                         since=session_status_at)
    if live:
        return Condition("reported", "unknown", "not_yet", "reported: not yet")
    state = str(attempt["state"])
    return Condition("reported", "false", "ended",
                     f"reported: the attempt ended {state} without a report" + (f" ({end_reason})" if end_reason else ""),
                     since=attempt["updated_at"], params={"state": state, "end_reason": end_reason})


def blocking(conditions: Sequence[Condition]) -> Condition | None:
    """The one condition the operator should read first: a failed step before a waiting one, since a
    failure is final and a wait may still resolve."""
    failed = [c for c in conditions if c.status == "false" and c.deadline is None]
    if failed:
        return failed[0]
    return next((c for c in conditions if c.status == "false"), None)


def line(conditions: Sequence[Condition]) -> str:
    """One English line for the orchestrator's ``Team(staff)``: only what is not yet true."""
    found = blocking(conditions)
    return f"condition: {found.message}" if found is not None else ""


def suspended(record: Mapping[str, Any] | None, session_status_at: str | None) -> Condition:
    """A member whose idle CLI the host stopped: not a failure, a process that is not running and
    comes back on the next message. Read from the suspension's own record (``staff_suspend``)."""
    minutes = int((record or {}).get("idle_minutes") or 0)
    since = str((record or {}).get("since") or "") or session_status_at
    after = f" after {minutes} min idle" if minutes else " while idle"
    return Condition("running", "false", "suspended",
                     f"running: suspended{after}; the next message or assignment resumes it on the same session",
                     since=since, params={"idle_minutes": minutes})


async def load(db: Database, *, staff_session_id: str, session_status: str, session_status_at: str | None = None,
               end_reason: str = "", now: datetime | None = None) -> list[Condition]:
    """The conditions of the newest attempt of one staff session, and of the session itself when it
    is suspended."""
    out: list[Condition] = []
    if session_status == "suspended":
        row = await db.fetchone("SELECT value FROM kv WHERE key = ?", (SUSPENSION_KEY + staff_session_id,))
        try:
            record = json.loads(row["value"]) if row is not None else None
        except (TypeError, ValueError):
            record = None
        out.append(suspended(record if isinstance(record, dict) else None, session_status_at))
    attempt = await db.fetchone(
        "SELECT id,state,created_at,updated_at FROM execution_attempts WHERE staff_session_id = ?"
        " ORDER BY created_at DESC LIMIT 1", (staff_session_id,))
    if attempt is None:
        return out
    clocks = await db.fetchall(
        "SELECT phase,started_at,deadline_at,last_signal_at,last_progress_at,outcome FROM attempt_phase_clocks"
        " WHERE attempt_id = ?", (attempt["id"],))
    report = await db.fetchone(
        "SELECT outcome,created_at FROM result_receipts WHERE attempt_id = ? ORDER BY created_at DESC LIMIT 1",
        (attempt["id"],))
    return out + derive(clocks=clocks, attempt=attempt, report=report, session_status=session_status,
                        session_status_at=session_status_at, end_reason=end_reason, now=now)


__all__ = ["STEPS", "SUSPENSION_KEY", "Condition", "blocking", "derive", "line", "load", "suspended"]
