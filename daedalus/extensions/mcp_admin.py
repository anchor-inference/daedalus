"""MCP servers added, changed and removed while the host runs, by the operator or by the agent.

The servers live in config.toml, and a saved configuration reaches the running manager at once:
a new server is offered to every session, a changed one reconnects on its next use, and a removed
one is stopped and forgotten with its OAuth tokens. Nothing here needs a restart.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import ValidationError

from daedalus.config import McpServerConfig
from daedalus.security import redact

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")
CONNECT_SECONDS = 20.0
"""How long an add waits to see the server answer before reporting what it has so far."""


class McpAdminError(ValueError):
    """A server that cannot be saved as asked; the message says why, for a person or a model."""


def _masked(values: dict[str, str]) -> dict[str, str]:
    return {key: (redact.MASK if value else value) for key, value in values.items()}


def _keep_masked(sent: dict[str, str], stored: dict[str, str]) -> dict[str, str]:
    """A value sent back as the mask keeps the stored one: the app never sees the real value."""
    kept = {}
    for key, value in sent.items():
        if value == redact.MASK:
            if key in stored:
                kept[key] = stored[key]
        else:
            kept[key] = value
    return kept


def describe(app: Application) -> list[dict[str, Any]]:
    """Every configured server with how it connects, its state, and whether its sign-in is linked.

    Header, env and client-secret values are masked, as everywhere the configuration is shown."""
    manager = app.manager.mcp if app.manager is not None else None
    status = {item["name"]: item for item in manager.status()} if manager is not None else {}
    rows = []
    for name, config in sorted(app.config.mcp.servers.items()):
        oauth = None
        if config.oauth is not None:
            client = manager.oauth_client(name) if manager is not None else None
            oauth = {
                "scopes": list(config.oauth.scopes), "issuer": config.oauth.issuer, "client_id": config.oauth.client_id,
                "client_secret": redact.MASK if config.oauth.client_secret else "",
                "linked": bool(client and client.linked()), "redirect_uri": client.redirect_uri if client else "",
            }
        live = status.get(name) or {}
        rows.append({
            "name": name, "transport": config.transport, "command": config.command, "args": list(config.args),
            "url": config.url, "description": config.description, "timeout_seconds": config.timeout_seconds,
            "headers": _masked(config.headers), "env": _masked(config.env), "oauth": oauth,
            "approval": config.approval, "tool_approval": dict(config.tool_approval),
            "state": live.get("state", "disabled"), "error": live.get("error"), "tools": live.get("tools", []),
        })
    return rows


def build(name: str, spec: dict[str, Any], stored: McpServerConfig | None) -> McpServerConfig:
    """The server ``spec`` asks for, checked; masked secrets keep what ``stored`` holds."""
    if not NAME.fullmatch(name or ""):
        raise McpAdminError("a server name is 1–48 letters, digits, '.', '_' or '-', starting with a letter or digit")
    body = dict(spec)
    body.pop("name", None)
    if stored is not None:
        body["headers"] = _keep_masked(dict(body.get("headers") or {}), stored.headers)
        body["env"] = _keep_masked(dict(body.get("env") or {}), stored.env)
        # The app's form has no fields for these, so a save from it left them out and a description
        # edited there put a server the operator had allowed, or held to ask, back to "auto".
        body.setdefault("approval", stored.approval)
        body.setdefault("tool_approval", dict(stored.tool_approval))
    oauth = body.get("oauth")
    if isinstance(oauth, dict):
        oauth = dict(oauth)
        if oauth.get("client_secret") == redact.MASK:
            oauth["client_secret"] = stored.oauth.client_secret if stored is not None and stored.oauth is not None else ""
        body["oauth"] = oauth
    try:
        config = McpServerConfig.model_validate(body)
    except ValidationError as exc:
        raise McpAdminError("; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())) from exc
    if config.transport == "stdio":
        if not config.command.strip():
            raise McpAdminError("a stdio server needs the command that starts it")
        if config.oauth is not None:
            raise McpAdminError("sign-in (OAuth) is for an HTTP server; a stdio server takes its keys in env")
    else:
        parsed = urlparse(config.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise McpAdminError("an HTTP server needs its full http(s) URL")
    return config


async def put(app: Application, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Add or replace one server and save; the running manager takes it at once."""
    servers = dict(app.config.mcp.servers)
    config = build(name, spec, servers.get(name))
    added = name not in servers
    servers[name] = config
    await app.save_config(app.config.model_copy(update={"mcp": app.config.mcp.model_copy(update={"servers": servers})}))
    return {"name": name, "added": added}


