"""Reserve two priced workers before either comparison launch can reach a provider."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from daedalus.extensions.orchestrator_domain import dependency_readiness, record_verdict
from daedalus.extensions.task_launch import member_digest, task_digest
from daedalus.host.events import AppEvent
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.worktrees import staff_slug
from daedalus.stores.comparison_funding import ComparisonFunding
from daedalus.stores.comparisons import (
    ComparisonRefused,
    choose_result,
    comparison_readiness,
    create_group,
)
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, now, one
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.runtime_release import attempt_released_in, physical_exit_in

if TYPE_CHECKING:
    from daedalus.app import Application


async def _task_events(conn: Any, bus: Any, principal: Principal, task_id: str,
                       prior_status: str | None) -> list[AppEvent]:
    task = await one(conn, "SELECT title,status,project_id,assignee_staff_id FROM board_tasks WHERE id = ?",
                     (task_id,))
    assert task is not None
    payload = {"task_id": task_id, "title": task["title"], "actor": principal.origin_class,
               "actor_id": principal.actor_id}
    events = [await bus.persist_in(conn, "task.changed", payload, project_id=task["project_id"],
                                   staff_id=task["assignee_staff_id"])]
    if prior_status is not None and prior_status != task["status"]:
        events.append(await bus.persist_in(conn, "task.moved",
                                           {**payload, "from": prior_status, "to": task["status"]},
                                           project_id=task["project_id"], staff_id=task["assignee_staff_id"]))
    return events


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
    bus = app.manager.bus
    events: list[AppEvent] = []
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
            if not await attempt_released_in(conn, row["id"]):
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
        events.extend(await _task_events(conn, bus, principal, task_id, current["status"]))
        return {"group_id": group_id, "task_id": task_id, "contract_revision": contract_revision,
                "budget_cap_microusd": budget_cap_microusd, "state": "queued",
                "slots": effects, "reserved_microusd": sum(item["reserved_microusd"] for item in reserved)}

    async with team.queue.admission_guard(), bus.transaction_guard():
        result = await control.mutate(principal, scope, "comparison.launch", client_operation_id,
                                      expected_entity_revision, Entity("task", task_id), payload,
                                      effect, effects=("execution.start",))
        for event in events:
            bus.announce_committed(event)
    dispatcher.notify()
    return result


async def comparison_state(app: Application, *, task_id: str, group_id: str) -> dict[str, Any]:
    """Project the current pair with exact cost and physical exit evidence."""
    funding = ComparisonFunding(app.db)
    async with app.db.transaction() as conn:
        group = await one(conn, "SELECT task_id FROM comparison_groups WHERE id = ?", (group_id,))
        if group is None or group["task_id"] != task_id:
            raise KeyError(group_id)
        return await _group_projection(conn, group_id, funding)


async def _group_projection(conn: Any, group_id: str, funding: ComparisonFunding) -> dict[str, Any]:
    state = await comparison_readiness(conn, group_id, observed_cost=funding.observed_cost_in,
                                       physical_exit=physical_exit_in)
    async with conn.execute(
        "SELECT f.id,f.slot,f.staff_id,f.attempt_id,f.state AS funding_state,f.launch_started_at,"
        " e.id AS launch_effect_id,e.state AS launch_state"
        " FROM comparison_funding_slots f LEFT JOIN effect_outbox e"
        " ON e.kind = CASE f.slot WHEN 1 THEN 'comparison.launch.first'"
        " ELSE 'comparison.launch.second' END"
        " AND json_extract(e.payload_json,'$.data.group_id') = f.group_id"
        " WHERE f.group_id = ? ORDER BY f.slot", (group_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {**state, "slots": [{"slot_id": row["id"], "slot": row["slot"],
                                 "staff_id": row["staff_id"], "attempt_id": row["attempt_id"],
                                 "funding_state": row["funding_state"],
                                 "launch_started_at": row["launch_started_at"],
                                 "launch_effect_id": row["launch_effect_id"],
                                 "launch_state": row["launch_state"]} for row in rows]}


async def comparison_history(app: Application, *, task_id: str, limit: int = 20,
                             before: str | None = None) -> dict[str, Any]:
    """Read newest durable groups for one task with a stable older-page cursor."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("comparison history limit must be between 1 and 50")
    funding = ComparisonFunding(app.db)
    async with app.db.transaction() as conn:
        if await one(conn, "SELECT id FROM board_tasks WHERE id = ?", (task_id,)) is None:
            raise KeyError(task_id)
        cursor_at = None
        if before is not None:
            cursor_at = await one(conn, "SELECT created_at,id FROM comparison_groups"
                                  " WHERE task_id = ? AND id = ?", (task_id, before))
            if cursor_at is None:
                raise KeyError(before)
        condition = " AND (created_at,id) < (?,?)" if cursor_at is not None else ""
        args = (task_id, cursor_at["created_at"], cursor_at["id"], limit + 1) if cursor_at else (task_id, limit + 1)
        async with conn.execute("SELECT id,created_at FROM comparison_groups WHERE task_id = ?" + condition +
                                " ORDER BY created_at DESC,id DESC LIMIT ?", args) as cursor:
            rows = await cursor.fetchall()
        page = rows[:limit]
        groups = []
        for row in page:
            state = await _group_projection(conn, row["id"], funding)
            groups.append({**state, "created_at": row["created_at"]})
        return {"task_id": task_id, "groups": groups,
                "next_before": page[-1]["id"] if len(rows) > limit else None}


