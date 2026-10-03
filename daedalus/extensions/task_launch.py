"""A queued launch retains its authenticated command until capacity and ownership are proven."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.launch_controls import launch_attempt
from daedalus.extensions.orchestrator_domain import dependency_readiness
from daedalus.host.launch_queue import Entry
from daedalus.host.worktrees import WorktreeRefused
from daedalus.stores.control import (
    ControlConflict,
    ControlDenied,
    ControlStore,
    Entity,
    Principal,
    Scope,
    canonical,
    digest,
    one,
)
from daedalus.stores.executions import ACTIVE
from daedalus.stores.goal_budget import requires_priced_native_in
from daedalus.stores.outbox import Claim, OutboxStore
from daedalus.stores.runtime_release import no_entry_in
from daedalus.stores.staff import StaffError, daedalus_cannot_reach

if TYPE_CHECKING:
    from daedalus.app import Application


def task_digest(task: Any) -> str:
    return digest({field: task[field] for field in ("project_id", "title", "brief_json", "depends_on",
                                            "contract_revision", "assignee_staff_id")})


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
        folder = None
        for folder_id in (task["folder_id"], member["default_folder_id"]):
            if folder_id:
                folder = await one(conn, "SELECT path,env FROM project_folders"
                                   " WHERE id = ? AND project_id = ?", (folder_id, task["project_id"]))
                if folder is not None:
                    break
        if folder is None:
            folder = await one(conn, "SELECT path,env FROM project_folders"
                               " WHERE project_id = ? ORDER BY position LIMIT 1", (task["project_id"],))
        if folder is None:
            raise StaffError("the project has no folder to work in")
        if member["harness"] == "daedalus" and folder["env"] != app.manager.projects.local_env:
            # A queued command must refuse an unreachable folder before it records an assignment.
            raise StaffError(f"{member['name']} cannot work on task {task_id}: "
                             + daedalus_cannot_reach(folder["path"], folder["env"],
                                                     app.manager.projects.local_env))
        if member["harness"] != "daedalus" and await requires_priced_native_in(conn, task["project_id"]):
            raise ControlConflict("a dollar-capped project needs priced native worker admission")
        if task["status"] not in ("todo", "blocked"):
            raise ControlConflict("reopen the task before launching a new attempt")
        if await one(conn, "SELECT 1 FROM comparison_groups WHERE task_id = ? AND state IN ('planned','active','ready')", (task_id,)):
            raise ControlConflict('reconcile the undecided comparison before launching an ordinary attempt')
        parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?", (task_id,))
        if parent is not None and parent["cancel_state"] != "active":
            raise ControlConflict("the task parent was cancelled; revise its contract before admitting new work")
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
                  "folder_id": task["folder_id"],
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
        if "folder_id" not in target:
            raise ControlDenied("the queued launch has no pinned folder")
        if task["folder_id"] != target["folder_id"]:
            # The host fills an initially unspecified folder while claiming its own session.
            # A changed folder before that claim, or a different claimed folder, is stale work.
            attempt = await self.app.db.fetchone(
                "SELECT s.folder_id FROM execution_attempts a JOIN staff_sessions s"
                " ON s.id = a.staff_session_id WHERE a.id = ?", (target["attempt_id"],),
            )
            if (target["folder_id"] is not None or task["current_attempt_id"] != target["attempt_id"]
                    or task["status"] != "doing" or attempt is None
                    or task["folder_id"] != attempt["folder_id"]):
                raise ControlDenied("the task folder changed after the launch command")
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
            if member.isolation == "worktree":
                from daedalus.extensions.staff import no_worktree  # Lazy: staff installs the launch handler

                try:
                    await team.worktrees.check(folder)
                except WorktreeRefused as exc:
                    return EffectOutcome("failed", no_worktree(member, task, exc))
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
            async with self.app.db.transaction() as conn:
                parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = ?",
                                   (claim.payload["task_id"],))
                if parent is not None and parent["cancel_state"] != "active":
                    raise ControlDenied("the parent cancelled this launch before provider admission")
                attempt = await one(conn, "SELECT id FROM execution_attempts WHERE id = ?", (claim.payload["attempt_id"],))
                if attempt is not None:
                    await self.app.executions._check(conn, attempt["id"], operation="result.submit")

        entry = Entry(project.id, member.id, member.name, task.id, task.priority,
                      member.harness != "daedalus", "operator" if claim.principal.origin_class == "operator" else "orchestrator",
                      env=folder.env, resume_from=claim.payload["resume_from"],
                      principal=claim.principal, check_authority=authorized)
        bound = launch_attempt.set(claim.payload["attempt_id"])
        try:
            admission = await team.queue.offer(entry)
        except Exception:
            async with self.app.db.transaction() as conn:
                refused = await no_entry_in(conn, claim.payload["attempt_id"])
            if refused:
                return EffectOutcome("failed", "the host refused this launch before runtime entry")
            raise
        finally:
            launch_attempt.reset(bound)
        if admission.state == "queued":
            return EffectOutcome("deferred", canonical({"reason": admission.reason or "capacity",
                                                       "detail": admission.detail or "waiting for worker capacity"}))
        return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        async with self.app.db.transaction() as conn:
            if await no_entry_in(conn, claim.payload["attempt_id"]):
                return EffectResolution("failed", {"attempt_id": claim.payload["attempt_id"], "proof": "host_no_entry"})
        attempt = await self.app.db.fetchone("SELECT id,provider_session_ref,state,staff_session_id FROM execution_attempts WHERE id = ? AND task_id = ?",
                                            (claim.payload["attempt_id"], claim.payload["task_id"]))
        if attempt is not None and attempt["provider_session_ref"]:
            return EffectResolution("completed", {"attempt_id": attempt["id"], "provider_session_ref": attempt["provider_session_ref"]})
        return None
