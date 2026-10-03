"""Deliver each prepaid comparison slot through the host's ordinary worker runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.launch_controls import launch_attempt, launch_capacity_slot
from daedalus.extensions.orchestrator_domain import dependency_readiness
from daedalus.extensions.task_launch import member_digest, task_digest
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.launch_queue import Entry
from daedalus.host.worktrees import WorktreeError
from daedalus.stores.control import ControlDenied, canonical, one
from daedalus.stores.inference_budget import BudgetRefused
from daedalus.stores.outbox import Claim

if TYPE_CHECKING:
    from daedalus.app import Application


class ComparisonLaunchEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def validate(self, claim: Claim) -> None:
        target = claim.payload
        expected_kind = "comparison.launch.first" if target["slot"] == 1 else "comparison.launch.second"
        if claim.kind != expected_kind or claim.operation != "comparison.launch":
            raise ControlDenied("the comparison effect does not own this slot")
        async with self.app.db.transaction() as conn:
            task = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (target["task_id"],))
            member = await one(conn, "SELECT * FROM staff WHERE id = ?", (target["staff_id"],))
            group = await one(conn, "SELECT task_id,contract_revision,state FROM comparison_groups WHERE id = ?",
                              (target["group_id"],))
            slot = await one(conn, "SELECT * FROM comparison_funding_slots WHERE id = ?", (target["slot_id"],))
            funded = await one(conn, "SELECT count(*) AS total FROM comparison_funding_slots"
                               " WHERE group_id = ?", (target["group_id"],))
            parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents"
                               " WHERE parent_kind = 'task' AND parent_id = ?", (target["task_id"],))
            generation = await self.app.executions._host(conn)
            if (task is None or member is None or group is None or slot is None or funded is None or
                    member["archived_at"] or task_digest(task) != target["task_digest"] or
                    member_digest(member) != target["member_digest"] or
                    group["task_id"] != target["task_id"] or group["contract_revision"] != target["contract_revision"] or
                    group["state"] not in ("planned", "active") or
                    task["contract_revision"] != target["contract_revision"] or
                    task["folder_id"] != target["folder_id"] or
                    task["status"] != "todo" or task["current_attempt_id"] or
                    slot["group_id"] != target["group_id"] or slot["slot"] != target["slot"] or
                    slot["staff_id"] != target["staff_id"] or slot["task_id"] != target["task_id"] or
                    slot["contract_revision"] != target["contract_revision"] or
                    slot["rate_version"] != target["rate_version"] or slot["state"] != "held" or
                    slot["host_generation"] != generation or
                    slot["attempt_id"] not in (None, target["attempt_id"]) or funded["total"] != 2 or
                    (parent is not None and parent["cancel_state"] != "active")):
                raise ControlDenied("the pair or one of its launch identities changed")
            readiness = await dependency_readiness(conn, target["task_id"])
            if not readiness["ready"]:
                raise ControlDenied("the comparison task dependencies changed")
        current_quote = await HostInferenceAdmission(self.app.manager).quote_for_member(
            target["staff_id"], slot_id=target["slot_id"], slot=target["slot"],
            allowance_microusd=slot["allowance_microusd"])
        if current_quote.rate_version != target["rate_version"]:
            raise ControlDenied("the comparison model or rate card changed before launch")

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        team = self.app.extensions.get("staff")
        if team is None:
            return EffectOutcome("deferred", "the staff runtime is not ready")
        try:
            await self.validate(claim)
            member = await team.member(claim.payload["staff_id"])
            task = await team.task(claim.payload["task_id"])
            if task is None or task.missing():
                return EffectOutcome("failed", "the task brief is incomplete")
            project = await team.project(member.project_id)
            folder = team.folder_for(project, member, task)
            if folder.id != claim.payload["folder_id"]:
                raise ControlDenied("the comparison worktree folder changed")
            await team.worktrees.check(folder)
        except (ControlDenied, BudgetRefused, WorktreeError, RuntimeError, ValueError, KeyError) as exc:
            return EffectOutcome("failed", str(exc))

        async def authorized() -> None:
            await check(claim)
            await self.validate(claim)

        entry = Entry(project.id, member.id, member.name, task.id, task.priority, False,
                      "operator", env=folder.env, principal=claim.principal,
                      check_authority=authorized, capacity_slot_id=claim.payload["slot_id"])
        attempt_bound = launch_attempt.set(claim.payload["attempt_id"])
        slot_bound = launch_capacity_slot.set(claim.payload["slot_id"])
        try:
            admission = await team.queue.offer(entry)
        finally:
            launch_capacity_slot.reset(slot_bound)
            launch_attempt.reset(attempt_bound)
        if admission.state == "queued":
            return EffectOutcome("deferred", canonical({"reason": admission.reason or "capacity",
                                                       "detail": admission.detail or "waiting for worker capacity"}))
        return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        target = claim.payload
        row = await self.app.db.fetchone(
            "SELECT a.id,a.provider_session_ref,a.host_generation,s.attempt_id,s.group_id,s.slot"
            " FROM execution_attempts a JOIN comparison_funding_slots s ON s.attempt_id = a.id"
            " WHERE a.id = ? AND a.task_id = ? AND s.id = ?",
            (target["attempt_id"], target["task_id"], target["slot_id"]),
        )
        if (row is None or not row["provider_session_ref"] or row["attempt_id"] != target["attempt_id"] or
                row["group_id"] != target["group_id"] or row["slot"] != target["slot"]):
            return None
        return EffectResolution("completed", {"attempt_id": target["attempt_id"],
                                              "provider_session_ref": row["provider_session_ref"],
                                              "host_generation": row["host_generation"]})


__all__ = ["ComparisonLaunchEffect"]
