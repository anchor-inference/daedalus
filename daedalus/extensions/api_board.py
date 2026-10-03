"""Board writes retain command receipts; launches and reviewed completion have their own routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.board import Board
from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.orchestrator_domain import DomainConflict
from daedalus.stores.control import ControlConflict, ControlDenied, Principal, Scope

if TYPE_CHECKING:
    from daedalus.app import Application


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str | None = Field(default=None, max_length=4000)
    deliverable: str | None = Field(default=None, max_length=4000)
    boundaries: str | None = Field(default=None, max_length=4000)
    done_when: str | None = Field(default=None, max_length=4000)

    def fields(self) -> dict[str, str]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_collection_revision: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=200)
    acceptance: str = Field(default="", max_length=2000)
    checklist: list[str] = Field(default_factory=list, max_length=12)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    priority: int = Field(default=3, ge=1, le=5, strict=True)
    notes: str = Field(default="", max_length=4000)
    brief: Brief = Field(default_factory=Brief)
    session_id: str | None = Field(default=None, max_length=200)
    assignee_staff_id: str | None = Field(default=None, max_length=200)
    folder_id: str | None = Field(default=None, min_length=1, max_length=200)


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    acceptance: str | None = Field(default=None, max_length=2000)
    checklist: list[str] | None = Field(default=None, max_length=12)
    depends_on: list[str] | None = Field(default=None, max_length=20)
    priority: int | None = Field(default=None, ge=1, le=5, strict=True)
    brief: Brief | None = None
    note: str = Field(default="", max_length=1000)
    status: str | None = None
    assignee_staff_id: str | None = Field(default=None, max_length=200)
    check_ids: list[str] | None = Field(default=None, max_length=12)
    uncheck_ids: list[str] | None = Field(default=None, max_length=12)
    folder_id: str | None = Field(default=None, min_length=1, max_length=200)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def commands() -> BoardCommands:
        return BoardCommands(app.db, bus=app.manager.bus)

    async def projection(result: dict[str, Any]) -> dict[str, Any]:
        board = app.extensions.get("board")
        if board is None:
            board = Board(app)
        task = await board.get(result["task_id"])
        # A replay returns the original command even when another editor has since changed the card.
        # Keeping the current projection separate prevents its version from rewriting the receipt.
        return {"command": result, "task": task}

    def rejected(exc: Exception) -> HTTPException:
        if isinstance(exc, ControlConflict):
            return HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision})
        if isinstance(exc, ControlDenied):
            return HTTPException(403, str(exc))
        if isinstance(exc, DomainConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, KeyError):
            return HTTPException(404, "no such task, session or project")
        return HTTPException(422, str(exc))

    async def create(body: CreateBody, authenticated: dict[str, Any], project_id: str | None) -> dict[str, Any]:
        scope = Scope("project", project_id) if project_id else Scope("global", "global")
        try:
            if body.session_id:
                session = await app.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (body.session_id,))
                if session is None:
                    raise KeyError(body.session_id)
                if session["project_id"] != project_id:
                    raise ControlDenied("the source session belongs to another board")
            fields = body.model_dump(exclude={"brief", "session_id"})
            result = await commands().create(Principal.operator(authenticated), scope, **fields,
                                            brief=body.brief.fields(), source_session_id=body.session_id)
            return await projection(result)
        except (ControlConflict, ControlDenied, KeyError, ValueError) as exc:
            raise rejected(exc) from exc

    @api.post("/api/board", status_code=201)
    async def add_global(body: CreateBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await create(body, authenticated, None)

    @api.post("/api/projects/{project_id}/board", status_code=201)
    async def add_project(project_id: str, body: CreateBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await create(body, authenticated, project_id)

    async def update(task_id: str, body: UpdateBody, authenticated: dict[str, Any]) -> dict[str, Any]:
        try:
            task = await app.db.fetchone("SELECT project_id FROM board_tasks WHERE id = ?", (task_id,))
            if task is None:
                raise KeyError(task_id)
            scope = Scope("project", task["project_id"]) if task["project_id"] else Scope("global", "global")
            fields = body.model_dump(exclude={"brief"})
            result = await commands().update(Principal.operator(authenticated), scope, task_id, **fields,
                                            brief=body.brief.fields() if body.brief is not None else None)
            return await projection(result)
        except (ControlConflict, ControlDenied, KeyError, ValueError) as exc:
            raise rejected(exc) from exc

    @api.put("/api/board/{task_id}")
    async def edit(task_id: str, body: UpdateBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await update(task_id, body, authenticated)

    @api.delete("/api/board/{task_id}")
    async def archive(task_id: str, client_operation_id: str = Query(min_length=1, max_length=160),
                      expected_entity_revision: int = Query(ge=1),
                      authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        task = await update(task_id, UpdateBody(client_operation_id=client_operation_id,
                            expected_entity_revision=expected_entity_revision, status="dropped"), authenticated)
        return {"archived": True, **task}
