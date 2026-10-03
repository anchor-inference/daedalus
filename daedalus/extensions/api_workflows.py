"""Operator API for bounded board workflows, separate from recorded browser workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.board_workflows import BoardWorkflows, WorkflowRefused, validate
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[dict[str, Any]] = Field(max_length=32)
    edges: list[list[str]] = Field(max_length=128)
    budget: dict[str, int]


class StartBody(DefinitionBody):
    project_id: str
    task_id: str
    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.post("/api/board-workflows/validate")
    async def validate_workflow(body: DefinitionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return validate(body.model_dump())
        except WorkflowRefused as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/board-workflows/runs")
    async def start_workflow(body: StartBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await BoardWorkflows(app.db).start(
                Principal.operator(who), body.project_id, body.task_id,
                body.model_dump(exclude={"project_id", "task_id", "expected_entity_revision", "client_operation_id"}),
                expected_entity_revision=body.expected_entity_revision,
                client_operation_id=body.client_operation_id,
            )
        except (WorkflowRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/board-workflows/runs/{run_id}")
    async def get_workflow(run_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT * FROM board_workflow_runs WHERE id = ?", (run_id,))
        if row is None:
            raise HTTPException(404, "no such workflow")
        steps = await app.db.fetchall(
            "SELECT * FROM board_workflow_steps WHERE run_id = ? ORDER BY node_id", (run_id,)
        )
        return {**dict(row), "steps": [dict(step) for step in steps]}
