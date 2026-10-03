"""Reserve each scheduled occurrence before its effect can leave the database."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from croniter import croniter

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.notifications import Draft
from daedalus.stores.control import (
    ControlConflict,
    ControlDenied,
    ControlStore,
    Entity,
    Principal,
    Scope,
    canonical,
    digest,
    now,
    one,
)
from daedalus.stores.outbox import Claim, OutboxStore
from daedalus.transport.telegram.front import is_subagent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from daedalus.app import Application


def _scope(schedule: dict[str, Any]) -> Scope:
    project_id = schedule.get("project_id")
    return Scope("project", project_id) if project_id else Scope("global", "global")


def _snapshot(schedule: dict[str, Any]) -> dict[str, Any]:
    return {key: schedule.get(key) for key in (
        "id", "name", "prompt", "kind", "target_session", "created_by_session", "run_in", "workspace",
        "files", "model", "project_id", "schedule_revision", "output_contract_json",
    )}


def _contract(schedule: dict[str, Any]) -> dict[str, Any]:
    kind = schedule.get("kind") or "agent"
    return {
        "kind": kind,
        "target_session": schedule.get("target_session"),
        "prompt_digest": digest(str(schedule.get("prompt") or "")),
        "delivery": {"message": "operator_notification", "lazy": "session_note",
                     "wake": "project_event", "agent": "session_run"}[kind],
    }


async def repoint_project_schedules_in(conn: Any, *, project_id: str, old_session_id: str | None,
                                       new_session_id: str) -> list[str]:
    """Retarget coordinator schedules in a proven office swap, leaving approval to the operator.

    The caller holds the project's authority effect lock outside its transaction. A grant
    cannot follow a new coordinator merely because an internal recovery moved the office.
    """
    target = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?", (new_session_id,))
    if (target is None or target["project_id"] != project_id
            or json.loads(target["metadata"] or "{}").get("orchestrator_of") != project_id):
        raise ControlDenied("the new coordinator session is outside the project")
    if old_session_id is not None:
        previous = await one(conn, "SELECT project_id FROM sessions WHERE id = ?", (old_session_id,))
        if previous is None or previous["project_id"] != project_id:
            raise ControlDenied("the former coordinator session is outside the project")
    async with conn.execute(
        "SELECT sc.*,s.metadata AS target_metadata,s.project_id AS target_project_id"
        " FROM schedules sc JOIN sessions s ON s.id = sc.target_session"
        " WHERE sc.deleted_at IS NULL AND s.project_id = ?"
        " AND ((? IS NOT NULL AND sc.target_session = ?)"
        " OR json_extract(s.metadata,'$.orchestrator_retired_of') = ?)"
        " ORDER BY sc.id", (project_id, old_session_id, old_session_id, project_id),
    ) as cursor:
        rows = await cursor.fetchall()
    changed: list[str] = []
    for row in rows:
        if row["project_id"] not in (None, project_id):
            raise ControlDenied("a schedule targets a different project")
        if row["target_project_id"] != project_id:
            raise ControlDenied("a schedule targets a session in a different project")
        if row["target_session"] == new_session_id and row["authority_state"] == "needs_approval":
            continue
        if row["target_session"] != old_session_id:
            metadata = json.loads(row["target_metadata"] or "{}")
            if metadata.get("orchestrator_retired_of") != project_id:
                raise ControlDenied("a schedule is aimed at a different project session")
        uncertain = await one(
            conn, "SELECT c.id FROM recurring_cycles c JOIN effect_outbox e ON e.id = c.effect_id"
            " WHERE c.schedule_id = ? AND e.state IN ('claimed','unknown') LIMIT 1", (row["id"],),
        )
        if uncertain is not None:
            raise ControlDenied("reconcile uncertain schedule delivery before handoff")
        if row["grant_id"]:
            grant = await one(conn, "SELECT actor_id,scope_kind,scope_id,project_id,generation,revoked_at"
                              " FROM actor_grants WHERE id = ?", (row["grant_id"],))
            if (grant is None or grant["actor_id"] != f"schedule:{row['id']}"
                    or grant["scope_kind"] != "project" or grant["scope_id"] != project_id
                    or grant["project_id"] != project_id or grant["generation"] != row["grant_generation"]):
                raise ControlDenied("the schedule grant no longer matches its project")
            if not grant["revoked_at"]:
                generation = int(grant["generation"]) + 1
                await conn.execute("UPDATE actor_grants SET revoked_at = ?,generation = ? WHERE id = ?",
                                   (now(), generation, row["grant_id"]))
                await conn.execute("INSERT INTO grant_events(grant_id,actor_id,generation,event,reason,at)"
                                   " VALUES (?,?,?,'revoked',?,?)",
                                   (row["grant_id"], "host:coordinator-handoff", generation,
                                    "coordinator handoff", now()))
            await conn.execute("UPDATE effect_outbox SET state = 'cancelled',error = ?"
                               " WHERE grant_id = ? AND state = 'pending'",
                               ("coordinator handoff", row["grant_id"]))
        revised = dict(row)
        revised["target_session"] = new_session_id
        await conn.execute(
            "UPDATE schedules SET project_id = ?,target_session = ?,schedule_revision = schedule_revision + 1,"
            "authority_state = 'needs_approval',grant_id = NULL,grant_generation = NULL,"
            "output_contract_json = ?,updated_at = ? WHERE id = ?",
            (project_id, new_session_id, canonical(_contract(revised)), now(), row["id"]),
        )
        changed.append(row["id"])
    return changed


class Recurring:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.db = app.db
        self.control = ControlStore(self.db)

    async def create(self, principal: Principal, *, name: str, prompt: str, cron: str | None,
                     run_at: str | None, kind: str, target_session: str | None,
                     project_id: str | None, expires_at: str, expected_collection_revision: int,
                     client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can approve a standing schedule")
        name, prompt = name.strip(), prompt.strip()
        if not name or len(name) > 160 or not prompt or len(prompt) > 4000:
            raise ValueError("a bounded name and prompt are required")
        if kind not in ("agent", "message", "lazy", "wake") or bool(cron) == bool(run_at):
            raise ValueError("choose one supported action and one time rule")
        if kind == "lazy" and not target_session:
            raise ValueError("a lazy note needs a target session")
        if kind == "wake" and (not project_id or not target_session):
            raise ValueError("a project wake needs its orchestrator session")
        if kind == "agent" and project_id and not target_session:
            raise ValueError("a project agent schedule needs its project session")
        if cron:
            if not croniter.is_valid(cron):
                raise ValueError("invalid cron expression")
            next_run = croniter(cron, datetime.now(UTC)).get_next(datetime).astimezone(UTC)
        else:
            next_run = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
            if next_run.tzinfo is None:
                raise ValueError("a timezone-aware run_at is required")
            next_run = next_run.astimezone(UTC)
        scope = Scope("project", project_id) if project_id else Scope("global", "global")
        payload = {"name": name, "prompt": prompt, "cron": cron, "run_at": run_at,
                   "kind": kind, "target_session": target_session, "project_id": project_id,
                   "expires_at": expires_at}
        key = (scope.kind, scope.id, principal.actor_id, "schedule.create", client_operation_id)
        reserved_id = "s" + uuid.uuid5(uuid.NAMESPACE_URL, canonical(key)).hex[:15]
        workspace = self.app.settings.workspaces_dir / f"sched-{reserved_id}"
        created_workspace = False
        if kind == "agent" and project_id is None and not workspace.exists():
            (workspace / "inbox").mkdir(parents=True)
            created_workspace = True

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            if target_session:
                owner = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?", (target_session,))
                if owner is None or owner["project_id"] != project_id:
                    raise ControlDenied("the target session does not belong to this schedule scope")
                if kind == "wake":
                    metadata = json.loads(owner["metadata"])
                    if metadata.get("orchestrator_of") != project_id:
                        raise ControlDenied("the wake target is not the project orchestrator")
            schedule_id = "s" + mutation.object_id[:15]
            if schedule_id != reserved_id:
                raise ControlConflict("schedule identity changed during creation")
            approval = await self.control.issue_grant_in(
                conn, principal, Principal(f"schedule:{schedule_id}", "system"), scope,
                operations=["schedule.fire"], effects=[f"schedule.{kind}"], expires_at=expires_at,
            )
            snapshot = {"kind": kind, "target_session": target_session, "prompt": prompt}
            contract = _contract(snapshot)
            await conn.execute(
                "INSERT INTO schedules(id,name,cron,run_at,prompt,files,model,recurring,enabled,workspace,"
                "next_run_at,created_by_session,created_at,kind,target_session,run_in,schedule_revision,project_id,"
                "actor_id,grant_id,grant_generation,authority_state,output_contract_json,timezone,updated_at)"
                " VALUES (?,?,?,?,?,'[]',NULL,?,1,?,?,NULL,?,?,?,'new',1,?,?,?,?, 'current',?,'UTC',?)",
                (schedule_id, name, cron, run_at, prompt, int(bool(cron)), str(workspace), next_run.isoformat(),
                 now(), kind, target_session, project_id, principal.actor_id, approval["grant_id"],
                 approval["generation"], canonical(contract), now()),
            )
            return {"id": schedule_id, "kind": kind, "next_run_at": next_run.isoformat(),
                    "schedule_revision": 1, "authority_state": "current", "output_contract": contract,
                    "grant_expires_at": approval["expires_at"]}

        try:
            return await self.control.mutate(principal, scope, "schedule.create", client_operation_id,
                                             expected_collection_revision, Entity("collection", scope.id), payload, effect)
        except Exception:
            if created_workspace:
                shutil.rmtree(workspace)
            raise

    async def approve(self, principal: Principal, schedule_id: str, *, expires_at: str,
                      expected_collection_revision: int, expected_schedule_revision: int,
                      client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can approve a standing schedule")
        row = await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            raise KeyError(schedule_id)
        schedule = dict(row)
        scope = _scope(schedule)
        payload = {"schedule_id": schedule_id, "expires_at": expires_at,
                   "expected_schedule_revision": expected_schedule_revision}

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            current = await one(conn, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if current is None:
                raise KeyError(schedule_id)
            if _scope(dict(current)) != scope or current["schedule_revision"] != expected_schedule_revision:
                raise ControlConflict("the schedule changed during approval", current_revision=current["schedule_revision"])
            uncertain = await one(conn, "SELECT c.id FROM recurring_cycles c JOIN effect_outbox e ON e.id=c.effect_id"
                                  " WHERE c.schedule_id = ? AND e.state IN ('claimed','unknown') LIMIT 1", (schedule_id,))
            if uncertain is not None:
                raise ControlDenied("reconcile an earlier uncertain occurrence before reapproval")
            if current["grant_id"]:
                await self.control.revoke_grant_in(conn, principal, current["grant_id"], reason="schedule reapproved")
            approval = await self.control.issue_grant_in(
                conn, principal, Principal(f"schedule:{schedule_id}", "system"), scope,
                operations=["schedule.fire"], effects=[f"schedule.{current['kind'] or 'agent'}"],
                expires_at=expires_at,
            )
            revision = int(current["schedule_revision"])
            contract = _contract(dict(current))
            await conn.execute("UPDATE schedules SET schedule_revision = ?,actor_id = ?,grant_id = ?,"
                               "grant_generation = ?,authority_state = 'current',output_contract_json = ?,"
                               "updated_at = ? WHERE id = ?",
                               (revision, principal.actor_id, approval["grant_id"], approval["generation"],
                                canonical(contract), now(), schedule_id))
            # Expiry can cancel an intent already reserved for this version. A cancelled
            # pending claim has no physical entry, so the same immutable occurrence may
            # resume under the new grant without advancing the timer or duplicating a send.
            await conn.execute(
                "UPDATE effect_outbox SET state = 'pending',grant_id = ?,grant_generation = ?,"
                "error = NULL,completed_at = NULL WHERE id IN"
                " (SELECT c.effect_id FROM recurring_cycles c WHERE c.schedule_id = ?"
                " AND c.schedule_revision = ? AND c.action_state = 'pending')"
                " AND state = 'cancelled' AND claimed_at IS NULL",
                (approval["grant_id"], approval["generation"], schedule_id, revision),
            )
            return {"id": schedule_id, "schedule_revision": revision, "authority_state": "current",
                    "grant_expires_at": approval["expires_at"], "output_contract": contract}

        async with self.db.authority_effect_lock(scope.kind, scope.id):
            return await self.control.mutate(principal, scope, "schedule.approve", client_operation_id,
                                             expected_collection_revision, Entity("collection", scope.id), payload, effect)

    async def change(self, principal: Principal, schedule_id: str, *, fields: dict[str, Any],
                     expected_collection_revision: int, expected_schedule_revision: int,
                     client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can change a standing schedule")
        allowed = {"name", "prompt", "cron", "run_at", "enabled"}
        if not fields or set(fields) - allowed:
            raise ValueError("choose a supported schedule change")
        row = await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row is None or row["deleted_at"]:
            raise KeyError(schedule_id)
        scope = _scope(dict(row))
        payload = {"schedule_id": schedule_id, "fields": fields,
                   "expected_schedule_revision": expected_schedule_revision}

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            current = await one(conn, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if current is None or current["deleted_at"]:
                raise KeyError(schedule_id)
            if _scope(dict(current)) != scope or current["schedule_revision"] != expected_schedule_revision:
                raise ControlConflict("the schedule changed", current_revision=current["schedule_revision"])
            name = str(fields.get("name", current["name"]) or "").strip()
            prompt = str(fields.get("prompt", current["prompt"]) or "").strip()
            if not name or len(name) > 160 or not prompt or len(prompt) > 4000:
                raise ValueError("a bounded name and prompt are required")
            cron = fields.get("cron", current["cron"])
            run_at = fields.get("run_at", current["run_at"])
            if "cron" in fields and cron:
                run_at = None
            if "run_at" in fields and run_at:
                cron = None
            if bool(cron) == bool(run_at):
                raise ValueError("choose exactly one time rule")
            if cron:
                if not croniter.is_valid(cron):
                    raise ValueError("invalid cron expression")
                next_run = croniter(cron, datetime.now(UTC)).get_next(datetime).astimezone(UTC).isoformat()
            elif "run_at" in fields or "cron" in fields:
                due = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
                if due.tzinfo is None:
                    raise ValueError("a timezone-aware run_at is required")
                next_run = due.astimezone(UTC).isoformat()
            else:
                next_run = current["next_run_at"]
            enabled = bool(fields.get("enabled", current["enabled"]))
            if enabled and next_run is None:
                raise ValueError("a completed one-shot needs a new run_at")
            uncertain = await one(conn, "SELECT c.id FROM recurring_cycles c JOIN effect_outbox e ON e.id=c.effect_id"
                                  " WHERE c.schedule_id = ? AND e.state IN ('claimed','unknown') LIMIT 1", (schedule_id,))
            if uncertain is not None:
                raise ControlDenied("reconcile an uncertain occurrence before changing the schedule")
            if current["grant_id"]:
                await self.control.revoke_grant_in(conn, principal, current["grant_id"], reason="schedule revised")
            revision = int(current["schedule_revision"]) + 1
            await conn.execute(
                "UPDATE schedules SET name = ?,prompt = ?,cron = ?,run_at = ?,recurring = ?,enabled = ?,"
                "next_run_at = ?,schedule_revision = ?,authority_state = 'needs_approval',grant_id = NULL,"
                "grant_generation = NULL,output_contract_json = ?,updated_at = ?,"
                "failure_count = CASE WHEN ? THEN 0 ELSE failure_count END,"
                "last_error = CASE WHEN ? THEN NULL ELSE last_error END WHERE id = ?",
                (name, prompt, cron, run_at, int(bool(cron)), int(enabled), next_run, revision,
                 canonical(_contract({"kind": current["kind"], "target_session": current["target_session"],
                                      "prompt": prompt})), now(), int(enabled and not current["enabled"]),
                 int(enabled and not current["enabled"]), schedule_id),
            )
            return {"id": schedule_id, "schedule_revision": revision, "authority_state": "needs_approval",
                    "next_run_at": next_run, "enabled": enabled}

        async with self.db.authority_effect_lock(scope.kind, scope.id):
            return await self.control.mutate(principal, scope, "schedule.change", client_operation_id,
                                             expected_collection_revision, Entity("collection", scope.id), payload, effect)

    async def remove(self, principal: Principal, schedule_id: str, *, expected_collection_revision: int,
                     expected_schedule_revision: int, client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can remove a standing schedule")
        row = await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            raise KeyError(schedule_id)
        scope = _scope(dict(row))
        payload = {"schedule_id": schedule_id, "expected_schedule_revision": expected_schedule_revision}

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            current = await one(conn, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if current is None or current["deleted_at"]:
                raise KeyError(schedule_id)
            if current["schedule_revision"] != expected_schedule_revision or _scope(dict(current)) != scope:
                raise ControlConflict("the schedule changed", current_revision=current["schedule_revision"])
            uncertain = await one(conn, "SELECT c.id FROM recurring_cycles c JOIN effect_outbox e ON e.id=c.effect_id"
                                  " WHERE c.schedule_id = ? AND e.state IN ('claimed','unknown') LIMIT 1", (schedule_id,))
            if uncertain is not None:
                raise ControlDenied("reconcile an uncertain occurrence before removing the schedule")
            if current["grant_id"]:
                await self.control.revoke_grant_in(conn, principal, current["grant_id"], reason="schedule removed")
            await conn.execute("UPDATE schedules SET enabled = 0,authority_state = 'needs_approval',"
                               "schedule_revision = schedule_revision + 1,deleted_at = ?,updated_at = ? WHERE id = ?",
                               (now(), now(), schedule_id))
            return {"id": schedule_id, "deleted": True}

        async with self.db.authority_effect_lock(scope.kind, scope.id):
            return await self.control.mutate(principal, scope, "schedule.remove", client_operation_id,
                                             expected_collection_revision, Entity("collection", scope.id), payload, effect)

    async def reserve(self, schedule_id: str, *, skip: bool = False) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row is None or not row["enabled"] or not row["next_run_at"]:
            return None
        schedule = dict(row)
        due = datetime.fromisoformat(schedule["next_run_at"].replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if due > datetime.now(UTC):
            return None
        due_utc = due.astimezone(UTC).isoformat()
        offset = int((due.utcoffset() or datetime.now(UTC).utcoffset()).total_seconds())
        cycle_key = f"{schedule_id}:{schedule['schedule_revision']}:{due_utc}:{offset}"
        cycle_id = uuid.uuid5(uuid.NAMESPACE_URL, f"schedule-cycle:{cycle_key}").hex
        existing = await self.db.fetchone("SELECT * FROM recurring_cycles WHERE id = ?", (cycle_id,))
        if existing is not None:
            return dict(existing)
        if schedule["authority_state"] != "current" or not schedule["grant_id"]:
            return None
        scope = _scope(schedule)
        principal = Principal(f"schedule:{schedule_id}", "system", schedule["grant_id"], schedule["grant_generation"])
        contract = json.loads(schedule["output_contract_json"])
        if contract != _contract(schedule):
            raise ControlDenied("the schedule output contract changed without review")
        payload = {"schedule_id": schedule_id, "revision": schedule["schedule_revision"],
                   "due_at": due_utc, "offset": offset, "skip": skip, "intent_digest": digest(_snapshot(schedule))}
        revision_row = await self.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind = ? AND scope_id = ?", (scope.kind, scope.id))
        if revision_row is None:
            raise KeyError(scope.id)
        bus = self.app.manager.bus if self.app.manager is not None else None
        events: list[Any] = []

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            current = await one(conn, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if (current is None or not current["enabled"] or current["authority_state"] != "current"
                    or current["schedule_revision"] != schedule["schedule_revision"]
                    or current["next_run_at"] != schedule["next_run_at"]
                    or current["grant_id"] != schedule["grant_id"]):
                raise ControlConflict("the scheduled occurrence changed before reservation")
            unresolved = await one(
                conn, "SELECT c.id FROM recurring_cycles c JOIN effect_outbox e ON e.id = c.effect_id"
                " WHERE c.schedule_id = ? AND e.state IN ('pending','claimed','unknown') LIMIT 1",
                (schedule_id,),
            )
            if unresolved is not None:
                raise ControlConflict("an earlier occurrence has not reached a known outcome")
            if current["cron"]:
                next_due = croniter(current["cron"], datetime.now(UTC)).get_next(datetime).isoformat()
                await conn.execute("UPDATE schedules SET last_run_at = ?,next_run_at = ? WHERE id = ?",
                                   (now(), next_due, schedule_id))
            else:
                await conn.execute("UPDATE schedules SET last_run_at = ?,enabled = 0,next_run_at = NULL WHERE id = ?",
                                   (now(), schedule_id))
            effect_id = None
            if not skip:
                effect_id = await OutboxStore.enqueue(
                    conn, mutation, principal, kind=f"schedule.{current['kind'] or 'agent'}",
                    operation="schedule.fire", payload={"cycle_id": cycle_id, "schedule": _snapshot(dict(current))},
                    effects=(f"schedule.{current['kind'] or 'agent'}",),
                )
            await conn.execute(
                "INSERT INTO recurring_cycles(id,schedule_id,schedule_revision,project_id,due_at,local_wall,"
                "utc_offset_seconds,kind,target_session,intent_digest,output_contract_json,receipt_id,effect_id,"
                "action_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cycle_id, schedule_id, current["schedule_revision"], current["project_id"], due_utc,
                 due.replace(tzinfo=None).isoformat(), offset, current["kind"] or "agent",
                 current["target_session"], digest(_snapshot(dict(current))), current["output_contract_json"],
                 mutation.receipt_id, effect_id, "skipped" if skip else "pending", now(), now()),
            )
            if bus is not None and not skip and (current["kind"] or "agent") != "wake":
                events.append(await bus.persist_in(conn, "schedule.fired", {
                    "schedule_id": schedule_id, "cycle_id": cycle_id,
                    "name": current["name"], "kind": current["kind"] or "agent",
                }, project_id=current["project_id"]))
            return {"cycle_id": cycle_id, "effect_id": effect_id, "state": "skipped" if skip else "pending"}

        try:
            if bus is None:
                response = await self.control.mutate(principal, scope, "schedule.fire", cycle_id,
                                                     int(revision_row["revision"]), Entity("collection", scope.id),
                                                     payload, effect, effects=() if skip else (f"schedule.{schedule['kind'] or 'agent'}",))
            else:
                async with bus.transaction_guard():
                    response = await self.control.mutate(principal, scope, "schedule.fire", cycle_id,
                                                         int(revision_row["revision"]), Entity("collection", scope.id),
                                                         payload, effect, effects=() if skip else (f"schedule.{schedule['kind'] or 'agent'}",))
                    for event in events:
                        bus.announce_committed(event)
        except ControlDenied:
            await self.db.execute("UPDATE schedules SET authority_state = 'needs_approval' WHERE id = ? AND schedule_revision = ?",
                                  (schedule_id, schedule["schedule_revision"]))
            return None
        except ControlConflict:
            return None
        dispatcher = self.app.extensions.get("effects")
        if dispatcher is not None and response["effect_id"]:
            dispatcher.notify()
        result = await self.db.fetchone("SELECT * FROM recurring_cycles WHERE id = ?", (cycle_id,))
        return dict(result) if result is not None else None

    async def run_now(self, principal: Principal, schedule_id: str, *,
                      expected_collection_revision: int, expected_schedule_revision: int,
                      client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can request an immediate scheduled action")
        row = await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row is None or row["deleted_at"]:
            raise KeyError(schedule_id)
        scope = _scope(dict(row))
        payload = {"schedule_id": schedule_id, "expected_schedule_revision": expected_schedule_revision}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            current = await one(conn, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            if current is None or current["deleted_at"]:
                raise KeyError(schedule_id)
            if current["schedule_revision"] != expected_schedule_revision or _scope(dict(current)) != scope:
                raise ControlConflict("the schedule changed", current_revision=current["schedule_revision"])
            uncertain = await one(conn, "SELECT c.id FROM recurring_cycles c JOIN effect_outbox e ON e.id=c.effect_id"
                                  " WHERE c.schedule_id = ? AND e.state IN ('claimed','unknown') LIMIT 1", (schedule_id,))
            if uncertain is not None:
                raise ControlDenied("reconcile an uncertain occurrence before starting another")
            cycle_id = uuid.uuid5(uuid.NAMESPACE_URL, f"schedule-manual:{mutation.receipt_id}").hex
            schedule = dict(current)
            contract = _contract(schedule)
            intent = _snapshot(schedule)
            effect_id = await OutboxStore.enqueue(
                conn, mutation, principal, kind=f"schedule.{current['kind'] or 'agent'}",
                operation="schedule.run", payload={"cycle_id": cycle_id, "schedule": intent},
                effects=(f"schedule.{current['kind'] or 'agent'}",),
            )
            moment = now()
            await conn.execute(
                "INSERT INTO recurring_cycles(id,schedule_id,schedule_revision,project_id,due_at,local_wall,"
                "utc_offset_seconds,kind,target_session,manual,intent_digest,output_contract_json,receipt_id,"
                "effect_id,action_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
                (cycle_id, schedule_id, current["schedule_revision"], current["project_id"], moment,
                 datetime.fromisoformat(moment).replace(tzinfo=None).isoformat(), 0,
                 current["kind"] or "agent", current["target_session"], digest(intent), canonical(contract),
                 mutation.receipt_id, effect_id, "pending", moment, moment),
            )
            return {"cycle_id": cycle_id, "effect_id": effect_id, "state": "pending"}

        response = await self.control.mutate(principal, scope, "schedule.run", client_operation_id,
                                             expected_collection_revision, Entity("collection", scope.id), payload, effect,
                                             effects=(f"schedule.{row['kind'] or 'agent'}",))
        dispatcher = self.app.extensions.get("effects")
        if dispatcher is not None:
            dispatcher.notify()
        return response

    async def cycles(self, schedule_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT c.*,e.state AS effect_state,e.error AS effect_error FROM recurring_cycles c"
            " LEFT JOIN effect_outbox e ON e.id=c.effect_id WHERE c.schedule_id = ?"
            " ORDER BY c.created_at DESC,c.id DESC LIMIT ?", (schedule_id, min(max(limit, 1), 100)),
        )
        return [dict(row) for row in rows]

    async def reconcile(self, principal: Principal, schedule_id: str, cycle_id: str, *,
                        outcome: str, reason: str, expected_collection_revision: int,
                        client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can attest an uncertain delivery")
        if outcome not in ("delivered", "not_delivered") or not reason.strip():
            raise ValueError("an explicit outcome and reason are required")
        row = await self.db.fetchone("SELECT project_id FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            raise KeyError(schedule_id)
        scope = Scope("project", row["project_id"]) if row["project_id"] else Scope("global", "global")
        payload = {"schedule_id": schedule_id, "cycle_id": cycle_id, "outcome": outcome, "reason": reason.strip()}

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            cycle = await one(conn, "SELECT c.*,e.state AS effect_state,e.claim_generation FROM recurring_cycles c"
                              " JOIN effect_outbox e ON e.id=c.effect_id WHERE c.id = ? AND c.schedule_id = ?",
                              (cycle_id, schedule_id))
            if cycle is None:
                raise KeyError(cycle_id)
            if cycle["effect_state"] != "unknown":
                raise ControlConflict("only an unknown physical outcome can be reconciled")
            state = "completed" if outcome == "delivered" else "failed"
            await conn.execute("UPDATE effect_outbox SET state = ?,error = ?,completed_at = ?"
                               " WHERE id = ? AND state = 'unknown' AND claim_generation = ?",
                               (state, canonical({"operator_attestation": reason.strip(), "outcome": outcome}),
                                now(), cycle["effect_id"], cycle["claim_generation"]))
            await conn.execute("UPDATE recurring_cycles SET action_state = ?,reconciled_by = ?,"
                               "reconciliation_reason = ?,reconciled_at = ?,updated_at = ? WHERE id = ?",
                               ("delivered" if outcome == "delivered" else "failed", principal.actor_id,
                                reason.strip(), now(), now(), cycle_id))
            return {"cycle_id": cycle_id, "outcome": outcome, "evidence_kind": "operator_attestation"}

        return await self.control.mutate(principal, scope, "schedule.reconcile", client_operation_id,
                                         expected_collection_revision, Entity("collection", scope.id), payload, effect)


class RecurringEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        cycle_id = claim.payload["cycle_id"]
        schedule = claim.payload["schedule"]
        scope = claim.scope
        async with self.app.db.authority_effect_lock(scope.kind, scope.id):
            await check(claim)
            row = await self.app.db.fetchone("SELECT c.*,s.schedule_revision AS current_revision,"
                                            "s.authority_state,s.enabled FROM recurring_cycles c"
                                            " LEFT JOIN schedules s ON s.id=c.schedule_id WHERE c.id = ?", (cycle_id,))
            if (row is None or row["schedule_revision"] != row["current_revision"]
                    or (not row["manual"] and row["authority_state"] != "current")
                    or row["action_state"] != "pending"):
                return EffectOutcome("failed", "the scheduled occurrence is no longer approved")
            if row["intent_digest"] != digest(schedule) or json.loads(row["output_contract_json"]) != _contract(schedule):
                return EffectOutcome("failed", "the scheduled action no longer matches its approved output")
            target_session = schedule.get("target_session")
            if target_session and row["project_id"]:
                target = await self.app.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (target_session,))
                if target is None or target["project_id"] != row["project_id"]:
                    return EffectOutcome("failed", "the target session left the approved project")
            await self.app.db.execute("UPDATE recurring_cycles SET action_state = 'dispatching',updated_at = ?"
                                      " WHERE id = ? AND action_state = 'pending'", (now(), cycle_id))
            kind = row["kind"]
            if kind == "message":
                await self._message(cycle_id, schedule)
            elif kind == "lazy":
                await self.app.db.execute(
                    "INSERT OR IGNORE INTO lazy_notes(schedule_id,session_id,text,fired_at,cycle_id)"
                    " VALUES (?,?,?,?,?)", (row["schedule_id"], schedule["target_session"],
                                             schedule["prompt"], now(), cycle_id),
                )
            elif kind == "wake":
                await self._wake(cycle_id, schedule)
            else:
                scheduler = self.app.extensions.get("scheduler")
                if scheduler is None:
                    return EffectOutcome("deferred", "scheduler is not installed")
                await scheduler._fire_agent({**schedule, "cycle_id": cycle_id})
            await self.app.db.execute("UPDATE recurring_cycles SET action_state = 'delivered',updated_at = ?"
                                      " WHERE id = ? AND action_state = 'dispatching'", (now(), cycle_id))
        return EffectOutcome("completed")

    async def _message(self, cycle_id: str, schedule: dict[str, Any]) -> None:
        scheduler = self.app.extensions.get("scheduler")
        if scheduler is None:
            raise RuntimeError("scheduler is not installed")
        front = self.app.front
        target = await self.app.manager.get_state(schedule["target_session"]) if schedule.get("target_session") and self.app.manager is not None else None
        if target is not None:
            if is_subagent(target.metadata):
                front = None
        sent = False
        if front is not None:
            text = f"⏰ **{schedule['name']}**\n\n{schedule['prompt']}"
            outbox = await front.outbox_for_session(schedule["target_session"]) if schedule.get("target_session") else None
            if outbox is not None:
                await outbox.send_text(text)
            else:
                await front.notify(text)
            sent = True
        notification = self.app.notifications
        if notification is not None:
            await notification.post(Draft("reminder", schedule["name"], schedule["prompt"],
                                          kind="reminder", session_id=schedule.get("target_session"),
                                          source="scheduler", dedupe_key=f"schedule-cycle:{cycle_id}",
                                          handled=frozenset({"telegram"}) if sent else frozenset()))

    async def _wake(self, cycle_id: str, schedule: dict[str, Any]) -> None:
        scheduler = self.app.extensions.get("scheduler")
        manager = self.app.manager
        if scheduler is None or manager is None:
            raise RuntimeError("scheduler or manager is not installed")
        project_id = schedule.get("project_id")
        project = await manager.projects.get(project_id) if project_id else None
        if project is None or not project.settings.orchestrator.enabled:
            raise ControlDenied("the project's orchestrator is not available")
        await manager.bus.publish("schedule.fired", {
            "schedule_id": schedule["id"], "cycle_id": cycle_id, "name": schedule["name"],
            "kind": "wake", "note": schedule["prompt"], "set_by": "operator",
        }, project_id=project.id)

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        cycle_id = claim.payload["cycle_id"]
        row = await self.app.db.fetchone("SELECT action_state,kind FROM recurring_cycles WHERE id = ?", (cycle_id,))
        if row is None:
            return None
        if row["action_state"] == "pending":
            # The durable pre-effect marker was never written. This exact claim cannot have
            # entered its external call, so re-admitting it cannot duplicate that call.
            async with self.app.db.transaction() as conn:
                current = await one(conn, "SELECT action_state FROM recurring_cycles WHERE id = ?", (cycle_id,))
                effect = await one(conn, "SELECT state,claim_generation FROM effect_outbox WHERE id = ?", (claim.id,))
                if (current is not None and current["action_state"] == "pending" and effect is not None
                        and effect["state"] == "unknown" and effect["claim_generation"] == claim.generation):
                    await conn.execute("UPDATE effect_outbox SET state = 'pending',claimed_at = NULL,error = ?"
                                       " WHERE id = ? AND state = 'unknown' AND claim_generation = ?",
                                       ("proven interrupted before effect entry", claim.id, claim.generation))
                    dispatcher = self.app.extensions.get("effects")
                    if dispatcher is not None:
                        dispatcher.notify()
            return None
        if row["action_state"] == "delivered":
            return EffectResolution("completed", {"cycle_id": cycle_id, "recorded_outcome": "delivered"})
        if row["kind"] == "lazy":
            note = await self.app.db.fetchone("SELECT id FROM lazy_notes WHERE cycle_id = ?", (cycle_id,))
            if note is not None:
                await self.app.db.execute("UPDATE recurring_cycles SET action_state = 'delivered',updated_at = ?"
                                          " WHERE id = ? AND action_state = 'dispatching'", (now(), cycle_id))
                return EffectResolution("completed", {"cycle_id": cycle_id, "lazy_note_id": note["id"]})
        if row["kind"] == "wake":
            event = await self.app.db.fetchone("SELECT seq FROM app_events WHERE type = 'schedule.fired'"
                                               " AND json_extract(payload_json,'$.cycle_id') = ?", (cycle_id,))
            if event is not None:
                await self.app.db.execute("UPDATE recurring_cycles SET action_state = 'delivered',updated_at = ?"
                                          " WHERE id = ? AND action_state = 'dispatching'", (now(), cycle_id))
                return EffectResolution("completed", {"cycle_id": cycle_id, "event_seq": event["seq"]})
        # A transport call may have completed just before the process died. No timer retry can prove it did not.
        return None