async def comparison_slot_review(app: Application, *, task_id: str, group_id: str,
                                 slot: int, principal: Principal) -> dict[str, Any]:
    """Read the exact contender's current branch and result without changing shared Board state."""
    if slot not in (1, 2):
        raise KeyError(slot)
    member = await app.db.fetchone(
        "SELECT m.attempt_id FROM comparison_group_attempts m"
        " JOIN comparison_groups g ON g.id = m.group_id"
        " WHERE g.id = ? AND g.task_id = ? AND m.slot = ?", (group_id, task_id, slot),
    )
    if member is None:
        raise KeyError(slot)
    team = app.extensions.get("staff")
    review = getattr(team, "review", None)
    if review is None:
        raise ControlDenied("comparison review service is unavailable")
    inspected = await review.review(task_id, comparison_attempt_id=member["attempt_id"])
    state = await comparison_state(app, task_id=task_id, group_id=group_id)
    alternative = next((item for item in state["alternatives"]
                        if item["attempt_id"] == member["attempt_id"]), None)
    blockers = list(inspected["blockers"])
    if alternative is None or not alternative["physical_exit_verified"]:
        blockers.append({"code": "exit_unknown", "text": "the worker has no exact observed physical exit"})
    if alternative is None or alternative["observed_cost_microusd"] is None:
        blockers.append({"code": "cost_unknown", "text": "the worker's actual cost is unknown"})
    result_id = inspected["result_id"]
    result = await app.db.fetchone("SELECT actor_id FROM result_receipts WHERE id = ? AND task_id = ?"
                                   " AND attempt_id = ?", (result_id, task_id, member["attempt_id"])) if result_id else None
    artifacts = await app.db.fetchall(
        "SELECT m.id AS manifest_id,m.artifact_kind,m.artifact_key,m.digest,m.size_bytes,m.file_id"
        " FROM result_artifacts a JOIN artifact_manifests m ON m.id = a.manifest_id"
        " WHERE a.result_id = ? ORDER BY m.artifact_key,m.id", (result_id,),
    ) if result_id else []
    source_current = bool(inspected["head_sha"] and inspected["base_sha"] and result is not None and
                          alternative is not None and alternative["physical_exit_verified"] and
                          alternative["observed_cost_microusd"] is not None and
                          not any(item["code"] in {"branch", "dirty", "unknown", "moved", "merged",
                                                       "result_incomplete"} for item in blockers))
    return {**inspected, "group_id": group_id, "slot": slot, "attempt_id": member["attempt_id"],
            "blockers": blockers, "can_choose": inspected["can_choose"] and not blockers,
            "source_current": source_current,
            "physical_exit_verified": bool(alternative and alternative["physical_exit_verified"]),
            "observed_cost_microusd": alternative["observed_cost_microusd"] if alternative else None,
            "self_review_waiver_required": bool(result is not None and result["actor_id"] == principal.actor_id),
            "artifacts": [dict(row) for row in artifacts]}


