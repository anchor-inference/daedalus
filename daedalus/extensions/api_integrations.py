"""Read-only integration availability and bounded authentication probes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI

from daedalus.extensions.integration_health import capability_matrix, integration_rows

if TYPE_CHECKING:
    from daedalus.app import Application


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/integrations/capabilities")
    async def capabilities(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return capability_matrix(
            github_configured=bool(app.settings.github_token.strip()),
            webhooks=set(app.config.webhooks),
        )

    @api.get("/api/integrations/health")
    async def health(probe: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The Health screen's integration card. Only ``probe`` reaches out, and only to GitHub."""
        mcp_status = app.manager.mcp.status() if app.manager is not None else []
        return {"rows": await integration_rows(app.settings, app.config, mcp_status=mcp_status, db=app.db, probe=probe)}
