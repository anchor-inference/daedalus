"""Read-only integration availability and bounded authentication probes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI

from daedalus.extensions.integration_health import capability_matrix, github_auth_probe

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
        if not probe:
            return {"github": {"state": "configured_unverified" if app.settings.github_token.strip() else "needs_reconnect"},
                    "gitlab": {"state": "unsupported"}, "forgejo": {"state": "unsupported"}}
        return {"github": await github_auth_probe(app.settings.github_token),
                "gitlab": {"state": "unsupported"}, "forgejo": {"state": "unsupported"}}
