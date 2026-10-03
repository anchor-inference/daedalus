"""Compare bounded worker alternatives under one task contract and retain losing evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite


class ComparisonRefused(ValueError):
    """An alternative cannot be admitted or selected with stale or unknown proof."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def _all(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> list[aiosqlite.Row]:
    async with conn.execute(sql, args) as cursor:
        return list(await cursor.fetchall())


@dataclass(frozen=True)
class AdmissionProof:
    """Only the host's budget and capacity owners may construct an admitted slot."""

    reservation_id: str
    reserved_microusd: int
    rate_version: str
    worktree_identity: str
    capacity_slot_id: str


# These callbacks must inspect durable rows on the supplied connection; no provider or Git I/O
# belongs inside the command transaction.
BudgetAdmission = Callable[[aiosqlite.Connection, str, str, AdmissionProof], Awaitable[bool]]
CapacityAdmission = Callable[[aiosqlite.Connection, str, str], Awaitable[bool]]
ObservedCost = Callable[[aiosqlite.Connection, str], Awaitable[int | None]]
PhysicalExit = Callable[[aiosqlite.Connection, str], Awaitable[bool]]


async def create_group(
    conn: aiosqlite.Connection, *, group_id: str, task_id: str,
    contract_revision: int, budget_cap_microusd: int, actor_id: str,
) -> dict[str, Any]:
    """Create a planned group; neither worker may launch until both reservations exist."""
    if not isinstance(budget_cap_microusd, int) or isinstance(budget_cap_microusd, bool) or budget_cap_microusd <= 0:
        raise ComparisonRefused("a positive priced comparison cap is required")
    task = await _one(conn, "SELECT contract_revision,status,project_id,current_attempt_id"
                      " FROM board_tasks WHERE id = ?", (task_id,))
    if task is None or task["contract_revision"] != contract_revision:
        raise ComparisonRefused("the comparison must pin the current immutable contract")
    if task["status"] not in ("todo", "blocked"):
        raise ComparisonRefused("return or reconcile the task before starting alternatives")
    if task["current_attempt_id"]:
        raise ComparisonRefused("reconcile or return the previous execution before starting alternatives")
    active = await _one(conn, "SELECT id FROM comparison_groups WHERE task_id = ?"
                        " AND state IN ('planned','active','ready')", (task_id,))
    if active is not None:
        raise ComparisonRefused("the task already has an undecided comparison")
    await conn.execute(
        "INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,created_at,state,"
        " budget_cap_microusd,reserved_microusd,created_by_actor_id)"
        " VALUES (?,?,?,2,?,'planned',?,0,?)",
        (group_id, task_id, contract_revision, _now(), budget_cap_microusd, actor_id),
    )
    return {"group_id": group_id, "task_id": task_id, "contract_revision": contract_revision,
            "state": "planned", "alternatives": 2, "budget_cap_microusd": budget_cap_microusd,
            "admitted_attempt_ids": []}


async def admit_attempt(
    conn: aiosqlite.Connection, *, group_id: str, attempt_id: str, slot: int,
    proof: AdmissionProof, verify_budget: BudgetAdmission, verify_capacity: CapacityAdmission,
) -> dict[str, Any]:
    """Bind a host-created attempt only after budget, capacity and isolated worktree proof."""
    if slot not in (1, 2) or not proof.reservation_id or proof.reserved_microusd <= 0 or not proof.rate_version:
        raise ComparisonRefused("the alternative has no priced reservation or slot")
    if not proof.worktree_identity or not proof.capacity_slot_id:
        raise ComparisonRefused("the alternative has no isolated worktree or capacity claim")
    group = await _one(conn, "SELECT * FROM comparison_groups WHERE id = ?", (group_id,))
    if group is None or group["state"] not in ("planned", "active"):
        raise ComparisonRefused("the comparison cannot admit an attempt now")
    task = await _one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (group["task_id"],))
    attempt = await _one(conn, "SELECT a.task_id,a.contract_revision,a.state,a.staff_session_id,"
                         " s.worktree_path,s.branch FROM execution_attempts a"
                         " JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?", (attempt_id,))
    if (task is None or task["contract_revision"] != group["contract_revision"] or attempt is None or
            attempt["task_id"] != group["task_id"] or attempt["contract_revision"] != group["contract_revision"] or
            attempt["state"] != "queued" or not attempt["worktree_path"] or not attempt["branch"]):
        raise ComparisonRefused("the attempt has no current isolated contract identity")
    actual_worktree = hashlib.sha256(attempt["worktree_path"].encode()).hexdigest()
    if proof.worktree_identity != actual_worktree:
        raise ComparisonRefused("the worktree proof belongs to another execution")
    collision = await _one(conn, "SELECT m.attempt_id FROM comparison_group_attempts m"
                           " JOIN comparison_groups g ON g.id = m.group_id"
                           " WHERE m.worktree_identity = ? AND g.state IN ('planned','active','ready','blocked')",
                           (proof.worktree_identity,))
    if collision is not None:
        raise ComparisonRefused("another unfinished alternative owns this worktree")
    if not await verify_budget(conn, group_id, attempt_id, proof):
        raise ComparisonRefused("the budget reservation is absent, stale, or unpriced")
    if not await verify_capacity(conn, attempt_id, proof.capacity_slot_id):
        raise ComparisonRefused("the capacity slot is absent or stale")
    members = await _all(conn, "SELECT slot,reserved_microusd FROM comparison_group_attempts"
                         " WHERE group_id = ?", (group_id,))
    if len(members) >= 2 or any(row["slot"] == slot for row in members):
        raise ComparisonRefused("the two alternative slots are already assigned")
    total = sum(row["reserved_microusd"] for row in members) + proof.reserved_microusd
    if total > group["budget_cap_microusd"]:
        raise ComparisonRefused("the reservations exceed the comparison cap")
    await conn.execute("INSERT INTO comparison_group_attempts(group_id,attempt_id,slot,budget_reservation_id,"
                       " reserved_microusd,rate_version,worktree_identity,admitted_at)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (group_id, attempt_id, slot, proof.reservation_id, proof.reserved_microusd,
                        proof.rate_version, proof.worktree_identity, _now()))
    await conn.execute("UPDATE comparison_groups SET reserved_microusd = ?,state = ? WHERE id = ?",
                       (total, "active" if len(members) == 1 else "planned", group_id))
    return {"group_id": group_id, "attempt_id": attempt_id, "slot": slot,
            "state": "active" if len(members) == 1 else "planned",
            "reserved_microusd": total}


