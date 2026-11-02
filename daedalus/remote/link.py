"""One connected terminal client: the socket, the requests in flight on it, and what the client said it is.

The contract is ``docs/architecture/cli.md`` ("The device connection"). Text frames carry JSON-RPC
2.0, exactly ptyd's control channel without its length prefix, so the client's executor is ptyd's
side-channel handlers lifted into the client. The server sends requests (``exec.run``, ``fs.*``,
``device.result``); the client answers them and sends notifications (``device.guard``,
``device.sessions``, ``exec.output``).

What a request's fate was matters more here than in ptyd, whose socket never drops while the host is
up: this one crosses the operator's network. :meth:`DeviceLink.request` therefore says, when the link
goes, whether the request left this process (:class:`LinkLost` ``delivered``) so the caller can tell
"never sent, send it again" from "sent, ask the client what became of it".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

PROTOCOL = 1
"""The relay protocol this build speaks; ``device.hello`` names the client's."""
CONTRACT = 1
"""The contract version of ``docs/architecture/cli.md``, answered in ``device.welcome``."""

MAX_MESSAGE = 1 << 20
"""The largest text frame either side sends. A longer one is a broken or hostile peer."""
HELLO_SECONDS = 15.0
"""How long a client that connected has to say ``device.hello``."""

CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_REPLACED = 4409
CLOSE_TOO_MANY = 4429
CLOSE_GOING_AWAY = 1001
CLOSE_UNRESPONSIVE = 1011

# ptyd's error codes, which the client's executor answers with (``daedalus.terminals.wire``).
NOT_FOUND = 1001
LIMIT = 1003
FORBIDDEN = 1004
TIMEOUT = 1005
UNSUPPORTED = 1007
METHOD_NOT_FOUND = -32601

GUARDS = ("ask", "auto", "plan")
"""The client's local guard levels; ``auto`` until a client says otherwise."""

REFUSAL_REASONS = ("refused_by_operator", "plan_guard", "outside_root", "denied_path")
"""What a ``1004`` from the client's guard or jail carries in ``data.reason``."""


class RelaySocket(Protocol):
    """The transport under a link: text frames both ways. The framework's WebSocket in production, a
    real socket pair in the tests."""

    async def receive(self) -> str | bytes | None: ...

    async def send_text(self, text: str) -> None: ...

    async def close(self, code: int, reason: str = "") -> None: ...


class RemoteError(Exception):
    """An error object the client answered a request with."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data

    @property
    def reason(self) -> str:
        """The guard's or the jail's reason for a ``1004``, or ``""``."""
        return str(self.data.get("reason") or "") if isinstance(self.data, dict) else ""


class LinkLost(Exception):
    """The link ended before the request was answered.

    ``delivered`` is whether the request was handed to the socket: ``False`` means the client never
    saw it, ``True`` that it may have (the frame may have been lost with the connection, or the
    client may be running it now)."""

    def __init__(self, delivered: bool, detail: str = "the terminal disconnected") -> None:
        super().__init__(detail)
        self.delivered = delivered


@dataclass(slots=True)
class Hello:
    """What the client said about itself in ``device.hello``."""

    protocol: int = PROTOCOL
    client: str = ""
    """The process's own id (the one its presence reports carry): a device holds one link per process."""
    client_version: str = ""
    os: str = ""
    arch: str = ""
    shell: str = ""
    home: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    sessions: dict[str, str] = field(default_factory=dict)
    """The sessions this client process shows, with the guard of each."""
    known: dict[str, str] = field(default_factory=dict)
    """The requests the client still holds from before a drop, by id: ``running`` or ``done``."""

    @classmethod
    def parse(cls, params: Any) -> Hello:
        """``device.hello``'s params, tolerant of fields a newer client adds and strict on the shapes it uses."""
        if not isinstance(params, dict):
            raise ValueError("device.hello carries an object")
        sessions: dict[str, str] = {}
        for item in params.get("sessions") or []:
            if isinstance(item, dict) and isinstance(item.get("session_id"), str):
                guard = str(item.get("guard") or "auto")
                sessions[item["session_id"]] = guard if guard in GUARDS else "auto"
        known: dict[str, str] = {}
        for item in params.get("known") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("state") in ("running", "done"):
                known[item["id"]] = str(item["state"])
        capabilities = params.get("capabilities")
        return cls(
            protocol=int(params.get("protocol") or PROTOCOL),
            client=str(params.get("client") or "")[:128],
            client_version=str(params.get("client_version") or "")[:64],
            os=str(params.get("os") or "")[:32],
            arch=str(params.get("arch") or "")[:32],
            shell=str(params.get("shell") or "")[:256],
            home=str(params.get("home") or "")[:4096],
            capabilities=capabilities if isinstance(capabilities, dict) else {},
            sessions=sessions,
            known=known,
        )

    def fs_methods(self) -> frozenset[str]:
        """The native file calls the client serves, by their names without ``fs.``."""
        raw = self.capabilities.get("fs")
        return frozenset(str(name) for name in raw) if isinstance(raw, list) else frozenset()


