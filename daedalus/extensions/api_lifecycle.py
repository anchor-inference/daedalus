"""Operator preview and receipted cancellation of explicitly owned work."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.lifecycle import Lifecycle
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Principal, Scope, one
from daedalus.stores.lifecycle import LifecycleRefused

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
                "EXISTS (SELECT 1 FROM runtime_exit_observations e WHERE e.attempt_id = a.id"
                " AND e.staff_session_id = a.staff_session_id AND e.host_generation = a.host_generation"
                " AND e.provider_session_ref = a.provider_session_ref"
                " AND e.contract_revision = a.contract_revision AND e.runtime_kind = a.runtime_kind"
                " AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance"
                " AND e.runtime_ref = s.terminal_id"
                " AND e.provider_session_ref = 'terminal:' || s.terminal_id))) AS exit_observed,"
                "EXISTS (SELECT 1 FROM runtime_no_entry_observations n WHERE n.attempt_id = a.id"
                " AND n.staff_session_id = a.staff_session_id AND n.host_generation = a.host_generation"
                " AND n.contract_revision = a.contract_revision) AS no_entry_observed"
                " FROM lifecycle_owners o JOIN execution_attempts a ON a.id = o.child_id"
                " JOIN board_tasks t ON t.id = a.task_id"
                " LEFT JOIN staff_sessions s ON s.id = a.staff_session_id"
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
        for row in items:
            row["generation_matches_host_record"] = row["host_generation"] == host
            row["exit_observed"] = bool(row["exit_observed"])
            row["no_entry_observed"] = bool(row["no_entry_observed"])
        return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}

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