async def comparison_readiness(
    conn: aiosqlite.Connection, group_id: str, *, observed_cost: ObservedCost,
    physical_exit: PhysicalExit,
) -> dict[str, Any]:
    """Unknown cost or exit stays visible as unknown, never zero or complete."""
    group = await _one(conn, "SELECT * FROM comparison_groups WHERE id = ?", (group_id,))
    if group is None:
        raise KeyError(group_id)
    task = await _one(conn, "SELECT contract_revision FROM board_tasks WHERE id = ?", (group["task_id"],))
    members = await _all(conn, "SELECT m.slot,m.attempt_id,m.reserved_microusd,m.rate_version,"
                         " a.state AS attempt_state,a.contract_revision FROM comparison_group_attempts m"
                         " JOIN execution_attempts a ON a.id = m.attempt_id WHERE m.group_id = ? ORDER BY m.slot",
                         (group_id,))
    alternatives = []
    blockers = []
    for member in members:
        results = await _all(conn, "SELECT id,outcome,original_digest,checks_json FROM result_receipts"
                             " WHERE attempt_id = ? AND task_id = ? ORDER BY created_at,id",
                             (member["attempt_id"], group["task_id"]))
        cost = await observed_cost(conn, member["attempt_id"])
        exited = await physical_exit(conn, member["attempt_id"])
        if cost is None or cost < 0:
            cost = None
            blockers.append(f"cost_unknown:{member['attempt_id']}")
        if not exited:
            blockers.append(f"exit_unknown:{member['attempt_id']}")
        if not results:
            blockers.append(f"result_missing:{member['attempt_id']}")
        alternatives.append({"slot": member["slot"], "attempt_id": member["attempt_id"],
                             "state": member["attempt_state"], "reserved_microusd": member["reserved_microusd"],
                             "observed_cost_microusd": cost, "physical_exit_verified": exited,
                             "results": [{"result_id": result["id"], "outcome": result["outcome"],
                                          "original_digest": result["original_digest"],
                                          "checks": json.loads(result["checks_json"])} for result in results]})
    if len(members) != 2:
        blockers.append("alternatives_missing")
    if task is None or task["contract_revision"] != group["contract_revision"]:
        blockers.append("contract_stale")
    # Known charges can already exceed the cap while another alternative is still unpriced.
    if sum(item["observed_cost_microusd"] or 0 for item in alternatives) > group["budget_cap_microusd"]:
        blockers.append("cost_cap_exceeded")
    return {"group_id": group_id, "task_id": group["task_id"],
            "contract_revision": group["contract_revision"], "state": group["state"],
            "budget_cap_microusd": group["budget_cap_microusd"],
            "reserved_microusd": group["reserved_microusd"],
            "selected_result_id": group["selected_result_id"],
            "alternatives": alternatives, "blockers": blockers}