async def remove(app: Application, name: str) -> bool:
    """Forget one server: its tools leave every session, its process stops, its tokens are deleted."""
    servers = dict(app.config.mcp.servers)
    if servers.pop(name, None) is None:
        return False
    await app.save_config(app.config.model_copy(update={"mcp": app.config.mcp.model_copy(update={"servers": servers})}))
    return True


async def probe(app: Application, name: str) -> dict[str, Any]:
    """Connect once and say how it went: ready with its tools, waiting for sign-in, or the error."""
    manager = app.manager.mcp
    try:
        await manager.ensure(name, deadline=CONNECT_SECONDS)
    except (TimeoutError, RuntimeError):
        pass  # still connecting, or failed: the status row below says which, with the error
    except Exception as exc:  # noqa: BLE001 — what failed is the answer to the person who asked
        return {"name": name, "state": "unavailable", "error": str(exc), "tools": []}
    row = next((item for item in manager.status() if item["name"] == name), None) or {}
    return {"name": name, "state": row.get("state", "disabled"), "error": row.get("error"), "tools": row.get("tools", [])}


async def agent_service(app: Application, op: str, *, session_id: str, name: str, spec: dict[str, Any] | None = None) -> str:
    """McpAdd and McpRemove: the agent's own way to change the servers, applied at once.

    The agent can already run any command on the host, so a stdio server it adds is no power it
    lacked; what it gains is not having to ask the operator to edit config.toml and restart."""
    if op == "remove":
        if not await remove(app, name):
            raise McpAdminError(f"no MCP server {name!r}; configured: {', '.join(sorted(app.config.mcp.servers)) or 'none'}")
        logger.info("session %s removed MCP server %s", session_id, name)
        return f"removed {name}: its tools left every session and its connection is stopped"
    saved = await put(app, name, spec or {})
    logger.info("session %s %s MCP server %s", session_id, "added" if saved["added"] else "changed", name)
    result = await probe(app, name)
    verb = "added" if saved["added"] else "changed"
    if result["state"] == "ready":
        return (f"{verb} {name}; it answers with {len(result['tools'])} tools. Enable it in a session with "
                f"McpEnable(server={name!r}).")
    if result["state"] == "auth_required" and app.config.mcp.servers[name].oauth is not None:
        try:
            url = await app.manager.mcp.oauth_begin(name)
        except Exception as exc:  # noqa: BLE001 — the server is saved; the sign-in is what failed
            return f"{verb} {name}, but its sign-in could not start: {exc}"
        return (f"{verb} {name}; it needs the owner to sign in. Give them this link — after approving, the browser "
                f"returns to the app and the link completes (Settings → MCP servers shows it):\n\n{url}\n\n"
                f"Then McpEnable(server={name!r}).")
    return f"{verb} {name}, but it does not answer yet: {result.get('error') or result['state']}. McpRemove takes it back."


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None

    async def service(op: str, *, session_id: str, name: str, spec: dict[str, Any] | None = None) -> str:
        return await agent_service(app, op, session_id=session_id, name=name, spec=spec)

    app.manager.service_hooks["mcp_admin"] = service
    return []


__all__ = ["McpAdminError", "agent_service", "build", "describe", "install", "probe", "put", "remove"]
