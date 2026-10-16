"""MCP servers: list, enable and disable per session; add and remove for the whole installation."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("mcp") if manager is not None else None


@tool_group("mcp")
@search_hint(
    "mcp servers list which integrations connectors external tools available "
    "мсп мцп интеграции коннекторы подключения внешние сервисы какие доступны серверы"
)
@tool(
    name="McpList",
    description=(
        "List the MCP servers configured for this installation, which of them are enabled in "
        "this session, and the tools they provide. Servers are off in a new session; enable "
        "one with McpEnable when its tools would help."
    ),
)
async def mcp_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    return ok(context, await hook("list", session_id=context.session_id))


@tool_group("mcp")
@search_hint(
    "enable mcp server connect integration turn on connector activate plugin "
    "включить включи подключить подключи мсп интеграцию коннектор активировать активируй плагин"
)
@tool(
    name="McpEnable",
    description=(
        "Enable an MCP server for this session. The connection is made if needed, and the result "
        "names the server's tools exactly (a large one by its exact name prefix, the count and a few "
        "names); call them by those names, and load them with ToolSearch first when the result says "
        "they are not loaded."
    ),
)
async def mcp_enable(context: ToolContext, server: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    try:
        return ok(context, await hook("enable", session_id=context.session_id, server=server))
    except (KeyError, RuntimeError) as exc:
        return error(context, str(exc))


@tool_group("mcp")
@search_hint(
    "disable mcp server turn off integration disconnect connector deactivate "
    "выключить выключи отключить отключи мсп интеграцию коннектор деактивировать убрать убери"
)
@tool(name="McpDisable", description="Disable an MCP server for this session; its tools, including any loaded earlier, disappear on the next step.")
async def mcp_disable(context: ToolContext, server: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    return ok(context, await hook("disable", session_id=context.session_id, server=server))


def _admin(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("mcp_admin") if manager is not None else None


@tool_group("mcp")
@search_hint(
    "add mcp server install connect new integration register connector configure remote url stdio command oauth "
    "добавить добавь мсп сервер установить подключить новую интеграцию коннектор настроить"
)
@tool(
    name="McpAdd",
    description=(
        "Add an MCP server to this installation, or change one, at once and without a restart. "
        "transport='stdio' runs `command` with `args` (keys go in `env`); transport='http' connects to `url` "
        "(a static key goes in `headers`, e.g. {\"Authorization\": \"Bearer …\"}). For a server that signs in "
        "with OAuth set oauth=true: the result carries the link the owner opens, and the sign-in completes "
        "by itself. `client_id`/`client_secret` only for a server that does not register clients itself. "
        "The result says whether it answers; then McpEnable turns it on in a session."
    ),
)
async def mcp_add(
    context: ToolContext, name: str, transport: str = "stdio", command: str = "", args: list[str] | None = None,
    url: str = "", env: dict[str, str] | None = None, headers: dict[str, str] | None = None, description: str = "",
    oauth: bool = False, scopes: list[str] | None = None, client_id: str = "", client_secret: str = "",
) -> ToolResult:
    hook = _admin(context)
    if hook is None:
        return error(context, "MCP servers cannot be changed in this session")
    spec = {"transport": transport, "command": command, "args": args or [], "url": url, "env": env or {},
            "headers": headers or {}, "description": description}
    if oauth:
        spec["oauth"] = {"scopes": scopes or [], "client_id": client_id, "client_secret": client_secret}
    try:
        return ok(context, await hook("add", session_id=context.session_id, name=name, spec=spec))
    except (KeyError, ValueError, RuntimeError) as exc:
        return error(context, str(exc))


@tool_group("mcp")
@search_hint(
    "remove mcp server delete uninstall integration drop connector forget "
    "удалить удали мсп сервер убрать интеграцию коннектор снести"
)
@tool(
    name="McpRemove",
    description=(
        "Remove an MCP server from this installation at once: its tools leave every session, its process or "
        "connection stops, and its stored sign-in is deleted. McpDisable only turns it off in this session."
    ),
)
async def mcp_remove(context: ToolContext, name: str) -> ToolResult:
    hook = _admin(context)
    if hook is None:
        return error(context, "MCP servers cannot be changed in this session")
    try:
        return ok(context, await hook("remove", session_id=context.session_id, name=name))
    except (KeyError, ValueError, RuntimeError) as exc:
        return error(context, str(exc))


TOOLS = [mcp_list, mcp_enable, mcp_disable, mcp_add, mcp_remove]

__all__ = ["TOOLS"]
