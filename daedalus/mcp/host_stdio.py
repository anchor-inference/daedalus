"""The stdio transport of an MCP server that runs on the operator's machine, not where this process does.

In Docker this process is a container, and a server started here sees the container: not the
operator's ``adb`` and the phone paired with it, not their CLIs, not their files. A server the
operator set to run on the host is started there by the host terminal daemon (``exec.stream``), and
its stdin and stdout come back over the daemon's socket. Nothing enters the container from outside
and nothing here enters the host by any other door: no ``nsenter``, no ``docker exec``, only the
daemon the operator installed, with the program list it enforces.

The process lives as long as the transport: leaving the context closes the stream, and the daemon
then closes the server's stdin, hangs up its process group after a grace and kills it after another.
The daemon also ends it when this process's connection to it ends, so a restart leaves none behind.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, Any

import anyio
from mcp import types
from mcp.shared.message import SessionMessage

from daedalus.terminals.model import TerminalError

if TYPE_CHECKING:
    from daedalus.terminals.bridge import HostBridge
    from daedalus.terminals.sidechannels import ProcessStream

logger = logging.getLogger(__name__)

LAUNCH = (
    'if ! command -v -- "$1" >/dev/null 2>&1; then '
    'if [ "$1" = uvx ] && command -v uv >/dev/null 2>&1; then shift; set -- uv tool run "$@"; '
    "else echo \"the command '$1' is not installed on the host machine\" >&2; exit 127; fi; "
    'fi; exec "$@"'
)
"""What the daemon runs a host server through. ``bash`` is on the daemon's program list (a host-folder
session's Exec runs through it), so a server's own launcher need not be, and ``exec`` leaves the server
as the leader of the group the daemon ends. A command that is not installed says so on stderr, which
the server's card shows, and ``uvx`` falls back to ``uv tool run`` as it does in the container. Not a
login shell: a profile that prints anything would put it on the server's stdout, which is the protocol."""


def host_argv(command: str, args: Sequence[str]) -> list[str]:
    """The argv the daemon is given for a server: the launch line, then the server's own command."""
    return ["bash", "-c", LAUNCH, "mcp-server", command, *args]


@contextlib.asynccontextmanager
async def host_stdio_client(bridge: HostBridge, command: str, args: Sequence[str], env: dict[str, str], *,
                            on_stderr: Callable[[str], None], on_end: Callable[[str], None] | None = None,
                            actor: str = "system") -> AsyncIterator[tuple[Any, Any]]:
    """``(read_stream, write_stream)`` of a server started on the host, as ``mcp.client.stdio.stdio_client``
    yields them for one started here, so ``ClientSession`` takes either. A refusal to start (the bridge
    down, an old daemon, a program the daemon will not run) is an ``OSError`` before anything is yielded.

    ``on_end`` hears why the stream ended when it ends by itself — the server exited, the daemon or the
    connection to it went away — so the connection can say so instead of staying "ready" on a stream
    nothing answers any more."""
    stream = await bridge.exec_stream(host_argv(command, args), env_vars=dict(env), actor=actor)
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async def reader() -> None:
        buffer = b""
        try:
            async with read_writer:
                while (piece := await stream.read()) is not None:
                    kind, data = piece
                    if kind == "err":
                        on_stderr(data.decode("utf-8", "replace"))
                        continue
                    lines = (buffer + data).split(b"\n")
                    buffer = lines.pop()
                    for line in lines:
                        if line.strip():
                            await read_writer.send(_parse(line))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return  # the session is gone; the stream is closed below
        if stream.exit is not None:
            logger.info("mcp host server %s ended: %s", command, stream.exit)
        if on_end is not None:
            on_end(_why(stream))

    async def writer() -> None:
        try:
            async with write_reader:
                async for message in write_reader:
                    body = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    await stream.write((body + "\n").encode())
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError, TerminalError):
            # The server went away under a write: the session must see the end, not wait on a reply.
            await read_writer.aclose()

    async with anyio.create_task_group() as group:
        group.start_soon(reader)
        group.start_soon(writer)
        try:
            yield read_stream, write_stream
        finally:
            with anyio.CancelScope(shield=True):
                await _close(stream)
            group.cancel_scope.cancel()


def _why(stream: ProcessStream) -> str:
    if stream.exit is None:
        return f"the stream from the host machine closed ({stream.reason or 'no reason given'})"
    code, signal = stream.exit.get("exit_code"), stream.exit.get("signal")
    return f"the server on the host machine exited ({f'signal {signal}' if signal else f'code {code}'})"


def _parse(line: bytes) -> SessionMessage | Exception:
    try:
        return SessionMessage(types.jsonrpc_message_adapter.validate_json(line, by_name=False))
    except ValueError as exc:
        logger.warning("an MCP host server wrote a line that is not JSON-RPC: %s", line[:200])
        return exc


async def _close(stream: ProcessStream) -> None:
    """End the server: closing the channel is the daemon's signal to end its whole group."""
    with contextlib.suppress(Exception):
        await stream.close()


__all__ = ["LAUNCH", "host_argv", "host_stdio_client"]
