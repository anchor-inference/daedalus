"""Reserve two priced workers before either comparison launch can reach a provider."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from daedalus.extensions.comparisons import (
    ComparisonRefused,
    choose_result,
    comparison_readiness,
    create_group,
)
from daedalus.extensions.orchestrator_domain import dependency_readiness
from daedalus.extensions.task_launch import member_digest, task_digest
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.worktrees import staff_slug
from daedalus.stores.comparison_funding import ComparisonFunding, physical_exit_in
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, now, one
from daedalus.stores.outbox import OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


async def queue_comparison(
    app: Application, *, task_id: str, principal: Principal,
    client_operation_id: str, expected_entity_revision: int, contract_revision: int,
    budget_cap_microusd: int, alternatives: tuple[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Commit both funded slots and launch intents under one task revision and capacity lock."""
    if principal.origin_class != "operator":
        raise ControlDenied("bounded comparisons need an explicit operator command")
    if (type(contract_revision) is not int or contract_revision < 1 or
            type(budget_cap_microusd) is not int or budget_cap_microusd < 1 or
            len(alternatives) != 2):
        raise ValueError("two alternatives, a contract revision and a positive priced cap are required")
    staff_ids = tuple(item.get("staff_id") for item in alternatives)
    allowances = tuple(item.get("allowance_microusd") for item in alternatives)
    if (not all(isinstance(value, str) and value for value in staff_ids) or
            len(set(staff_ids)) != 2 or
            not all(type(value) is int and 0 < value <= 2**63 - 1 for value in allowances) or
            sum(allowances) > budget_cap_microusd):
        raise ValueError("two distinct workers need bounded allocations within the group cap")
    task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if not task["project_id"]:
        raise ControlConflict("a bounded comparison requires a project task")
    scope = Scope("project", task["project_id"])
    team = app.extensions.get("staff")
    dispatcher = app.extensions.get("effects")
    if team is None or dispatcher is None:
        raise ControlDenied("comparison launch services are unavailable")
    control = ControlStore(app.db)
    funding = ComparisonFunding(app.db)
    admission = HostInferenceAdmission(app.manager)
    payload = {"contract_revision": contract_revision, "budget_cap_microusd": budget_cap_microusd,
               "alternatives": [{"staff_id": staff_id, "allowance_microusd": allowance}
                                for staff_id, allowance in zip(staff_ids, allowances, strict=True)]}

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        current = await one(conn, "SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        if current is None or current["project_id"] != scope.id or current["contract_revision"] != contract_revision:
            raise ComparisonRefused("the task contract changed before comparison admission")
        if current["status"] != "todo" or current["current_attempt_id"]:
            raise ComparisonRefused("return the task to todo before comparing alternatives")
        snapshot = await one(conn, "SELECT snapshot_json FROM task_contract_versions"
                             " WHERE task_id = ? AND contract_revision = ?", (task_id, contract_revision))
        if snapshot is None or not current["folder_id"] or json.loads(snapshot["snapshot_json"]).get("folder_id") != current["folder_id"]:
            raise ComparisonRefused("the comparison needs a pinned folder in the immutable task contract")
        folder = await one(conn, "SELECT project_id,readonly FROM project_folders WHERE id = ?",
                           (current["folder_id"],))
        if folder is None or folder["project_id"] != scope.id or folder["readonly"]:
            raise ComparisonRefused("the task folder cannot host isolated alternatives")
        parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents"
                           " WHERE parent_kind = 'task' AND parent_id = ?", (task_id,))
        if parent is not None and parent["cancel_state"] != "active":
            raise ComparisonRefused("the task is cancelling")
        prior = await conn.execute("SELECT id FROM execution_attempts WHERE task_id = ?", (task_id,))
        prior_attempts = await prior.fetchall()
        await prior.close()
        for row in prior_attempts:
            if not await physical_exit_in(conn, row["id"]):
                raise ComparisonRefused("the previous task execution has no observed physical exit")
        pending_launch = await one(conn, "SELECT id FROM effect_outbox WHERE kind = 'task.launch'"
                                   " AND state IN ('pending','claimed','unknown')"
                                   " AND json_extract(payload_json,'$.control.task_id') = ?", (task_id,))
        if pending_launch is not None:
            raise ComparisonRefused("a single worker launch is still pending or uncertain")
        dependencies = await dependency_readiness(conn, task_id)
        if not dependencies["ready"]:
            raise ComparisonRefused("the task dependencies are not accepted at this contract")
        members = []
        for staff_id in staff_ids:
            member = await one(conn, "SELECT * FROM staff WHERE id = ?", (staff_id,))
            if (member is None or member["project_id"] != scope.id or member["archived_at"] or
                    member["harness"] != "daedalus" or member["isolation"] != "worktree"):
                raise ComparisonRefused("each alternative needs an active isolated native worker")
            members.append(member)
        if staff_slug(members[0]["name"]) == staff_slug(members[1]["name"]):
            raise ComparisonRefused("the alternatives would share one physical worktree path")
        if not await team.queue.check_pair_capacity_in(conn, scope.id, count=2):
            raise ComparisonRefused("two worker capacity slots are not currently available")
        group_id = mutation.object_id
        await create_group(conn, group_id=group_id, task_id=task_id, contract_revision=contract_revision,
                           budget_cap_microusd=budget_cap_microusd, actor_id=principal.actor_id)
        allocations = []
        for slot, (staff_id, allowance) in enumerate(zip(staff_ids, allowances, strict=True), 1):
            slot_id = uuid.uuid5(uuid.NAMESPACE_URL, f"comparison:{group_id}:slot:{slot}").hex
            allocations.append(await admission.quote_for_member_in(conn, staff_id, slot_id=slot_id,
                                                                   slot=slot, allowance_microusd=allowance))
        generation = await app.executions._host(conn)
        constraints = admission.prelaunch_constraints(tuple(item.provider_id for item in allocations))
        reserved = await funding.reserve_pair_in(conn, group_id=group_id,
                                                  allocations=(allocations[0], allocations[1]),
                                                  constraints=constraints, host_generation=generation)
        effects = []
        for slot, (member, allocation) in enumerate(zip(members, allocations, strict=True), 1):
            kind = "comparison.launch.first" if slot == 1 else "comparison.launch.second"
            attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, f"comparison:{group_id}:attempt:{slot}").hex
            target = {"group_id": group_id, "task_id": task_id, "contract_revision": contract_revision,
                      "slot": slot, "slot_id": allocation.id, "attempt_id": attempt_id,
                      "staff_id": member["id"], "folder_id": current["folder_id"],
                      "task_digest": task_digest(current), "member_digest": member_digest(member),
                      "rate_version": allocation.rate_version}
            effect_id = await OutboxStore.enqueue(conn, mutation, principal, kind=kind,
                                                  operation="comparison.launch", payload=target,
                                                  task_id=task_id, effects=("execution.start",))
            effects.append({"slot": slot, "staff_id": member["id"], "slot_id": allocation.id,
                            "attempt_id": attempt_id, "effect_id": effect_id,
                            "allowance_microusd": allocation.allowance_microusd,
                            "state": "queued"})
        return {"group_id": group_id, "task_id": task_id, "contract_revision": contract_revision,
                "budget_cap_microusd": budget_cap_microusd, "state": "queued",
                "slots": effects, "reserved_microusd": sum(item["reserved_microusd"] for item in reserved)}

    async with team.queue.admission_guard():
        result = await control.mutate(principal, scope, "comparison.launch", client_operation_id,
                                      expected_entity_revision, Entity("task", task_id), payload,
                                      effect, effects=("execution.start",))
    dispatcher.notify()
    return result


