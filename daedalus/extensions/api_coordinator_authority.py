"""Operator previews and receipted approvals for the project's current coordinator."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.coordinator_authority import approve_authority, authority_view, withdraw_authority
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    expected_coordinator_session_id: str = Field(min_length=1, max_length=200)
    bundle_id: str
    expires_at: str
    task_id: str | None = Field(default=None, max_length=200)


class Withdrawal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    expected_coordinator_session_id: str | None
    expected_grant_generation: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=1000)


def rejected(exc: Exception) -> HTTPException:
    if isinstance(exc, ControlConflict):
        return HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision})
    if isinstance(exc, ControlDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, "no such project or approval")
    return HTTPException(422, str(exc))


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/projects/{project_id}/orchestrator/authority")
    async def view(project_id: str, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(who)
        try:
            return await authority_view(app, project_id)
        except KeyError as exc:
            raise rejected(exc) from exc

    @api.post("/api/projects/{project_id}/orchestrator/authority")
    async def approve(project_id: str, body: Approval, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await approve_authority(app, project_id, Principal.operator(who), **body.model_dump())
        except (ControlConflict, ControlDenied, KeyError, ValueError) as exc:
            raise rejected(exc) from exc

    @api.post("/api/projects/{project_id}/orchestrator/authority/{grant_id}/revoke")
    async def withdraw(project_id: str, grant_id: str, body: Withdrawal,
                       who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await withdraw_authority(app, project_id, Principal.operator(who), grant_id, **body.model_dump())
        except (ControlConflict, ControlDenied, KeyError, ValueError) as exc:
            raise rejected(exc) from exc
