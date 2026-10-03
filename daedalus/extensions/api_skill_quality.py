"""Operator review and activation of exact, locally authored skill bundles."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.skill_quality import SkillQuality, SkillRefused, assess
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    markdown: str = Field(max_length=60_000)
    dependencies: list[dict[str, str]] = Field(default_factory=list, max_length=16)


class SkillBody(DraftBody):
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class ActivationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_digest: str = Field(min_length=64, max_length=64)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None
    service = SkillQuality(app.db, manager.skills)
    if hasattr(app, "background"):
        app.background.append(asyncio.create_task(service.reconcile(), name="skill-reconcile"))

    @api.post("/api/skills/validate")
    async def validate_skill(body: DraftBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return assess(body.id, body.markdown, body.dependencies)

    @api.post("/api/skills/candidates")
    async def submit_skill(body: SkillBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service.submit_command(
                Principal.operator(who), body.id, body.version, body.markdown, body.dependencies,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (SkillRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.post("/api/skills/{skill_id}/{version}/activate")
    async def activate_skill(skill_id: str, version: str, body: ActivationBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service.activate_command(
                Principal.operator(who), skill_id, version, body.expected_digest,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (SkillRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
