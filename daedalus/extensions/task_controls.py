"""Stop the execution bound to a task, without stopping its later replacement.

A stop request binds the exact run or staff session before dispatch. A runtime's acknowledgement
is not proof it stopped: uncertain or disconnected execution remains visible until observed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, one
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
            target["run_id"] = state.run_id
        elif target["kind"] == "cli":
            if not target["terminal_id"] or app.extensions.get("staff") is None:
                raise ControlConflict("the task execution cannot be reached; inspect its connection")
        else:
            raise ControlConflict("this task runtime cannot stop an execution")
        if target["staff_session_id"]:
            await conn.execute("UPDATE staff_sessions SET pause_requested = 1 WHERE id = ?", (target["staff_session_id"],))
        if target["attempt_id"]:
            await conn.execute("UPDATE execution_attempts SET state = 'cancelled' WHERE id = ? AND state IN ('queued','starting','running','waiting','recovering')", (target["attempt_id"],))
        action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="task.stop", operation="task.stop", payload=target, task_id=task_id, effects=("execution.stop",))
        return {"task_id": task_id, "effect_id": action_id, "state": "queued"}

    result = await ControlStore(app.db).mutate(principal, scope, "task.stop", client_operation_id, expected_entity_revision, Entity("task", task_id), payload, effect, effects=("execution.stop",))
    app.extensions["effects"].notify()
    return result


class TaskStopEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def observe(self, claim: Claim) -> EffectResolution | None:
        target = claim.payload
        if target["kind"] == "daedalus":
            run = await self.app.db.fetchone("SELECT status,session_id FROM runs WHERE id = ?", (target["run_id"],))
            if run is not None and run["session_id"] == target["session_id"] and run["status"] in ("completed", "error", "cancelled"):
                return EffectResolution("completed", {"run_id": target["run_id"], "observed_status": run["status"]})
        else:
            terminals = self.app.extensions.get("terminals")
            if terminals is not None:
                terminal = await terminals.get(target["terminal_id"])
                if terminal["status"] == "exited":
                    return EffectResolution("completed", {"terminal_id": target["terminal_id"], "observed_status": "exited"})
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
