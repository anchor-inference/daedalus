"""Retain exact provider refusals and decisions to resume their sessions."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
from protocore.contracts.llm import LLMRequest

from daedalus.providers.failure_evidence import FailureEvidence
from daedalus.providers.openai_compat import ProviderEndpoint
from daedalus.stores.control import ControlConflict, now, one
from daedalus.stores.database import Database

EXHAUSTED_CLASSES = ("quota", "billing", "auth")
"""Failure classes that stay true until someone or something changes the account, unlike a rate limit."""

UNDATED_REST = timedelta(hours=1)
"""How long a quota or billing refusal that named no reset still counts against its provider. Long enough
that a chain does not retry an empty account on every run, short enough that a top-up is noticed within
the hour; an auth refusal with no date is not rested at all, as a key is usually fixed at once."""

PROVIDER_RESUME_NOTE = (
    "[The provider declared that its rate limit should reset. The operator approved continuing "
    "this session on the same provider and model. Continue from the failed turn without changing the task.]"
)


class ProviderHolds:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def failure(self, endpoint: ProviderEndpoint, request: LLMRequest,
                      reservation_id: str | None, evidence: FailureEvidence) -> str | None:
        """The adapter supplies this fact after HTTP replied; no request body is retained."""
        observer = request.observability
        if observer is None or not observer.session_id or not observer.run_id:
            return None
        async with self.db.transaction() as conn:
            run = await one(conn, "SELECT r.session_id,s.project_id FROM runs r JOIN sessions s ON s.id = r.session_id"
                            " WHERE r.id = ?", (observer.run_id,))
            if run is None or run["session_id"] != observer.session_id:
                return None
            attempt_id = None
            if reservation_id:
                reservation = await one(conn, "SELECT execution_attempt_id,session_id,run_id,provider_id,model"
                                        " FROM inference_reservations WHERE id = ?", (reservation_id,))
                if reservation is None or (reservation["session_id"], reservation["run_id"],
                                           reservation["provider_id"], reservation["model"]) != (
                                               observer.session_id, observer.run_id, endpoint.id, request.model):
                    raise ControlConflict("the refusal does not belong to its inference reservation")
                attempt_id = reservation["execution_attempt_id"]
            observation_id = uuid.uuid4().hex
            await conn.execute(
                "INSERT INTO provider_failure_observations(id,session_id,run_id,inference_reservation_id,"
                "execution_attempt_id,project_id,provider_id,provider_kind,provider_source_digest,model,status,failure_class,"
                "provider_code,reset_at,reset_source,retry_after_at,evidence_digest,observed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, observer.session_id, observer.run_id, reservation_id, attempt_id,
                 run["project_id"] or None, endpoint.id, endpoint.kind, endpoint.source_digest(), request.model, evidence.status,
                 evidence.failure_class, evidence.provider_code, evidence.reset_at, evidence.reset_source,
                 evidence.retry_after_at, evidence.digest, now()),
            )
            return observation_id


async def failure_view_in(conn: aiosqlite.Connection, observation_id: str) -> aiosqlite.Row:
    row = await one(conn, "SELECT * FROM provider_failure_observations WHERE id = ?", (observation_id,))
    if row is None:
        raise KeyError(observation_id)
    return row


async def _session_safe_in(conn: aiosqlite.Connection, session_id: str,
                           project_id: str | None) -> None:
    session = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?", (session_id,))
    if session is None or (session["project_id"] or None) != project_id:
        raise ControlConflict("the observed session changed scope")
    metadata = json.loads(session["metadata"])
    worker = await one(conn, "SELECT 1 FROM staff_sessions WHERE session_id = ? LIMIT 1", (session_id,))
    if worker is not None or metadata.get("staff_session_id") or metadata.get("subagent_of"):
        raise ControlConflict("a worker or child needs a new approved execution, not an old-run resume")
    if metadata.get("orchestrator_of"):
        office = await one(conn, "SELECT settings FROM projects WHERE id = ?",
                           (metadata["orchestrator_of"],))
        current = json.loads(office["settings"]).get("orchestrator", {}) if office else {}
        if (metadata["orchestrator_of"] != session["project_id"] or not current.get("enabled")
                or current.get("session_id") != session_id):
            raise ControlConflict("the coordinator was disabled or replaced")


async def validate_hold_in(conn: aiosqlite.Connection, observation_id: str, *,
                           allowed_input_id: str | None = None) -> aiosqlite.Row:
    """A held run has failed and no later run can quietly replace its model or context."""
    observation = await failure_view_in(conn, observation_id)
    if observation["failure_class"] != "rate" or not observation["reset_at"]:
        raise ControlConflict("this provider did not supply a verified rate reset")
    latest = await one(conn, "SELECT id FROM provider_failure_observations WHERE run_id = ?"
                       " ORDER BY rowid DESC LIMIT 1", (observation["run_id"],))
    if latest is None or latest["id"] != observation_id:
        raise ControlConflict("a later provider refusal replaced this observation")
    run = await one(conn, "SELECT status,session_id,created_at,updated_at FROM runs WHERE id = ?",
                    (observation["run_id"],))
    if run is None or run["session_id"] != observation["session_id"] or run["status"] != "error":
        raise ControlConflict("the observed run has not settled as failed")
    latest_run = await one(conn, "SELECT id FROM runs WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
                           (observation["session_id"],))
    if latest_run is None or latest_run["id"] != observation["run_id"]:
        raise ControlConflict("another run already moved the session beyond this refusal")
    newer_input = await one(conn, "SELECT 1 FROM input_receipts WHERE session_id = ? AND created_at > ?"
                            " AND client_message_id != ? LIMIT 1",
                            (observation["session_id"], run["updated_at"], allowed_input_id or ""))
    if newer_input is not None:
        raise ControlConflict("new operator input changed the failed session context")
    if allowed_input_id:
        input_row = await one(conn, "SELECT kind,payload FROM input_receipts WHERE session_id = ?"
                              " AND client_message_id = ?", (observation["session_id"], allowed_input_id))
        if input_row is not None:
            payload = json.loads(input_row["payload"])
            if (input_row["kind"] != "input" or payload.get("text") != PROVIDER_RESUME_NOTE
                    or payload.get("origin") != "core" or payload.get("as_answer") is not False
                    or payload.get("steer") is not False or payload.get("attachments") != []):
                raise ControlConflict("the resume input receipt is not the approved continuation")
    if observation["execution_attempt_id"]:
        raise ControlConflict("a worker or child needs a new approved execution, not an old-run resume")
    await _session_safe_in(conn, observation["session_id"], observation["project_id"])
    return observation


async def create_hold_in(conn: aiosqlite.Connection, *, hold_id: str, observation_id: str,
                         receipt_id: str) -> dict[str, Any]:
    observation = await validate_hold_in(conn, observation_id)
    scope_kind = "project" if observation["project_id"] else "global"
    scope_id = observation["project_id"] or "global"
    created_at = now()
    await conn.execute(
        "INSERT INTO provider_resume_holds(id,observation_id,session_id,failed_run_id,project_id,"
        "provider_id,model,scope_kind,scope_id,created_receipt_id,"
        "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'held',?,?)",
        (hold_id, observation_id, observation["session_id"], observation["run_id"],
         observation["project_id"], observation["provider_id"], observation["model"],
         scope_kind, scope_id, receipt_id,
         created_at, created_at),
    )
    await conn.execute("INSERT INTO provider_hold_events(id,hold_id,event,receipt_id,occurred_at)"
                       " VALUES (?,?,?,?,?)", (uuid.uuid4().hex, hold_id, "created", receipt_id, created_at))
    return {"hold_id": hold_id, "observation_id": observation_id, "state": "held",
            "session_id": observation["session_id"], "failed_run_id": observation["run_id"],
            "provider_id": observation["provider_id"], "model": observation["model"],
            "reset_at": observation["reset_at"]}


async def queue_resume_in(conn: aiosqlite.Connection, *, hold_id: str, action_id: str,
                          receipt_id: str) -> dict[str, Any]:
    hold = await one(conn, "SELECT * FROM provider_resume_holds WHERE id = ?", (hold_id,))
    if hold is None:
        raise KeyError(hold_id)
    if hold["state"] != "held":
        raise ControlConflict("the provider hold has already been decided")
    observation = await validate_hold_in(conn, hold["observation_id"])
    if (observation["session_id"], observation["run_id"], observation["provider_id"],
            observation["model"]) != (
                hold["session_id"], hold["failed_run_id"], hold["provider_id"],
                hold["model"]):
        raise ControlConflict("the held provider source changed")
    if observation["reset_at"] > now():
        raise ControlConflict("the provider's declared reset has not arrived")
    await conn.execute("UPDATE provider_resume_holds SET state = 'resume_queued',resume_action_id = ?,"
                       "updated_at = ? WHERE id = ? AND state = 'held'", (action_id, now(), hold_id))
    await conn.execute("INSERT INTO provider_hold_events(id,hold_id,event,receipt_id,action_id,occurred_at)"
                       " VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, hold_id, "resume_queued", receipt_id, action_id, now()))
    return {"hold_id": hold_id, "state": "resume_queued", "effect_id": action_id,
            "session_id": hold["session_id"], "failed_run_id": hold["failed_run_id"],
            "provider_id": hold["provider_id"], "model": hold["model"]}


async def recovery_candidate_in(conn: aiosqlite.Connection, session_id: str,
                                failed_run_id: str) -> bool:
    """A declared reset waits for a human decision instead of the old outage timer."""
    hold = await one(conn, "SELECT 1 FROM provider_resume_holds WHERE session_id = ?"
                     " AND failed_run_id = ? AND state IN ('held','resume_queued','unknown') LIMIT 1",
                     (session_id, failed_run_id))
    if hold is not None:
        return True
    row = await one(conn, "SELECT failure_class,reset_at FROM provider_failure_observations"
                    " WHERE session_id = ? AND run_id = ? ORDER BY rowid DESC LIMIT 1",
                    (session_id, failed_run_id))
    return bool(row and row["failure_class"] == "rate" and row["reset_at"])


async def resume_target_in(conn: aiosqlite.Connection, session_id: str, failed_run_id: str,
                           provider_id: str, model: str, client_message_id: str) -> dict[str, Any]:
    """The runner may send only the model selected by one committed operator command."""
    prefix = "provider-resume:"
    if not client_message_id.startswith(prefix) or not client_message_id[len(prefix):]:
        raise ControlConflict("the continuation has no exact effect identity")
    action_id = client_message_id[len(prefix):]
    hold = await one(conn, "SELECT h.*,e.state AS effect_state FROM provider_resume_holds h"
                     " JOIN effect_outbox e ON e.id = h.resume_action_id"
                     " WHERE h.resume_action_id = ?", (action_id,))
    if (hold is None or hold["state"] != "resume_queued" or hold["effect_state"] != "claimed"
            or (hold["session_id"], hold["failed_run_id"], hold["provider_id"], hold["model"]) != (
                session_id, failed_run_id, provider_id, model)):
        raise ControlConflict("the provider continuation is not the claimed approved command")
    observation = await validate_hold_in(conn, hold["observation_id"],
                                         allowed_input_id=client_message_id)
    if observation["reset_at"] > now():
        raise ControlConflict("the provider's declared reset has not arrived")
    return {**dict(hold), "provider_source_digest": observation["provider_source_digest"]}


async def pin_resumed_run_in(conn: aiosqlite.Connection, *, action_id: str,
                             session_id: str, run_id: str) -> None:
    """Pin the resumed run in the same transaction that creates it, before its task starts."""
    hold = await one(conn, "SELECT h.id,h.session_id,h.state,e.state AS effect_state"
                     " FROM provider_resume_holds h JOIN effect_outbox e ON e.id = h.resume_action_id"
                     " WHERE h.resume_action_id = ?", (action_id,))
    run = await one(conn, "SELECT session_id FROM runs WHERE id = ?", (run_id,))
    if (hold is None or hold["session_id"] != session_id or hold["state"] != "resume_queued"
            or hold["effect_state"] != "claimed" or run is None or run["session_id"] != session_id):
        raise ControlConflict("the resumed run is not owned by the claimed hold")
    receipt = await one(conn, "SELECT kind,payload FROM input_receipts WHERE session_id = ?"
                        " AND client_message_id = ?", (session_id, f"provider-resume:{action_id}"))
    if receipt is None or receipt["kind"] != "input":
        raise ControlConflict("the resumed run has no exact input receipt")
    payload = json.loads(receipt["payload"])
    if (payload.get("text") != PROVIDER_RESUME_NOTE or payload.get("origin") != "core"
            or payload.get("as_answer") is not False or payload.get("steer") is not False
            or payload.get("attachments") != []):
        raise ControlConflict("the resumed run input differs from its approved continuation")
    existing = await one(conn, "SELECT run_id FROM provider_hold_events WHERE hold_id = ?"
                         " AND event = 'run_pinned'", (hold["id"],))
    if existing is not None:
        if existing["run_id"] != run_id:
            raise ControlConflict("the hold is already bound to another resumed run")
        return
    await conn.execute("INSERT INTO provider_hold_events(id,hold_id,event,action_id,run_id,occurred_at)"
                       " VALUES (lower(hex(randomblob(16))),?,'run_pinned',?,?,?)",
                       (hold["id"], action_id, run_id, now()))


async def pinned_target_in(conn: aiosqlite.Connection, session_id: str,
                           run_id: str) -> dict[str, Any] | None:
    """Recover only the one model attached to this run before it could reach transport."""
    row = await one(conn, "SELECT h.*,p.action_id,o.provider_source_digest FROM provider_hold_events p"
                    " JOIN provider_resume_holds h ON h.id = p.hold_id"
                    " JOIN provider_failure_observations o ON o.id = h.observation_id"
                    " WHERE p.event = 'run_pinned' AND p.run_id = ? AND h.session_id = ?",
                    (run_id, session_id))
    if row is None:
        return None
    run = await one(conn, "SELECT session_id FROM runs WHERE id = ?", (run_id,))
    latest = await one(conn, "SELECT id FROM runs WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
                       (session_id,))
    if (run is None or run["session_id"] != session_id or latest is None or latest["id"] != run_id
            or row["resume_action_id"] != row["action_id"]
            or row["state"] not in ("resume_queued", "unknown", "resumed")):
        raise ControlConflict("the pinned continuation was replaced or invalidated")
    receipt = await one(conn, "SELECT kind,payload,status,run_id FROM input_receipts WHERE session_id = ?"
                        " AND client_message_id = ?", (session_id, f"provider-resume:{row['action_id']}"))
    if receipt is None or receipt["kind"] != "input":
        raise ControlConflict("the pinned run has no matching input receipt")
    payload = json.loads(receipt["payload"])
    if (payload.get("text") != PROVIDER_RESUME_NOTE or payload.get("origin") != "core"
            or payload.get("as_answer") is not False or payload.get("steer") is not False
            or payload.get("attachments") != [] or (receipt["run_id"] and receipt["run_id"] != run_id)):
        raise ControlConflict("the pinned run input changed")
    await _session_safe_in(conn, session_id, row["project_id"])
    return {"hold_id": row["id"], "action_id": row["action_id"], "provider_id": row["provider_id"],
            "model": row["model"], "run_id": run_id, "session_id": session_id,
            "provider_source_digest": row["provider_source_digest"]}


async def resting_providers_in(conn: aiosqlite.Connection, provider_ids: list[str],
                               at: datetime | None = None) -> dict[str, str]:
    """Provider id to why it is known to be unusable right now, for the ones that are.

    Scoped to the provider and not the model: a subscription or key is exhausted as a whole, so a refusal
    on one model of it says the next model would be refused too. Only the latest observation counts, so a
    later transient refusal (the account answered) lifts the rest. Reads the ``provider_failure_by_provider``
    index, one row per provider.
    """
    moment = at or datetime.now(UTC)
    resting: dict[str, str] = {}
    for provider_id in dict.fromkeys(provider_ids):
        row = await one(conn, "SELECT failure_class,reset_at,retry_after_at,observed_at"
                        " FROM provider_failure_observations WHERE provider_id = ?"
                        " ORDER BY observed_at DESC, rowid DESC LIMIT 1", (provider_id,))
        if row is None or row["failure_class"] not in EXHAUSTED_CLASSES:
            continue
        until = None
        try:
            for column in ("reset_at", "retry_after_at"):
                if row[column]:
                    until = max(filter(None, (until, datetime.fromisoformat(row[column]))))
            if until is None and row["failure_class"] != "auth":
                until = datetime.fromisoformat(row["observed_at"]) + UNDATED_REST
        except ValueError:
            continue
        if until is not None and until > moment:
            resting[provider_id] = (f"{provider_id} refused with {row['failure_class']} and is not expected to "
                                    f"answer before {until.isoformat(timespec='minutes')}")
    return resting
