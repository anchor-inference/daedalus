"""Only host-owned run and daemon exit observations can release an attempt's runtime."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from daedalus.stores.control import ControlDenied, now, one
from daedalus.stores.executions import ACTIVE
from daedalus.stores.lifecycle import record_owned_exit

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.events import EventBus


async def admit_native_run(app: Application, staff_session_id: str, session_id: str, run_id: str) -> None:
    """Pin a native run before its provider task is scheduled, including the first fast report."""
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
            await conn.execute("INSERT OR IGNORE INTO runtime_exit_observations(attempt_id,runtime_ref,provider_session_ref,"
                               "staff_session_id,contract_revision,host_generation,runtime_kind,runtime_instance,"
                               "observed_status,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (row["id"], runtime_ref, row["provider_session_ref"], staff_session_id,
                                row["contract_revision"], generation, row["runtime_kind"], runtime_instance, observed_status, now()))
            owner = await one(conn, "SELECT 1 FROM lifecycle_owners WHERE child_kind = 'execution_attempt' AND child_id = ?"
                              " AND cancel_state IN ('requested','acknowledged','unknown')", (row["id"],))
            if owner is not None or row["state"] == "recovering":
                await conn.execute("UPDATE staff_sessions SET ended_at = COALESCE(ended_at,?),status = 'exited',"
                                   "status_at = ?,waiting_for = '' WHERE id = ?", (now(), now(), staff_session_id))
                if row["state"] in (*ACTIVE, "recovering"):
                    await conn.execute("UPDATE execution_attempts SET state = 'cancelled',updated_at = ? WHERE id = ?",
                                       (now(), row["id"]))
                await record_owned_exit(conn, attempt_id=row["id"], staff_session_id=staff_session_id,
                                        host_generation=generation, provider_session_ref=row["provider_session_ref"])
            elif row["runtime_kind"] == "cli" and row["state"] in ACTIVE:
                # The daemon has proved this exact terminal stopped. A missing report must not
                # strand its task behind a logically running attempt or invent a completed result.
                await conn.execute("UPDATE execution_attempts SET state = 'failed',updated_at = ? WHERE id = ?"
                                   " AND state IN ('queued','starting','running','waiting')",
                                   (now(), row["id"]))
                await conn.execute("UPDATE staff_sessions SET ended_at = COALESCE(ended_at,?),status = 'exited',"
                                   "status_at = ?,waiting_for = '' WHERE id = ?", (now(), now(), staff_session_id))
            after = await one(conn, "SELECT status FROM staff_sessions WHERE id = ?", (staff_session_id,))
            if (bus is not None and row["runtime_kind"] != "cli" and before is not None
                    and before["status"] != "exited" and after["status"] == "exited"):
                event = await bus.persist_in(conn, "staff.status", {"status": "exited", "previous": before["status"],
                                             "detail": "the host observed the owned runtime exit", "actor": "system"},
                                             project_id=row["project_id"], staff_id=row["worker_staff_id"],
                                             session_id=before["session_id"])
        if event is not None:
            bus.announce_committed(event)
        return True
