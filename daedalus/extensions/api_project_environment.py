"""The routes of a project's environment: read it, replace its spec, prepare it.

The spec is replaced whole, so sending it twice is the same as sending it once; Prepare starts an
attempt only when none is running (a compare-and-set in the store), so a double press is one run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from daedalus.stores.projects import ProjectError

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.project_environment import ProjectEnvironments


class SpecBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def environments() -> ProjectEnvironments:
        found = app.extensions.get("project_environment")
        if found is None:
            raise HTTPException(503, "project environments are not running on this installation")
        return found  # type: ignore[return-value]

    async def project_or_404(project_id: str) -> None:
        if app.manager is None or await app.manager.projects.get(project_id) is None:
            raise HTTPException(404, "no such project")

    @api.get("/api/projects/{project_id}/environment")
    async def read_environment(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await project_or_404(project_id)
        return await environments().view(project_id)

    @api.put("/api/projects/{project_id}/environment")
    async def write_environment(project_id: str, body: SpecBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await project_or_404(project_id)
        try:
            await environments().save(project_id, body.spec)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await environments().view(project_id)

    @api.post("/api/projects/{project_id}/environment/prepare")
    async def prepare_environment(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await project_or_404(project_id)
        found = environments()
        record = await found.store.get(project_id)
        if not record.spec.needs_preparation:
            raise HTTPException(409, "the environment asks for nothing to be prepared: add a repository, a seed file or a setup goal")
        if record.state == "preparing":
            raise HTTPException(409, "the environment is being prepared already")
        await found.prepare(project_id, by="operator")
        return await found.view(project_id)


__all__ = ["register"]