async def choose_result(
    conn: aiosqlite.Connection, *, group_id: str, result_id: str, verdict_id: str,
    operation_receipt_id: str, selection_receipt_id: str,
    observed_cost: ObservedCost, physical_exit: PhysicalExit,
) -> dict[str, Any]:
    """Select an exact reviewed result; merge remains a separate operator action."""
    readiness = await comparison_readiness(conn, group_id, observed_cost=observed_cost,
                                            physical_exit=physical_exit)
    if readiness["state"] not in ("active", "ready") or readiness["blockers"]:
        raise ComparisonRefused("the alternatives lack comparable final proof: " + ", ".join(readiness["blockers"]))
    group = await _one(conn, "SELECT * FROM comparison_groups WHERE id = ?", (group_id,))
    selected = await _one(conn, "SELECT r.id,r.attempt_id,r.outcome,r.contract_revision,r.task_id,"
                          " v.id AS verdict_id,v.verification,v.accepted,v.head,v.base"
                          " FROM result_receipts r JOIN review_verdicts v ON v.result_id = r.id"
                          " WHERE r.id = ? AND v.id = ?", (result_id, verdict_id))
    if (group is None or selected is None or selected["task_id"] != group["task_id"] or
            selected["contract_revision"] != group["contract_revision"] or
            selected["outcome"] != "complete" or selected["verification"] != "verified" or
            selected["accepted"] != 1 or not selected["head"] or not selected["base"]):
        raise ComparisonRefused("the selected result lacks an accepted current-contract verdict")
    member = next((item for item in readiness["alternatives"]
                   if item["attempt_id"] == selected["attempt_id"]), None)
    if member is None or result_id not in {item["result_id"] for item in member["results"]}:
        raise ComparisonRefused("the result is outside this comparison")
    latest_result = await _one(conn, "SELECT id FROM result_receipts WHERE task_id = ?"
                               " AND contract_revision = ? AND attempt_id = ?"
                               " ORDER BY created_at DESC,rowid DESC LIMIT 1",
                               (group["task_id"], group["contract_revision"], selected["attempt_id"]))
    if latest_result is None or latest_result["id"] != result_id:
        raise ComparisonRefused("a newer result superseded this selection")
    newer = await _one(conn, "SELECT id FROM review_verdicts WHERE result_id = ?"
                       " ORDER BY created_at DESC,id DESC LIMIT 1", (result_id,))
    if newer is None or newer["id"] != verdict_id:
        raise ComparisonRefused("a newer verdict superseded this selection")
    source = await _one(conn, "SELECT a.staff_session_id,s.branch,s.folder_id,s.base_ref,"
                        " s.worktree_path,f.project_id AS folder_project,t.project_id AS task_project,"
                        " m.worktree_identity FROM execution_attempts a"
                        " JOIN staff_sessions s ON s.id = a.staff_session_id"
                        " JOIN comparison_group_attempts m ON m.attempt_id = a.id AND m.group_id = ?"
                        " JOIN board_tasks t ON t.id = a.task_id"
                        " LEFT JOIN project_folders f ON f.id = s.folder_id"
                        " WHERE a.id = ?", (group_id, selected["attempt_id"]))
    if (source is None or not source["branch"] or not source["folder_id"] or
            not source["worktree_path"] or not source["task_project"] or
            source["folder_project"] != source["task_project"] or
            not source["worktree_identity"] or
            hashlib.sha256(source["worktree_path"].encode()).hexdigest() != source["worktree_identity"]):
        raise ComparisonRefused("the selected worktree has no exact project branch binding")
    # Selection pins the reviewed bytes. The later merge effect checks the live head before Git.
    head, base = selected["head"], selected["base"]
    total_cost = sum(item["observed_cost_microusd"] for item in readiness["alternatives"])
    if total_cost > group["budget_cap_microusd"]:
        raise ComparisonRefused("the observed comparison cost exceeded its cap")
    evidence = {"group_id": group_id, "result_id": result_id, "verdict_id": verdict_id,
                "contract_revision": group["contract_revision"], "alternatives": readiness["alternatives"],
                "head": head, "base": base, "cost_microusd": total_cost,
                "folder_id": source["folder_id"], "branch": source["branch"],
                "worktree_identity": source["worktree_identity"]}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    await conn.execute("INSERT INTO comparison_selection_receipts(id,operation_receipt_id,group_id,task_id,"
                       " contract_revision,result_id,verdict_id,attempt_id,head_sha,base_sha,comparison_digest,"
                       " observed_cost_microusd,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (selection_receipt_id, operation_receipt_id, group_id, group["task_id"],
                        group["contract_revision"], result_id, verdict_id, selected["attempt_id"],
                        head, base, digest, total_cost, _now()))
    await conn.execute("UPDATE comparison_groups SET state = 'chosen',selected_result_id = ?,"
                       " selected_verdict_id = ?,selection_receipt_id = ?,selected_at = ? WHERE id = ?",
                       (result_id, verdict_id, selection_receipt_id, _now(), group_id))
    # The selected verdict already passed independent verification; operator approval still follows merge.
    await conn.execute("UPDATE board_tasks SET current_attempt_id = ?,accepted_result_id = NULL,"
                       " accepted_contract_revision = NULL,acceptance_state = 'accepted',"
                       " status = 'review',folder_id = ?,branch = ?,merge_state = 'proposed'"
                       " WHERE id = ?", (selected["attempt_id"], source["folder_id"],
                                        source["branch"], group["task_id"]))
    return {"group_id": group_id, "selected_result_id": result_id,
            "selected_verdict_id": verdict_id, "selection_receipt_id": selection_receipt_id,
            "comparison_digest": digest, "observed_cost_microusd": total_cost,
            "state": "chosen"}


__all__ = ["AdmissionProof", "ComparisonRefused", "create_group", "admit_attempt",
           "comparison_readiness", "choose_result"]
