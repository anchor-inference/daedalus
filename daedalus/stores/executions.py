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
        async with conn.execute("SELECT a.id,a.state FROM execution_attempts a WHERE a.task_id = ?"
                                " AND NOT EXISTS (SELECT 1 FROM runtime_exit_observations e"
                                " WHERE e.attempt_id = a.id AND e.contract_revision = a.contract_revision"
                                " AND e.host_generation = a.host_generation AND e.staff_session_id = a.staff_session_id"
                                " AND e.provider_session_ref = a.provider_session_ref AND e.runtime_kind = a.runtime_kind"
                                " AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                                " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance"
                                " AND a.provider_session_ref = 'terminal:' || e.runtime_ref)))", (task_id,)) as cursor:
            previous = await cursor.fetchone()
        if previous is not None:
            if previous["state"] in (*ACTIVE, "recovering"):
                raise ControlDenied("the previous execution must be stopped or reconciled first")
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

    async def check_inference(self, conn: aiosqlite.Connection, session_id: str,
                              run_id: str | None) -> AttemptIdentity | None:
        """An admitted native worker can infer only while its exact physical run remains owned.

        A done report may precede the final model reply; that same run can finish its reply,
        but cancellation, a new contract or a withdrawn approval cannot start another call.
        """
        original_session, identity = session_id, session_id
        seen = set()
        worker = None
        while identity:
            if identity in seen or len(seen) >= 256:
                raise ControlDenied("the inference session ownership is cyclic or too deep")
            seen.add(identity)
            session = await one(conn, "SELECT metadata FROM sessions WHERE id = ?", (identity,))
            if session is None:
                raise ControlDenied("the inference session no longer exists")
            metadata = json.loads(session["metadata"] or "{}")
            async with conn.execute("SELECT id FROM staff_sessions WHERE session_id = ?", (identity,)) as cursor:
                owners = await cursor.fetchall()
            if len(owners) > 1:
                raise ControlDenied("the inference session has ambiguous worker ownership")
            if owners:
                worker = owners[0]["id"]
                if metadata.get("staff_session_id") not in (None, worker):
                    raise ControlDenied("the inference session names another worker")
                break
            if metadata.get("staff_session_id"):
                raise ControlDenied("the inference worker has no host binding")
            identity = str(metadata.get("subagent_of") or "")
        if worker is None:
            return None
        generation = await self._host(conn)
        row = await one(conn, "SELECT a.*,t.project_id,t.current_attempt_id,t.contract_revision AS current_contract,"
                        "s.staff_id,s.task_id AS session_task,s.session_id,s.ended_at FROM execution_attempts a"
                        " JOIN board_tasks t ON t.id = a.task_id JOIN staff_sessions s ON s.id = a.staff_session_id"
                        " WHERE a.staff_session_id = ? AND a.id = t.current_attempt_id", (worker,))
        if (row is None or row["host_generation"] != generation or row["runtime_kind"] != "daedalus"
                or row["state"] not in (*ACTIVE, "completed", "failed")
                or row["current_contract"] != row["contract_revision"] or row["ended_at"]
                or row["session_task"] != row["task_id"] or row["session_id"] != identity
                or row["provider_session_ref"] != f"session:{identity}"):
            raise ControlDenied("the inference worker or contract is no longer owned")
        admitted = self._identity(row)
        if admitted.principal.actor_id != f"staff:{row['staff_id']}":
            raise ControlDenied("the inference actor does not own the worker")
        await self.control.attest(conn, admitted.principal, self._scope(row["project_id"]), task_id=row["task_id"])
        native = await one(conn, "SELECT session_id,status FROM runs WHERE id = ?", (row["native_run_id"],))
        if native is None or native["session_id"] != identity or native["status"] != "running":
            raise ControlDenied("the admitted native run is no longer running")
        if original_session == identity:
            if run_id is not None and row["native_run_id"] != run_id:
                raise ControlDenied("the inference belongs to another native run")
        else:
            child = await one(conn, "SELECT session_id,status FROM runs WHERE id = ?", (run_id,))
            if child is None or child["session_id"] != original_session or child["status"] != "running":
                raise ControlDenied("the child inference has no current owned run")
        exit_proof = await one(conn, "SELECT 1 FROM runtime_exit_observations WHERE attempt_id = ?"
                               " AND runtime_ref = ? AND host_generation = ?",
                               (admitted.id, row["native_run_id"], generation))
        cancelled = await one(conn, "SELECT 1 FROM lifecycle_owners WHERE child_kind = 'execution_attempt'"
                              " AND child_id = ? AND cancel_state != 'active' LIMIT 1", (admitted.id,))
        if exit_proof is not None or cancelled is not None:
            raise ControlDenied("the owned inference run has stopped or is cancelling")
        return admitted

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