Notify = Callable[[str, dict[str, Any]], Awaitable[None]]


class DeviceLink:
    """One live relay of one device. Made by the hub once ``device.hello`` has been read."""

    def __init__(self, device_id: str, name: str, socket: RelaySocket, hello: Hello, *, on_notify: Notify | None = None) -> None:
        self.device_id = device_id
        self.name = name
        self.socket = socket
        self.hello = hello
        self.on_notify = on_notify
        self.ended = asyncio.Event()
        self.reason = ""
        self._waiting: dict[str, asyncio.Future[Any]] = {}
        self._output: dict[str, Callable[[str], None]] = {}
        self._early: dict[str, dict[str, Any]] = {}
        """Answers to requests an earlier link sent that arrived before anyone adopted them: a command the
        client reported running may finish in the instant between its hello and the turn resuming."""
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return not self.ended.is_set()

    async def _send(self, message: dict[str, Any]) -> None:
        text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(text.encode()) > MAX_MESSAGE:
            raise ValueError(f"a message to the terminal carries at most {MAX_MESSAGE} bytes")
        async with self._send_lock:
            await self.socket.send_text(text)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """A notification to the client; a link that is gone drops it."""
        if self.ended.is_set():
            return
        with contextlib.suppress(Exception):
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, request_id: str, method: str, params: dict[str, Any], *, timeout: float, on_output: Callable[[str], None] | None = None) -> Any:
        """Send one request and wait for its answer.

        ``request_id`` is the caller's (``<session>:<call>:<n>``), stable across a reconnect, so the
        answer to a request the client finishes after a new link came up still finds its caller (see
        :meth:`adopt`). A :class:`RemoteError` for an error answer, :class:`LinkLost` when the link
        ends first, :class:`TimeoutError` when the client says nothing for ``timeout`` seconds.
        """
        if self.ended.is_set():
            raise LinkLost(False)
        future = self._expect(request_id, on_output)
        try:
            try:
                await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            except Exception as exc:  # noqa: BLE001 — a socket that will not take the frame never delivered it
                raise LinkLost(False, f"the terminal disconnected: {exc}") from None
            return await self._await(future, timeout)
        finally:
            self._forget(request_id, future)

    async def adopt(self, request_id: str, *, timeout: float, on_output: Callable[[str], None] | None = None) -> Any:
        """Wait for the answer to a request an earlier link sent and the client reported still running."""
        early = self._early.pop(request_id, None)
        if early is not None:
            return self._answer(early)
        if self.ended.is_set():
            raise LinkLost(True)
        future = self._expect(request_id, on_output)
        try:
            return await self._await(future, timeout)
        finally:
            self._forget(request_id, future)

    @staticmethod
    def _answer(message: dict[str, Any]) -> Any:
        error = message.get("error")
        if isinstance(error, dict):
            raise RemoteError(int(error.get("code") or 0), str(error.get("message") or "the terminal refused"), error.get("data"))
        return message.get("result")

    def _expect(self, request_id: str, on_output: Callable[[str], None] | None) -> asyncio.Future[Any]:
        if request_id in self._waiting:
            raise ValueError(f"request {request_id} is already waiting on this link")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        if on_output is not None:
            self._output[request_id] = on_output
        return future

    def _forget(self, request_id: str, future: asyncio.Future[Any]) -> None:
        if self._waiting.get(request_id) is future:
            self._waiting.pop(request_id, None)
            self._output.pop(request_id, None)

    async def _await(self, future: asyncio.Future[Any], timeout: float) -> Any:
        ended = asyncio.ensure_future(self.ended.wait())
        try:
            done, _ = await asyncio.wait({future, ended}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ended.cancel()
        if future in done:
            return future.result()
        if self.ended.is_set():
            raise LinkLost(True)
        raise TimeoutError("the terminal did not answer in time")

    async def carry(self) -> None:
        """Read the client's frames until the link ends; answers go to their callers, notifications to ``on_notify``."""
        try:
            while not self.ended.is_set():
                frame = await self.socket.receive()
                if frame is None:
                    self.reason = self.reason or "the terminal disconnected"
                    return
                if isinstance(frame, bytes):
                    continue  # reserved for ptyd's framed channels; nothing uses them yet
                if len(frame.encode()) > MAX_MESSAGE:
                    await self.close(CLOSE_PROTOCOL, "frame too large")
                    return
                try:
                    message = json.loads(frame)
                except ValueError:
                    await self.close(CLOSE_PROTOCOL, "not JSON")
                    return
                if not isinstance(message, dict):
                    await self.close(CLOSE_PROTOCOL, "a message is an object")
                    return
                await self._take(message)
        finally:
            self._end(self.reason or "the terminal disconnected")

    async def _take(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" not in message:
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method == "exec.output":
                sink = self._output.get(str(params.get("id") or ""))
                data = params.get("data")
                if sink is not None and isinstance(data, str) and data:
                    sink(data)
                return
            if self.on_notify is not None:
                try:
                    await self.on_notify(method, params)
                except Exception:  # noqa: BLE001 — one bad notification must not end the operator's link
                    logger.exception("device %s: notification %s failed", self.device_id, method)
            return
        if "method" in message:
            # The server does not take requests from a terminal: everything the client may do on its
            # own goes through the REST routes and their scope.
            with contextlib.suppress(Exception):
                await self._send({"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": METHOD_NOT_FOUND, "message": "the server takes no requests on the relay"}})
            return
        request_id = str(message.get("id") or "")
        future = self._waiting.get(request_id)
        if future is None or future.done():
            if request_id in self.hello.known and len(self._early) < 1000:
                self._early[request_id] = message
            return  # otherwise an answer nobody waits for any more: its caller was cancelled or timed out
        try:
            future.set_result(self._answer(message))
        except RemoteError as exc:
            future.set_exception(exc)

    def _end(self, reason: str) -> None:
        if not self.ended.is_set():
            self.reason = reason
            self.ended.set()

    async def close(self, code: int, reason: str = "") -> None:
        self._end(reason or f"closed ({code})")
        with contextlib.suppress(Exception):
            await self.socket.close(code, reason)


__all__ = [
    "CLOSE_FORBIDDEN",
    "CLOSE_GOING_AWAY",
    "CLOSE_PROTOCOL",
    "CLOSE_REPLACED",
    "CLOSE_TOO_MANY",
    "CLOSE_UNAUTHORIZED",
    "CLOSE_UNRESPONSIVE",
    "CONTRACT",
    "FORBIDDEN",
    "GUARDS",
    "HELLO_SECONDS",
    "MAX_MESSAGE",
    "PROTOCOL",
    "REFUSAL_REASONS",
    "DeviceLink",
    "Hello",
    "LinkLost",
    "RelaySocket",
    "RemoteError",
]
