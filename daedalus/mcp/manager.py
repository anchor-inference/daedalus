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
import os
import re
import secrets
import shutil
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from protocore.contracts.tool_registry import IToolRegistry
from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult

from daedalus.config import McpServerConfig, inherited_environment
from daedalus.mcp.content import McpAnswer, read_content
from daedalus.mcp.oauth import MCPOAuthClient, NeedsAuthorization
from daedalus.security import operator_secrets, redact
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


def annotation_approval(annotations: Any, *, server: str = "auto", tool: str = "auto") -> str:
    """``allow``, ``ask`` or ``deny`` for one MCP tool: the operator's rule for the tool, else for its
    server, else what the tool's annotations say (which never deny).

    The annotations are read with the protocol's own defaults, and those are the cautious ones: a
    hint left out means the tool *may* destroy and *may* reach the world, so a server that annotates
    nothing is asked about tool by tool until the operator says otherwise. Reading a missing hint as
    harmless would let a new server act on the first call without anyone having looked at it.
    ``read_only_hint`` settles it the other way, because a destructive hint means nothing for a tool
    that only reads."""
    for rule in (tool, server):
        if rule in ("allow", "ask", "deny"):
            return rule
    def hint(name: str) -> bool | None:
        value = getattr(annotations, name, None) if annotations is not None else None
        return value if isinstance(value, bool) else None
    if hint("read_only_hint") is True:
        return "allow"
    destructive = hint("destructive_hint") is not False
    open_world = hint("open_world_hint") is not False
    return "ask" if destructive or open_world else "allow"


class McpToolProxy(Tool):
    """A core ``Tool`` that forwards to a remote MCP tool."""

    def __init__(self, connection: McpConnection, remote_name: str, definition: ToolDefinition, *, approval: str = "ask") -> None:
        self._connection = connection
        self._remote = remote_name
        self._definition = definition
        self.server = connection.name
        self.approval = approval
        """``allow``, ``ask`` or ``deny``: what the policy answers for a call no other rule decides (:func:`annotation_approval`)."""

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def remote_name(self) -> str:
        """The tool's own name on its server, which the operator's per-tool rule is keyed by."""
        return self._remote

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        call_id = str(context.metadata.get("tool_call_id") or "")
        if (store := operator_secrets.shared()) is not None:
            # The operator's secrets first, then the server's own references: a «secret:…» is the operator's
            # value for whatever this session calls, a «ref:…» only ever goes back to the server that gave it.
            arguments, _used = store.substitute(arguments, context.session_id, used_by=f"MCP {self.server} ({self.name}) in {context.session_id}")
        answer = await self.run(arguments, limit=_output_limit(context.session_id))
        return ToolResult(tool_call_id=call_id, content=answer.text, is_error=answer.failed, images=answer.images)

    async def run(self, arguments: dict[str, Any], *, limit: int) -> McpAnswer:
        """Call the remote tool and render its answer: the text fenced as outside content, the pictures
        apart (:mod:`daedalus.mcp.content`). What a session's call and a command-line member's call share;
        a member is handed the text alone, which still names every picture."""
        try:
            result = await self._connection.call(self._remote, self._connection.vault.resolve(arguments))
        except Exception as exc:  # noqa: BLE001
            return McpAnswer(f"MCP tool failed: {self._connection.describe(exc)}", True)
        content, images = read_content(getattr(result, "content", []))
        content = content or "(empty result)"
        content, concealed = self._connection.vault.conceal(content)
        content = clip(content, limit, note="the MCP tool returned more")
        content = fenced(content, kind="tool output", origin=f"MCP server {self.server}", source="an outside server",
                         quoted_by="the tool")
        if concealed:
            content += "\n" + VAULT_NOTE
        return McpAnswer(content, bool(getattr(result, "is_error", None) or getattr(result, "isError", False)), images)


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


