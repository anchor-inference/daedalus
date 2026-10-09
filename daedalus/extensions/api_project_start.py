"""Authenticated first-project command with a pinned goal and first task."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.api_projects import environments
from daedalus.extensions.project_start import ProjectStart
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.projects import FolderSpec, ProjectError

if TYPE_CHECKING:
    from daedalus.app import Application


class Folder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4000)
    label: str = Field(default="", max_length=60)
    env: Literal["container", "host"] | None = None
    readonly: bool = False


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_collection_revision: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=4000)
    constraints: str = Field(min_length=1, max_length=4000)
    task_title: str = Field(min_length=1, max_length=200)
    checks: list[str] = Field(min_length=1, max_length=12)
    owner_intent: Literal["manual", "later"]
    folder: Folder | None = None
    folder_name: str = Field(default="", max_length=80)
    """The new folder's name under the workspaces when no folder is chosen; empty is the project's id."""


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.post("/api/project-start")
    async def start(body: StartBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            spec = FolderSpec(**body.folder.model_dump()) if body.folder else None
            if spec and spec.env and spec.env not in environments(app.settings, app.manager.projects.local_env,
                                                                   app.extensions.get("terminals"))["available"]:
                raise ProjectError("the selected folder environment is unavailable")
            return await ProjectStart(app.db, app.manager.projects, app.manager.bus).create(
                Principal.operator(who), client_operation_id=body.client_operation_id,
                expected_collection_revision=body.expected_collection_revision, name=body.name,
                goal=body.goal, constraints=body.constraints, task_title=body.task_title,
                checks=body.checks, owner_intent=body.owner_intent, folder=spec,
                managed_name=body.folder_name)
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from exc
