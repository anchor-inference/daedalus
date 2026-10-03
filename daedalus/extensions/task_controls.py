"""Stop the execution bound to a task, without stopping its later replacement.

A stop request binds the exact run or staff session before dispatch. A runtime's acknowledgement
is not proof it stopped: uncertain or disconnected execution remains visible until observed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, now, one
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


async def queue_stop(app: Application, task_id: str, principal: Principal, *, client_operation_id: str, expected_entity_revision: int, reason: str = "") -> dict[str, Any]:
    row = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if row is None:
        raise KeyError(task_id)
    scope = Scope("project", row["project_id"]) if row["project_id"] else Scope("global", "global")
    payload = {"task_id": task_id, "reason": reason}

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        task = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        assert task is not None
        async with conn.execute("SELECT id FROM effect_outbox WHERE kind = 'task.launch' AND state = 'pending'"
                                " AND json_extract(payload_json,'$.control.task_id') = ?", (task_id,)) as cursor:
            waiting = await cursor.fetchall()
        if waiting:
            ids = [row["id"] for row in waiting]
            await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?,completed_at = ?"
                               " WHERE kind = 'task.launch' AND state = 'pending'"
                               " AND json_extract(payload_json,'$.control.task_id') = ?",
                               (reason or "the operator stopped the queued launch", now(), task_id))
            return {"task_id": task_id, "cancelled_launches": ids, "state": "completed"}
        async with conn.execute("SELECT id,staff_id,kind,session_id,terminal_id FROM staff_sessions WHERE task_id = ? AND ended_at IS NULL", (task_id,)) as cursor:
            staff = await cursor.fetchall()
        if len(staff) > 1:
            raise ControlConflict("multiple executions claim this task; reconcile ownership before stopping")
        target: dict[str, Any] = {"task_id": task_id, "attempt_id": task["current_attempt_id"], "reason": reason}
        if staff:
            target.update(dict(staff[0]))
            target["staff_session_id"] = target.pop("id")
        else:
            target.update({"kind": "daedalus", "session_id": task["session_id"], "staff_session_id": None, "terminal_id": None})
        if target["kind"] == "daedalus":
            state = app.manager.live_state(target["session_id"]) if target["session_id"] else None
            if state is None or not state.running or not state.run_id:
                raise ControlConflict("this task has no active native run to stop")
            if not staff and task["run_id"] != state.run_id:
                raise ControlConflict("the session is running other work; this task cannot stop it")
            if target["attempt_id"]:
                attempt = await one(conn, "SELECT native_run_id,staff_session_id,provider_session_ref FROM execution_attempts WHERE id = ?",
                                    (target["attempt_id"],))
                if (attempt is None or attempt["native_run_id"] != state.run_id
                        or attempt["staff_session_id"] != target["staff_session_id"]
                        or attempt["provider_session_ref"] != f"session:{target['session_id']}"):
                    raise ControlConflict("the task's exact native run cannot be reached")
            target["run_id"] = state.run_id
        elif target["kind"] == "cli":
            if not target["terminal_id"] or app.extensions.get("staff") is None:
                raise ControlConflict("the task execution cannot be reached; inspect its connection")
        else:
            raise ControlConflict("this task runtime cannot stop an execution")
        if target["staff_session_id"]:
            await conn.execute("UPDATE staff_sessions SET pause_requested = 1 WHERE id = ?", (target["staff_session_id"],))
        if target["attempt_id"]:
            # Until physical termination is observed, this execution still prevents a replacement.
            # Marking it cancelled at request time used to permit two workers on the same task.
            await conn.execute("UPDATE execution_attempts SET state = 'recovering',updated_at = ?"
                               " WHERE id = ? AND state IN ('queued','starting','running','waiting','recovering')",
                               (now(), target["attempt_id"]))
        action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="task.stop", operation="task.stop", payload=target, task_id=task_id, effects=("execution.stop",))
        return {"task_id": task_id, "effect_id": action_id, "state": "queued"}

    result = await ControlStore(app.db).mutate(principal, scope, "task.stop", client_operation_id, expected_entity_revision, Entity("task", task_id), payload, effect, effects=("execution.stop",))
    app.extensions["effects"].notify()
    return result


class TaskStopEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def completed(self, target: dict[str, Any], evidence: dict[str, Any]) -> EffectResolution | None:
        if target.get("attempt_id"):
            async with self.app.db.transaction() as conn:
                generation = await self.app.executions._host(conn)
                proof = await one(conn, "SELECT e.attempt_id FROM runtime_exit_observations e"
                                  " JOIN execution_attempts a ON a.id = e.attempt_id"
                                  " JOIN board_tasks t ON t.current_attempt_id = a.id"
                                  " WHERE e.attempt_id = ? AND e.host_generation = ?"
                                  " AND e.contract_revision = a.contract_revision AND t.contract_revision = a.contract_revision"
                                  " AND e.staff_session_id = ? AND e.provider_session_ref = a.provider_session_ref"
                                  " AND e.runtime_ref = ?",
                                  (target["attempt_id"], generation, target["staff_session_id"],
                                   target.get("run_id") if target["kind"] == "daedalus" else target["terminal_id"]))
                if proof is None:
                    return None
                await conn.execute("UPDATE execution_attempts SET state = 'cancelled',updated_at = ?"
                                   " WHERE id = ? AND task_id = ? AND state = 'recovering'"
                                   " AND EXISTS(SELECT 1 FROM board_tasks WHERE id = ? AND current_attempt_id = ?)",
                                   (now(), target["attempt_id"], target["task_id"], target["task_id"], target["attempt_id"]))
        return EffectResolution("completed", evidence)

    async def observe(self, claim: Claim) -> EffectResolution | None:
        target = claim.payload
        if target["kind"] == "daedalus":
            run = await self.app.db.fetchone("SELECT status,session_id FROM runs WHERE id = ?", (target["run_id"],))
            if run is not None and run["session_id"] == target["session_id"] and run["status"] in ("completed", "error", "cancelled"):
                return await self.completed(target, {"run_id": target["run_id"], "observed_status": run["status"]})
        else:
            terminals = self.app.extensions.get("terminals")
            if terminals is not None:
                terminal = await terminals.get(target["terminal_id"])
                if terminal["status"] == "exited":
                    return await self.completed(target, {"terminal_id": target["terminal_id"], "observed_status": "exited"})
        return None

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        if await self.observe(claim) is not None:
            return EffectOutcome("completed")
        target = claim.payload
        task = await self.app.db.fetchone("SELECT current_attempt_id FROM board_tasks WHERE id = ?", (target["task_id"],))
        if task is None or task["current_attempt_id"] != target["attempt_id"]:
            return EffectOutcome("failed", "the task execution has been replaced")
        if target["kind"] == "daedalus":
            state = self.app.manager.live_state(target["session_id"])
            if state is None or state.run_id != target["run_id"]:
                return EffectOutcome("unknown", "the original run cannot be observed")
            await check(claim)
            if not await self.app.manager.stop_run(target["session_id"], target["run_id"]):
                if await self.observe(claim) is not None:
                    return EffectOutcome("completed")
                return EffectOutcome("unknown", "the bound run changed before stopping")
        else:
            team = self.app.extensions.get("staff")
            if team is None:
                return EffectOutcome("unknown", "the staff runtime is not available")
            async with team.execution_lock(target["staff_id"]):
                live = await team.live(target["staff_session_id"])
                if live is None or live.session.task_id != target["task_id"] or live.terminal_id != target["terminal_id"]:
                    return EffectOutcome("failed", "the staff execution no longer belongs to this task")
                await check(claim)
                await team.runtime(live.staff).stop(live)
        if await self.observe(claim) is not None:
            return EffectOutcome("completed")
        return EffectOutcome("unknown", "stop requested; execution termination is not yet observed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        return await self.observe(claim)
