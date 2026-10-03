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


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1)
    expected_step_revision: int = Field(ge=1)
    expected_source_contract_revision: int = Field(ge=1)
    expected_input_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    client_operation_id: str = Field(min_length=1, max_length=160)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)


class ReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=64)
    expected_input_hash: str = Field(min_length=64, max_length=64)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def workflows() -> BoardWorkflows:
        available = getattr(app, "extensions", {}).get("board_workflows")
        if not isinstance(available, BoardWorkflows):
            raise HTTPException(503, "board workflow subsystem is unavailable")
        return available

    @api.post("/api/board-workflows/validate")
    async def validate_workflow(body: DefinitionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return validate(body.model_dump())
        except WorkflowRefused as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/board-workflows/runs")
    async def start_workflow(body: StartBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await workflows().start(
                Principal.operator(who), body.project_id, body.task_id,
                body.model_dump(exclude={"project_id", "task_id", "expected_entity_revision", "client_operation_id"}),
                expected_entity_revision=body.expected_entity_revision,
                client_operation_id=body.client_operation_id,
            )
        except (WorkflowRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.post("/api/board-workflows/runs/{run_id}/steps/{node_id}/approve")
    async def approve_workflow_step(
        run_id: str, node_id: str, body: ApprovalBody, who: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        try:
            return await workflows().approve_command(
                Principal.operator(who), run_id, node_id,
                expected_entity_revision=body.expected_entity_revision,
                expected_step_revision=body.expected_step_revision,
                expected_source_contract_revision=body.expected_source_contract_revision,
                expected_input_digest=body.expected_input_digest,
                client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such workflow") from exc
        except (WorkflowRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/board/{task_id}/board-workflows")
    async def list_task_workflows(task_id: str, before: str = "",
                                  who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            Principal.operator(who)
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        workflows()
        task = await app.db.fetchone("SELECT project_id,entity_revision FROM board_tasks WHERE id = ?", (task_id,))
        if task is None or task["project_id"] is None:
            raise HTTPException(404, "no such project task")
        cursor_at = ""
        cursor_id = ""
        if before:
            cursor = await app.db.fetchone(
                "SELECT created_at,id FROM board_workflow_runs WHERE id = ? AND task_id = ?", (before, task_id),
            )
            if cursor is None:
                raise HTTPException(404, "no such workflow cursor")
            cursor_at, cursor_id = cursor["created_at"], cursor["id"]
        rows = await app.db.fetchall(
            "SELECT id,project_id,task_id,definition_digest,status,created_at,updated_at "
            "FROM board_workflow_runs WHERE task_id = ? "
            "AND (? = '' OR (created_at,id) < (?,?)) ORDER BY created_at DESC,id DESC LIMIT 21",
            (task_id, before, cursor_at, cursor_id),
        )
        items = [{**dict(row), "run_id": row["id"]} for row in rows[:20]]
        return {"task_id": task_id, "project_id": task["project_id"],
                "task_entity_revision": task["entity_revision"], "items": items,
                "next_before": items[-1]["run_id"] if len(rows) > 20 else None}

    @api.get("/api/board-workflows/runs/{run_id}")
    async def get_workflow(run_id: str, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            Principal.operator(who)
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        try:
            return await workflows().inspect(run_id)
        except KeyError as exc:
            raise HTTPException(404, "no such workflow") from exc

    @api.post("/api/board-workflows/runs/{run_id}/reconcile")
    async def reconcile_workflow(run_id: str, body: ReconcileBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await workflows().reconcile_preview(run_id, body.node_id, body.expected_input_hash)
        except KeyError as exc:
            raise HTTPException(404, "no such workflow step") from exc
        except WorkflowRefused as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/board-workflows/runs/{run_id}/cancel")
    async def cancel_workflow(
        run_id: str, body: CancelBody, who: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        try:
            return await workflows().cancel_command(
                Principal.operator(who), run_id, body.reason,
                expected_entity_revision=body.expected_entity_revision,
                client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such workflow") from exc
        except (WorkflowRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
