"""Operator preview and reviewed application of project issue changes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.issue_sync import IssueSync, IssueSyncRefused, repository_of
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


class ListBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    repository: str = Field(min_length=3, max_length=201)
    label: str | None = Field(default=None, max_length=50)


class SelectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int = Field(ge=1, strict=True)
    action: Literal["import", "update"]
    preview_digest: str = Field(min_length=64, max_length=64)
    expected_entity_revision: int | None = Field(default=None, ge=1, strict=True)


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    repository: str = Field(min_length=3, max_length=201)
    items: list[SelectionItem] = Field(min_length=1, max_length=100)
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=120)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> IssueSync:
        available = app.extensions.get("issue_sync")
        if not isinstance(available, IssueSync):
            raise HTTPException(503, "issue sync is unavailable")
        return available

    @api.get("/api/issues/sync/source")
    async def source(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the import sheet starts from: the repository the project's folder points at, read
        from its git config on disk, and whether a GitHub token is configured at all."""
        project = await app.manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "project is unavailable")
        return {"project_id": project_id, "repository": repository_of(project.folders, app.manager.projects.local_env),
                "configured": bool(app.settings.github_token.strip())}

    @api.post("/api/issues/sync/list")
    async def listing(body: ListBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().list_issues(body.project_id, body.repository.strip(), (body.label or "").strip() or None)
        except KeyError as exc:
            raise HTTPException(404, "project is unavailable") from exc
        except IssueSyncRefused as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/issues/sync/import")
    async def import_selection(body: SelectionBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().apply_selection(
                Principal.operator(who), body.project_id, body.repository.strip(),
                [item.model_dump() for item in body.items],
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "project is unavailable") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, IssueSyncRefused) as exc:
            raise HTTPException(409, str(exc)) from exc

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