_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&][^=&\s#]*(?:key|token|secret|auth|password|passwd|signature|sig|credential|session)[^=&\s#]*=)([^&\s#\"'<>]+)"
)
"""A query parameter named like a credential, in any URL an error quotes: many hosted servers take
their key as ``?api_key=…``, and an HTTP library's error repeats the URL it failed on."""

MIN_CONFIGURED_SECRET = 4
"""Configured values shorter than this are left in place: they would match inside ordinary words."""


def mask_server_secrets(config: McpServerConfig, text: str) -> str:
    """``text`` with what this server was configured with masked: its header and environment values,
    every query value of its URL, credential-named query values of any URL, then the shared shapes.

    The process-wide redactor knows the servers configured at start, and only values of eight
    characters or more; a server added from the app since, or a short key, reached the app's status
    row and the model as written.
    """
    if not text:
        return text
    values = [*config.headers.values(), *config.env.values()]
    for value in config.headers.values():
        # ``Bearer abc``: the credential is the last word, and an error may quote it without the scheme.
        values.extend(value.split()[1:])
    if config.url and "?" in config.url:
        values.extend(value for _, value in parse_qsl(urlsplit(config.url).query))
    for value in sorted({v for v in values if len(v) >= MIN_CONFIGURED_SECRET}, key=len, reverse=True):
        text = text.replace(value, redact.MASK)
    text = _QUERY_SECRET_RE.sub(lambda match: match.group(1) + redact.MASK, text)
    return redact.redact(text)


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


STDERR_LINES = 40
"""How many of a stdio server's last standard-error lines are kept to show with its state."""

PASSED_ENVIRONMENT = ("UV_CACHE_DIR", "UV_PYTHON", "UV_PYTHON_DOWNLOADS", "UV_TOOL_DIR", "npm_config_cache", "NPM_CONFIG_CACHE",
                      "PLAYWRIGHT_BROWSERS_PATH", "ANDROID_HOME", "ANDROID_SDK_ROOT")
"""What a stdio server inherits beyond the MCP library's short list (home, path, shell, user). The
library passes nothing else, so a ``uv`` started for a server did not see where the installation keeps
its interpreter and cache and fetched a second CPython on the first start, and a server that finds its
tools through one of these looked in the wrong place. A value in the server's own ``env`` wins."""


class StderrTail:
    """The last lines a stdio server wrote to its standard error, read off a pipe by a small thread.

    The library sends a server's standard error to the host's own, which in Docker is the container
    log: a server that failed to start said why there and nowhere the operator or the agent looked,
    and the only error they saw was the protocol's "Connection closed". The lines are kept here to be
    shown with the server's state, and still logged."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: deque[str] = deque(maxlen=STDERR_LINES)
        read_fd, write_fd = os.pipe()
        self.sink = os.fdopen(write_fd, "w")
        self._source = os.fdopen(read_fd, "r", errors="replace")
        self._thread = threading.Thread(target=self._read, name=f"mcp-stderr:{name}", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        with self._source:
            for line in self._source:
                line = line.rstrip()
                if line:
                    self.lines.append(line[:500])
                    logger.info("mcp server %s stderr: %s", self.name, redact.redact(line[:500]))

    def close(self, *, settle: float = 0.5) -> None:
        """Let go of the host's end of the pipe and give the reader a moment to take the last lines:
        a server that exited has closed its end, so the reader reaches the end at once."""
        try:
            self.sink.close()
        except OSError:
            pass
        self._thread.join(settle)

    def text(self, last: int = STDERR_LINES) -> str:
        return "\n".join(list(self.lines)[-last:])


def resolve_command(command: str, args: Sequence[str], env: dict[str, str]) -> tuple[str, list[str]]:
    """The program a stdio server is started with, as it can be started where the host runs.

    ``uvx`` is ``uv tool run`` under another name, and the container image keeps ``uv`` but not the
    alias, so a server pasted from a README as ``uvx some-server`` failed with "No such file or
    directory". A command that is not there at all fails here, with words that say where it was looked
    for, instead of as an exception about a file."""
    path = env.get("PATH") or inherited_environment(("PATH",)).get("PATH", "")
    if shutil.which(command, path=path) is not None:
        return command, list(args)
    if command == "uvx" and shutil.which("uv", path=path) is not None:
        return "uv", ["tool", "run", *args]
    raise FileNotFoundError(f"the command {command!r} is not installed where MCP servers start ({runs_in()})")


WHERE = ["machine"]
"""Where a stdio server's process starts: ``container`` in Docker — the host's own container, which
is not the machine a session's host-folder Exec runs on — or ``machine``. Set once at start; the app
says it in its own words from this, the model reads :func:`runs_in`."""


def runs_in() -> str:
    return "the Daedalus container, not the host machine" if WHERE[0] == "container" else "this machine"


CONNECT_DEADLINE_SECONDS = 30.0
"""How long a caller waits for a server to connect before it is told the server is still starting."""
RECONNECT_DEADLINE_SECONDS = 10.0
"""How long a tool call waits for a dropped transport to come back before it fails."""
WEDGED_AFTER_SECONDS = 120.0
"""How long a connection may stay connecting before it is stopped and started again. A server that
installs its packages on first start (``npx -y``, ``uvx``) takes tens of seconds; the warm-up used to
stop anything not ready after five, so such a server was killed on every run and never came up."""
WARM_UP_SECONDS = 8.0
"""The whole warm-up of a run's servers, together: a dead server must never hold the run longer."""