async def comparison_state(app: Application, *, task_id: str, group_id: str) -> dict[str, Any]:
    """Project the current pair with exact cost and physical exit evidence."""
    funding = ComparisonFunding(app.db)
    async with app.db.transaction() as conn:
        group = await one(conn, "SELECT task_id FROM comparison_groups WHERE id = ?", (group_id,))
        if group is None or group["task_id"] != task_id:
            raise KeyError(group_id)
        return await comparison_readiness(conn, group_id, observed_cost=funding.observed_cost_in,
                                          physical_exit=physical_exit_in)


async def choose_comparison(
    app: Application, *, task_id: str, group_id: str, principal: Principal,
    client_operation_id: str, expected_entity_revision: int, result_id: str, verdict_id: str,
) -> dict[str, Any]:
    """Choose only after both workers have reviewed results and exact observed exits."""
    if principal.origin_class != "operator":
        raise ControlDenied("comparison selection needs an explicit operator command")
    task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if not task["project_id"]:
        raise ControlConflict("a bounded comparison needs a project task")
    funding = ComparisonFunding(app.db)

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        group = await one(conn, "SELECT task_id FROM comparison_groups WHERE id = ?", (group_id,))
        if group is None or group["task_id"] != task_id:
            raise ComparisonRefused("the comparison does not own this task")
        current = await one(conn, "SELECT status,current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
        parent = await one(conn, "SELECT cancel_state FROM lifecycle_parents"
                           " WHERE parent_kind = 'task' AND parent_id = ?", (task_id,))
        if (current is None or current["status"] not in ("todo", "blocked") or
                current["current_attempt_id"] is not None or
                (parent is not None and parent["cancel_state"] != "active")):
            raise ComparisonRefused("the comparison task is no longer awaiting a choice")
        selected = await choose_result(conn, group_id=group_id, result_id=result_id, verdict_id=verdict_id,
                                       operation_receipt_id=mutation.receipt_id,
                                       selection_receipt_id=mutation.object_id,
                                       observed_cost=funding.observed_cost_in,
                                       physical_exit=physical_exit_in)
        async with conn.execute("SELECT id FROM comparison_funding_slots WHERE group_id = ? ORDER BY slot",
                                (group_id,)) as cursor:
            slots = await cursor.fetchall()
        if len(slots) != 2:
            raise ComparisonRefused("both comparison allocations must exist at selection")
        for slot in slots:
            await funding.release_in(conn, slot["id"])
        return selected

    return await ControlStore(app.db).mutate(
        principal, Scope("project", task["project_id"]), "comparison.choose", client_operation_id,
        expected_entity_revision, Entity("task", task_id),
        {"group_id": group_id, "result_id": result_id, "verdict_id": verdict_id}, effect,
    )


async def close_comparison(
    app: Application, *, task_id: str, group_id: str, principal: Principal,
    client_operation_id: str, expected_entity_revision: int,
) -> dict[str, Any]:
    """Close only when queued starts can be cancelled and every bound worker has exited."""
    if principal.origin_class != "operator":
        raise ControlDenied("comparison closure needs an explicit operator command")
    task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None:
        raise KeyError(task_id)
    if not task["project_id"]:
        raise ControlConflict("a bounded comparison needs a project task")
    funding = ComparisonFunding(app.db)

    async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
        group = await one(conn, "SELECT task_id,state,selected_result_id FROM comparison_groups WHERE id = ?",
                          (group_id,))
        if (group is None or group["task_id"] != task_id or group["state"] not in ("planned", "active", "ready")
                or group["selected_result_id"] is not None):
            raise ComparisonRefused("the comparison is no longer open")
        async with conn.execute("SELECT id,state FROM effect_outbox WHERE kind IN"
                                " ('comparison.launch.first','comparison.launch.second')"
                                " AND json_extract(payload_json,'$.data.group_id') = ? ORDER BY kind",
                                (group_id,)) as cursor:
            effects = await cursor.fetchall()
        if len(effects) != 2 or any(item["state"] in ("claimed", "unknown") for item in effects):
            raise ComparisonRefused("a launch outcome must be reconciled before closing the comparison")
        if any(item["state"] not in ("pending", "completed", "failed", "cancelled") for item in effects):
            raise ComparisonRefused("the comparison launch state is unsupported")
        for item in effects:
            if item["state"] == "pending":
                cursor = await conn.execute("UPDATE effect_outbox SET state = 'cancelled',"
                                            "error = 'comparison closed before launch',completed_at = ?"
                                            " WHERE id = ? AND state = 'pending'", (now(), item["id"]))
                if cursor.rowcount != 1:
                    raise ComparisonRefused("a comparison launch was claimed during closure")
                await cursor.close()
        async with conn.execute("SELECT id FROM comparison_funding_slots WHERE group_id = ? ORDER BY slot",
                                (group_id,)) as cursor:
            slots = await cursor.fetchall()
        if len(slots) != 2:
            raise ComparisonRefused("the comparison has no complete funded pair")
        for slot in slots:
            await funding.release_in(conn, slot["id"])
        await conn.execute("UPDATE comparison_groups SET state = 'blocked' WHERE id = ?", (group_id,))
        return {"group_id": group_id, "task_id": task_id, "state": "blocked",
                "cancelled_launch_ids": [item["id"] for item in effects if item["state"] == "pending"],
                "released_slot_ids": [slot["id"] for slot in slots]}

    return await ControlStore(app.db).mutate(
        principal, Scope("project", task["project_id"]), "comparison.close", client_operation_id,
        expected_entity_revision, Entity("task", task_id), {"group_id": group_id}, effect,
    )


__all__ = ["queue_comparison", "comparison_state", "choose_comparison", "close_comparison"]
