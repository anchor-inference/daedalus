"""Project signed CI webhook facts onto exact repository and commit identities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from daedalus.extensions.inbound import webhook_facts

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
FINAL = {"success": "passed", "failure": "failed", "timed_out": "failed",
         "error": "failed", "startup_failure": "failed", "cancelled": "cancelled", "action_required": "failed",
         "neutral": "unknown", "skipped": "unknown", "stale": "unknown"}


def observation_from_webhook(provider: str, event: str, delivery_id: str, payload: dict[str, Any],
                             *, payload_digest: str) -> dict[str, Any] | None:
    """Return only identity-complete GitHub check facts; signature verification happens at ingress."""
    if not provider or not delivery_id or not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
        raise ValueError("signed delivery identity and payload digest are required")
    if provider != "github" or event not in ("check_run", "check_suite", "workflow_run", "status"):
        return None
    repo = payload.get("repository")
    run = payload if event == "status" else payload.get(event)
    if not isinstance(repo, dict) or not isinstance(run, dict):
        return None
    repository_id = repo.get("id")
    run_id = run.get("id")
    suite = run.get("check_suite")
    if event == "status":
        head = run.get("sha")
        check_name = run.get("context")
        state = "running" if run.get("state") == "pending" else "final"
        raw_conclusion = run.get("state")
    else:
        head = run.get("head_sha") or (suite.get("head_sha") if isinstance(suite, dict) else None)
        check_name = run.get("name") or run.get("display_title")
        state = "final" if run.get("status") == "completed" else "running"
        raw_conclusion = run.get("conclusion")
    if event == "check_suite":
        check_name = "check_suite"
    if (type(repository_id) is not int or not 0 < repository_id <= 2**63 - 1 or
            type(run_id) is not int or not 0 < run_id <= 2**63 - 1 or
            not isinstance(head, str) or not SHA.fullmatch(head.lower()) or
            not isinstance(check_name, str) or not check_name.strip()):
        return None
    run_attempt = run.get("run_attempt", 1)
    if type(run_attempt) is not int or not 0 < run_attempt <= 2**63 - 1:
        return None
    conclusion = FINAL.get(raw_conclusion, "unknown") if state == "final" else None
    return {"provider": provider, "delivery_id": delivery_id, "repository_id": str(repository_id),
            "head_sha": head.lower(), "run_id": str(run_id), "run_attempt": run_attempt,
            "check_name": check_name.strip()[:200], "state": state,
            "conclusion": conclusion, "payload_digest": payload_digest}


async def record_observation(conn: aiosqlite.Connection, observation: dict[str, Any], *,
                             event_seq: int | None = None) -> dict[str, Any]:
    """Append one trusted delivery in the same transaction as its dedupe and event row."""
    canonical = json.dumps(observation, sort_keys=True, separators=(",", ":"))
    observation_id = hashlib.sha256(canonical.encode()).hexdigest()
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO ci_observations(id,provider,delivery_id,repository_id,head_sha,run_id,"
        "run_attempt,check_name,state,conclusion,payload_digest,event_seq,observed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, observation["provider"], observation["delivery_id"],
         observation["repository_id"], observation["head_sha"], observation["run_id"],
         observation["run_attempt"], observation["check_name"], observation["state"],
         observation["conclusion"], observation["payload_digest"], event_seq,
         datetime.now(UTC).isoformat()),
    )
    if not cursor.rowcount:
        row = await conn.execute("SELECT id,payload_digest FROM ci_observations"
                                 " WHERE provider = ? AND delivery_id = ?",
                                 (observation["provider"], observation["delivery_id"]))
        stored = await row.fetchone()
        await row.close()
        if stored is None or stored["payload_digest"] != observation["payload_digest"]:
            raise ValueError("the delivery ID was reused for a different payload")
        return {"observation_id": stored["id"], "recorded": False}
    return {"observation_id": observation_id, "recorded": True}


async def record_signed_delivery(db: Any, bus: Any, inbound: Any, *, provider: str, event: str,
                                 delivery_id: str, payload: Any,
                                 payload_digest: str, summary: str,
                                 observe: Callable[[aiosqlite.Connection], Awaitable[Any]] | None = None) -> dict[str, Any]:
    """Persist dedupe, event and optional CI proof together after ingress checked the signature."""
    facts = webhook_facts(event, payload)
    observation = observation_from_webhook(provider, event, delivery_id, payload,
                                            payload_digest=payload_digest) if isinstance(payload, dict) else None
    async with bus.transaction_guard():
        async with db.transaction() as conn:
            cursor = await conn.execute("SELECT payload_digest FROM webhook_deliveries"
                                        " WHERE provider = ? AND delivery_id = ?", (provider, delivery_id))
            prior_delivery = await cursor.fetchone()
            await cursor.close()
            if prior_delivery is not None and prior_delivery["payload_digest"] not in (None, payload_digest):
                raise ValueError("the delivery ID was reused for a different payload")
            if observation is not None:
                cursor = await conn.execute("SELECT payload_digest FROM ci_observations"
                                            " WHERE provider = ? AND delivery_id = ?", (provider, delivery_id))
                prior = await cursor.fetchone()
                await cursor.close()
                if prior is not None:
                    if prior["payload_digest"] != payload_digest:
                        raise ValueError("the delivery ID was reused for a different payload")
                    return {"fresh": False, "observation_id": None, "event_seq": None}
            fresh = await inbound.record_delivery_in(conn, provider, delivery_id)
            if not fresh:
                return {"fresh": False, "observation_id": None, "event_seq": None}
            await conn.execute("UPDATE webhook_deliveries SET payload_digest = ?"
                               " WHERE provider = ? AND delivery_id = ?", (payload_digest, provider, delivery_id))
            accepted = await bus.persist_in(
                conn, "webhook.received",
                {"provider": provider, "event": event, "delivery_id": delivery_id,
                 "summary": summary, **facts},
            )
            recorded = await record_observation(conn, observation, event_seq=accepted.seq) if observation else None
            if observe is not None:
                await observe(conn)
        bus.announce_committed(accepted)
    return {"fresh": True, "observation_id": recorded["observation_id"] if recorded else None,
            "event_seq": accepted.seq, "observation": observation if recorded else None}


async def set_required_checks(conn: aiosqlite.Connection, *, task_id: str, provider: str,
                              repository_id: str, check_names: list[str], origin_ref: str) -> dict[str, Any]:
    """A check policy is a semantic task-contract revision, even when its text is unchanged."""
    from daedalus.extensions.orchestrator_domain import (
        DomainConflict,  # Lazy: verdicts read CI readiness, creating a cycle.
    )

    if (provider != "github" or not repository_id.isdecimal() or
            int(repository_id) <= 0 or not check_names or len(check_names) > 32 or
            any(not name.strip() or len(name) > 200 for name in check_names)):
        raise ValueError("CI checks need a provider, repository identity and bounded names")
    names = sorted({name.strip() for name in check_names})
    cursor = await conn.execute("SELECT contract_revision,status,current_attempt_id FROM board_tasks WHERE id = ?",
                                (task_id,))
    task = await cursor.fetchone()
    await cursor.close()
    if task is None:
        raise KeyError(task_id)
    if task["status"] in ("done", "archived", "dropped"):
        raise DomainConflict("reopen the task before changing required CI")
    if task["status"] == "review":
        cursor = await conn.execute("SELECT 1 FROM task_merge_receipts m JOIN effect_outbox e ON e.id = m.id"
                                    " WHERE m.task_id = ? AND e.state IN ('pending','claimed','unknown') LIMIT 1",
                                    (task_id,))
        pending_merge = await cursor.fetchone()
        await cursor.close()
        if pending_merge is not None:
            raise DomainConflict("reconcile the pending merge before changing required CI")
    if task["current_attempt_id"]:
        cursor = await conn.execute("SELECT state FROM execution_attempts WHERE id = ?",
                                    (task["current_attempt_id"],))
        attempt = await cursor.fetchone()
        await cursor.close()
        if attempt is not None and attempt["state"] in ("queued", "starting", "running", "waiting", "recovering"):
            raise DomainConflict("stop the active attempt before revising required CI")
    cursor = await conn.execute("SELECT snapshot_json FROM task_contract_versions"
                                " WHERE task_id = ? AND contract_revision = ?",
                                (task_id, task["contract_revision"]))
    old = await cursor.fetchone()
    await cursor.close()
    if old is None:
        raise DomainConflict("the current contract version is missing")
    snapshot = json.loads(old["snapshot_json"])
    selected = [{"provider": provider, "repository_id": repository_id, "check_name": name} for name in names]
    if snapshot.get("ci_checks", []) == selected:
        raise DomainConflict("required CI already matches this contract")
    revision = int(task["contract_revision"]) + 1
    snapshot["ci_checks"] = selected
    await conn.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                       "snapshot_json,created_at) VALUES (?,?,?,?,?,?)",
                       (task_id, revision, "operator", origin_ref,
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat()))
    for name in names:
        await conn.execute("INSERT INTO ci_required_checks(task_id,contract_revision,provider,repository_id,"
                           "check_name,created_at) VALUES (?,?,?,?,?,?)",
                           (task_id, revision, provider, repository_id, name, datetime.now(UTC).isoformat()))
    if task["status"] == "review":
        # A new CI contract cannot inherit an old review result or verdict. The policy change is the
        # operator's explicit return, even when no verdict exists to use the result-return command.
        await conn.execute("UPDATE board_tasks SET contract_revision = ?,status = 'todo',"
                           " current_attempt_id = NULL,acceptance_state = 'returned' WHERE id = ?",
                           (revision, task_id))
        await conn.execute("UPDATE open_loops SET closed_at = ?,closed_by = 'system',decision = ?"
                           " WHERE task_id = ? AND contract_revision = ? AND cause = 'report_done'"
                           " AND closed_at IS NULL",
                           (datetime.now(UTC).isoformat(), "returned for CI requirements", task_id,
                            task["contract_revision"]))
    else:
        await conn.execute("UPDATE board_tasks SET contract_revision = ?,acceptance_state = 'returned' WHERE id = ?",
                           (revision, task_id))
    await conn.execute("UPDATE next_actions SET state = 'cancelled' WHERE task_id = ?"
                       " AND contract_revision < ? AND state = 'active'", (task_id, revision))
    return {"task_id": task_id, "contract_revision": revision, "required_checks": selected,
            "returned_from_review": task["status"] == "review"}


async def ci_readiness(conn: aiosqlite.Connection, task_id: str, contract_revision: int,
                       head_sha: str | None) -> dict[str, Any]:
    """A prior green head never satisfies a new head; missing order or check remains unknown.

    A task with no declared checks is ``not_required``: the reviewer's verdict and the operator's
    acceptance are its gate, and CI joins it only once the operator names checks for the task.
    """
    cursor = await conn.execute("SELECT provider,repository_id,check_name FROM ci_required_checks"
                                " WHERE task_id = ? AND contract_revision = ? ORDER BY check_name",
                                (task_id, contract_revision))
    required = await cursor.fetchall()
    await cursor.close()
    if not required:
        # Blocking on absent checks made every branch task in a local repository unmergeable: most
        # folders have no GitHub CI at all, and the review card told the operator to invent some.
        return {"state": "not_required", "checks": []}
    checks = []
    for item in required:
        cursor = await conn.execute(
            "SELECT run_id,run_attempt,state,conclusion,delivery_id,observed_at FROM ci_observations"
            " WHERE provider = ? AND repository_id = ? AND head_sha = ? AND check_name = ?"
            " ORDER BY CAST(run_id AS INTEGER) DESC,run_attempt DESC,observed_at DESC,id DESC",
            (item["provider"], item["repository_id"], head_sha or "", item["check_name"]),
        )
        observations = await cursor.fetchall()
        await cursor.close()
        latest = observations[0] if observations else None
        finals = [row for row in observations if latest is not None and row["run_id"] == latest["run_id"]
                  and row["run_attempt"] == latest["run_attempt"] and row["state"] == "final"]
        # Arrival order is not provider order: a delayed running event cannot undo a final result.
        # Conflicting final facts for the same run and attempt cannot prove that its head passed.
        conclusions = {row["conclusion"] for row in finals}
        state = next(iter(conclusions)) if len(conclusions) == 1 else "unknown"
        if finals:
            latest = finals[0]
        checks.append({"provider": item["provider"], "repository_id": item["repository_id"],
                       "check_name": item["check_name"], "head_sha": head_sha,
                       "state": state, "delivery_id": latest["delivery_id"] if latest else None})
    return {"state": "passed" if all(check["state"] == "passed" for check in checks) else "blocked",
            "checks": checks}


__all__ = ["observation_from_webhook", "record_observation", "record_signed_delivery",
           "set_required_checks", "ci_readiness"]
