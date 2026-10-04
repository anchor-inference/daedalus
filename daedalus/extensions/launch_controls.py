"""Bind an authorized launch to its worker before that worker can publish a report."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from daedalus.extensions.runtime_observations import observe_exit
from daedalus.staff_runtime import BoardTask, Started
from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope, one
from daedalus.stores.executions import ACTIVE, AttemptIdentity
from daedalus.stores.phase_clocks import DEFAULT_TIMEOUTS, PhaseClocks
from daedalus.stores.staff import Staff, StaffSession

if TYPE_CHECKING:
    from daedalus.app import Application

launch_attempt: ContextVar[str | None] = ContextVar("launch_attempt", default=None)
launch_capacity_slot: ContextVar[str | None] = ContextVar("launch_capacity_slot", default=None)
launch_resources: ContextVar[dict | None] = ContextVar("launch_resources", default=None)


async def prepare_attempt(app: Application, principal: Principal, member: Staff, task: BoardTask,
                          session: StaffSession, *, fence_token: str,
                          capacity_slot_id: str | None = None) -> AttemptIdentity:
    """Issue only report authority and claim the current contract in one transaction."""
    if task.project_id != member.project_id or session.staff_id != member.id or session.task_id != task.id:
        raise ControlDenied("the worker, session and task must belong to the same project")
    if capacity_slot_id != launch_capacity_slot.get():
        raise ControlDenied("the launch lost its funded capacity slot binding")
    control = ControlStore(app.db)
    scope = Scope("project", member.project_id)
    async with app.db.transaction() as conn:
        await control.authorize(conn, principal, scope, "task.launch", task_id=task.id, effects=("execution.start",))
        row = await one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (task.id,))
        if row is None:
            raise KeyError(task.id)
        grant = await control.issue_worker_grant_in(conn, principal, scope, staff_session_id=session.id,
                                                   expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat())
        worker = Principal(f"staff:{member.id}", "agent", grant["grant_id"], grant["generation"])
        identity = await app.executions.create(conn, attempt_id=launch_attempt.get() or uuid.uuid4().hex, task_id=task.id,
                                           contract_revision=row["contract_revision"], launcher=principal,
                                           worker=worker, staff_session_id=session.id, runtime_kind=session.kind,
                                           fence_token=fence_token, comparison_slot_id=capacity_slot_id)
        await PhaseClocks(app.executions).start(conn, identity, "prepare",
                                                timeout_seconds=DEFAULT_TIMEOUTS["prepare"])
        resource = launch_resources.get()
        if resource is not None:
            from daedalus.stores.resource_profiles import bind_attempt_in  # Lazy: resource profiles are optional.

            if session.kind != "cli":
                raise ControlDenied("in-process workers cannot use a strict attempt resource profile")
            await bind_attempt_in(conn, attempt_id=identity.id, project_id=member.project_id,
                                  host_generation=identity.host_generation, resource=resource)
        return identity


async def observe_bind(app: Application, identity: AttemptIdentity, session: StaffSession,
                       started: Started) -> None:
    """Record the runtime's returned reference, including a worker that finished very quickly."""
    reference = f"session:{started.session_id}" if session.kind == "daedalus" else f"terminal:{started.terminal_id}"
    if (session.kind == "daedalus" and not started.session_id) or (session.kind == "cli" and not started.terminal_id):
        raise ControlDenied("the runtime returned no observed execution reference")
    async with app.db.transaction() as conn:
        generation = await app.executions._host(conn)
        if generation != identity.host_generation:
            raise ControlDenied("the observed execution belongs to a previous host generation")
        row = await one(conn, "SELECT a.*,t.current_attempt_id,s.session_id,s.terminal_id FROM execution_attempts a"
                        " JOIN board_tasks t ON t.id = a.task_id JOIN staff_sessions s ON s.id = a.staff_session_id"
                        " WHERE a.id = ? AND s.id = ?", (identity.id, session.id))
        if (row is None or app.executions._identity(row) != identity or
                not await app.executions.current_binding_in(conn, row)):
            raise ControlDenied("the observed runtime belongs to a superseded attempt")
        if (session.kind == "daedalus" and row["session_id"] != started.session_id) or (session.kind == "cli" and row["terminal_id"] != started.terminal_id):
            raise ControlDenied("the observed reference is not bound to this staff session")
        if row["provider_session_ref"] and row["provider_session_ref"] != reference:
            raise ControlDenied("the execution reference cannot be replaced")
        if session.kind == "cli":
            terminal = await one(conn, "SELECT ptyd_instance FROM terminals WHERE id = ?", (started.terminal_id,))
            if terminal is not None and terminal["ptyd_instance"]:
                await conn.execute("UPDATE execution_attempts SET runtime_instance = ? WHERE id = ? AND runtime_instance IS NULL",
                                   (terminal["ptyd_instance"], identity.id))
        if row["state"] in ACTIVE:
            await app.executions.bind(conn, identity, provider_session_ref=reference)
            await PhaseClocks(app.executions).advance(conn, identity, from_phase="spawn", to_phase="ready")
        elif row["state"] in ("completed", "failed", "cancelled", "recovering") and row["host_generation"] == identity.host_generation:
            # A report or cancellation can commit inside runtime.start before Started is returned.
            # Record the observed reference without reviving the worker or granting another delivery.
            await conn.execute("UPDATE execution_attempts SET provider_session_ref = ? WHERE id = ?",
                               (reference, identity.id))
        else:
            raise ControlDenied("the execution needs explicit recovery before binding")

    if session.kind == "cli":
        terminal = await app.db.fetchone("SELECT ptyd_instance FROM terminals WHERE id = ?", (started.terminal_id,))
        if terminal is not None:
            await observe_exit(app, staff_session_id=session.id, runtime_ref=started.terminal_id,
                               observed_status="exited", runtime_instance=terminal["ptyd_instance"])
