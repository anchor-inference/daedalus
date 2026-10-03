"""Operator preview and reviewed application of project issue changes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.issue_sync import IssueSync, IssueSyncRefused
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    repository: str = Field(min_length=3, max_length=201)
    issue_number: int = Field(ge=1, strict=True)


class ApplyBody(PreviewBody):
    action: Literal["import", "resolve_fields", "push_title_body"]
    preview_digest: str = Field(min_length=64, max_length=64)
    expected_collection_revision: int | None = Field(default=None, ge=1, strict=True)
    expected_entity_revision: int | None = Field(default=None, ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)
    fields: dict[str, Literal["local", "remote"]] | None = None


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> IssueSync:
        available = app.extensions.get("issue_sync")
        if not isinstance(available, IssueSync):
            raise HTTPException(503, "issue sync is unavailable")
        return available

    @api.post("/api/issues/sync/preview")
    async def preview(body: PreviewBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().preview(body.project_id, body.repository, body.issue_number)
        except KeyError as exc:
            raise HTTPException(404, "project is unavailable") from exc
        except IssueSyncRefused as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/issues/sync/apply")
    async def apply(body: ApplyBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            principal = Principal.operator(who)
            if body.action == "import":
                if body.expected_collection_revision is None:
                    raise IssueSyncRefused("collection revision is required for import")
                result = await service().apply_import(
                    principal, body.project_id, body.repository, body.issue_number,
                    preview_digest=body.preview_digest,
                    expected_collection_revision=body.expected_collection_revision,
                    client_operation_id=body.client_operation_id,
                )
            elif body.action == "resolve_fields":
                if body.expected_entity_revision is None or body.fields is None:
                    raise IssueSyncRefused("task revision and field choices are required")
                result = await service().resolve_remote(
                    principal, body.project_id, body.repository, body.issue_number,
                    preview_digest=body.preview_digest,
                    expected_entity_revision=body.expected_entity_revision,
                    client_operation_id=body.client_operation_id, fields=body.fields,
                )
            else:
                if body.expected_entity_revision is None:
                    raise IssueSyncRefused("task revision is required for a push")
                result = await service().queue_push(
                    principal, body.project_id, body.repository, body.issue_number,
                    preview_digest=body.preview_digest,
                    expected_entity_revision=body.expected_entity_revision,
                    client_operation_id=body.client_operation_id,
                )
            if result.get("effect_id"):
                app.extensions["effects"].notify()
            return result
        except KeyError as exc:
            raise HTTPException(404, "project or issue link is unavailable") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, IssueSyncRefused) as exc:
            raise HTTPException(409, str(exc)) from exc
