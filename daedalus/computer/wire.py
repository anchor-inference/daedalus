"""The host's connection to the desktop daemon: the terminal daemon's framing, deskd's handshake and calls.

The socket, the token and the frames are ``ptyd``'s byte for byte, so the connection is the terminals'
client under the desktop's own names. What is the desktop's own is checked here: the greeting must say
protocol 1 and the role ``host`` (the run directory's token is the host's; a greeting that names the
operator means the files were mixed up, and this process must not act with the operator's power), and
every refusal arrives as :class:`DeskError` with the daemon's code and data untouched, for
:func:`daedalus.computer.refusals.explain` to put into words.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from daedalus.terminals import wire as terminal_wire
from daedalus.terminals.client import PtydClient, Unavailable
from daedalus.terminals.endpoint import EndpointMissing, read_endpoint

logger = logging.getLogger(__name__)

PROTOCOL = 1
LABEL = "desktop service"
LOCK_FILE = "deskd.lock"
OUTDATED = "update the Daedalus desktop application, which restarts the desktop service"
CALL_TIMEOUT = 10.0
ACT_SLACK = 10.0
"""What an action's timeout exceeds its own longest run by: a ``wait`` of ten seconds or a long drag
must be answered by the daemon, not cut off here while the daemon is still moving the mouse."""


class DeskError(Exception):
    """A refusal the daemon answered with: its JSON-RPC ``code``, ``message`` and ``data`` as sent."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class DeskUnavailable(Exception):
    """The daemon cannot be reached now; ``reason`` is a code (``not_installed``, ``not_running``,
    ``refused``, ``timeout`` …) and ``detail`` says it in words."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def endpoint_live(run_dir: Path | None) -> bool:
    """Whether the run directory names a daemon that accepts connections now.

    The daemon writes its endpoint only once it listens and removes it first thing when it stops, so
    the file's presence is the cheap answer; a daemon killed without its shutdown leaves the file and
    is found out by the first call."""
    if run_dir is None:
        return False
    try:
        read_endpoint(run_dir, label=LABEL, lock=LOCK_FILE)
    except EndpointMissing:
        return False
    return True


class DeskClient:
    """One connection to deskd with the host's token, opened on first use and again after a loss.

    Every session's tool calls share it: the daemon serialises input itself and answers concurrent
    calls, and one connection is what its audit expects of the host."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self._client: PtydClient | None = None
        self._lock = asyncio.Lock()
        self.hello: dict[str, Any] = {}
        self.info: dict[str, Any] = {}
        """The daemon's ``info`` as read when the connection opened: platform, displays, limits."""

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    async def _connected(self) -> PtydClient:
        async with self._lock:
            if self._client is not None and self._client.connected:
                return self._client
            if self._client is not None:
                await self._client.close()
                self._client = None
            client = PtydClient("host", self.run_dir, label=LABEL, outdated=OUTDATED, lock=LOCK_FILE)
            try:
                hello = await client.connect()
            except Unavailable as exc:
                raise DeskUnavailable(exc.reason, exc.detail) from None
            # The terminals' client already refuses a protocol other than its own, which is 1 as well;
            # the desktop's is checked by its own name so a later change to either is not silent.
            if hello.get("protocol") != PROTOCOL:
                await client.close()
                raise DeskUnavailable("protocol_mismatch", f"the {LABEL} speaks protocol {hello.get('protocol')} and this build speaks {PROTOCOL}: {OUTDATED}")
            if hello.get("role") != "host":
                await client.close()
                raise DeskUnavailable("refused", f"the {LABEL} greeted this process as {hello.get('role')!r}, not as the host; the run directory holds the wrong token")
            try:
                info = await client.call("info", {}, timeout=CALL_TIMEOUT)
            except terminal_wire.RpcError as exc:
                await client.close()
                raise DeskError(exc.code, exc.message, exc.data) from None
            except Unavailable as exc:
                await client.close()
                raise DeskUnavailable(exc.reason, exc.detail) from None
            self.hello = hello
            self.info = info if isinstance(info, dict) else {}
            self._client = client
            logger.info("desktop service connected: %s on %s", hello.get("version"), (hello.get("platform") or {}).get("os"))
            return client

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = CALL_TIMEOUT) -> Any:
        """One call; raises :class:`DeskError` for a refusal and :class:`DeskUnavailable` when gone."""
        client = await self._connected()
        try:
            return await client.call(method, params or {}, timeout=timeout)
        except terminal_wire.RpcError as exc:
            raise DeskError(exc.code, exc.message, exc.data) from None
        except Unavailable as exc:
            raise DeskUnavailable(exc.reason, exc.detail) from None

    def limits(self) -> dict[str, Any]:
        limits = self.info.get("limits")
        return limits if isinstance(limits, dict) else {}

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.close()
                self._client = None


__all__ = ["ACT_SLACK", "CALL_TIMEOUT", "LABEL", "LOCK_FILE", "PROTOCOL", "DeskClient", "DeskError", "DeskUnavailable", "endpoint_live"]
