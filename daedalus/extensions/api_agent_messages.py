"""What the agents of a project said to each other, read-only for the app.

The log (:class:`daedalus.stores.agent_messages.AgentMessages`) is written by the team's mesh as
agents call SendToAgent; nothing here writes. ``staff`` narrows it to what one member sent or
received. Like ``api.py`` this module may import the HTTP framework.

The command-line team server's direct route posts the three agent tools here as well, under the
launch's team token, for a launch that reaches the host's port rather than the daemon's listener.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from daedalus.harness.team import AGENT_OPS
from daedalus.stores.agent_messages import AgentMessages

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None

    @api.get("/api/projects/{project_id}/agent-messages")
    async def agent_messages(project_id: str, staff: str = Query(default="", max_length=64), before: str = Query(default="", max_length=64),
                             limit: int = Query(default=50, ge=1, le=200), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The project's messages between agents, newest first. ``staff``: only those the member sent
        or received. ``before``: the id of the oldest message the reader has, for the page before it."""
        if await manager.projects.get(project_id) is None:
            raise HTTPException(404, "no such project")
        if staff:
            member = await manager.staff.get(staff)
            if member is None or member.project_id != project_id:
                raise HTTPException(404, "no such member of this project")
        rows = await AgentMessages(manager.db).list(project_id, staff_id=staff or None, before=before or None, limit=limit)
        return {"messages": [row.view() for row in rows], "more": len(rows) == limit}

    async def team_agents(staff_session_id: str, op: str, request: Request) -> dict[str, Any]:
        """ListAgents, ReadAgent and SendToAgent from a command-line member's team server, by the token
        minted for its launch. A refusal is an answer the member reads, not an HTTP error."""
        team = app.extensions.get("staff")
        if team is None:
            raise HTTPException(503, "the team is not running")
        try:
            live = await team.authenticate(staff_session_id, request.headers.get("x-daedalus-team-token", ""))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        try:
            fields = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "the body is not JSON") from exc
        if not isinstance(fields, dict):
            raise HTTPException(400, "the body is an object")
        text, failed = await team.ingress.agents(live, op, fields)
        return {"text": text, "error": failed}

    # One path per operation rather than ``/{op}``: a parameter there would also match the team's
    # ``report`` and ``ask`` routes, which are registered after this module's.
    def route_for(op: str) -> Callable[..., Any]:
        async def route(staff_session_id: str, request: Request) -> dict[str, Any]:
            return await team_agents(staff_session_id, op, request)

        return route

    for op in AGENT_OPS:
        api.add_api_route(f"/api/team/{{staff_session_id}}/{op}", route_for(op), methods=["POST"], name=f"team_{op}")
