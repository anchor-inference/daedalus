"""Attempt-fenced phase clocks; liveness signals never count as useful output."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import aiosqlite

from daedalus.stores.control import ControlDenied, one

if TYPE_CHECKING:
    from daedalus.stores.executions import AttemptIdentity, ExecutionStore

Phase = Literal["prepare", "spawn", "auth", "ready", "first_output", "idle"]
PHASES: tuple[Phase, ...] = ("prepare", "spawn", "auth", "ready", "first_output", "idle")
DEFAULT_TIMEOUTS: dict[Phase, float] = {
    "prepare": 120, "spawn": 120, "auth": 600, "ready": 30,
    "first_output": 300, "idle": 300,
}


class PhaseClocks:
    def __init__(self, executions: ExecutionStore) -> None:
        self.executions = executions

    async def _check(self, conn: aiosqlite.Connection, identity: AttemptIdentity) -> None:
        _, current = await self.executions._check(conn, identity.id, operation="result.submit")
        if current != identity:
            raise ControlDenied("the clock belongs to another execution identity")

    async def start(self, conn: aiosqlite.Connection, identity: AttemptIdentity, phase: Phase,
                    *, timeout_seconds: float, at: datetime | None = None) -> None:
        """Open one phase for the currently owned attempt; a closed phase cannot reopen."""
        if phase not in PHASES or timeout_seconds <= 0:
            raise ValueError("a known phase and positive timeout are required")
        await self._check(conn, identity)
        stamp = (at or datetime.now(UTC)).astimezone(UTC)
        if await one(conn, "SELECT 1 FROM attempt_phase_clocks WHERE attempt_id = ? AND outcome = 'timed_out'",
                     (identity.id,)) is not None:
            raise ControlDenied("a timed-out attempt needs reconciliation")
        if await one(conn, "SELECT 1 FROM attempt_phase_clocks WHERE attempt_id = ? AND phase = ?",
                     (identity.id, phase)) is not None:
            raise ControlDenied("this attempt phase was already recorded")
        current = await one(conn, "SELECT phase FROM attempt_phase_clocks WHERE attempt_id = ? AND outcome = 'active'",
                            (identity.id,))
        if current is not None:
            await conn.execute("UPDATE attempt_phase_clocks SET outcome = 'completed' WHERE attempt_id = ? AND phase = ?",
                               (identity.id, current["phase"]))
        await conn.execute("INSERT INTO attempt_phase_clocks(attempt_id,phase,started_at,deadline_at,outcome)"
                           " VALUES (?,?,?,?,'active')",
                           (identity.id, phase, stamp.isoformat(),
                            (stamp + timedelta(seconds=timeout_seconds)).isoformat()))

    async def heartbeat(self, conn: aiosqlite.Connection, identity: AttemptIdentity,
                        *, at: datetime | None = None) -> None:
        """Record transport liveness without extending a deadline or satisfying first output."""
        await self._check(conn, identity)
        stamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        await conn.execute("UPDATE attempt_phase_clocks SET last_signal_at = ?"
                           " WHERE attempt_id = ? AND outcome = 'active' AND (last_signal_at IS NULL OR last_signal_at < ?)",
                           (stamp, identity.id, stamp))

    async def advance(self, conn: aiosqlite.Connection, identity: AttemptIdentity, *,
                      from_phase: Phase, to_phase: Phase, timeout_seconds: float | None = None) -> bool:
        """Move only the expected active phase; a late observer cannot rewind another signal."""
        await self._check(conn, identity)
        current = await one(conn, "SELECT phase FROM attempt_phase_clocks WHERE attempt_id = ? AND outcome = 'active'",
                            (identity.id,))
        if current is None or current["phase"] != from_phase:
            return False
        await self.start(conn, identity, to_phase,
                         timeout_seconds=timeout_seconds or DEFAULT_TIMEOUTS[to_phase])
        return True

    async def output(self, conn: aiosqlite.Connection, identity: AttemptIdentity,
                     *, idle_timeout_seconds: float, at: datetime | None = None) -> None:
        """Only actual output clears first-output waiting; further output renews idle work."""
        await self._check(conn, identity)
        stamp = (at or datetime.now(UTC)).astimezone(UTC)
        current = await one(conn, "SELECT phase FROM attempt_phase_clocks WHERE attempt_id = ? AND outcome = 'active'",
                            (identity.id,))
        if current is None or current["phase"] not in ("first_output", "idle"):
            raise ControlDenied("this attempt is not waiting for output")
        if current["phase"] == "first_output":
            await conn.execute("UPDATE attempt_phase_clocks SET last_progress_at = ?"
                               " WHERE attempt_id = ? AND phase = 'first_output'", (stamp.isoformat(), identity.id))
            await self.start(conn, identity, "idle", timeout_seconds=idle_timeout_seconds, at=stamp)
            await conn.execute("UPDATE attempt_phase_clocks SET last_progress_at = ?"
                               " WHERE attempt_id = ? AND phase = 'idle'", (stamp.isoformat(), identity.id))
        else:
            if idle_timeout_seconds <= 0:
                raise ValueError("a positive idle timeout is required")
            await conn.execute("UPDATE attempt_phase_clocks SET last_progress_at = ?,deadline_at = ?"
                               " WHERE attempt_id = ? AND phase = 'idle' AND outcome = 'active'",
                               (stamp.isoformat(), (stamp + timedelta(seconds=idle_timeout_seconds)).isoformat(),
                                identity.id))

    async def expire(self, conn: aiosqlite.Connection, identity: AttemptIdentity,
                     *, at: datetime | None = None) -> Phase | None:
        """Mark a due clock once; the caller decides whether observation permits stopping work."""
        await self._check(conn, identity)
        stamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        row = await one(conn, "SELECT phase FROM attempt_phase_clocks WHERE attempt_id = ?"
                        " AND outcome = 'active' AND deadline_at <= ?", (identity.id, stamp))
        if row is None:
            return None
        await conn.execute("UPDATE attempt_phase_clocks SET outcome = 'timed_out'"
                           " WHERE attempt_id = ? AND phase = ? AND outcome = 'active'",
                           (identity.id, row["phase"]))
        return row["phase"]

    async def close(self, conn: aiosqlite.Connection, identity: AttemptIdentity) -> None:
        """Settle the active clock with the attempt's terminal result transaction."""
        await self._check(conn, identity)
        await conn.execute("UPDATE attempt_phase_clocks SET outcome = 'completed'"
                           " WHERE attempt_id = ? AND outcome = 'active'", (identity.id,))
