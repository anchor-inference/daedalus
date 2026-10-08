"""The operator's secrets in the app: hand one over for a chat or its project, list them, take one back.

A value comes in on its own request and never goes out again: every answer here is the secret without
its value. A message names the secrets it carries; the value is never part of a message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.security.operator_secrets import MAX_NOTE, MAX_VALUE, SecretError

if TYPE_CHECKING:
    from daedalus.app import Application


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    """The chat the operator is in: the scope itself, or the way to its project."""
    scope: str = Field(pattern="^(session|project)$")
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=MAX_VALUE)
    note: str = Field(default="", max_length=MAX_NOTE)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager

    async def titles() -> dict[tuple[str, str], str]:
        sessions = {row["id"]: row["title"] for row in await app.db.fetchall("SELECT id, title FROM sessions")}
        projects = {row["id"]: row["name"] for row in await app.db.fetchall("SELECT id, name FROM projects")}
        return {**{("session", k): v for k, v in sessions.items()}, **{("project", k): v for k, v in projects.items()}}

    @api.get("/api/secrets")
    async def list_secrets(session_id: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every secret, or with ``session_id`` the ones that chat may use, and whether it can file one under a project."""
        names = await titles()
        if session_id:
            state = await manager.get_state(session_id)
            if state is None:
                raise HTTPException(404, "no such session")
            items = manager.secrets.available(session_id)
            project = state.project if state.project is not None and not state.project.settings.ephemeral else None
            scope = {"project_id": project.id if project else None, "project_name": project.name if project else None}
        else:
            items = manager.secrets.listed()
            scope = {}
        listed = [{**s.public(), "scope_title": names.get((s.scope_kind, s.scope_id), "")} for s in items]
        return {"secrets": listed, **scope}

    @api.post("/api/secrets")
    async def put_secret(body: SecretBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(body.session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if body.scope == "project":
            # A chat's own throwaway project is the chat: a secret filed there would look shared and not be.
            if state.project is None or state.project.settings.ephemeral:
                raise HTTPException(400, "this chat is not in a project; keep the secret for the chat")
            scope_id = state.project.id
        else:
            scope_id = body.session_id
        try:
            secret = await manager.secrets.put(body.scope, scope_id, body.name, body.value, body.note)
        except SecretError as exc:
            raise HTTPException(400, str(exc)) from exc
        return secret.public()

    @api.delete("/api/secrets/{secret_id}")
    async def delete_secret(secret_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await manager.secrets.delete(secret_id):
            raise HTTPException(404, "no such secret")
        return {"ok": True}
