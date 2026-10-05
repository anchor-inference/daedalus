"""Operator preview and receipted cancellation of explicitly owned work."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.lifecycle import Lifecycle
from daedalus.extensions.task_launch import TaskLaunchEffect
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Principal, Scope, one
from daedalus.stores.lifecycle import LifecycleRefused
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.runtime_release import attempt_released_in, no_entry_in, physical_exit_in

if TYPE_CHECKING:
    from daedalus.app import Application


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1, strict=True)
    expected_source_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)
    preview_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> Lifecycle:
        available = getattr(app, "extensions", {}).get("lifecycle")
        if not isinstance(available, Lifecycle):
            raise HTTPException(503, "lifecycle subsystem is unavailable")
        return available

    async def authorize(project_id: str, who: dict[str, Any], operation: str) -> None:
        try:
            principal = Principal.operator(who)
            async with app.db.transaction() as conn:
                if await one(conn, "SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
                    raise HTTPException(404, "no such project")
                await ControlStore(app.db).authorize(conn, principal, Scope("project", project_id), operation)
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/projects/{project_id}/unknown-stops")
    async def unknown_stops(project_id: str, after: str = Query("", max_length=160),
                            limit: int = Query(50, ge=1, le=100),
                            who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Show bounded, exact timeout identities and saved observations for operator inspection."""
        await authorize(project_id, who, "control.read")
        async with app.db.transaction() as conn:
            generation = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
            host = json.loads(generation["value"]) if generation else None
            async with conn.execute(
                "SELECT a.id,a.task_id,a.state,a.host_generation,a.staff_session_id,a.runtime_kind,"
                "a.provider_session_ref,a.native_run_id,a.runtime_instance,o.parent_kind,o.parent_id,"
                "o.generation,o.cancel_state,o.updated_at,c.phase,c.deadline_at,"
                "EXISTS (SELECT 1 FROM runtime_no_entry_observations n WHERE n.attempt_id = a.id"
                " AND n.staff_session_id = a.staff_session_id AND n.host_generation = a.host_generation"
                " AND n.contract_revision = a.contract_revision) AS no_entry_observed"
                " FROM lifecycle_owners o JOIN execution_attempts a ON a.id = o.child_id"
                " JOIN board_tasks t ON t.id = a.task_id"
                " JOIN attempt_phase_clocks c ON c.attempt_id = a.id AND c.outcome = 'timed_out'"
                " AND c.phase = (SELECT latest.phase FROM attempt_phase_clocks latest"
                " WHERE latest.attempt_id = a.id AND latest.outcome = 'timed_out'"
                " ORDER BY latest.deadline_at DESC,latest.phase DESC LIMIT 1)"
                " WHERE o.child_kind = 'execution_attempt' AND o.cancel_state = 'unknown'"
                " AND o.project_id = ? AND t.project_id = ? AND a.id > ?"
                " ORDER BY a.id,c.deadline_at LIMIT ?", (project_id, project_id, after, limit + 1),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        items = rows[:limit]
        async with app.db.transaction() as conn:
            for row in items:
                row["generation_matches_host_record"] = row["host_generation"] == host
                row["no_entry_observed"] = bool(row["no_entry_observed"])
                # An exit flag alone hid which immutable observation matched the blocked attempt.
                exit_proof = await one(
                    conn, "SELECT e.runtime_ref,e.host_generation,e.contract_revision,e.observed_status,e.observed_at"
                    " FROM runtime_exit_observations e JOIN execution_attempts a ON a.id = e.attempt_id"
                    " LEFT JOIN staff_sessions s ON s.id = a.staff_session_id"
                    " WHERE a.id = ? AND e.staff_session_id = a.staff_session_id"
                    " AND e.host_generation = a.host_generation AND e.provider_session_ref = a.provider_session_ref"
                    " AND e.contract_revision = a.contract_revision AND e.runtime_kind = a.runtime_kind"
                    " AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                    " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance"
                    " AND e.runtime_ref = s.terminal_id AND e.provider_session_ref = 'terminal:' || s.terminal_id))"
                    " ORDER BY e.observed_at DESC LIMIT 1", (row["id"],),
                )
                row["exit_evidence"] = dict(exit_proof) if exit_proof else None
                row["exit_observed"] = exit_proof is not None
                no_entry_proven = row["no_entry_observed"] and await no_entry_in(conn, row["id"])
                no_entry_proof = await one(conn,
                    "SELECT n.host_generation,n.contract_revision,n.observed_at"
                    " FROM runtime_no_entry_observations n JOIN execution_attempts a ON a.id = n.attempt_id"
                    " WHERE a.id = ? AND n.staff_session_id = a.staff_session_id"
                    " AND n.runtime_kind = a.runtime_kind AND n.host_generation = a.host_generation"
                    " AND n.contract_revision = a.contract_revision",
                    (row["id"],)) if no_entry_proven else None
                row["no_entry_evidence"] = dict(no_entry_proof) if no_entry_proof else None
                binding = await one(conn,
                    "SELECT 'profile' AS source,state,host_generation,daemon_instance,launch_id FROM attempt_resource_bindings"
                    " WHERE attempt_id = ? UNION ALL"
                    " SELECT 'writer' AS source,state,host_generation,daemon_instance,launch_id FROM writer_attempt_bindings"
                    " WHERE attempt_id = ?", (row["id"], row["id"]))
                row["containment_evidence"] = None
                release = None
                if binding is not None:
                    table = "attempt_resource_observations" if binding["source"] == "profile" else "writer_attempt_observations"
                    latest = await one(conn,
                        f"SELECT id,host_generation,observation_kind,enforced,populated,observed_at FROM {table}"
                        " WHERE attempt_id = ? AND launch_id = ? AND host_generation = ?"
                        " AND daemon_instance = ? ORDER BY observed_at DESC,id DESC LIMIT 1",
                        (row["id"], binding["launch_id"], binding["host_generation"], binding["daemon_instance"]))
                    release = await one(conn,
                        f"SELECT id,host_generation,observation_kind,enforced,populated,observed_at FROM {table}"
                        " WHERE attempt_id = ? AND launch_id = ? AND observation_kind IN ('stop','exit')"
                        " AND host_generation = ? AND daemon_instance = ? AND enforced = 1 AND populated = 0"
                        " ORDER BY observed_at DESC,id DESC LIMIT 1",
                        (row["id"], binding["launch_id"], binding["host_generation"], binding["daemon_instance"]))
                    stale = await one(conn,
                        f"SELECT id,host_generation,observation_kind,enforced,populated,observed_at FROM {table}"
                        " WHERE attempt_id = ? AND launch_id = ?"
                        " AND (host_generation != ? OR daemon_instance != ?)"
                        " ORDER BY observed_at DESC,id DESC LIMIT 1",
                        (row["id"], binding["launch_id"], binding["host_generation"], binding["daemon_instance"]))
                    row["containment_evidence"] = {"source": binding["source"], "state": binding["state"],
                        "host_generation": binding["host_generation"],
                        "latest_observation": dict(latest) if latest else None,
                        "release_observation": dict(release) if release else None,
                        "stale_observation": dict(stale) if stale else None}
                if no_entry_proven:
                    row["recovery_blocker"] = "ready"
                elif not row["generation_matches_host_record"]:
                    row["recovery_blocker"] = "previous_host"
                elif not row["staff_session_id"] or not row["provider_session_ref"]:
                    row["recovery_blocker"] = "runtime_identity_missing"
                elif not row["exit_observed"]:
                    row["recovery_blocker"] = "exit_unobserved"
                elif row["runtime_kind"] == "cli" and binding is None:
                    row["recovery_blocker"] = "containment_unavailable"
                elif row["runtime_kind"] == "cli" and binding is not None and release is None:
                    row["recovery_blocker"] = "container_not_empty"
                elif not await attempt_released_in(conn, row["id"]):
                    row["recovery_blocker"] = "container_not_empty"
                else:
                    row["recovery_blocker"] = "ready"
        return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}

    @api.get("/api/projects/{project_id}/uncertain-launches")
    async def uncertain_launches(project_id: str, after: str = Query("", max_length=160),
                                 limit: int = Query(50, ge=1, le=100),
                                 who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Expose in-flight and unknown launches without disclosing their command payloads."""
        await authorize(project_id, who, "control.read")
        async with app.db.transaction() as conn:
            async with conn.execute(
                "SELECT e.id,e.state,e.claim_generation,e.created_at,e.claimed_at,e.completed_at,"
                "e.error,json_extract(e.payload_json,'$.data.attempt_id') AS attempt_id,"
                "t.id AS task_id,"
                "a.state AS attempt_state,a.provider_session_ref IS NOT NULL AS provider_session_recorded"
                " FROM effect_outbox e JOIN operation_receipts r ON r.id = e.receipt_id"
                " JOIN board_tasks t ON t.id = json_extract(e.payload_json,'$.control.task_id')"
                " LEFT JOIN execution_attempts a ON a.id = json_extract(e.payload_json,'$.data.attempt_id')"
                " WHERE r.scope_kind = 'project' AND r.scope_id = ? AND e.kind = 'task.launch'"
                " AND t.project_id = ? AND e.state IN ('claimed','unknown') AND e.id > ? ORDER BY e.id LIMIT ?",
                (project_id, project_id, after, limit + 1),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        items = rows[:limit]
        async with app.db.transaction() as conn:
            for row in items:
                attempt_id = row["attempt_id"]
                row["no_entry_observed"] = bool(attempt_id and await no_entry_in(conn, attempt_id))
                row["exit_observed"] = bool(attempt_id and await physical_exit_in(conn, attempt_id))
        for row in items:
            row["provider_session_recorded"] = bool(row["provider_session_recorded"])
        return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}

    @api.post("/api/projects/{project_id}/uncertain-launches/{effect_id}/reconcile")
    async def reconcile_launch(project_id: str, effect_id: str,
                               who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Apply only the launch handler's exact observation to one uncertain receipt."""
        await authorize(project_id, who, "task.launch")
        row = await app.db.fetchone(
            "SELECT e.state FROM effect_outbox e JOIN operation_receipts r ON r.id = e.receipt_id"
            " JOIN board_tasks t ON t.id = json_extract(e.payload_json,'$.control.task_id')"
            " WHERE e.id = ? AND e.kind = 'task.launch' AND r.scope_kind = 'project'"
            " AND r.scope_id = ? AND t.project_id = ?", (effect_id, project_id, project_id),
        )
        if row is None:
            raise HTTPException(404, "no such project launch")
        if row["state"] != "unknown":
            return {"state": row["state"], "reconciled": False}
        store = OutboxStore(app.db)
        claim = await store.unknown_one(effect_id, "task.launch")
        if claim is None:
            return {"state": (await store.view(effect_id))["state"], "reconciled": False}
        resolution = await TaskLaunchEffect(app).reconcile(claim)
        if resolution is None:
            return {"state": "unknown", "reconciled": False,
                    "reason": "exact runtime outcome is unobserved; the launch remains fenced"}
        changed = await store.reconcile(effect_id, generation=claim.generation,
                                        state=resolution.state, evidence=resolution.evidence)
        return {"state": resolution.state if changed else (await store.view(effect_id))["state"],
                "reconciled": changed, "proof": resolution.evidence if changed else None}

    @api.post("/api/projects/{project_id}/unknown-stops/{attempt_id}/reconcile")
    async def reconcile_expired(project_id: str, attempt_id: str,
                                who: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        await authorize(project_id, who, "lifecycle.cancel")
        try:
            return {"cancel_state": await service().reconcile_expired(attempt_id, project_id)}
        except KeyError as exc:
            raise HTTPException(404, "no such timed-out attempt") from exc

    @api.get("/api/lifecycle/{parent_kind}/{parent_id}")
    async def preview(parent_kind: str, parent_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().preview(parent_kind, parent_id)
        except KeyError as exc:
            raise HTTPException(404, "no such parent") from exc
        except LifecycleRefused as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/lifecycle/{parent_kind}/{parent_id}/cancel")
    async def cancel(
        parent_kind: str, parent_id: str, body: CancelBody, who: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        try:
            return await service().cancel_command(
                Principal.operator(who), parent_kind, parent_id, body.reason,
                expected_entity_revision=body.expected_entity_revision,
                expected_source_revision=body.expected_source_revision,
                client_operation_id=body.client_operation_id,
                preview_fingerprint=body.preview_fingerprint,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such parent") from exc
        except (LifecycleRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