class McpStillStarting(RuntimeError):
    """The server did not connect within the caller's deadline and is still starting."""


class McpAuthorizationRequired(RuntimeError):
    """A server refused a call with 401; the message carries its challenge and what was done about it."""


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
        self._token = ""
        """The access token the transport presents, so a 401 renews that one and not a newer one."""
        self._refusals = 0
        self._challenge = ""
        """How many 401s the server answered on this connection, and the last one's ``WWW-Authenticate``."""
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
        self._started_at = 0.0
        self.stderr: StderrTail | None = None
        """The running (or last) stdio process's standard error; ``None`` for an HTTP server."""

    def describe(self, exc: BaseException) -> str:
        """The failure as the app and the model may see it, with this server's secrets masked."""
        return mask_server_secrets(self.config, _describe(exc))

    def stderr_text(self, last: int = STDERR_LINES) -> str:
        """What the stdio process last wrote to its standard error, with this server's secrets masked."""
        return mask_server_secrets(self.config, self.stderr.text(last)) if self.stderr is not None else ""

    @property
    def starting(self) -> bool:
        """Connecting now, and not for so long that the attempt should be called wedged."""
        return (self.session is None and self._task is not None and not self._task.done()
                and asyncio.get_running_loop().time() - self._started_at < WEDGED_AFTER_SECONDS)

    async def start(self, *, deadline: float | None = CONNECT_DEADLINE_SECONDS) -> None:
        """Connect, or join the connection already being made, waiting at most ``deadline`` seconds.

        A server that has not answered by then raises :class:`McpStillStarting` and goes on starting:
        only the wait ends. The Telegram button and the app's switch waited with no bound at all,
        and a caller that bounded the wait by cancelling it gave the next caller a half-started
        connection that looked wedged and was killed.
        """
        async with self._start_lock:
            if self._task is None or self._task.done():
                self._ready.clear()
                self._closing.clear()
                self.error = None
                self.last_error_code = None
                self.state = "connecting"
                self._started_at = asyncio.get_running_loop().time()
                self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=deadline)
        except TimeoutError:
            raise McpStillStarting(f"MCP server {self.name} is still starting after {deadline:.0f}s; it goes on starting, so try again shortly") from None
        if self.error:
            raise RuntimeError(self.error)

    async def _run(self) -> None:
        try:
            if self.config.transport == "stdio":
                self.stderr = None
                env = inherited_environment(PASSED_ENVIRONMENT) | dict(self.config.env)
                command, args = resolve_command(self.config.command, self.config.args, env)
                params = StdioServerParameters(command=command, args=args, env=env or None)
                self.stderr = StderrTail(self.name)
                try:
                    async with stdio_client(params, errlog=self.stderr.sink) as (read, write):
                        await self._serve(read, write)
                finally:
                    self.stderr.close()
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
                            "The owner can sign in from Settings → MCP servers, or McpOAuthBegin(server=...) gives the "
                            "authorization URL to hand over. Then enable this server again."
                        )
                        self._ready.set()
                        return
                    headers["Authorization"] = f"Bearer {token}"
                    self._token = token
                # The SDK turns a 401 into a status-less "Server returned an error response", so the
                # status and the server's challenge are read here, off every response it receives.
                async with httpx.AsyncClient(headers=headers, timeout=60.0, event_hooks={"response": [self._watch_response]}) as client:
                    async with streamable_http_client(self.config.url, http_client=client) as streams:
                        await self._serve(streams[0], streams[1])
        except BaseException as exc:  # noqa: BLE001 — anyio wraps transport failures in groups
            self.error = self.describe(exc)
            said = self.stderr_text(last=6) if self.stderr is not None else ""
            if said and not isinstance(exc, asyncio.CancelledError):
                # "Connection closed" says only that the process went away; its last words say why.
                self.error += f" · the server wrote: {said}"
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
        approval = annotation_approval(getattr(remote, "annotations", None), server=self.config.approval,
                                       tool=self.config.tool_approval.get(remote.name, "auto"))
        return McpToolProxy(self, remote.name, definition, approval=approval)

    async def _watch_response(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            self._refusals += 1
            self._challenge = ", ".join(response.headers.get_list("www-authenticate"))

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        # An expired access token makes every call fail with a 401 that the remote may
        # report opaquely. Refresh and reconnect up front rather than after the fact.
        self.in_flight += 1
        try:
            refresh_generation = self._refresh_generation
            if self.oauth is not None and self.oauth.needs_refresh():
                await self._refresh_and_reconnect(refresh_generation)
                refresh_generation = self._refresh_generation
            if self.session is None:
                # The transport dropped before dispatch (server restart, idle reset, prior stop),
                # so reconnecting cannot repeat an effect whose outcome is unknown.
                try:
                    await self.start(deadline=RECONNECT_DEADLINE_SECONDS)
                except McpStillStarting:
                    raise RuntimeError(f"MCP server {self.name}: reconnect timed out during connect") from None
                if self.session is None:
                    raise RuntimeError(f"MCP server {self.name} is not connected: {self.error or 'connection failed'}")
            refusals = self._refusals
            try:
                return await asyncio.wait_for(self.session.call_tool(tool, arguments), timeout=self.config.timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                raise
            except Exception as exc:
                if self.oauth is None or (self._refusals == refusals and not _looks_like_auth_failure(exc)):
                    raise
                # Refused mid-call. The call used to be refreshed and sent again, but the host cannot
                # tell what part of a call a server acted on before it refused it, and a tool with an
                # effect sent twice is the worse failure. The token is renewed for the next call, and
                # the answer carries the server's challenge, so the caller decides whether to call again.
                challenge = self._challenge if self._refusals != refusals else ""
                renewed = "the access token was renewed"
                try:
                    await self._refresh_and_reconnect(refresh_generation, rejected=self._token)
                except NeedsAuthorization:
                    renewed = "the link has to be made again (McpOAuthBegin)"
                except Exception as renewal:  # noqa: BLE001 — the refusal is the answer; the renewal's failure is its detail
                    renewed = f"renewing the token failed: {renewal}"
                said = f" ({challenge})" if challenge else ""
                raise McpAuthorizationRequired(
                    f"MCP server {self.name}: authorization required{said}; {renewed}. The call was not repeated: call it again if it should run."
                ) from exc
        finally:
            self.in_flight -= 1

    async def _refresh_and_reconnect(self, observed_generation: int, *, rejected: str = "") -> None:
        """The access token expired, or the server refused it: renew it and restart the transport."""
        if self.oauth is None:
            return
        async with self._refresh_lock:
            if self._refresh_generation != observed_generation:
                return
            try:
                await self.oauth.refresh(rejected=rejected)
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
    def __init__(self, servers: dict[str, McpServerConfig], registry: IToolRegistry, token_dir: Path | None = None,
                 redirect_default: Callable[[], str] | None = None) -> None:
        self._configs = servers
        self._redirect_default = redirect_default
        """The OAuth redirect a server without its own ``redirect_uri`` registers: the app's callback."""
        self._retiring: set[asyncio.Task[None]] = set()
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
                # A removed server only lost its tools here, and its connection — a stdio server's
                # whole process — ran on until the host stopped. A changed one keeps no stale OAuth
                # client either: the next ensure() builds both from the new config.
                try:
                    task = asyncio.get_running_loop().create_task(self._retire(name, forget=replacement is None))
                except RuntimeError:
                    continue
                self._retiring.add(task)
                task.add_done_callback(self._retiring.discard)

    async def _retire(self, name: str, *, forget: bool) -> None:
        """Stop a server's connection and drop its OAuth client; ``forget`` also deletes its tokens."""
        async with self._ensure_locks.setdefault(name, asyncio.Lock()):
            config = self._configs.get(name)
            if config is not None and forget:
                return  # added back before this ran: it is someone else's now
            # Only what the current config no longer describes: an ensure() that took the lock first
            # (a check right after the save) has already built the replacement, and stopping it here
            # left the server just added looking dead.
            connection = self._connections.get(name)
            if connection is not None and (config is None or connection.config_digest != _config_digest(config)):
                self._connections.pop(name, None)
                try:
                    await connection.stop()
                except Exception:  # noqa: BLE001 — a connection that fails to stop is still forgotten
                    logger.exception("MCP server %s did not stop cleanly", name)
            oauth = self._oauth_clients.get(name)
            if oauth is not None and (config is None or oauth.config != config.oauth or oauth.server_url != config.url):
                self._oauth_clients.pop(name, None)
                if forget:
                    oauth.disconnect()
                await oauth.http.aclose()
            elif oauth is None and forget and self._token_dir is not None:
                (self._token_dir / f"{_safe_slug(name)}.json").unlink(missing_ok=True)

    def find_pending(self, state: str) -> str | None:
        """The server whose authorization is waiting for the callback that carries ``state``."""
        if not state:
            return None
        for name in self.available():
            oauth = self.oauth_client(name)
            if oauth is not None and secrets.compare_digest(oauth.pending_state(), state):
                return name
        return None

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
            redirect_default=self._redirect_default() if self._redirect_default is not None else "",
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

    def approvals(self) -> dict[str, str]:
        """Every live MCP tool's default answer, by the name a session calls it, for the policy."""
        return {name: tool.approval for catalog in self._registered_tools.values() for name, tool in catalog.items()}

    def servers_by_tool(self) -> dict[str, str]:
        """Which server each live MCP tool belongs to, by the name a session calls it, so that a grant
        for "every tool of this server" is matched by the server and not by a sanitised name prefix
        two servers can share."""
        return {name: server for server, catalog in self._registered_tools.items() for name in catalog}

    async def trial(self, name: str, config: McpServerConfig, *, deadline: float) -> dict[str, Any]:
        """Start a server that is not saved, list its tools, say what its process wrote, and stop it.

        What the app shows before the operator adds a pasted command: nothing is registered and
        nothing is kept, so a command that fails leaves no server behind to be removed."""
        connection = McpConnection(name, config)
        try:
            await connection.start(deadline=deadline)
            state, error = "ready", None
        except McpStillStarting as exc:
            state, error = "connecting", str(exc)
        except Exception as exc:  # noqa: BLE001 — the failure is the answer
            state, error = connection.state if connection.state != "connecting" else "unavailable", connection.error or connection.describe(exc)
        tools = [{"name": tool.remote_name, "description": (tool.definition.description or "").removeprefix(f"[MCP {name}] ")[:300],
                  "approval": tool.approval} for tool in connection.tools]
        stderr = connection.stderr_text()
        await connection.stop()
        return {"name": name, "state": state, "error": error, "tools": tools, "stderr": stderr, "runs_in": WHERE[0]}

    def all_tool_names(self) -> set[str]:
        return {name for catalog in self._registered_tools.values() for name in catalog}

    def tool_names(self, server: str) -> set[str]:
        return set(self._registered_tools.get(server, {}))

    def proxies(self, server: str) -> list[McpToolProxy]:
        """The server's tools as last listed, by name; empty until it has been connected."""
        return [tool for _, tool in sorted(self._registered_tools.get(server, {}).items())]

    async def ensure(self, name: str, *, deadline: float | None = CONNECT_DEADLINE_SECONDS) -> McpConnection:
        """Connect a configured server (idempotent) and register its tools.

        :class:`McpStillStarting` when it is not connected within ``deadline``; it goes on starting."""
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
            connecting = connection.session is None and connection._task is not None and not connection._task.done()
            if connection.error or (connecting and not connection.starting):
                # A connection that failed or wedged is rebuilt, not reused: McpEnable is the agent's way to recover.
                # One that is merely slow is joined: stopping it is what kept a slow server from ever starting.
                await connection.stop()
            await connection.start(deadline=deadline)
            self._replace_catalog(name, connection.tools)
            return connection

    async def warm(self, names: Sequence[str], *, budget: float = WARM_UP_SECONDS) -> dict[str, str]:
        """Connect ``names`` together within ``budget`` seconds; each server's outcome by name.

        ``ready``, ``starting`` (still connecting; left to finish on its own), or ``failed``. One
        that fails while time is left is tried once more, since a server's first start after the host
        restarts is the one most likely to lose a race. The warm-up went one server at a time with
        five seconds each, so three slow servers cost a run fifteen seconds and none of them came up.
        """
        loop = asyncio.get_running_loop()
        until = loop.time() + budget

        async def one(name: str) -> str:
            if name not in self._configs:
                return "failed"  # enabled once, removed from the config since
            for attempt in (1, 2):
                remaining = until - loop.time()
                if remaining <= 0:
                    return "starting" if self._connections.get(name) is not None and self._connections[name].starting else "failed"
                try:
                    await self.ensure(name, deadline=remaining)
                    return "ready"
                except McpStillStarting:
                    logger.info("MCP server %s is still starting; it goes on in the background", name)
                    return "starting"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — a server that fails must not fail the run
                    connection = self._connections.get(name)
                    detail = connection.describe(exc) if connection is not None else type(exc).__name__
                    logger.warning("MCP warm-up for %s failed (attempt %d): %s", name, attempt, detail)
            return "failed"

        results = await asyncio.gather(*(one(name) for name in names))
        return dict(zip(names, results, strict=True))

    async def close(self) -> None:
        if self._retiring:
            await asyncio.gather(*tuple(self._retiring), return_exceptions=True)
        # A copy: a retirement still running pops from the map while a stop awaits.
        for connection in tuple(self._connections.values()):
            await connection.stop()
        for name in tuple(self._registered_tools):
            self._replace_catalog(name, ())
        for client in tuple(self._oauth_clients.values()):
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
                    "stderr": connection.stderr_text(last=12) if connection and not stale else "",
                }
            )
        return out


def blocked_for(manager: McpManager, enabled: Sequence[str]) -> set[str]:
    """Tool names hidden from a run that has only ``enabled`` servers switched on."""
    enabled_set = set(enabled)
    return {name for name in manager.all_tool_names() if not any(name in manager.tool_names(s) for s in enabled_set)}


__all__ = ["WHERE", "runs_in", "McpConnection", "McpManager", "McpStillStarting", "McpToolProxy", "mask_server_secrets", "blocked_for", "mcp_group_name", "mcp_tool_name", "mcp_tool_prefix"]