async def review_comparison_slot(
    app: Application, *, task_id: str, group_id: str, slot: int, principal: Principal,
    client_operation_id: str, expected_entity_revision: int, result_id: str,
    verification: str, accepted: bool, evidence_ids: list[str], reason: str,
    self_review_waiver_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Bind an operator verdict to the host-observed contender head and immutable result."""
    if principal.origin_class != "operator":
        raise ControlDenied("comparison review needs an authenticated operator")
    task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
    if task is None or not task["project_id"]:
        raise KeyError(task_id)
    request = {"group_id": group_id, "slot": slot, "result_id": result_id,
               "verification": verification, "accepted": accepted, "evidence_ids": evidence_ids,
               "reason": reason, "self_review_waiver_receipt_id": self_review_waiver_receipt_id}
    preflight = None
    failure = None
    try:
        preflight = await comparison_slot_review(app, task_id=task_id, group_id=group_id,
                                                 slot=slot, principal=principal)
    except (KeyError, ValueError, ControlDenied) as exc:
        failure = exc
    bus = app.manager.bus
    events: list[AppEvent] = []

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        if failure is not None:
            raise failure
        assert preflight is not None
        if preflight["result_id"] != result_id or (accepted and not preflight["source_current"]):
            raise ComparisonRefused("the contender result or source branch is no longer reviewable")
        binding = await one(conn, "SELECT m.attempt_id,g.task_id,g.contract_revision,g.state"
                            " FROM comparison_group_attempts m JOIN comparison_groups g ON g.id = m.group_id"
                            " WHERE g.id = ? AND g.task_id = ? AND m.slot = ?", (group_id, task_id, slot))
        if (binding is None or binding["attempt_id"] != preflight["attempt_id"] or
                binding["state"] not in ("planned", "active", "ready")):
            raise ComparisonRefused("the contender binding changed before review")
        response = await record_verdict(conn, verdict_id=mutation.object_id, result_id=result_id,
                                        reviewer_actor_id=principal.actor_id, verification=verification,
                                        accepted=accepted, head=preflight["head_sha"],
                                        base=preflight["base_sha"], environment_digest=None,
                                        evidence_ids=evidence_ids, reason=reason,
                                        self_review_waiver_receipt_id=self_review_waiver_receipt_id)
        events.extend(await _task_events(conn, bus, principal, task_id, None))
        return {**response, "group_id": group_id, "slot": slot, "attempt_id": binding["attempt_id"],
                "head_sha": preflight["head_sha"], "base_sha": preflight["base_sha"]}

    async with bus.transaction_guard():
        response = await ControlStore(app.db).mutate(
            principal, Scope("project", task["project_id"]), "review.verdict", client_operation_id,
            expected_entity_revision, Entity("task", task_id), request, effect,
        )
        for event in events:
            bus.announce_committed(event)
    return response


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
    bus = app.manager.bus
    events: list[AppEvent] = []
    preflight = None
    preflight_error = None
    try:
        member = await app.db.fetchone(
            "SELECT m.slot FROM result_receipts r JOIN comparison_group_attempts m ON m.attempt_id = r.attempt_id"
            " WHERE r.id = ? AND r.task_id = ? AND m.group_id = ?", (result_id, task_id, group_id),
        )
        if member is None:
            raise ComparisonRefused("the winner is outside this comparison")
        preflight = await comparison_slot_review(app, task_id=task_id, group_id=group_id,
                                                 slot=member["slot"], principal=principal)
        if (preflight["result_id"] != result_id or preflight["verdict_id"] != verdict_id or
                not preflight["source_current"] or not preflight["can_choose"]):
            raise ComparisonRefused("the reviewed contender branch or verdict changed before selection")
    except (KeyError, ValueError, ControlDenied) as exc:
        preflight_error = exc

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        if preflight_error is not None:
            raise preflight_error
        assert preflight is not None
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
        receipt = await one(conn, "SELECT head_sha,base_sha FROM comparison_selection_receipts WHERE id = ?",
                            (mutation.object_id,))
        if (receipt is None or receipt["head_sha"] != preflight["head_sha"] or
                receipt["base_sha"] != preflight["base_sha"]):
            raise ComparisonRefused("the selected verdict no longer matches the observed branch")
        async with conn.execute("SELECT id FROM comparison_funding_slots WHERE group_id = ? ORDER BY slot",
                                (group_id,)) as cursor:
            slots = await cursor.fetchall()
        if len(slots) != 2:
            raise ComparisonRefused("both comparison allocations must exist at selection")
        for slot in slots:
            await funding.release_in(conn, slot["id"])
        events.extend(await _task_events(conn, bus, principal, task_id, current["status"]))
        return selected

    async with bus.transaction_guard():
        response = await ControlStore(app.db).mutate(
            principal, Scope("project", task["project_id"]), "comparison.choose", client_operation_id,
            expected_entity_revision, Entity("task", task_id),
            {"group_id": group_id, "result_id": result_id, "verdict_id": verdict_id}, effect,
        )
        for event in events:
            bus.announce_committed(event)
    return response


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
    bus = app.manager.bus
    events: list[AppEvent] = []

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
        events.extend(await _task_events(conn, bus, principal, task_id, None))
        return {"group_id": group_id, "task_id": task_id, "state": "blocked",
                "cancelled_launch_ids": [item["id"] for item in effects if item["state"] == "pending"],
                "released_slot_ids": [slot["id"] for slot in slots]}

    async with bus.transaction_guard():
        response = await ControlStore(app.db).mutate(
            principal, Scope("project", task["project_id"]), "comparison.close", client_operation_id,
            expected_entity_revision, Entity("task", task_id), {"group_id": group_id}, effect,
        )
        for event in events:
            bus.announce_committed(event)
    if events:
        team = app.extensions.get("staff")
        if team is not None:
            team.queue.pump_soon(task["project_id"])
    return response


__all__ = ["queue_comparison", "comparison_state", "comparison_history",
           "comparison_slot_review", "review_comparison_slot", "choose_comparison", "close_comparison"]
