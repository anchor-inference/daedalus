"""Only host-owned run and daemon exit observations can release an attempt's runtime."""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from datetime import datetime
from typing import TYPE_CHECKING

from daedalus.extensions.resource_runtime import binding, reconcile
from daedalus.stores.attempt_faults import record_fault
from daedalus.stores.comparison_funding import ComparisonFunding
from daedalus.stores.control import ControlDenied, now, one
from daedalus.stores.executions import ACTIVE, AttemptIdentity
from daedalus.stores.lifecycle import record_owned_exit
from daedalus.stores.phase_clocks import DEFAULT_TIMEOUTS, PhaseClocks

if TYPE_CHECKING:
    import aiosqlite

    from daedalus.app import Application
    from daedalus.host.events import EventBus

logger = logging.getLogger(__name__)


async def enter_runtime(app: Application, identity: AttemptIdentity, *, capacity_slot_id: str | None) -> None:
    """Persist the send boundary before calling a runtime that can lose its response."""
    async with app.db.transaction() as conn:
        row, current = await app.executions._check(conn, identity.id, operation="result.submit")
        if current != identity or row["state"] != "queued" or row["runtime_entered_at"] is not None:
            raise ControlDenied("the runtime entry is no longer an unentered current launch")
        if capacity_slot_id is not None:
            await ComparisonFunding(app.db).start_launch_in(conn, capacity_slot_id, identity.id)
        cursor = await conn.execute("UPDATE execution_attempts SET runtime_entered_at = ? WHERE id = ?"
                                    " AND runtime_entered_at IS NULL AND state = 'queued'", (now(), identity.id))
        changed = cursor.rowcount
        await cursor.close()
        if changed != 1:
            raise ControlDenied("the runtime entry boundary changed")


async def observe_no_entry(app: Application, identity: AttemptIdentity, *, staff_session_id: str,
                           reason: str) -> bool:
    """Called only by the host's local pre-entry failure path, never from a worker report."""
    async with app.db.transaction() as conn:
        return await observe_no_entry_in(app, conn, identity, staff_session_id=staff_session_id, reason=reason)


async def observe_no_entry_in(app: Application, conn: aiosqlite.Connection, identity: AttemptIdentity, *,
                              staff_session_id: str, reason: str) -> bool:
    """Attest a queued send boundary as unentered in the caller's safety transaction."""
    generation = await app.executions._host(conn)
    row = await one(conn, "SELECT a.*,s.session_id,s.terminal_id FROM execution_attempts a"
                    " JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?", (identity.id,))
    if (row is None or app.executions._identity(row) != identity or identity.host_generation != generation
            or row["staff_session_id"] != staff_session_id or row["state"] != "queued"
            or row["runtime_entered_at"] is not None or row["provider_session_ref"] is not None
            or row["native_run_id"] is not None or row["session_id"] is not None or row["terminal_id"] is not None):
        return False
    if await one(conn, "SELECT 1 FROM inference_reservations WHERE execution_attempt_id = ?"
                 " OR comparison_slot_id IN (SELECT id FROM comparison_funding_slots WHERE attempt_id = ?)",
                 (identity.id, identity.id)):
        return False
    if await one(conn, "SELECT 1 FROM comparison_funding_slots WHERE attempt_id = ?"
                 " AND launch_started_at IS NOT NULL", (identity.id,)):
        return False
    await conn.execute("INSERT INTO runtime_no_entry_observations(attempt_id,staff_session_id,contract_revision,"
                       "host_generation,runtime_kind,reason,observed_at) VALUES (?,?,?,?,?,?,?)",
                       (identity.id, staff_session_id, identity.contract_revision, generation, row["runtime_kind"],
                        reason[:500], now()))
    await conn.execute("UPDATE execution_attempts SET state = 'failed',updated_at = ? WHERE id = ?", (now(), identity.id))
    await conn.execute("UPDATE staff_sessions SET ended_at = ?,status = 'exited',status_at = ?,end_reason = ?"
                       " WHERE id = ?", (now(), now(), reason[:500], staff_session_id))
    return True


