"""Standing approvals that belong to a project: grant one from a chat, list a project's, take one back.

The grant is a route of the session it is made from, because it answers that session's open request;
the list and the revocation are the project's. All three are the operator's alone: none is opened to
a device token, a panel or a shared link (``remote.door``, ``api_panel``, ``api_session_share``),
since a rule that outlives the chat is not something a guest or a paired phone should be able to write.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager

    @api.post("/api/sessions/{session_id}/policy/grant-project")
    async def policy_grant_project(session_id: str, body: dict[str, Any], who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Allow calls like a refused one in every chat of this chat's project: ``{"key": "<approval key>"}``.

        Answers 400 with the reason for a chat that belongs to no project and for a question no project
        may answer for good (see ``policy.project_scope_refusal``)."""
        try:
            return await manager.grant_project(session_id, str(body.get("key") or ""), via="cli" if who.get("via") == "device" else "app")
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/projects/{project_id}/approval-rules")
    async def approval_rules(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the operator allowed for every chat of the project, oldest first."""
        if await manager.projects.get(project_id) is None:
            raise HTTPException(404, "no such project")
        return {"rules": manager.project_rules.rows(project_id)}

    @api.delete("/api/projects/{project_id}/approval-rules/{rule_id}")
    async def revoke_approval_rule(project_id: str, rule_id: str, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Take one back; what it allowed is asked about again in every chat of the project."""
        if await manager.projects.get(project_id) is None:
            raise HTTPException(404, "no such project")
        try:
            return {"rules": await manager.revoke_project_rule(project_id, rule_id, via="cli" if who.get("via") == "device" else "app")}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
