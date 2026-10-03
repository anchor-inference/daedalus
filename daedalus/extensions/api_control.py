"""Authenticated inspection of command revisions, receipts and effect outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.coordinator_authority import grant_review
from daedalus.extensions.task_controls import queue_stop
from daedalus.extensions.task_launch import queue_launch
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.outbox import OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    reason: str = Field(default="", max_length=2000)


class LaunchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    staff_id: str = Field(min_length=1, max_length=200)
    resume_from: str | None = Field(default=None, max_length=200)


class ReviewAuthorityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    expires_at: str


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.post("/api/projects/{project_id}/orchestrator/review-authority")
    async def authorize_review(project_id: str, body: ReviewAuthorityBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await grant_review(app, project_id, Principal.operator(authenticated), **body.model_dump())
        except KeyError:
            raise HTTPException(404, "no such project") from None
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/board/{task_id}/launch")
    async def launch_task(task_id: str, body: LaunchBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if "effects" not in app.extensions:
            raise HTTPException(503, "execution controls are not available")
        try:
            return await queue_launch(app, task_id, Principal.operator(authenticated), **body.model_dump())
        except KeyError:
            raise HTTPException(404, "no such task") from None
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.post("/api/board/{task_id}/stop")
    async def stop_task(task_id: str, body: StopBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if "effects" not in app.extensions:
            raise HTTPException(503, "execution controls are not available")
        try:
            return await queue_stop(app, task_id, Principal.operator(authenticated), **body.model_dump())
        except KeyError:
            raise HTTPException(404, "no such task") from None
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/control/revisions")
    async def revisions(project: str | None = None, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        scope = Scope("project", project) if project else Scope("global", "global")
        store = ControlStore(app.db)
        try:
            async with app.db.transaction() as conn:
                await store.authorize(conn, Principal.operator(authenticated), scope, "control.read")
                collection = await store._entity(conn, scope, Entity("collection", scope.id))
                entity_revision = await store._entity(conn, scope, Entity("project", scope.id)) if project else None
        except KeyError:
            raise HTTPException(404, "no such project") from None
        return {"scope": {"kind": scope.kind, "id": scope.id}, "entity_revision": entity_revision, "collection_revision": collection}

    @api.get("/api/control/receipts/{receipt_id}")
    async def receipt(receipt_id: str, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        row = await app.db.fetchone("SELECT id,scope_kind,scope_id,actor_id,operation_kind,request_entity_revision,entity_revision,state,response_json,created_at FROM operation_receipts WHERE id = ?", (receipt_id,))
        if row is None:
            raise HTTPException(404, "no such command receipt")
        data = dict(row)
        data["response"] = json.loads(data.pop("response_json"))
        return data

    @api.get("/api/control/effects/{action_id}")
    async def effect(action_id: str, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        Principal.operator(authenticated)
        try:
            return await OutboxStore(app.db).view(action_id)
        except KeyError:
            raise HTTPException(404, "no such effect") from None
