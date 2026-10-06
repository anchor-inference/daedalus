"""Long-lived MCP connections and the tool proxies that stand in for remote tools.

Every configured server is connected lazily the first time a session enables it.
Its tools are registered once, under ``Mcp_<server>_<tool>``, in the shared registry;
which sessions actually see them is decided per run by the tool visibility policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from protocore.contracts.tool_registry import IToolRegistry
from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult

from daedalus.config import McpServerConfig
from daedalus.mcp.oauth import MCPOAuthClient, NeedsAuthorization
from daedalus.security import redact
from daedalus.security.untrusted import fenced
from daedalus.tools._common import clip

logger = logging.getLogger(__name__)


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{mcp_tool_prefix(server)}{re.sub(r'[^A-Za-z0-9_]+', '_', tool)}"[:64]


def mcp_tool_prefix(server: str) -> str:
    """What every proxy of ``server`` is named with, and so what its tool group is declared by."""
    return f"Mcp_{re.sub(r'[^A-Za-z0-9]+', '', server.title()) or 'Server'}_"


def mcp_group_name(server: str) -> str:
    """The name the core's catalogue of held-back tools shows for ``server``'s tools."""
    return f"MCP server {server}"


VAULT_NOTE = (
    "[«ref:…» stands for a value this server returned and the host keeps (a token, a key). To use it, pass that exact "
    "string — «ref:» and all — as the argument of this server's tools, even where the schema asks for a UUID or "
    "another format: the host swaps the real value in before the call. Do not guess or reconstruct the value.]"
)
LOOSE_REF_RE = re.compile(r"[«\"'`]*\s*«?ref:([0-9a-f]{10})»?\s*[»\"'`]*")
"""A placeholder as a model may reproduce it: with doubled or missing guillemets, quotes, or spaces around it."""


class SecretVault:
    """Values a server returned that look like credentials (an edit token, a one-time key) — kept here,
    shown to the model as ``«ref:…»`` placeholders, and put back only into calls to the same server.

    The model never sees the value, so it cannot copy it anywhere else; the transcript and the
    provider's logs carry the placeholder; the workflow that needs the value round-tripped works.
    """

    LIMIT = 512

    def __init__(self) -> None:
        self._values: OrderedDict[str, str] = OrderedDict()  # keyed by the 10 hex characters

    def keep(self, value: str) -> str:
        key = hashlib.sha256(value.encode()).hexdigest()[:10]
        self._values[key] = value
        self._values.move_to_end(key)
        while len(self._values) > self.LIMIT:
            self._values.popitem(last=False)
        return f"«ref:{key}»"

    def conceal(self, text: str) -> tuple[str, bool]:
        """The text with secret-shaped values replaced by placeholders; whether anything was replaced."""
        out = redact.shared().vault(text, self.keep)
        return out, out != text

    def resolve(self, value: Any) -> Any:
        """Arguments with every known placeholder put back — strings, nested containers alike."""
        if isinstance(value, str):
            if not self._values or "ref:" not in value:
                return value
            if (whole := LOOSE_REF_RE.fullmatch(value)) and whole.group(1) in self._values:
                return self._values[whole.group(1)]  # the argument is the placeholder itself, however it was quoted
            return redact.REF_RE.sub(lambda m: self._values.get(m.group(0)[5:-1], m.group(0)), value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value


REF_HINT = "A «ref:…» placeholder from this server's earlier result is accepted here as is; the host puts the real value in."


def loosen_for_refs(properties: dict[str, Any]) -> dict[str, Any]:
    """The schema the model sees: a string property that demands a format or a pattern (``uuid``, a hex shape)
    would make the model refuse to pass a ``«ref:…»`` placeholder, so the constraint is dropped and the
    description says a placeholder is fine. The server still validates what it receives after resolution."""
    out: dict[str, Any] = {}
    for name, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "string" and ("format" in prop or "pattern" in prop):
            loose = {k: v for k, v in prop.items() if k not in ("format", "pattern")}
            expected = prop.get("format") or "the documented shape"
            loose["description"] = f"{prop.get('description', '').strip()} ({expected}). {REF_HINT}".strip()
            out[name] = loose
        else:
            out[name] = prop
    return out


class McpToolProxy(Tool):
    """A core ``Tool`` that forwards to a remote MCP tool."""

    def __init__(self, connection: McpConnection, remote_name: str, definition: ToolDefinition) -> None:
        self._connection = connection
        self._remote = remote_name
        self._definition = definition
        self.server = connection.name

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        call_id = str(context.metadata.get("tool_call_id") or "")
        try:
            result = await self._connection.call(self._remote, self._connection.vault.resolve(arguments))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call_id, content=f"MCP tool failed: {_describe(exc)}", is_error=True)
        parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            elif getattr(item, "type", "") == "image":
                parts.append(f"[image {getattr(item, 'mimeType', '')} returned by the tool]")
            else:
                dump = getattr(item, "model_dump", None)
                parts.append(json.dumps(dump() if dump else str(item), default=str)[:2000])
        content = "\n".join(parts) or "(empty result)"
        content, concealed = self._connection.vault.conceal(content)
        content = clip(content, _output_limit(context.session_id), note="the MCP tool returned more")
        content = fenced(content, kind="tool output", origin=f"MCP server {self.server}", source="an outside server",
                         quoted_by="the tool")
        if concealed:
            content += "\n" + VAULT_NOTE
        return ToolResult(tool_call_id=call_id, content=content, is_error=bool(getattr(result, "is_error", None) or getattr(result, "isError", False)))


def _output_limit(session_id: str) -> int:
    """The session's configured output cap (Exec, Read and MCP results share it)."""
    from daedalus.host.services import locator  # Lazy: keeps the MCP package importable without the host layer

    try:
        return locator.get(session_id).max_tool_output_chars
    except RuntimeError:
        return 60_000


