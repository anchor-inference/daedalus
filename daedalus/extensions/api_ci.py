"""Review current-head checks and version a task's required CI policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.ci_observations import ci_readiness, set_required_checks
from daedalus.extensions.orchestrator_domain import DomainConflict
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope

if TYPE_CHECKING:
    from daedalus.app import Application


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    provider: str = Field(min_length=1, max_length=80)
    repository_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=30)
    check_names: list[str] = Field(max_length=32)


def install_routes(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    """Register task-scoped CI routes after host authentication is available."""
    @api.get("/api/board/{task_id}/ci")
    async def readiness(task_id: str, head_sha: str | None = None,
                        _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        task = await app.db.fetchone("SELECT contract_revision,branch FROM board_tasks WHERE id = ?", (task_id,))
        if task is None:
            raise HTTPException(404, "no such task")
        if task["branch"]:
            review = getattr(app.extensions.get("staff"), "review", None)
            if review is None:
                raise HTTPException(503, "review service is unavailable")
            current = await review.review(task_id)
            if head_sha is not None and head_sha != current["head_sha"]:
                raise HTTPException(409, "the requested CI head is not the current branch HEAD")
            head_sha = current["head_sha"]
        async with app.db.transaction() as conn:
            return {"task_id": task_id, "contract_revision": task["contract_revision"],
                    "head_sha": head_sha,
                    **await ci_readiness(conn, task_id, task["contract_revision"], head_sha)}

    @api.post("/api/board/{task_id}/ci/requirements")
    async def requirements(task_id: str, body: Requirements,
                           who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
            if task is None:
                raise HTTPException(404, "no such task")
            scope = Scope("project", task["project_id"]) if task["project_id"] else Scope("global", "global")

            async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
                return await set_required_checks(conn, task_id=task_id, provider=body.provider,
                                                 repository_id=body.repository_id,
                                                 check_names=body.check_names,
                                                 origin_ref=mutation.receipt_id)

            return await ControlStore(app.db).mutate(
                Principal.operator(who), scope, "ci.requirements.set", body.client_operation_id,
                body.expected_entity_revision, Entity("task", task_id), body.model_dump(), effect,
            )
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, DomainConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc


__all__ = ["install_routes"]