async def admit_native_run(app: Application, staff_session_id: str, session_id: str, run_id: str, *,
                           first_output_s: float = DEFAULT_TIMEOUTS["first_output"]) -> None:
    """Pin a native run before its provider task is scheduled, including the first fast report.

    An admitted run is a ready one: the agent loop is running and its next sign is the model's
    output. The attempt's clock is moved on to wait for that output here, because nothing else does
    for a native run: a command-line member's terminal says when it is ready, a native run never did,
    so its "ready" phase passed its 30 s deadline in the middle of good work and the deadline sweep
    stopped the run, which the member's session showed as nothing more than a runtime exit.
    """
    async with app.db.transaction() as conn:
        identity = await app.executions.check_staff(conn, staff_session_id)
        row = await one(conn, "SELECT a.native_run_id,a.provider_session_ref,s.session_id FROM execution_attempts a"
                        " JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?", (identity.id,))
        session = await one(conn, "SELECT metadata FROM sessions WHERE id = ?", (session_id,))
        if session is None or json.loads(session["metadata"]).get("staff_session_id") != staff_session_id:
            raise ControlDenied("the native session is not owned by this worker")
        parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?",
                           (identity.task_id,))
        if parent is not None and parent["cancel_state"] != "active":
            raise ControlDenied("the task is cancelling and cannot start another run")
        reference = f"session:{session_id}"
        if row["session_id"] not in (None, session_id) or row["provider_session_ref"] not in (None, reference):
            raise ControlDenied("the worker's native session cannot be replaced")
        if row["native_run_id"] and row["native_run_id"] != run_id:
            prior = await one(conn, "SELECT 1 FROM runtime_exit_observations WHERE attempt_id = ? AND runtime_ref = ?",
                              (identity.id, row["native_run_id"]))
            if prior is None:
                raise ControlDenied("the previous native run has no observed exit")
        await conn.execute("UPDATE staff_sessions SET session_id = ? WHERE id = ?", (session_id, staff_session_id))
        await conn.execute("UPDATE execution_attempts SET native_run_id = ?,provider_session_ref = ?,state = 'running',"
                           "updated_at = ? WHERE id = ?", (run_id, reference, now(), identity.id))
        clocks = PhaseClocks(app.executions)
        # Admission can come before the start that binds the attempt returns, so from either phase.
        await clocks.advance(conn, identity, from_phase="spawn", to_phase="ready")
        await clocks.advance(conn, identity, from_phase="ready", to_phase="first_output", timeout_seconds=first_output_s)


async def observe_native_output(app: Application, staff_session_id: str) -> bool:
    """The model of a native run answered: its start is over, and its clock is settled.

    From here a native run is watched the way it was before phase clocks: by the session's silence
    and its own provider timeouts. An idle deadline would stop a run that is only waiting on a long
    command, which says nothing while it runs. False when there was no clock waiting for output."""
    async with app.db.transaction() as conn:
        try:
            identity = await app.executions.check_staff(conn, staff_session_id)
        except ControlDenied:
            return False
        row = await one(conn, "SELECT phase FROM attempt_phase_clocks WHERE attempt_id = ? AND outcome = 'active'",
                        (identity.id,))
        if row is None or row["phase"] not in ("ready", "first_output"):
            return False
        clocks = PhaseClocks(app.executions)
        await clocks.advance(conn, identity, from_phase="ready", to_phase="first_output")
        await clocks.output(conn, identity, idle_timeout_seconds=DEFAULT_TIMEOUTS["idle"])
        await clocks.close(conn, identity)
        return True


