"""MCP servers from Settings: list, add or change, remove, connect, and sign in with OAuth."""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from daedalus.extensions import mcp_admin
from daedalus.mcp.oauth import OAuthError, mcp_callback_url

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)
SETTINGS_PAGE = "/app/settings/tools"


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def manager() -> Any:
        if app.manager is None:
            raise HTTPException(503, "the host is still starting")
        return app.manager.mcp

    def known(name: str) -> None:
        if name not in app.config.mcp.servers:
            raise HTTPException(404, f"no MCP server {name!r}")

    @api.get("/api/mcp/servers")
    async def servers(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"servers": mcp_admin.describe(app), "callback": mcp_callback_url(app.settings.miniapp_public_url)}

    @api.put("/api/mcp/servers/{name}")
    async def put_server(name: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Add or change a server; it is connected once at once so the answer says whether it works."""
        try:
            saved = await mcp_admin.put(app, name, body)
        except mcp_admin.McpAdminError as exc:
            raise HTTPException(422, str(exc)) from exc
        probe = await mcp_admin.probe(app, name)
        return {**saved, "probe": probe, "servers": mcp_admin.describe(app)}

    @api.delete("/api/mcp/servers/{name}")
    async def delete_server(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        known(name)
        await mcp_admin.remove(app, name)
        return {"removed": name, "servers": mcp_admin.describe(app)}

    @api.post("/api/mcp/servers/{name}/connect")
    async def connect(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        known(name)
        return {"probe": await mcp_admin.probe(app, name), "servers": mcp_admin.describe(app)}

    @api.post("/api/mcp/servers/{name}/oauth/begin")
    async def oauth_begin(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        known(name)
        try:
            return {"auth_url": await manager().oauth_begin(name)}
        except KeyError as exc:
            raise HTTPException(409, f"{name} has no sign-in configured; turn sign-in on for it first") from exc
        except OAuthError as exc:
            raise HTTPException(502, str(exc)) from exc

    @api.post("/api/mcp/servers/{name}/oauth/disconnect")
    async def oauth_disconnect(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        known(name)
        await manager().oauth_disconnect(name)
        return {"servers": mcp_admin.describe(app)}

    @api.get("/api/mcp/oauth/callback")
    async def oauth_callback(request: Request) -> Any:
        """Where an MCP server's sign-in sends the browser back.

        No login is asked for: the browser arrives from the server's page, and what binds it to a
        request this host made is ``state``, random and pending only until it is used, plus the
        PKCE verifier that never left the host — a forged callback has neither."""
        query = request.query_params
        state = query.get("state", "")
        name = manager().find_pending(state)
        if query.get("error"):
            return _back(name, error=query.get("error_description") or query["error"])
        if name is None:
            return _page("This sign-in link has expired or was already used; start it again from Settings.", 400)
        try:
            await manager().oauth_finish(name, str(request.url))
        except OAuthError as exc:
            logger.warning("MCP server %s: sign-in did not finish: %s", name, exc)
            return _back(name, error=str(exc))
        try:
            await mcp_admin.probe(app, name)
        except Exception:  # noqa: BLE001 — the link is made; connecting again is the app's next step
            logger.exception("MCP server %s: linked but did not connect", name)
        return _back(name)


def _back(name: str | None, *, error: str = "") -> RedirectResponse:
    query = {"mcp": name or "", **({"mcp_error": error[:300]} if error else {"mcp_linked": "1"})}
    return RedirectResponse(f"{SETTINGS_PAGE}?{urlencode(query)}", status_code=303)


def _page(text: str, status: int) -> HTMLResponse:
    body = f"<!doctype html><meta charset=utf-8><title>MCP</title><p>{html.escape(text)}</p><p><a href='{SETTINGS_PAGE}'>Settings</a></p>"
    return HTMLResponse(body, status_code=status)


__all__ = ["register"]
