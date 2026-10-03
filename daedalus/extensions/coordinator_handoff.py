"""Prepare a successor before moving the sole project coordinator office.

The office, its journal receipt, and schedule ownership move in one SQLite transaction. Process
retirement happens afterward and is reconciled after a crash; it cannot restore the old office.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.extensions.recurring import repoint_project_schedules_in
from daedalus.stores.control import ControlConflict, ControlDenied, Principal, canonical, digest
from daedalus.stores.projects import OrchestratorSettings, Project, ProjectError

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators


def now() -> str:
    return datetime.now(UTC).isoformat()


def blocker_for(error: BaseException) -> str:
    if isinstance(error, ControlConflict):
        return "configuration_changed"
    message = str(error).lower()
    if isinstance(error, ControlDenied) and "in-flight coordinator mutation" in message:
        return "mutation_in_flight"
    if isinstance(error, ControlDenied) and "uncertain delivery" in message:
        return "schedule_delivery_unconfirmed"
    if isinstance(error, ControlDenied) and "schedule" in message:
        return "schedule_scope_changed"
    if "http 401" in message or "http 403" in message:
        return "authentication_failed"
    if "no model" in message or "no usable model" in message:
        return "model_unavailable"
    if isinstance(error, (OSError, ValueError, RuntimeError, KeyError)):
        return "catalogue_unavailable"
    return "preparation_failed"


class CoordinatorHandoff:
    def __init__(self, orchestrators: Orchestrators) -> None:
        self.orchestrators = orchestrators
        self.app = orchestrators.app
        self.manager = orchestrators.manager
        self.db = self.manager.db

    def fingerprint(self, project: Project) -> str:
        # This digest binds the private credential without ever persisting or returning its bytes.
        return digest({"config": self.app.config.model_dump(mode="json"), "project": project.view()})

    async def readiness(self, project: Project) -> dict[str, Any]:
        config = self.manager.config
        preset_id = config.orchestrator_preset(project.settings.orchestrator.model)
        if not preset_id or preset_id not in config.presets:
            raise ProjectError("model_unavailable")
        preset = config.presets[preset_id]
        provider, running_model = self.manager.providers.rungs_for(config, preset_id)[0]
        if (self.orchestrators.catalogue_lookup is None
                and (running_model != preset.model or provider.endpoint.id != preset.provider)):
            raise ProjectError("model_unavailable")
        endpoint = provider.endpoint
        base_url = str(getattr(endpoint, "base_url", ""))
        api_key = str(getattr(endpoint, "api_key", "")) or None
        if not base_url and self.orchestrators.catalogue_lookup is None:
            raise ProjectError("catalogue_unavailable")
        if self.orchestrators.catalogue_lookup is not None:
            found = await self.orchestrators.catalogue_lookup(base_url, api_key)
        else:
            # The existing discovery path uses the configured key on GET /models, never an inference.
            from daedalus.extensions.api import (
                lookup_openai_models,  # Lazy: API composition imports this handoff service.
            )

            found = await lookup_openai_models(base_url, api_key)
        if preset.model not in found.get("models", []):
            raise ProjectError("model_not_advertised")
        return {"preset": preset_id, "provider": preset.provider, "model": preset.model,
                "configuration_digest": self.fingerprint(project),
                "checked_at": now(), "claim": "model_catalogue_checked"}

    async def view(self, project_id: str, handoff_id: str | None = None) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM coordinator_handoffs WHERE project_id = ? AND id = ?" if handoff_id else
            "SELECT * FROM coordinator_handoffs WHERE project_id = ? ORDER BY created_at DESC,id DESC LIMIT 1",
            (project_id, handoff_id) if handoff_id else (project_id,),
        )
        if row is None:
            return None
        project = await self.manager.projects.get(project_id)
        current = project.settings.orchestrator.session_id if project is not None else ""
        response = json.loads(row["response_json"]) if row["response_json"] else {}
        return {"handoff_id": row["id"], "project_id": project_id, "state": row["state"],
                "blocker": row["blocker"], "old_active": current == row["old_session_id"],
                "new_active": current == row["replacement_session_id"], "receipt_id": row["receipt_id"],
                "schedules_needing_approval": response.get("schedules_needing_approval", 0),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    async def replace(self, project_id: str, reason: str, *, principal: Principal,
                      client_operation_id: str, expected_entity_revision: int,
                      expected_coordinator_session_id: str) -> dict[str, Any]:
        if principal.origin_class not in ("operator", "system") or not principal.actor_id:
            raise PermissionError("the replacement requires an authenticated operator or host system")
        if not client_operation_id or len(client_operation_id) > 160 or expected_entity_revision < 1:
            raise ValueError("an operation identity and positive project revision are required")
        if len(reason) > 300:
            raise ValueError("the reason is too long")
        request = {"reason": reason, "expected_entity_revision": expected_entity_revision,
                   "expected_coordinator_session_id": expected_coordinator_session_id}
        request_digest = digest(request)
        reason = " ".join(reason.split()) or "replaced"
        # Only other handoffs wait for discovery. The incumbent may still act while the provider
        # catalogue is checked; its actions or a config edit make the later fingerprint/CAS fail.
        async with self.orchestrators.handoff_lock(project_id):
            existing = await self.db.fetchone(
                "SELECT * FROM coordinator_handoffs WHERE project_id = ? AND actor_id = ? AND client_operation_id = ?",
                (project_id, principal.actor_id, client_operation_id),
            )
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ControlConflict("the command identity was reused with a different request")
                if existing["state"] == "preparing":
                    await self.reconcile(project_id, existing["id"])
                    existing = await self.db.fetchone("SELECT * FROM coordinator_handoffs WHERE id = ?", (existing["id"],))
                elif existing["state"] == "retirement_pending":
                    await self.reconcile(project_id, existing["id"])
                if existing["response_json"]:
                    return json.loads(existing["response_json"])
                raise RuntimeError("the handoff command has no durable result")
            project = await self.manager.projects.get(project_id)
            if project is None:
                raise KeyError(project_id)
            old = project.settings.orchestrator.session_id
            if not project.settings.orchestrator.enabled or not old:
                raise ProjectError("the project has no current coordinator to replace")
            if old != expected_coordinator_session_id or project.entity_revision != expected_entity_revision:
                raise ControlConflict("the coordinator or project changed", current_revision=project.entity_revision)
            handoff_id = uuid.uuid4().hex
            successor = uuid.uuid4().hex[:12]
            at = now()
            async with self.db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO coordinator_handoffs(id,project_id,actor_id,client_operation_id,request_digest,"
                    "expected_entity_revision,old_session_id,replacement_session_id,reason,state,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'preparing',?,?)",
                    (handoff_id, project_id, principal.actor_id, client_operation_id, request_digest,
                     expected_entity_revision, old, successor, reason, at, at),
                )
            try:
                readiness = await self.readiness(project)
                await self.orchestrators._new_session(project, predecessor=old, reason=reason,
                                                      prepared_handoff_id=handoff_id, session_id=successor)
                current_project = await self.manager.projects.get(project_id)
                if current_project is None or self.fingerprint(current_project) != readiness["configuration_digest"]:
                    raise ProjectError("configuration_changed")
                async with self.db.authority_effect_lock("project", project_id):
                    response = await self._swap(handoff_id, project, old, successor, reason,
                                                principal, request_digest, readiness)
            except Exception as exc:
                current = await self.manager.projects.get(project_id)
                if current is not None and current.settings.orchestrator.session_id == successor:
                    raise
                code = blocker_for(exc)
                if str(exc) in ("configuration_changed", "model_not_advertised", "model_unavailable", "catalogue_unavailable"):
                    code = str(exc)
                elif str(exc) in ("prepared successor is missing", "current coordinator identity changed"):
                    code = "configuration_changed"
                response = {"handoff_id": handoff_id, "state": "blocked", "old_active": True,
                            "receipt_id": None, "blocker": code}
                async with self.db.transaction() as conn:
                    await conn.execute("UPDATE coordinator_handoffs SET state = 'blocked',blocker = ?,response_json = ?,updated_at = ? WHERE id = ? AND state = 'preparing'",
                                       (code, canonical(response), now(), handoff_id))
                await self._delete_prepared(successor, project_id)
                return response
        await self.reconcile(project_id, handoff_id)
        return response

    async def _swap(self, handoff_id: str, project: Project, old: str, successor: str, reason: str,
                    principal: Principal, request_digest: str, readiness: dict[str, Any]) -> dict[str, Any]:
        if self.fingerprint(project) != readiness["configuration_digest"]:
            raise ProjectError("configuration_changed")
        if self.orchestrators._mutation_flights.get(project.id):
            raise ControlDenied("an in-flight coordinator mutation must finish before replacement")
        at = now()
        receipt_id = uuid.uuid4().hex
        response = {"handoff_id": handoff_id, "state": "retirement_pending", "old_active": False,
                    "receipt_id": receipt_id, "session_id": successor,
                    "readiness": {"claim": readiness["claim"], "model": readiness["model"]}}
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT entity_revision,settings FROM projects WHERE id = ?", (project.id,)) as cursor:
                row = await cursor.fetchone()
            office = json.loads(row["settings"]).get("orchestrator", {}) if row else {}
            if (row is None or row["entity_revision"] != project.entity_revision or not office.get("enabled")
                    or office.get("session_id") != old
                    or OrchestratorSettings.load(office) != project.settings.orchestrator):
                raise ControlConflict("the coordinator or project changed", current_revision=row["entity_revision"] if row else None)
            async with conn.execute("SELECT metadata FROM sessions WHERE id = ? AND project_id = ?", (successor, project.id)) as cursor:
                candidate = await cursor.fetchone()
            candidate_meta = json.loads(candidate["metadata"]) if candidate is not None else {}
            if candidate_meta.get("orchestrator_prepared_of") != project.id or candidate_meta.get("handoff_id") != handoff_id:
                raise ProjectError("prepared successor is missing")
            async with conn.execute("SELECT metadata FROM sessions WHERE id = ? AND project_id = ?", (old, project.id)) as cursor:
                incumbent = await cursor.fetchone()
            if incumbent is None or json.loads(incumbent["metadata"]).get("orchestrator_of") != project.id:
                raise ProjectError("current coordinator identity changed")
            await conn.execute("UPDATE projects SET settings = json_set(settings,'$.orchestrator.session_id',?),"
                               "entity_revision = entity_revision + 1 WHERE id = ?", (successor, project.id))
            await conn.execute("UPDATE sessions SET metadata = json_set(json_remove(metadata,'$.orchestrator_prepared_of','$.handoff_id'),'$.orchestrator_of',?) WHERE id = ?",
                               (project.id, successor))
            await conn.execute("UPDATE sessions SET metadata = json_set(json_remove(metadata,'$.orchestrator_of'),'$.orchestrator_retired_of',?,'$.successor',?) WHERE id = ?",
                               (project.id, successor, old))
            schedules = await repoint_project_schedules_in(
                conn, project_id=project.id, old_session_id=old, new_session_id=successor,
            )
            response["schedules_needing_approval"] = len(schedules)
            text = f"The orchestrator {old} was replaced by {successor} ({'operator' if principal.origin_class == 'operator' else 'system'}): {reason}"
            await conn.execute("INSERT INTO project_journal(project_id,at,author,kind,text,refs_json) VALUES (?,?,?,?,?,?)",
                               (project.id, at, "system", "replacement", text,
                                canonical({"predecessor": old, "successor": successor, "handoff_id": handoff_id})))
            async with conn.execute("SELECT entity_revision FROM projects WHERE id = ?", (project.id,)) as cursor:
                published = await cursor.fetchone()
            response["entity_revision"] = published["entity_revision"]
            await conn.execute("UPDATE coordinator_handoffs SET state = 'retirement_pending',readiness_digest = ?,"
                               "readiness_json = ?,receipt_id = ?,response_json = ?,updated_at = ? WHERE id = ? AND state = 'preparing'",
                               (digest(readiness), canonical(readiness), receipt_id, canonical(response), at, handoff_id))
            await conn.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,project_id,actor_id,operation_kind,"
                               "client_operation_id,grant_id,request_entity_revision,payload_hash,entity_revision,state,response_json,created_at)"
                               " VALUES (?,'project',?,?,?,'orchestrator.replace',?,NULL,?,?,?,'committed',?,?)",
                               (receipt_id, project.id, project.id, principal.actor_id,
                                (await self._operation_id(conn, handoff_id)), project.entity_revision,
                                request_digest, published["entity_revision"], canonical(response), at))
        candidate_state = await self.manager.get_state(successor)
        if candidate_state is not None:
            for metadata in (candidate_state.metadata, candidate_state.session.metadata):
                metadata.pop("orchestrator_prepared_of", None)
                metadata.pop("handoff_id", None)
                metadata["orchestrator_of"] = project.id
        old_state = await self.manager.get_state(old)
        if old_state is not None:
            for metadata in (old_state.metadata, old_state.session.metadata):
                metadata.pop("orchestrator_of", None)
                metadata["orchestrator_retired_of"] = project.id
                metadata["successor"] = successor
        return response

    async def _operation_id(self, conn: Any, handoff_id: str) -> str:
        async with conn.execute("SELECT client_operation_id FROM coordinator_handoffs WHERE id = ?", (handoff_id,)) as cursor:
            row = await cursor.fetchone()
        return row["client_operation_id"]

    async def _delete_prepared(self, session_id: str, project_id: str) -> None:
        project = await self.manager.projects.get(project_id)
        if project is not None and project.settings.orchestrator.session_id == session_id:
            return
        state = await self.manager.get_state(session_id)
        if state is not None and state.metadata.get("orchestrator_prepared_of") == project_id:
            await self.manager.delete_session(session_id)

    async def reconcile(self, project_id: str | None = None, handoff_id: str | None = None) -> None:
        query = "SELECT * FROM coordinator_handoffs WHERE state IN ('preparing','retirement_pending')"
        params: list[str] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if handoff_id:
            query += " AND id = ?"
            params.append(handoff_id)
        for row in await self.db.fetchall(query, params):
            project = await self.manager.projects.get(row["project_id"])
            if row["state"] == "preparing":
                await self._delete_prepared(row["replacement_session_id"], row["project_id"])
                response = {"handoff_id": row["id"], "state": "blocked",
                            "old_active": project is not None and project.settings.orchestrator.session_id == row["old_session_id"],
                            "receipt_id": None, "blocker": "preparation_interrupted"}
                await self.db.execute("UPDATE coordinator_handoffs SET state = 'blocked',blocker = 'preparation_interrupted',"
                                      "response_json = ?,updated_at = ? WHERE id = ? AND state = 'preparing'",
                                      (canonical(response), now(), row["id"]))
                continue
            if project is None or project.settings.orchestrator.session_id != row["replacement_session_id"]:
                continue
            try:
                await self.orchestrators._retire(row["old_session_id"], row["project_id"], successor=row["replacement_session_id"])
                await self.orchestrators._sync_model(project)
                await self.orchestrators._changed(row["project_id"], "orchestrator.replaced",
                                                   "operator" if row["actor_id"].startswith("operator:") else "system")
                queue = self.orchestrators.queues.get(row["project_id"])
                if queue is not None:
                    queue.poke()
                await self.db.execute("UPDATE coordinator_handoffs SET state = 'completed',blocker = NULL,updated_at = ?"
                                      " WHERE id = ? AND state = 'retirement_pending'", (now(), row["id"]))
            except Exception:  # noqa: BLE001 — the durable retirement intent remains for restart.
                await self.db.execute("UPDATE coordinator_handoffs SET blocker = 'retirement_unconfirmed',updated_at = ?"
                                      " WHERE id = ? AND state = 'retirement_pending'", (now(), row["id"]))