def _describe(exc: BaseException) -> str:
    """Flatten exception groups into the first real cause."""
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    if isinstance(inner, MCPError):
        data = inner.data
        detail = f" · {json.dumps(data, default=str)[:600]}" if data not in (None, "", {}) else ""
        return f"MCP error {inner.code}: {inner.message}{detail}"
    return f"{type(inner).__name__}: {inner}"


def _looks_like_auth_failure(exc: BaseException) -> bool:
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    if isinstance(inner, MCPError) and (inner.code in (401, 403, -32001) or "401" in json.dumps(inner.data, default=str)):
        return True
    text = f"{type(exc).__name__}: {exc} {_describe(exc)}".lower()
    return "401" in text or "unauthorized" in text or "invalid_token" in text or "invalid token" in text


def _safe_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "server"


class McpConnection:
    """One server; the transport and session live in a background task."""

    def __init__(
        self,
        name: str,
        config: McpServerConfig,
        oauth: MCPOAuthClient | None = None,
        catalog_changed: Callable[[Sequence[McpToolProxy]], None] | None = None,
    ) -> None:
        self.vault = SecretVault()
        self.name = name
        self.config = config
        self.oauth = oauth
        self.session: ClientSession | None = None
        self.tools: list[McpToolProxy] = []
        self._task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._refresh_generation = 0
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self.error: str | None = None
        self.state = "disabled"
        self.last_error_code: str | None = None
        self.in_flight = 0
        self.catalog_revision = 0
        self._catalog_digest = ""
        self._catalog_changed = catalog_changed
        self.config_digest = _config_digest(config)

    async def start(self) -> None:
        async with self._start_lock:
            if self._task is None or self._task.done():
                self._ready.clear()
                self._closing.clear()
                self.error = None
                self.last_error_code = None
                self.state = "connecting"
                self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")
        await self._ready.wait()
        if self.error:
            raise RuntimeError(self.error)

    async def _run(self) -> None:
        try:
            if self.config.transport == "stdio":
                params = StdioServerParameters(command=self.config.command, args=list(self.config.args), env=dict(self.config.env) or None)
                async with stdio_client(params) as (read, write):
                    await self._serve(read, write)
            else:
                headers = dict(self.config.headers)
                if self.oauth is not None:
                    try:
                        token = await self.oauth.access_token()
                    except NeedsAuthorization:
                        self.state = "auth_required"
                        self.last_error_code = "authorization_required"
                        self.error = (
                            f"{self.name}: this MCP server needs an OAuth link before it can connect. "
                            "Ask the owner to run McpOAuthBegin(server=...) to get the authorization URL, open it, "
                            "and paste the redirect URL back via McpOAuthFinish. Then enable this server again."
                        )
                        self._ready.set()
                        return
                    headers["Authorization"] = f"Bearer {token}"
                async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
                    async with streamable_http_client(self.config.url, http_client=client) as streams:
                        await self._serve(streams[0], streams[1])
        except BaseException as exc:  # noqa: BLE001 — anyio wraps transport failures in groups
            self.error = _describe(exc)
            self.last_error_code = "authorization_required" if _looks_like_auth_failure(exc) else "connection_failed"
            self.state = "auth_required" if self.last_error_code == "authorization_required" else "unavailable"
            logger.warning("mcp server %s failed: %s", self.name, self.error)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.session = None
            self._ready.set()

    async def _serve(self, read: Any, write: Any) -> None:
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            tools = [self._proxy(t) for t in listing.tools]
            digest = hashlib.sha256(json.dumps([tool.definition.model_dump(mode="json") for tool in tools], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if digest != self._catalog_digest:
                self.catalog_revision += 1
                self._catalog_digest = digest
            self.tools = tools
            if self._catalog_changed is not None:
                self._catalog_changed(tools)
            self.session = session
            self.state = "ready"
            self._ready.set()
            await self._closing.wait()

    def _proxy(self, remote: Any) -> McpToolProxy:
        schema = getattr(remote, "input_schema", None) or getattr(remote, "inputSchema", None) or {}
        definition = ToolDefinition(
            name=mcp_tool_name(self.name, remote.name),
            description=f"[MCP {self.name}] {remote.description or remote.name}",
            parameters=ToolParameterSchema(properties=loosen_for_refs(dict(schema.get("properties") or {})), required=list(schema.get("required") or [])),
        )
        return McpToolProxy(self, remote.name, definition)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        # An expired access token makes every call fail with a 401 that the remote may
        # report opaquely. Refresh and reconnect up front rather than after the fact.
        self.in_flight += 1
        try:
            refresh_generation = self._refresh_generation
            if self.oauth is not None and self.oauth.needs_refresh():
                await self._refresh_and_reconnect(refresh_generation)
            for attempt in (1, 2):
                if self.session is None:
                    if attempt == 2:
                        raise RuntimeError(f"MCP server {self.name} is not connected")
                    # The transport dropped before dispatch (server restart, idle reset, prior stop),
                    # so reconnecting cannot repeat an effect whose outcome is unknown.
                    try:
                        await asyncio.wait_for(self.start(), timeout=10)
                    except TimeoutError:
                        raise RuntimeError(f"MCP server {self.name}: reconnect timed out during connect") from None
                    if self.session is None:
                        raise RuntimeError(f"MCP server {self.name} is not connected: {self.error or 'connection failed'}")
                try:
                    return await asyncio.wait_for(self.session.call_tool(tool, arguments), timeout=self.config.timeout_seconds)
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    if attempt == 1 and self.oauth is not None and _looks_like_auth_failure(exc):
                        await self._refresh_and_reconnect(refresh_generation)
                        refresh_generation = self._refresh_generation
                        continue
                    raise
            raise RuntimeError(f"MCP server {self.name}: call did not complete")
        finally:
            self.in_flight -= 1

    async def _refresh_and_reconnect(self, observed_generation: int) -> None:
        """The access token expired mid-session: refresh it and restart the transport."""
        if self.oauth is None:
            return
        async with self._refresh_lock:
            if self._refresh_generation != observed_generation:
                return
            try:
                await self.oauth.refresh()
            except NeedsAuthorization:
                self.state = "auth_required"
                self.last_error_code = "authorization_required"
                raise
            self._refresh_generation += 1
            await self.stop()
            await self.start()

    async def stop(self) -> None:
        self.state = "draining"
        self._closing.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            except asyncio.CancelledError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                self.session = None
                self.state = "disabled"
                raise
        self.session = None
        self.state = "disabled"


def _config_digest(config: McpServerConfig) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


class McpManager:
    def __init__(self, servers: dict[str, McpServerConfig], registry: IToolRegistry, token_dir: Path | None = None) -> None:
        self._configs = servers
        self._registry = registry
        self._connections: dict[str, McpConnection] = {}
        self._token_dir = token_dir
        self._oauth_clients: dict[str, MCPOAuthClient] = {}
        self._ensure_locks: dict[str, asyncio.Lock] = {}
        self._registered_tools: dict[str, dict[str, McpToolProxy]] = {}
        self.catalog_listeners: list[Callable[[str], None]] = []
        """Called with the server's name, synchronously, whenever its proxies change in the registry.

        The sessions' policies refuse the proxies of servers they did not enable BY NAME, so a proxy
        that appears after a policy was computed is admitted by it. A server that re-lists its tools
        mid-session (``notifications/tools/list_changed``) registered new proxies that every other
        session's running engine then advertised and let ToolSearch load, until something unrelated
        refreshed the policies. Called before any await, so no request is built in between."""

    def reload(self, servers: dict[str, McpServerConfig]) -> None:
        previous = self._configs
        self._configs = servers
        for name, config in previous.items():
            replacement = servers.get(name)
            if replacement is None or _config_digest(replacement) != _config_digest(config):
                self._replace_catalog(name, ())

    def _replace_catalog(self, server: str, tools: Sequence[McpToolProxy]) -> None:
        """Publish one server's current catalogue without disturbing another server's bindings.

        The server's tools are declared a dynamic group by their name prefix, which is what lets the
        core hold them back behind ToolSearch and name them in one catalogue line instead of putting
        a server of three hundred tools on the surface. The declaration is made again with every
        catalogue, so an edited description reaches the next run, and withdrawn with the last tool: a
        declaration left behind by a removed or reconfigured server would hand its old description to
        the next server whose name sanitises to the same prefix.
        """
        previous = self._registered_tools.get(server, {})
        current = {tool.name: tool for tool in tools}
        if current:
            self._registry.declare_group(mcp_group_name(server), self.group_description(server), dynamic=True, prefix=mcp_tool_prefix(server))
            self._registered_tools[server] = current
        else:
            self._registered_tools.pop(server, None)
            self._registry.undeclare_group(mcp_group_name(server))
        for name in previous.keys() - current.keys():
            self._registry.unregister(name)
            # Sanitised server names can collide. Removing one owner must reveal the
            # other live binding rather than erase a tool that server still publishes.
            for other_server, catalog in self._registered_tools.items():
                if other_server != server and name in catalog:
                    self._registry.register(catalog[name])
                    break
        for tool in current.values():
            self._registry.register(tool)
        if previous.keys() != current.keys():
            for listener in tuple(self.catalog_listeners):
                try:
                    listener(server)
                except Exception:  # noqa: BLE001 — the catalogue is published either way; the next refresh retries
                    logger.exception("a listener of MCP server %s's catalogue failed", server)

    def available(self) -> list[str]:
        return sorted(self._configs)

    def describe(self, name: str) -> str:
        cfg = self._configs.get(name)
        return cfg.description if cfg else ""

    def group_description(self, name: str) -> str:
        """One line for the catalogue: the operator's description of the server, or what it is when there is none."""
        line = " ".join(self.describe(name).split())
        return line[:200] if line else f"Tools of the MCP server {name}"

    def oauth_client(self, name: str) -> MCPOAuthClient | None:
        """The lazily-created OAuth client for an HTTP server that declares an ``oauth`` block."""
        config = self._configs.get(name)
        if config is None or config.oauth is None or not config.url or self._token_dir is None:
            return None
        existing = self._oauth_clients.get(name)
        if existing is not None:
            return existing
        client = MCPOAuthClient(
            server=name,
            config=config.oauth,
            server_url=config.url,
            token_path=self._token_dir / f"{_safe_slug(name)}.json",
            http=httpx.AsyncClient(timeout=30.0),
        )
        self._oauth_clients[name] = client
        return client

    def oauth_status(self, name: str) -> dict[str, Any]:
        if name not in self._configs:
            raise KeyError(f"unknown MCP server {name!r}; configured: {self.available()}")
        oauth = self.oauth_client(name)
        if oauth is None:
            return {"configured": False, "note": "this server has no oauth block in its config"}
        return oauth.status()

    async def oauth_begin(self, name: str) -> str:
        oauth = self.oauth_client(name)
        if oauth is None:
            raise KeyError(f"MCP server {name!r} is not configured for OAuth")
        return await oauth.authorization_url()

    async def oauth_finish(self, name: str, redirect_url: str) -> dict[str, Any]:
        oauth = self.oauth_client(name)
        if oauth is None:
            raise KeyError(f"MCP server {name!r} is not configured for OAuth")
        result = await oauth.finish_authorization(redirect_url)
        connection = self._connections.get(name)
        if connection is not None:
            await connection.stop()
        return result

    async def oauth_disconnect(self, name: str) -> bool:
        oauth = self.oauth_client(name)
        if oauth is None:
            return False
        oauth.disconnect()
        connection = self._connections.get(name)
        if connection is not None:
            if connection.session is not None:
                await connection.stop()
            connection.state = "auth_required"
            connection.last_error_code = "authorization_required"
        return True

    def all_tool_names(self) -> set[str]:
        return {name for catalog in self._registered_tools.values() for name in catalog}

    def tool_names(self, server: str) -> set[str]:
        return set(self._registered_tools.get(server, {}))

    async def ensure(self, name: str) -> McpConnection:
        """Connect a configured server (idempotent) and register its tools."""
        if name not in self._configs:
            raise KeyError(f"unknown MCP server {name!r}; configured: {self.available()}")
        lock = self._ensure_locks.setdefault(name, asyncio.Lock())
        async with lock:
            expected_digest = _config_digest(self._configs[name])
            connection = self._connections.get(name)
            if connection is not None and connection.config_digest != expected_digest:
                self._replace_catalog(name, ())
                await connection.stop()
                self._connections.pop(name, None)
                oauth = self._oauth_clients.pop(name, None)
                if oauth is not None:
                    await oauth.http.aclose()
                connection = None
            if connection is None:
                connection = McpConnection(
                    name,
                    self._configs[name],
                    oauth=self.oauth_client(name),
                    catalog_changed=lambda tools, server=name: self._replace_catalog(server, tools),
                )
                self._connections[name] = connection
            if connection.error or (connection.session is None and connection._task is not None and not connection._task.done()):
                # A connection that failed or wedged is rebuilt, not reused: McpEnable is the agent's way to recover.
                await connection.stop()
            await connection.start()
            self._replace_catalog(name, connection.tools)
            return connection

    async def close(self) -> None:
        for connection in self._connections.values():
            await connection.stop()
        for name in tuple(self._registered_tools):
            self._replace_catalog(name, ())
        for client in self._oauth_clients.values():
            await client.http.aclose()
        self._oauth_clients.clear()
        self._ensure_locks.clear()

    def status(self) -> list[dict[str, Any]]:
        out = []
        for name in self.available():
            connection = self._connections.get(name)
            stale = bool(connection and connection.config_digest != _config_digest(self._configs[name]))
            out.append(
                {
                    "name": name,
                    "description": self.describe(name),
                    "connected": bool(connection and connection.session is not None and not stale),
                    "error": connection.error if connection else None,
                    "tools": sorted(t.name for t in connection.tools) if connection else [],
                    "state": "disabled" if stale or connection is None else connection.state,
                    "catalog_revision": connection.catalog_revision if connection and not stale else 0,
                    "last_error_code": connection.last_error_code if connection and not stale else None,
                    "in_flight": connection.in_flight if connection and not stale else 0,
                }
            )
        return out


def blocked_for(manager: McpManager, enabled: Sequence[str]) -> set[str]:
    """Tool names hidden from a run that has only ``enabled`` servers switched on."""
    enabled_set = set(enabled)
    return {name for name in manager.all_tool_names() if not any(name in manager.tool_names(s) for s in enabled_set)}


__all__ = ["McpConnection", "McpManager", "McpToolProxy", "blocked_for", "mcp_group_name", "mcp_tool_name", "mcp_tool_prefix"]