async def observe_exit(app: Application, *, staff_session_id: str, runtime_ref: str,
                       observed_status: str, runtime_instance: str | None = None,
                       bus: EventBus | None = None) -> bool:
    """Verify a trusted host callback against the current exact binding before persisting it."""
    event = None
    async with AsyncExitStack() as stack:
        if bus is not None:
            await stack.enter_async_context(bus.transaction_guard())
        async with app.db.transaction() as conn:
            before = await one(conn, "SELECT status,session_id FROM staff_sessions WHERE id = ?", (staff_session_id,))
            generation = await app.executions._host(conn)
            row = await one(conn, "SELECT a.*,t.current_attempt_id,t.contract_revision AS current_contract,"
                            "s.session_id,s.terminal_id,s.staff_id AS worker_staff_id,t.project_id FROM execution_attempts a"
                            " JOIN board_tasks t ON t.id = a.task_id JOIN staff_sessions s ON s.id = a.staff_session_id"
                            " WHERE a.staff_session_id = ? AND ((a.runtime_kind = 'daedalus' AND a.native_run_id = ?)"
                            " OR (a.runtime_kind = 'cli' AND a.provider_session_ref = ?))",
                            (staff_session_id, runtime_ref, f"terminal:{runtime_ref}"))
            # Projection changes refuse further worker writes, but cannot erase physical ownership.
            # The exact old run still has to release its slot before the replacement can start.
            if row is None or row["host_generation"] != generation:
                return False
            if row["runtime_kind"] == "daedalus":
                run = await one(conn, "SELECT session_id,status FROM runs WHERE id = ?", (runtime_ref,))
                if (runtime_ref != row["native_run_id"] or run is None or run["session_id"] != row["session_id"]
                        or observed_status not in ("completed", "error", "cancelled") or run["status"] != observed_status
                        or row["provider_session_ref"] != f"session:{row['session_id']}"):
                    return False
            else:
                terminal = await one(conn, "SELECT status,ptyd_instance FROM terminals WHERE id = ?", (runtime_ref,))
                proof = await one(conn, "SELECT 1 FROM terminal_exit_observations WHERE terminal_id = ? AND runtime_instance = ?",
                                  (runtime_ref, runtime_instance))
                if (runtime_ref != row["terminal_id"] or terminal is None or terminal["status"] != "exited"
                        or observed_status != "exited" or not runtime_instance
                        or proof is None
                        or runtime_instance != row["runtime_instance"] or terminal["ptyd_instance"] != runtime_instance
                        or row["provider_session_ref"] != f"terminal:{runtime_ref}"):
                    return False
            observation = await conn.execute("INSERT OR IGNORE INTO runtime_exit_observations(attempt_id,runtime_ref,provider_session_ref,"
                               "staff_session_id,contract_revision,host_generation,runtime_kind,runtime_instance,"
                               "observed_status,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (row["id"], runtime_ref, row["provider_session_ref"], staff_session_id,
                                row["contract_revision"], generation, row["runtime_kind"], runtime_instance, observed_status, now()))
            fresh_observation = observation.rowcount == 1
            await observation.close()
            owner = await one(conn, "SELECT 1 FROM lifecycle_owners WHERE child_kind = 'execution_attempt' AND child_id = ?"
                              " AND cancel_state IN ('requested','acknowledged','unknown')", (row["id"],))
            if (fresh_observation and row["runtime_kind"] == "daedalus" and observed_status == "error"
                    and owner is None and row["state"] != "recovering"):
                await record_fault(conn, row["id"], "runtime_error")
            detail = "the host observed the owned runtime exit"
            if owner is not None or row["state"] == "recovering":
                # The bare "runtime exit" was all a member stopped by a deadline left behind: the
                # orchestrator read it as a crash and hired around it. The reason goes with the end.
                detail = await _stop_reason(conn, row["id"], observed_status)
                await conn.execute("UPDATE staff_sessions SET ended_at = COALESCE(ended_at,?),status = 'exited',"
                                   "status_at = ?,waiting_for = '',end_reason = CASE WHEN end_reason = '' THEN ?"
                                   " ELSE end_reason END WHERE id = ?", (now(), now(), detail[:500], staff_session_id))
                if row["state"] in (*ACTIVE, "recovering"):
                    await conn.execute("UPDATE execution_attempts SET state = 'cancelled',updated_at = ? WHERE id = ?",
                                       (now(), row["id"]))
                    if fresh_observation:
                        await record_fault(conn, row["id"], "cancelled", cancelled_by="system")
                await record_owned_exit(conn, attempt_id=row["id"], staff_session_id=staff_session_id,
                                        host_generation=generation, provider_session_ref=row["provider_session_ref"])
            elif row["runtime_kind"] == "cli" and row["state"] in ACTIVE:
                # The daemon has proved this exact terminal stopped. A missing report must not
                # strand its task behind a logically running attempt or invent a completed result.
                await conn.execute("UPDATE execution_attempts SET state = 'failed',updated_at = ? WHERE id = ?"
                                   " AND state IN ('queued','starting','running','waiting')",
                                   (now(), row["id"]))
                if fresh_observation:
                    await record_fault(conn, row["id"], "runtime_error")
                await conn.execute("UPDATE staff_sessions SET ended_at = COALESCE(ended_at,?),status = 'exited',"
                                   "status_at = ?,waiting_for = '' WHERE id = ?", (now(), now(), staff_session_id))
            after = await one(conn, "SELECT status FROM staff_sessions WHERE id = ?", (staff_session_id,))
            if (bus is not None and row["runtime_kind"] != "cli" and before is not None
                    and before["status"] != "exited" and after["status"] == "exited"):
                event = await bus.persist_in(conn, "staff.status", {"status": "exited", "previous": before["status"],
                                             "detail": detail, "actor": "system"},
                                             project_id=row["project_id"], staff_id=row["worker_staff_id"],
                                             session_id=before["session_id"])
        if event is not None:
            bus.announce_committed(event)
        if row["runtime_kind"] == "cli":
            if await binding(app, row["id"]) is not None:
                try:
                    await reconcile(app, row["id"])
                except Exception:
                    # The terminal exit remains an exact observation; the resource slot stays
                    # held until a later probe proves the whole attempt group empty.
                    logger.exception("the exited terminal's attempt containment could not be reconciled")
        return True


async def _stop_reason(conn: aiosqlite.Connection, attempt_id: str, observed_status: str) -> str:
    """Why the host stopped an owned runtime, in words for the session's end and its event."""
    clock = await one(conn, "SELECT phase,started_at,deadline_at FROM attempt_phase_clocks"
                      " WHERE attempt_id = ? AND outcome = 'timed_out'", (attempt_id,))
    if clock is not None:
        allowed = datetime.fromisoformat(clock["deadline_at"]) - datetime.fromisoformat(clock["started_at"])
        return (f"the host stopped it: its {clock['phase']} phase passed its deadline of"
                f" {round(allowed.total_seconds())} s; the run ended {observed_status}")
    return f"the host stopped it on a cancellation; the run ended {observed_status}"
