"""One database owner and exact worker attempts; old callbacks cannot publish new work.

The host keeps the fence token in its runtime. Only its digest is durable. A restart
advances the generation and retains interrupted attempts for explicit reconciliation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from daedalus.stores.control import ControlDenied, ControlStore, Principal, Scope, canonical, now, one
from daedalus.stores.database import Database
from daedalus.stores.lifecycle import admit_child

ACTIVE = ("queued", "starting", "running", "waiting")


@dataclass(frozen=True, slots=True)
class AttemptIdentity:
    id: str
    task_id: str
    contract_revision: int
    host_generation: int
    principal: Principal


class ExecutionStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.control = ControlStore(db)
        self.generation: int | None = None
        self._lease: int | None = None

    def acquire(self) -> None:
        """Hold the database's runtime lease until shutdown, before opening its stores."""
        if self._lease is not None:
            return
        path = self.db.path.with_suffix(self.db.path.suffix + ".execution.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt  # Lazy: this lock implementation exists only on Windows.

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # Lazy: this lock implementation exists only on Unix hosts.

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise RuntimeError("another runtime owns this database") from None
        self._lease = descriptor

    def release(self) -> None:
        if self._lease is not None:
            os.close(self._lease)
            self._lease = None
        self.generation = None

    async def boot(self) -> int:
        if self._lease is None:
            raise RuntimeError("the runtime lease is required before boot")
        if self.generation is not None:
            return self.generation
        async with self.db.transaction() as conn:
            row = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
            generation = int(json.loads(row["value"])) + 1 if row else 1
            await conn.execute("INSERT INTO kv(key,value) VALUES ('execution_host_generation',?)"
                               " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (canonical(generation),))
            # A process may already have sent work before it disappeared. Recovery is an observation,
            # never permission to run that work again or call an unreachable process stopped.
            await conn.execute("UPDATE execution_attempts SET state = 'recovering',updated_at = ?"
                               " WHERE state IN ('queued','starting','running','waiting')", (now(),))
        self.generation = generation
        return generation

    async def _host(self, conn: aiosqlite.Connection) -> int:
        row = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
        if self._lease is None or self.generation is None or row is None or json.loads(row["value"]) != self.generation:
            raise ControlDenied("this runtime no longer owns the execution generation")
        return self.generation

    @staticmethod
    def _scope(project_id: str | None) -> Scope:
        return Scope("project", project_id) if project_id else Scope("global", "global")

    @staticmethod
    def _identity(row: aiosqlite.Row) -> AttemptIdentity:
        if not row["actor_id"] or not row["grant_id"] or row["grant_generation"] is None:
            raise ControlDenied("the attempt has no host-attested worker authority")
        return AttemptIdentity(row["id"], row["task_id"], int(row["contract_revision"]),
                               int(row["host_generation"]), Principal(row["actor_id"], "agent",
                                                                      row["grant_id"], row["grant_generation"]))

    async def create(self, conn: aiosqlite.Connection, *, attempt_id: str, task_id: str,
                     contract_revision: int, launcher: Principal, worker: Principal, staff_session_id: str,
                     runtime_kind: str, fence_token: str) -> AttemptIdentity:
        """Claim in the authorized launch command's transaction, before starting the provider."""
        generation = await self._host(conn)
        if runtime_kind not in ("daedalus", "cli") or len(fence_token) < 24 or not attempt_id:
            raise ValueError("a runtime and an unguessable fence token are required")
        task = await one(conn, "SELECT project_id,contract_revision,current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
        member = await one(conn, "SELECT staff_id,task_id,kind,ended_at FROM staff_sessions WHERE id = ?", (staff_session_id,))
        if task is None or member is None:
            raise KeyError(task_id)
        if member["task_id"] != task_id or member["kind"] != runtime_kind or member["ended_at"] or worker.origin_class != "agent" or worker.actor_id != f"staff:{member['staff_id']}":
            raise ControlDenied("the worker session does not own this task")
        if task["contract_revision"] != contract_revision:
            raise ControlDenied("the task contract changed before launch")
        if await one(conn, "SELECT 1 FROM task_contract_versions WHERE task_id = ? AND contract_revision = ?", (task_id, contract_revision)) is None:
            raise ControlDenied("the immutable task contract is missing")
        await self.control.authorize(conn, launcher, self._scope(task["project_id"]), "task.launch",
                                     task_id=task_id, effects=("execution.start",))
        await self.control.authorize(conn, worker, self._scope(task["project_id"]), "result.submit", task_id=task_id)
        if task["current_attempt_id"]:
            previous = await one(conn, "SELECT state,native_run_id,runtime_instance FROM execution_attempts WHERE id = ?", (task["current_attempt_id"],))
            if previous is not None and previous["state"] in (*ACTIVE, "recovering"):
                raise ControlDenied("the previous execution must be stopped or reconciled first")
            if previous is not None:
                proof = await one(conn, "SELECT 1 FROM runtime_exit_observations WHERE attempt_id = ?"
                                  " AND (runtime_ref = ? OR runtime_instance = ?) LIMIT 1",
                                  (task["current_attempt_id"], previous["native_run_id"], previous["runtime_instance"]))
                if proof is None:
                    raise ControlDenied("the previous execution has no host-observed runtime exit")
        await conn.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                           " fence_token_hash,state,created_at,updated_at,actor_id,grant_id,grant_generation,"
                           " staff_session_id,runtime_kind) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
                           (attempt_id, task_id, contract_revision, generation, hashlib.sha256(fence_token.encode()).hexdigest(),
                            now(), now(), worker.actor_id, worker.grant_id, worker.grant_generation, staff_session_id, runtime_kind))
        await conn.execute("UPDATE board_tasks SET current_attempt_id = ?,accepted_result_id = NULL,"
                           " accepted_contract_revision = NULL,acceptance_state = '' WHERE id = ?", (attempt_id, task_id))
        if task["project_id"]:
            await admit_child(conn, control=self.control, principal=launcher, parent_kind="task", parent_id=task_id,
                              project_id=task["project_id"], child_kind="execution_attempt", child_id=attempt_id)
        return AttemptIdentity(attempt_id, task_id, contract_revision, generation, worker)

    async def _check(self, conn: aiosqlite.Connection, attempt_id: str, *, operation: str) -> tuple[aiosqlite.Row, AttemptIdentity]:
        generation = await self._host(conn)
        row = await one(conn, "SELECT a.*,t.project_id,t.current_attempt_id,t.contract_revision AS current_contract,"
                        " s.task_id AS session_task,s.ended_at,s.staff_id FROM execution_attempts a"
                        " JOIN board_tasks t ON t.id = a.task_id LEFT JOIN staff_sessions s ON s.id = a.staff_session_id"
                        " WHERE a.id = ?", (attempt_id,))
        if row is None:
            raise ControlDenied("the execution attempt is missing")
        identity = self._identity(row)
        if row["state"] not in ACTIVE or identity.host_generation != generation or row["current_attempt_id"] != attempt_id or row["current_contract"] != identity.contract_revision:
            raise ControlDenied("the execution attempt or contract has been superseded")
        if row["session_task"] != identity.task_id or row["ended_at"] or identity.principal.actor_id != f"staff:{row['staff_id']}":
            raise ControlDenied("the execution session is no longer owned")
        await self.control.authorize(conn, identity.principal, self._scope(row["project_id"]), operation, task_id=identity.task_id)
        return row, identity

    async def check_staff(self, conn: aiosqlite.Connection, staff_session_id: str, *, operation: str = "result.submit") -> AttemptIdentity:
        """Resolve only after the ingress authenticated this staff session through the host."""
        row = await one(conn, "SELECT a.id FROM execution_attempts a JOIN board_tasks t ON t.current_attempt_id = a.id"
                        " WHERE a.staff_session_id = ?", (staff_session_id,))
        if row is None:
            raise ControlDenied("the staff session has no current execution attempt")
        _, identity = await self._check(conn, row["id"], operation=operation)
        return identity

    async def bind(self, conn: aiosqlite.Connection, identity: AttemptIdentity, *, provider_session_ref: str,
                   state: str = "running") -> None:
        if not provider_session_ref or state not in ("starting", "running", "waiting"):
            raise ValueError("an observed provider reference and active state are required")
        row, current = await self._check(conn, identity.id, operation="result.submit")
        if current != identity or (row["provider_session_ref"] and row["provider_session_ref"] != provider_session_ref):
            raise ControlDenied("the provider binding cannot be replaced")
        await conn.execute("UPDATE execution_attempts SET provider_session_ref = ?,state = ?,updated_at = ? WHERE id = ?",
                           (provider_session_ref, state, now(), identity.id))

    async def complete(self, conn: aiosqlite.Connection, identity: AttemptIdentity, *, outcome: str) -> None:
        """Close in the same transaction as the worker's immutable result receipt."""
        states = {"complete": "completed", "partial": "completed", "failed": "failed",
                  "needs_input": "waiting", "cancelled": "cancelled"}
        if outcome not in states:
            raise ValueError("a structured result outcome is required")
        _, current = await self._check(conn, identity.id, operation="result.submit")
        if current != identity:
            raise ControlDenied("the result belongs to another execution identity")
        await conn.execute("UPDATE execution_attempts SET state = ?,updated_at = ? WHERE id = ?",
                           (states[outcome], now(), identity.id))

    async def ingest(self, identity: AttemptIdentity, fence_token: str, event: dict[str, Any],
                     write: Callable[[aiosqlite.Connection], Awaitable[None]]) -> bool:
        """Atomically fence a worker callback; rejected events remain observable in quarantine."""
        async with self.db.transaction() as conn:
            try:
                row, current = await self._check(conn, identity.id, operation="result.submit")
                if current != identity or not hmac.compare_digest(row["fence_token_hash"], hashlib.sha256(fence_token.encode()).hexdigest()):
                    raise ControlDenied("the callback's execution identity does not match")
            except ControlDenied as exc:
                await conn.execute("INSERT INTO quarantined_attempt_events(attempt_id,event_json,reason,created_at) VALUES (?,?,?,?)",
                                   (identity.id, canonical(event), str(exc), now()))
                return False
            await write(conn)
            return True
