"""Operator inspection and exact approval of scheduled effects."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.recurring import _contract
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=4000)
    cron: str | None = None
    run_at: str | None = None
    kind: Literal["agent", "message", "lazy", "wake"] = "message"
    target_session: str | None = None
    project_id: str | None = None


class CreateInput(ScheduleInput):
    expires_at: str
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_at: str
    expected_schedule_revision: int = Field(ge=1, strict=True)
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["delivered", "not_delivered"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=160)
    prompt: str | None = Field(default=None, max_length=4000)
    cron: str | None = None
    run_at: str | None = None
    enabled: bool | None = None
    expected_schedule_revision: int = Field(ge=1, strict=True)
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class RemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_schedule_revision: int = Field(ge=1, strict=True)
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> Any:
        recurring = app.extensions.get("recurring")
        if recurring is None:
            raise HTTPException(503, "durable scheduling is unavailable")
        return recurring

    def refused(exc: Exception) -> HTTPException:
        if isinstance(exc, KeyError):
            return HTTPException(404, "no such schedule or occurrence")
        if isinstance(exc, ControlConflict):
            return HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision})
        if isinstance(exc, ControlDenied):
            return HTTPException(403, str(exc))
        return HTTPException(400, str(exc))

    @api.get("/api/recurring/overview")
    async def overview(who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        row = await app.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                    " WHERE scope_kind = 'global' AND scope_id = 'global'")
        return {"global_collection_revision": row["revision"] if row is not None else None}

    @api.post("/api/recurring/preview")
    async def preview(body: ScheduleInput, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        if bool(body.cron) == bool(body.run_at):
            raise HTTPException(400, "choose exactly one time rule")
        try:
            if body.cron:
                if not croniter.is_valid(body.cron):
                    raise ValueError("invalid cron expression")
                due = croniter(body.cron, datetime.now(UTC)).get_next(datetime).astimezone(UTC)
            else:
                due = datetime.fromisoformat(str(body.run_at).replace("Z", "+00:00"))
                if due.tzinfo is None:
                    raise ValueError("a timezone-aware run_at is required")
                due = due.astimezone(UTC)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        contract = _contract(body.model_dump())
        return {"next_run_at": due.isoformat(), "timezone": "UTC", "dst_policy": "UTC has no skipped or repeated local hour",
                "output_contract": contract, "authority": "operator approval with expiry required"}

    @api.post("/api/recurring")
    async def create(body: CreateInput, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().create(
                Principal.operator(who), name=body.name, prompt=body.prompt, cron=body.cron,
                run_at=body.run_at, kind=body.kind, target_session=body.target_session,
                project_id=body.project_id, expires_at=body.expires_at,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (KeyError, ValueError, ControlDenied) as exc:
            raise refused(exc) from exc

    @api.post("/api/recurring/{schedule_id}/approve")
    async def approve(schedule_id: str, body: ApprovalInput,
                      who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().approve(
                Principal.operator(who), schedule_id, expires_at=body.expires_at,
                expected_collection_revision=body.expected_collection_revision,
                expected_schedule_revision=body.expected_schedule_revision,
                client_operation_id=body.client_operation_id,
            )
        except (KeyError, ValueError, ControlDenied) as exc:
            raise refused(exc) from exc

    @api.patch("/api/recurring/{schedule_id}")
    async def change(schedule_id: str, body: ChangeInput,
                     who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True, exclude={
            "expected_schedule_revision", "expected_collection_revision", "client_operation_id",
        })
        try:
            return await service().change(
                Principal.operator(who), schedule_id, fields=fields,
                expected_schedule_revision=body.expected_schedule_revision,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (KeyError, ValueError, ControlDenied) as exc:
            raise refused(exc) from exc

    @api.post("/api/recurring/{schedule_id}/remove")
    async def remove(schedule_id: str, body: RemoveInput,
                     who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().remove(
                Principal.operator(who), schedule_id,
                expected_schedule_revision=body.expected_schedule_revision,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (KeyError, ValueError, ControlDenied) as exc:
            raise refused(exc) from exc

    @api.post("/api/recurring/{schedule_id}/run")
    async def run_now(schedule_id: str, body: RemoveInput,
                      who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().run_now(
                Principal.operator(who), schedule_id,
                expected_schedule_revision=body.expected_schedule_revision,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (KeyError, ValueError, ControlDenied) as exc:
            raise refused(exc) from exc

    @api.get("/api/recurring/{schedule_id}/cycles")
    async def cycles(schedule_id: str, limit: int = Query(default=25, ge=1, le=100),
                     who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        row = await app.db.fetchone("SELECT id,project_id,schedule_revision,authority_state,"
                                    "output_contract_json,next_run_at FROM schedules WHERE id = ?", (schedule_id,))
        if row is None:
            raise HTTPException(404, "no such schedule")
        scope_kind, scope_id = ("project", row["project_id"]) if row["project_id"] else ("global", "global")
        revision = await app.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                         " WHERE scope_kind = ? AND scope_id = ?", (scope_kind, scope_id))
        return {"schedule_id": schedule_id, "schedule_revision": row["schedule_revision"],
                "authority_state": row["authority_state"], "next_run_at": row["next_run_at"],
                "output_contract": json.loads(row["output_contract_json"]),
                "scope": {"kind": scope_kind, "id": scope_id},
                "collection_revision": revision["revision"] if revision is not None else None,
                "cycles": await service().cycles(schedule_id, limit=limit)}

    @api.post("/api/recurring/{schedule_id}/cycles/{cycle_id}/reconcile")
    async def reconcile(schedule_id: str, cycle_id: str, body: ReconcileInput,
                        who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().reconcile(
                Principal.operator(who), schedule_id, cycle_id, outcome=body.outcome,
                reason=body.reason, expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (KeyError, ValueError, ControlDenied) as exc:
            raise refused(exc) from exc
