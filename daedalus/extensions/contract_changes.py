"""Stage a contract change until the preceding execution has physically stopped."""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from daedalus.extensions.orchestrator_domain import DomainConflict, capture_contract_change
from daedalus.extensions.task_contract import REQUIREMENT_KINDS, REQUIREMENT_MAX_CHARS, REQUIREMENTS_MAX
from daedalus.stores.control import Principal, canonical, now, one
from daedalus.stores.runtime_release import attempt_released_in


async def _exited_attempt(conn: aiosqlite.Connection, attempt_id: str) -> bool:
    released = await attempt_released_in(conn, attempt_id)
    ended = await one(conn, "SELECT 1 FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id"
                      " WHERE a.id = ? AND s.ended_at IS NOT NULL", (attempt_id,))
    return released and ended is not None


async def stage_change_in(conn: aiosqlite.Connection, *, intent_id: str, receipt_id: str,
                          principal: Principal, project_id: str, task_id: str,
                          client_operation_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Remember the full request before asking another command to stop its current attempt."""
    task = await one(conn, "SELECT project_id,status,contract_revision,current_attempt_id"
                     " FROM board_tasks WHERE id = ?", (task_id,))
    if task is None or task["project_id"] != project_id:
        raise DomainConflict("the requested task is outside the project")
    if task["status"] in ("review", "done", "dropped"):
        raise DomainConflict("return or reopen the exact result before changing its contract")
    comparison = await one(conn, "SELECT id FROM comparison_groups WHERE task_id = ?"
                           " AND state IN ('planned','active','ready') LIMIT 1", (task_id,))
    if comparison is not None:
        raise DomainConflict("choose or reconcile the active comparison before changing its contract")
    if request["mode"] != "withdraw":
        active = await one(conn, "SELECT COUNT(*) AS count FROM task_requirements"
                           " WHERE task_id = ? AND state = 'active'", (task_id,))
        if active is None or active["count"] - (1 if request["mode"] == "replace" else 0) >= REQUIREMENTS_MAX:
            raise DomainConflict(f"the card already has {REQUIREMENTS_MAX} requirements in force; merge some before adding more")
        duplicate = await one(conn, "SELECT id FROM task_requirements WHERE task_id = ?"
                              " AND state = 'active' AND lower(text) = lower(?) AND file_id IS ?",
                              (task_id, request["text"], request.get("file_id")))
        if duplicate is not None and duplicate["id"] != request.get("target_id"):
            raise DomainConflict("the same requirement is already active")
    pending = await one(conn, "SELECT id FROM contract_change_intents WHERE task_id = ?"
                        " AND state IN ('pending_stop','ready') LIMIT 1", (task_id,))
    if pending is not None:
        raise DomainConflict("another contract change awaits the task's physical stop or application")
    attempt_id = task["current_attempt_id"]
    if attempt_id is not None:
        attempt = await one(conn, "SELECT state FROM execution_attempts WHERE id = ?", (attempt_id,))
        if attempt is None:
            raise DomainConflict("the task's current attempt is missing")
        pending_stop = not await _exited_attempt(conn, attempt_id)
    else:
        pending_stop = False
        if task["status"] in ("doing", "blocked"):
            raise DomainConflict("the task claims active work without an execution identity")
    waiting_launch = await one(conn, "SELECT id,state FROM effect_outbox WHERE kind = 'task.launch'"
                               " AND state IN ('pending','claimed','unknown')"
                               " AND json_extract(payload_json,'$.control.task_id') = ? LIMIT 1", (task_id,))
    if waiting_launch is not None and (waiting_launch["state"] != "pending" or attempt_id is not None):
        raise DomainConflict("reconcile the existing launch before changing the contract")
    pending_stop = pending_stop or waiting_launch is not None
    state = "pending_stop" if pending_stop else "ready"
    await conn.execute("INSERT INTO contract_change_intents(id,project_id,task_id,actor_id,grant_id,operation_kind,"
                       " client_operation_id,operation_receipt_id,base_contract_revision,base_attempt_id,"
                       " request_json,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (intent_id, project_id, task_id, principal.actor_id, principal.grant_id,
                        "contract.withdraw" if request["mode"] == "withdraw" else "contract.require",
                        client_operation_id, receipt_id, task["contract_revision"], attempt_id,
                        canonical(request), state, now()))
    return {"intent_id": intent_id, "task_id": task_id, "state": state,
            "base_contract_revision": task["contract_revision"], "base_attempt_id": attempt_id}


async def link_stop_in(conn: aiosqlite.Connection, *, intent_id: str, actor_id: str,
                       stop_receipt_id: str, stop_effect_id: str | None) -> None:
    """Project the separately receipted stop on the staged request for inspection."""
    intent = await one(conn, "SELECT task_id,state,stop_effect_id FROM contract_change_intents"
                       " WHERE id = ? AND actor_id = ?", (intent_id, actor_id))
    receipt = await one(conn, "SELECT actor_id,operation_kind,response_json FROM operation_receipts WHERE id = ?",
                        (stop_receipt_id,))
    response = json.loads(receipt["response_json"]) if receipt is not None else {}
    effect = await one(conn, "SELECT kind,json_extract(payload_json,'$.control.task_id') AS task_id,"
                       " receipt_id FROM effect_outbox WHERE id = ?",
                       (stop_effect_id,)) if stop_effect_id else None
    if (intent is None or intent["state"] != "pending_stop" or receipt is None or
            receipt["actor_id"] != actor_id or receipt["operation_kind"] != "task.stop" or
            response.get("task_id") != intent["task_id"] or
            (stop_effect_id is not None and (effect is None or effect["kind"] != "task.stop" or
             effect["task_id"] != intent["task_id"] or effect["receipt_id"] != stop_receipt_id)) or
            (stop_effect_id is None and not response.get("cancelled_launches"))):
        raise DomainConflict("the stop receipt does not belong to this contract request")
    if intent["stop_effect_id"] and intent["stop_effect_id"] != stop_effect_id:
        previous = await one(conn, "SELECT state FROM effect_outbox WHERE id = ?",
                             (intent["stop_effect_id"],))
        if previous is None or previous["state"] not in ("cancelled", "failed"):
            raise DomainConflict("the previous stop effect still needs exact outcome reconciliation")
    await conn.execute("UPDATE contract_change_intents SET stop_receipt_id = ?,stop_effect_id = ? WHERE id = ?",
                       (stop_receipt_id, stop_effect_id, intent_id))


async def apply_change_in(conn: aiosqlite.Connection, *, intent_id: str, principal: Principal,
                          receipt_id: str) -> dict[str, Any]:
    """Apply only after exact physical exit and while the staged contract is still current."""
    intent = await one(conn, "SELECT * FROM contract_change_intents WHERE id = ?", (intent_id,))
    if intent is None or intent["actor_id"] != principal.actor_id:
        raise DomainConflict("the staged contract change is outside this actor's authority")
    if intent["state"] == "applied":
        raise DomainConflict("the contract change was already applied")
    if intent["state"] not in ("ready", "pending_stop"):
        raise DomainConflict("the contract change needs operator reconciliation")
    task = await one(conn, "SELECT project_id,status,contract_revision,current_attempt_id FROM board_tasks"
                     " WHERE id = ?", (intent["task_id"],))
    if task is None or task["project_id"] != intent["project_id"] or (
            task["contract_revision"] != intent["base_contract_revision"]):
        raise DomainConflict("the task changed since the contract request was staged")
    if task["current_attempt_id"] != intent["base_attempt_id"]:
        raise DomainConflict("another execution replaced the staged attempt")
    comparison = await one(conn, "SELECT id FROM comparison_groups WHERE task_id = ?"
                           " AND state IN ('planned','active','ready') LIMIT 1", (intent["task_id"],))
    if comparison is not None:
        raise DomainConflict("choose or reconcile the active comparison before changing its contract")
    if intent["state"] == "ready" and intent["base_attempt_id"] is not None:
        if not await _exited_attempt(conn, intent["base_attempt_id"]):
            raise DomainConflict("the preceding execution lacks an exact physical exit")
    if intent["state"] == "pending_stop":
        receipt = await one(conn, "SELECT response_json FROM operation_receipts WHERE id = ?",
                            (intent["stop_receipt_id"],)) if intent["stop_receipt_id"] else None
        if receipt is None:
            raise DomainConflict("the exact stop operation has not been receipted")
        if intent["base_attempt_id"] is None:
            stopped = json.loads(receipt["response_json"]).get("cancelled_launches", [])
            if not stopped:
                raise DomainConflict("the queued launch was not cancelled")
            for action_id in stopped:
                action = await one(conn, "SELECT state FROM effect_outbox WHERE id = ?", (action_id,))
                if action is None or action["state"] != "cancelled":
                    raise DomainConflict("the queued launch is not confirmed cancelled")
        else:
            attempt = await one(conn, "SELECT state,staff_session_id FROM execution_attempts WHERE id = ?",
                                (intent["base_attempt_id"],))
            if attempt is None or attempt["state"] != "cancelled" or intent["stop_effect_id"] is None:
                raise DomainConflict("the preceding execution has not been stopped and reconciled")
            stop = await one(conn, "SELECT state FROM effect_outbox WHERE id = ?", (intent["stop_effect_id"],))
            exited = await one(conn, "SELECT 1 FROM runtime_exit_observations e"
                               " JOIN execution_attempts a ON a.id = e.attempt_id"
                               " WHERE e.attempt_id = ? AND e.host_generation = a.host_generation"
                               " AND e.contract_revision = a.contract_revision"
                               " AND e.staff_session_id = a.staff_session_id"
                               " AND e.provider_session_ref = a.provider_session_ref",
                               (intent["base_attempt_id"],))
            if stop is None or stop["state"] != "completed" or exited is None:
                raise DomainConflict("the exact physical exit proof is missing")
            if attempt["staff_session_id"]:
                session = await one(conn, "SELECT ended_at FROM staff_sessions WHERE id = ?",
                                    (attempt["staff_session_id"],))
                if session is None or session["ended_at"] is None:
                    raise DomainConflict("the preceding staff session has not ended")
    request = json.loads(intent["request_json"])
    literal_source = request.get("source_literal", "")
    if literal_source.startswith("rule:"):
        rule_id = literal_source.partition(":")[2]
        if not rule_id.isdigit():
            raise DomainConflict("the staged rule source is invalid")
        rule = await one(conn, "SELECT 1 FROM project_journal r WHERE r.id = ? AND r.project_id = ?"
                         " AND r.kind = 'rule' AND NOT EXISTS (SELECT 1 FROM project_journal l"
                         " WHERE l.project_id = r.project_id AND l.kind = 'rule_lifted'"
                         " AND CAST(json_extract(l.refs_json,'$.rule_id') AS INTEGER) = r.id)",
                         (int(rule_id), intent["project_id"]))
        if rule is None:
            raise DomainConflict("the staged operator rule is no longer in force")
    elif literal_source.startswith("answer:"):
        short_id = literal_source.partition(":")[2]
        answer = await one(conn, "SELECT 1 FROM asks WHERE project_id = ? AND short_id = ?"
                           " AND resolved_by = 'operator' LIMIT 1", (intent["project_id"], short_id))
        if answer is None:
            raise DomainConflict("the staged operator answer is no longer valid")
    mode = request["mode"]
    if request.get("kind") not in REQUIREMENT_KINDS and mode != "withdraw":
        raise DomainConflict("the staged requirement kind is invalid")
    if mode == "withdraw":
        target = await one(conn, "SELECT id,state FROM task_requirements WHERE id = ? AND task_id = ?",
                           (request["target_id"], intent["task_id"]))
        if target is None or target["state"] != "active":
            raise DomainConflict("the withdrawn requirement is no longer active")
        await conn.execute("UPDATE task_requirements SET state = 'withdrawn',updated_at = ? WHERE id = ?",
                           (now(), target["id"]))
        requirement_id = target["id"]
    else:
        if mode not in ("add", "replace"):
            raise ValueError("unsupported contract change")
        body = request["text"].strip()
        if not body or len(body) > REQUIREMENT_MAX_CHARS:
            raise DomainConflict("the staged requirement text is invalid")
        active = await one(conn, "SELECT COUNT(*) AS count FROM task_requirements"
                           " WHERE task_id = ? AND state = 'active'", (intent["task_id"],))
        if active is None or active["count"] - (1 if mode == "replace" else 0) >= REQUIREMENTS_MAX:
            raise DomainConflict("the task has reached its requirement limit")
        duplicate = await one(conn, "SELECT id FROM task_requirements WHERE task_id = ?"
                              " AND state = 'active' AND lower(text) = lower(?) AND file_id IS ?",
                              (intent["task_id"], body, request.get("file_id")))
        if duplicate is not None and duplicate["id"] != request.get("target_id"):
            raise DomainConflict("the same requirement is already active")
        if mode == "replace":
            target = await one(conn, "SELECT id,state FROM task_requirements WHERE id = ? AND task_id = ?",
                               (request["target_id"], intent["task_id"]))
            if target is None or target["state"] != "active":
                raise DomainConflict("the replaced requirement is no longer active")
            await conn.execute("UPDATE task_requirements SET state = 'superseded',updated_at = ? WHERE id = ?",
                               (now(), target["id"]))
        file_id = request.get("file_id")
        if file_id is not None:
            access = await one(conn, "SELECT 1 FROM file_access WHERE file_id = ? AND scope = ?",
                               (file_id, intent["project_id"]))
            if access is None:
                raise DomainConflict("the input file is no longer available in this project")
            await conn.execute("INSERT OR IGNORE INTO task_files(task_id,file_id,added_at,added_by)"
                               " VALUES (?,?,?,?)", (intent["task_id"], file_id, now(), principal.actor_id))
        next_number = await one(conn, "SELECT COALESCE(MAX(number),0)+1 AS n FROM task_requirements"
                                " WHERE task_id = ?", (intent["task_id"],))
        assert next_number is not None
        requirement_id = uuid.uuid5(uuid.NAMESPACE_URL, f"requirement:{intent_id}").hex[:20]
        await conn.execute("INSERT INTO task_requirements(id,task_id,project_id,number,text,kind,source,"
                           " state,replaces,file_id,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,?,?,'active',?,?,?,?)",
                           (requirement_id, intent["task_id"], intent["project_id"], next_number["n"],
                            request["text"], request["kind"], request["source"],
                            request.get("target_id"), file_id, now(), now()))
    revision = await capture_contract_change(conn, intent["task_id"], origin_kind="requirement",
                                             origin_ref=request["source"])
    if revision == intent["base_contract_revision"]:
        raise DomainConflict("the proposed requirement did not change the contract")
    await conn.execute("UPDATE board_tasks SET status = 'todo',current_attempt_id = NULL WHERE id = ?",
                       (intent["task_id"],))
    if mode != "withdraw" and request["kind"] == "constraint" and request["source"] == "orchestrator":
        grant = await one(conn, "SELECT number,text FROM task_requirements WHERE task_id = ?"
                          " AND kind = 'scope' AND state = 'active'"
                          " AND (source = 'operator' OR source LIKE 'operator:%' OR source LIKE 'answer:%')"
                          " ORDER BY number LIMIT 1", (intent["task_id"],))
        if grant is not None:
            explanation = " ".join(request.get("why", "").split())
            if len(explanation) < 8:
                raise DomainConflict("a condition narrowing operator scope needs a reason")
            await conn.execute("INSERT INTO project_journal(project_id,at,author,kind,text,refs_json)"
                               " VALUES (?,?,?,'narrowing',?,?)",
                               (intent["project_id"], now(), "orchestrator",
                                f"Added R{next_number['n']} to task {intent['task_id']} against operator scope R{grant['number']}: "
                                f"{request['text']} — why: {explanation}",
                                canonical({"task_id": intent["task_id"], "intent_id": intent_id,
                                           "requirement_id": requirement_id, "contract_revision": revision})))
    await conn.execute("UPDATE contract_change_intents SET state = 'applied',apply_receipt_id = ?,applied_at = ?"
                       " WHERE id = ?", (receipt_id, now(), intent_id))
    return {"intent_id": intent_id, "task_id": intent["task_id"], "state": "applied",
            "requirement_id": requirement_id, "contract_revision": revision,
            "launch_required": True}
