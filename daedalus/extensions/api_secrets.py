"""The operator's secrets in the app: hand one over for a chat or its project, list them, change one, take one back.

A value comes in on its own request. Every list and every write answers with the secret without its value;
the one way a value goes out is ``POST /api/secrets/{id}/reveal``, for the operator to read or copy, which
writes an audit line first and is never cached. A message names the secrets it carries; the value is never
part of a message.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from daedalus.security.operator_secrets import MAX_NOTE, MAX_VALUE, SecretError, normalise_name

if TYPE_CHECKING:
    from daedalus.app import Application


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = ""
    """The chat the operator is in: the scope itself, or the way to its project."""
    project_id: str = ""
    """A project named directly, for a secret filed from Settings, where there is no chat to go through."""
    scope: str = Field(pattern="^(session|project)$")
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=MAX_VALUE)
    note: str = Field(default="", max_length=MAX_NOTE)


class SecretChange(BaseModel):
    """What the operator changed; a field left out stays as it is. A value is replaced, never kept beside the old one."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    value: str | None = Field(default=None, min_length=1, max_length=MAX_VALUE)
    note: str | None = Field(default=None, max_length=MAX_NOTE)


class RevealBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(default="reveal", pattern="^(reveal|copy)$")
    """Shown on screen or put on the clipboard: the audit says which."""


CLOSED_DOORS = ("panel", "device")
"""Ways in that never see a value. Neither door forwards any ``/api/secrets`` route today; the reveal
refuses them by name as well, so a later widening of either list cannot open it by accident."""


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
            mine = {s.id for s in items}
            # The rest, for the manager to show below the chat's own: changed or removed from here, never
            # attached, since the host would leave a secret of another scope out of the message anyway.
            others = [s for s in manager.secrets.listed() if s.id not in mine]
            scope = {"project_id": project.id if project else None, "project_name": project.name if project else None,
                     "others": [{**s.public(), "scope_title": names.get((s.scope_kind, s.scope_id), "")} for s in others]}
        else:
            items = manager.secrets.listed()
            # The sign-ins saved from the browser's private form: the operator's, never a chat's, so only
            # the whole list carries them. The projects are where Settings may file a new secret.
            scope = {"logins": [login.public() for login in manager.secrets.saved_logins()], "projects": await filing_projects()}
        listed = [{**s.public(), "scope_title": names.get((s.scope_kind, s.scope_id), "")} for s in items]
        return {"secrets": listed, **scope}

    async def filing_projects() -> list[dict[str, str]]:
        """The projects a secret can be filed under from Settings: every one that is not a single chat's."""
        chosen: list[dict[str, str]] = []
        for row in await app.db.fetchall("SELECT id, name, settings FROM projects ORDER BY name"):
            try:
                ephemeral = bool(json.loads(row["settings"] or "{}").get("ephemeral"))
            except ValueError:
                ephemeral = False
            if not ephemeral:
                chosen.append({"id": row["id"], "name": row["name"]})
        return chosen

    @api.post("/api/secrets")
    async def put_secret(body: SecretBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not body.session_id:
            if body.scope != "project" or not body.project_id:
                raise HTTPException(400, "a secret belongs to a chat or to a project")
            project = await app.db.fetchone("SELECT id, settings FROM projects WHERE id = ?", (body.project_id,))
            if project is None:
                raise HTTPException(404, "no such project")
            try:
                ephemeral = bool(json.loads(project["settings"] or "{}").get("ephemeral"))
            except ValueError:
                ephemeral = False
            if ephemeral:
                # A chat's own throwaway project is the chat: a secret filed there would look shared and not be.
                raise HTTPException(400, "that project is a single chat's; keep the secret for the chat")
            return await keep("project", body.project_id, body, who)
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
        return await keep(body.scope, scope_id, body, who)

    async def keep(scope_kind: str, scope_id: str, body: SecretBody, who: dict[str, Any]) -> dict[str, Any]:
        try:
            name = normalise_name(body.name)
        except SecretError as exc:
            raise HTTPException(400, str(exc)) from exc
        replaced = any(s.name == name for s in manager.secrets.listed(scope_kind=scope_kind, scope_id=scope_id))
        try:
            secret = await manager.secrets.put(scope_kind, scope_id, body.name, body.value, body.note)
        except SecretError as exc:
            raise HTTPException(400, str(exc)) from exc
        await manager.secrets.audit(secret, "change" if replaced else "create", str(who.get("via") or ""))
        return secret.public()

    def found(secret_id: str) -> Any:
        secret = manager.secrets.get(secret_id)
        if secret is None:
            raise HTTPException(404, "no such secret")
        return secret

    @api.patch("/api/secrets/{secret_id}")
    async def change_secret(secret_id: str, body: SecretChange, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Rename, give a new value, or change the note. The answer is the secret without its value."""
        found(secret_id)
        try:
            outcome = await manager.secrets.update(secret_id, name=body.name, value=body.value, note=body.note)
        except SecretError as exc:
            raise HTTPException(400, str(exc)) from exc
        if outcome is None:
            raise HTTPException(404, "no such secret")
        secret, changed = outcome
        for what in changed:
            await manager.secrets.audit(secret, {"name": "rename", "value": "change", "note": "note"}[what], str(who.get("via") or ""))
        return {**secret.public(), "changed": changed}

    @api.post("/api/secrets/{secret_id}/reveal")
    async def reveal_secret(secret_id: str, body: RevealBody | None = None, who: dict[str, Any] = Depends(auth)) -> JSONResponse:
        """One value, on its own request, for the operator to read or copy. Audited before it is answered,
        and marked so that no cache along the way keeps it."""
        if who.get("via") in CLOSED_DOORS:
            raise HTTPException(403, "a secret's value is shown only in the app")
        found(secret_id)
        shown = await manager.secrets.reveal(secret_id, purpose=(body or RevealBody()).purpose, via=str(who.get("via") or ""))
        if shown is None:
            raise HTTPException(409, "this secret's value cannot be opened: the key changed")
        _secret, value = shown
        return JSONResponse({"value": value}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @api.get("/api/secrets/{secret_id}/usage")
    async def secret_usage(secret_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Where the secret is named: the conversations of its scope whose history carries its placeholder,
        and the trail of who looked at or changed it. Asked when the operator is about to remove it."""
        secret = found(secret_id)
        count, conversations = await manager.secrets.references(secret)
        return {"conversations": count, "titles": conversations, "audit": await manager.secrets.audit_trail(secret_id, 10)}

    @api.delete("/api/secrets/logins/{login_id}")
    async def forget_login(login_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a sign-in saved from the browser's private form; the next request for its site asks for it again."""
        if not await manager.secrets.forget_login(login_id):
            raise HTTPException(404, "no such saved sign-in")
        return {"ok": True}

    @api.delete("/api/secrets/{secret_id}")
    async def delete_secret(secret_id: str, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        secret = found(secret_id)
        if not await manager.secrets.delete(secret_id):
            raise HTTPException(404, "no such secret")
        await manager.secrets.audit(secret, "delete", str(who.get("via") or ""))
        return {"ok": True}
