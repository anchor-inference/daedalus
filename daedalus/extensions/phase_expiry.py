"""Fence overdue attempts before one bounded stop request; uncertainty never permits replay."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from daedalus.extensions.runtime_observations import observe_no_entry_in
from daedalus.stores.attempt_faults import record_fault
from daedalus.stores.control import ControlDenied, one
from daedalus.stores.lifecycle import record_owned_no_entry

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)
BATCH_SIZE = 32
STOP_BUDGET_SECONDS = 30


class PhaseExpiry:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def step(self, *, at: datetime | None = None) -> int:
        """Reserve due timeouts atomically; interrupted stop delivery stays unknown after restart."""
        stamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        targets = []
        async with self.app.db.transaction() as conn:
            host = await self.app.executions._host(conn)
            async with conn.execute(
                "SELECT a.*,c.phase,s.terminal_id,s.session_id FROM attempt_phase_clocks c"
                " JOIN execution_attempts a ON a.id = c.attempt_id"
                " LEFT JOIN staff_sessions s ON s.id = a.staff_session_id"
                " WHERE c.outcome = 'active' AND c.deadline_at <= ?"
                " AND a.state IN ('queued','starting','running','waiting','recovering')"
                " ORDER BY c.deadline_at,a.id LIMIT ?", (stamp, BATCH_SIZE),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                # The host owns safety cleanup even after worker authority expires. No deadline
                # signal is physical exit proof, and a prior host's runtime must never be stopped.
                await conn.execute("UPDATE attempt_phase_clocks SET outcome = 'timed_out'"
                                   " WHERE attempt_id = ? AND phase = ? AND outcome = 'active'",
                                   (row["id"], row["phase"]))
                unentered = False
                if row["state"] == "queued" and row["host_generation"] == host:
                    try:
                        identity = self.app.executions._identity(row)
                        unentered = await observe_no_entry_in(
                            self.app, conn, identity, staff_session_id=row["staff_session_id"],
                            reason="attempt phase deadline elapsed before runtime entry",
                        )
                    except ControlDenied:
                        # An incomplete identity cannot establish the local no-entry proof.
                        pass
                if not unentered:
                    await conn.execute("UPDATE execution_attempts SET state = 'recovering',updated_at = ? WHERE id = ?",
                                       (stamp, row["id"]))
                await conn.execute("UPDATE staff_sessions SET pause_requested = 1 WHERE id = ?", (row["staff_session_id"],))
                await record_fault(conn, row["id"], "runtime_error")
                owner = await one(conn, "SELECT * FROM lifecycle_owners WHERE child_kind = 'execution_attempt'"
                                  " AND child_id = ? AND cancel_state = 'active'", (row["id"],))
                if owner is None:
                    continue
                runtime_ref = row["native_run_id"] if row["runtime_kind"] == "daedalus" else row["terminal_id"]
                expected_ref = (f"session:{row['session_id']}" if row["runtime_kind"] == "daedalus"
                                else f"terminal:{row['terminal_id']}")
                reachable = (row["host_generation"] == host and bool(runtime_ref)
                             and row["provider_session_ref"] == expected_ref
                             and (row["runtime_kind"] == "daedalus" or bool(row["runtime_instance"])))
                await conn.execute("UPDATE lifecycle_owners SET cancel_state = 'unknown',updated_at = ?"
                                   " WHERE child_kind = 'execution_attempt' AND child_id = ? AND cancel_state = 'active'",
                                   (stamp, row["id"]))
                if unentered:
                    await record_owned_no_entry(conn, attempt_id=row["id"])
                    continue
                if reachable:
                    targets.append({**dict(owner), "runtime_ref": runtime_ref, "staff_session_id": row["staff_session_id"],
                                    "host_generation": row["host_generation"], "runtime_kind": row["runtime_kind"],
                                    "provider_session_ref": row["provider_session_ref"], "runtime_instance": row["runtime_instance"]})
        stop_deadline = asyncio.get_running_loop().time() + STOP_BUDGET_SECONDS
        for target in targets:
            try:
                service = self.app.extensions.get("lifecycle")
                remaining = stop_deadline - asyncio.get_running_loop().time()
                if service is not None and remaining > 0:
                    # A slow stop must not delay deadline fencing for every other attempt. Targets
                    # outside this delivery budget stay unknown, without an automatic resend.
                    async with asyncio.timeout(remaining):
                        await service.stop_expired(target)
            except Exception:
                logger.exception("expired attempt stop remains unconfirmed")
            finally:
                async with self.app.db.transaction() as conn:
                    await conn.execute("UPDATE lifecycle_owners SET cancel_state = 'unknown',updated_at = ?"
                                       " WHERE child_kind = 'execution_attempt' AND child_id = ?"
                                       " AND cancel_state IN ('requested','acknowledged')", (stamp, target["child_id"]))
        return len(rows)

    async def sweep(self) -> None:
        while True:
            try:
                await self.step()
            except Exception:
                logger.exception("attempt deadline sweep failed")
            await asyncio.sleep(5)


async def install(app: Application) -> list[asyncio.Task[None]]:
    service = PhaseExpiry(app)
    app.extensions["phase_expiry"] = service
    return [asyncio.create_task(service.sweep(), name="attempt-phase-expiry")]
