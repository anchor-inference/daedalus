"""A queued launch retains its authenticated command until capacity and ownership are proven."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.launch_controls import launch_attempt
from daedalus.extensions.orchestrator_domain import dependency_readiness
from daedalus.host.launch_queue import Entry
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, digest, one
from daedalus.stores.executions import ACTIVE
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


def task_digest(task: Any) -> str:
    return digest({field: task[field] for field in ("project_id", "title", "brief_json", "depends_on", "contract_revision", "assignee_staff_id")})


def member_digest(member: Any) -> str:
    return digest({field: member[field] for field in ("project_id", "harness", "agent", "model", "effort", "permission_mode", "default_folder_id", "isolation", "instructions", "role_revision")})


async def queue_launch(app: Application, task_id: str, principal: Principal, *, staff_id: str,
                       client_operation_id: str, expected_entity_revision: int,
                       resume_from: str | None = None) -> dict[str, Any]:
    row = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if row is None:
        raise KeyError(task_id)
    if not row["project_id"]:
        raise ControlConflict("a worker task needs a project")
    scope = Scope("project", row["project_id"])
    payload = {"staff_id": staff_id, "resume_from": resume_from}

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        task = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        member = await one(conn, "SELECT * FROM staff WHERE id = ?", (staff_id,))
        assert task is not None
        if member is None or member["project_id"] != task["project_id"] or member["archived_at"]:
            raise ControlDenied("the worker is not an active member of this project")
        if task["status"] not in ("todo", "blocked"):
            raise ControlConflict("reopen the task before launching a new attempt")
        if await one(conn, "SELECT 1 FROM task_contract_versions WHERE task_id = ? AND contract_revision = ?",
                     (task_id, task["contract_revision"])) is None:
            raise ControlConflict("the task needs an immutable contract before launch")
        if task["current_attempt_id"]:
            prior = await one(conn, "SELECT state FROM execution_attempts WHERE id = ?", (task["current_attempt_id"],))
            if prior is not None and prior["state"] in (*ACTIVE, "recovering"):
                raise ControlConflict("stop or reconcile the previous execution before launching again")
        if await one(conn, "SELECT 1 FROM effect_outbox WHERE kind = 'task.launch' AND state IN ('pending','claimed','unknown')"
                     " AND json_extract(payload_json,'$.control.task_id') = ?", (task_id,)):
            raise ControlConflict("this task already has a queued or uncertain launch")
        await conn.execute("UPDATE board_tasks SET assignee_staff_id = ? WHERE id = ?", (staff_id, task_id))
        assigned = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        action_id = uuid.uuid5(uuid.NAMESPACE_URL, f"effect:{mutation.receipt_id}:task.launch").hex
        attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, f"attempt:{action_id}").hex
        target = {"task_id": task_id, "staff_id": staff_id, "resume_from": resume_from,
                  "previous_attempt_id": task["current_attempt_id"], "attempt_id": attempt_id,
                  "task_digest": task_digest(assigned), "member_digest": member_digest(member)}
        await OutboxStore.enqueue(conn, mutation, principal, kind="task.launch", operation="task.launch",
                                  payload=target, task_id=task_id, effects=("execution.start",))
        return {"task_id": task_id, "effect_id": action_id, "state": "queued"}

    result = await ControlStore(app.db).mutate(principal, scope, "task.launch", client_operation_id,
                                              expected_entity_revision, Entity("task", task_id), payload,
                                              effect, effects=("execution.start",))
    app.extensions["effects"].notify()
    return result


class TaskLaunchEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def validate(self, claim: Claim) -> None:
        target = claim.payload
        task = await self.app.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (target["task_id"],))
        member = await self.app.db.fetchone("SELECT * FROM staff WHERE id = ?", (target["staff_id"],))
        if task is None or member is None or member["archived_at"] or task_digest(task) != target["task_digest"] or member_digest(member) != target["member_digest"]:
            raise ControlDenied("the task contract or worker configuration changed after the launch command")
        if task["current_attempt_id"] not in (target["previous_attempt_id"], target["attempt_id"]):
            raise ControlDenied("another attempt replaced this launch command")
        if task["status"] not in ("todo", "blocked", "doing"):
            raise ControlDenied("the task was closed or handed in before launch")

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        team = self.app.extensions.get("staff")
        if team is None:
            return EffectOutcome("deferred", "the staff runtime is not ready")
        try:
            await self.validate(claim)
            member = await team.member(claim.payload["staff_id"])
            task = await team.task(claim.payload["task_id"])
            assert task is not None
            if task.missing():
                return EffectOutcome("failed", "the task brief is incomplete")
            project = await team.project(member.project_id)
            folder = team.folder_for(project, member, task)
            if claim.payload["resume_from"]:
                await team._resume_source(member, task, folder, claim.payload["resume_from"])
            async with self.app.db.transaction() as conn:
                readiness = await dependency_readiness(conn, task.id)
            if not readiness["ready"]:
                return EffectOutcome("deferred", "the task waits for current accepted dependency results")
        except (ControlDenied, ValueError, KeyError) as exc:
            return EffectOutcome("failed", str(exc))

        async def authorized() -> None:
            await check(claim)
            await self.validate(claim)

        entry = Entry(project.id, member.id, member.name, task.id, task.priority,
                      member.harness != "daedalus", "operator" if claim.principal.origin_class == "operator" else "orchestrator",
                      env=folder.env, resume_from=claim.payload["resume_from"],
                      principal=claim.principal, check_authority=authorized)
        bound = launch_attempt.set(claim.payload["attempt_id"])
        try:
            admission = await team.queue.offer(entry)
        finally:
            launch_attempt.reset(bound)
        if admission.state == "queued":
            return EffectOutcome("deferred", admission.detail or "waiting for worker capacity")
        return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        attempt = await self.app.db.fetchone("SELECT id,provider_session_ref,state,staff_session_id FROM execution_attempts WHERE id = ? AND task_id = ?",
                                            (claim.payload["attempt_id"], claim.payload["task_id"]))
        if attempt is not None and attempt["provider_session_ref"]:
            return EffectResolution("completed", {"attempt_id": attempt["id"], "provider_session_ref": attempt["provider_session_ref"]})
        return None
