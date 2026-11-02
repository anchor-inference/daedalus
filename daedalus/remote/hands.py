"""The hands of a remote conversation: its shell and file tools, run on the operator's terminal client.

A remote conversation's tools reach the operator's machine the way a host-folder session's reach
the host: through the session's :class:`~daedalus.host.filesystem.ExecBackend`. Here that backend is
:class:`DeviceExecBackend`, which sends each command down the device's relay as ``exec.run``, and the
file tools go through :class:`DeviceFS`, which uses the client's native ``fs.*`` calls (one round
trip for a read or a write instead of the five or so a shell-built edit takes) and falls back to the
shell for what the client does not serve.

Three rules live here rather than in the tools.

**Nothing runs without a turn.** A request names the tool call it serves (``daedalus.host.call_scope``).
A command or a write with no call behind it, or for a session whose run is not going, is refused
before it leaves: the server never drives the operator's computer on its own. The host's own reads
(the notes file at a run's start) and the operator's attachments are the only requests without a
call, and they never park a turn.

**A disconnect parks the call instead of failing it.** When the relay is down, or drops while a
request is out, the backend raises
:class:`~protocore.contracts.tools.ExternalCallPauseRequested`: the core parks the call, the turn
waits, and the host hands the same call over again when the terminal is back. The request ids are
``<session>:<tool call>:<n>``, the same on the repeat, and :mod:`daedalus.remote.journal` remembers
each one's fate: an answered request is answered again from the journal, a request never sent is sent
now, and one that was sent and not answered is the terminal's to account for, by what its
``device.hello`` reported (``running``: its answer is awaited; ``done``: it is fetched with
``device.result``; unknown: it is not run again and the agent is told so).

**The terminal's refusal is the tool's refusal.** A ``1004`` from the client's guard or jail becomes
:class:`~protocore.contracts.tools.ToolPolicyDenied`, which the dispatcher writes as the call's result
in the operator's words, not as a failed command for the agent to work around.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ExternalCallPauseRequested, ToolPolicyDenied

from daedalus.host import call_scope
from daedalus.host.filesystem import (
    TEXT_MAX_BYTES,
    ExecOutcome,
    FileTooLarge,
    NotRegularFile,
    ShellFS,
    TextWindow,
    window_of_bytes,
)
from daedalus.host.host_exec import PRELUDE
from daedalus.remote import journal as entries
from daedalus.remote.journal import Journal
from daedalus.remote.link import (
    CLOSE_UNRESPONSIVE,
    FORBIDDEN,
    NOT_FOUND,
    REFUSAL_REASONS,
    DeviceLink,
    LinkLost,
    RemoteError,
)
from daedalus.remote.relay import DeviceHub

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 30 * 60.0
"""The longest one ``exec.run`` may be given (``timeout_ms ≤ 1 800 000``)."""
ANSWER_GRACE = 30.0
"""How long past a command's own timeout the client has to answer before its link is taken for dead."""
FS_TIMEOUT = 120.0
READ_PIECE = 512 << 10
"""Bytes asked per ``fs.read``; one reply carries at most 640 KiB of a file."""
WRITE_PIECE = 384 << 10
"""Bytes per ``fs.write``: their base64 is 512 KiB, under the 1 MiB a message may carry."""
OUTPUT_FLUSH_BYTES = 4096
OUTPUT_FLUSH_SECONDS = 0.1

OFFLINE = "the operator's terminal is not connected, so nothing can run on their machine now; start daedalus-cli there"
NO_TURN = "nothing runs on the operator's machine outside a turn of its conversation"
LOST_OUTCOME = (
    "The operator's terminal disconnected while running this and came back without knowing it (daedalus-cli was "
    "restarted, which ended what it was running). It was not run again and its outcome is unknown: check the state "
    "on their machine before you try it again."
)

REFUSALS = {
    "refused_by_operator": "The operator refused this in their terminal",
    "plan_guard": "The operator's terminal is in plan mode, which refuses writes and commands that change things; plan instead, and say what you would do",
    "outside_root": "This is outside the folder the conversation is bound to on the operator's machine, which their terminal never reaches past",
    "denied_path": "This path is one the operator's terminal never hands to the agent (keys, credentials, other tools' logins)",
}

Output = Callable[[str, str, str], None]
"""``(session_id, tool_call_id, text)``: a running command's output, as the terminal sends it."""


class RemoteLost(OSError):
    """A request the terminal came back without knowing: not run again, outcome unknown."""


def refusal_text(error: RemoteError) -> str:
    """What the agent is told of a refusal by the operator's terminal."""
    lead = REFUSALS.get(error.reason, "The operator's terminal refused this")
    said = str(error.data.get("message") or "").strip() if isinstance(error.data, dict) else ""
    return f"{lead}: {said}" if said else f"{lead}."


def _first_line(text: str, width: int = 120) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:width]


class DeviceExecBackend:
    """One remote conversation's commands, sent to its home device. ``root`` is the folder the
    conversation is bound to there, as the device spells it, and the working directory of a command
    that names none."""

    foreign_paths = True

    def __init__(
        self,
        hub: DeviceHub,
        journal: Journal,
        *,
        device_id: str,
        device_name: str,
        session_id: str,
        root: str,
        turn_active: Callable[[str], bool],
        on_output: Output | None = None,
    ) -> None:
        self.hub = hub
        self.journal = journal
        self.device_id = device_id
        self.device_name = device_name or "your terminal"
        self.session_id = session_id
        self.root = root
        self.turn_active = turn_active
        self.on_output = on_output

    @property
    def machine(self) -> str:
        return f"device:{self.device_id}"

    def filesystem(self, timeout: float) -> DeviceFS:
        return DeviceFS(self, timeout=timeout)

    def prompt_note(self, workdir: Path | str) -> str:
        return (
            f"- You work on the operator's own computer, through their terminal ({self.device_name}), in {workdir}: Exec, "
            "background jobs and the file tools run there with no sandbox of this server around them, so take the care "
            "you would take on someone's own machine. Their terminal may ask them before a write or a command, or refuse "
            "it; a refusal is their decision, not an error to work around. ServiceStart is not available there; start a "
            "server with Exec(background=true)."
        )

    def _windows(self) -> bool:
        link = self.hub.link_of(self.device_id)
        return link is not None and link.hello.os.lower().startswith("windows")

    # -- the commands ----------------------------------------------------------------------------

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float, intent: str = "exec") -> ExecOutcome:
        limit = min(max(float(timeout), 1.0), MAX_TIMEOUT)
        argv = ["powershell", "-NoProfile", "-Command", command] if self._windows() else ["bash", "-c", PRELUDE + command]
        params: dict[str, Any] = {"argv": argv, "cwd": cwd or self.root, "timeout_ms": int(limit * 1000)}
        if env:
            params["env"] = dict(env)
        try:
            result = await self.call("exec.run", params, intent=intent, title=_first_line(command), timeout=limit + ANSWER_GRACE, stream=intent == "exec")
        except RemoteLost as exc:
            return ExecOutcome(exit_code=255, output=str(exc))
        except (RemoteError, OSError) as exc:
            # Not the guard (that is a refusal, raised as one) but the executor: a limit, a missing
            # folder, a program it cannot start. A failed command, which the agent reads as such.
            return ExecOutcome(exit_code=255, output=f"the operator's terminal could not run the command: {exc}")
        result = result if isinstance(result, dict) else {}
        stdout, stderr = str(result.get("stdout") or ""), str(result.get("stderr") or "")
        output = stdout + (("\n" + stderr) if stderr.strip() else "")
        if result.get("truncated"):
            output += "\n[... the terminal cut the output; write it to a file and read that instead ...]"
        code = int(result.get("exit_code") if result.get("exit_code") is not None else -1)
        if code < 0 and result.get("signal"):
            output += f"\n[ended by {result['signal']}]"
        return ExecOutcome(exit_code=code, output=output, timed_out=bool(result.get("timed_out")))

    async def put_bytes(self, path: str, data: bytes) -> None:
        """An attachment of the operator's own message, put into the folder on their machine."""
        await DeviceFS(self, timeout=FS_TIMEOUT).write_bytes(Path(path), data)

    # -- one request -----------------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any], *, intent: str, title: str = "", timeout: float = FS_TIMEOUT, stream: bool = False) -> Any:
        """One request to the device, with the journal, the pause and the refusal rules of this module."""
        scope = call_scope.current()
        if scope is not None and scope.session_id == self.session_id and scope.tool_call_id:
            # Before any await, so the ordinals of calls a tool makes in parallel follow the order it made them in.
            seq = scope.next_request()
            if intent != "read" and not self.turn_active(self.session_id):
                raise ToolPolicyDenied(NO_TURN)
            return await self._in_call(scope, seq, method, params, intent=intent, title=title, timeout=timeout, stream=stream)
        if intent == "exec":
            raise ToolPolicyDenied(NO_TURN)
        return await self._outside_call(method, params, intent=intent, title=title, timeout=timeout)

    def _meta(self, tool_call_id: str, tool: str, intent: str, title: str) -> dict[str, Any]:
        return {"session_id": self.session_id, "tool_call_id": tool_call_id, "tool": tool, "intent": intent, "title": title}

    def _pause(self, scope: call_scope.CallScope, request_id: str, method: str, delivery: str) -> ExternalCallPauseRequested:
        return ExternalCallPauseRequested(
            {"device_id": self.device_id, "device_name": self.device_name, "delivery": delivery, "request_id": request_id, "tool": scope.tool, "method": method}
        )

    async def _in_call(self, scope: call_scope.CallScope, seq: int, method: str, params: dict[str, Any], *, intent: str, title: str, timeout: float, stream: bool) -> Any:
        request_id = f"{self.session_id}:{scope.tool_call_id}:{seq}"
        entry = await self.journal.get(request_id)
        if entry is not None and entry.has_response and entry.state in (entries.DONE, entries.FAILED, entries.REFUSED):
            return self._replayed(entry)
        if entry is not None and entry.state == entries.LOST:
            raise RemoteLost(LOST_OUTCOME)
        sink = self._sink(scope.tool_call_id) if stream else None
        if entry is not None and entry.state == entries.SENT:
            return await self._account(scope, request_id, method, timeout=timeout, sink=sink)
        link = self.hub.link_of(self.device_id)
        record = {"session_id": self.session_id, "tool_call_id": scope.tool_call_id, "seq": seq, "device_id": self.device_id, "tool": scope.tool, "method": method, "intent": intent, "title": title}
        if link is None:
            await self.journal.record(request_id, **record, state=entries.HELD)
            raise self._pause(scope, request_id, method, entries.NOT_DELIVERED)
        await self.journal.record(request_id, **record, state=entries.SENT)
        payload = {**params, "_meta": self._meta(scope.tool_call_id, scope.tool, intent, title)}
        try:
            result = await link.request(request_id, method, payload, timeout=timeout, on_output=sink)
        except LinkLost as lost:
            if not lost.delivered:
                await self.journal.record(request_id, **record, state=entries.HELD)
                raise self._pause(scope, request_id, method, entries.NOT_DELIVERED) from None
            raise self._pause(scope, request_id, method, entries.UNKNOWN) from None
        except TimeoutError:
            # A client that took the request and says nothing past its own timeout has a dead socket:
            # the link is closed so it reconnects, and its hello says what became of the request.
            await link.close(CLOSE_UNRESPONSIVE, "the terminal stopped answering")
            raise self._pause(scope, request_id, method, entries.UNKNOWN) from None
        except RemoteError as exc:
            await self._failed(request_id, exc)
            raise self._mapped(exc) from None
        except asyncio.CancelledError:
            await asyncio.shield(self._cancelled(link, request_id))
            raise
        finally:
            if sink is not None:
                sink.flush()
        await self.journal.settle(request_id, entries.DONE, outcome=self._outcome(method, result), response=result, keep=True)
        return result

    async def _account(self, scope: call_scope.CallScope, request_id: str, method: str, *, timeout: float, sink: _OutputSink | None) -> Any:
        """A request an earlier link carried and nobody heard the answer to: what the terminal says of it."""
        link = self.hub.link_of(self.device_id)
        if link is None:
            raise self._pause(scope, request_id, method, entries.UNKNOWN)
        held = link.hello.known.get(request_id)
        try:
            if held == "running":
                result = await link.adopt(request_id, timeout=timeout, on_output=sink)
            elif held == "done":
                result = await link.request(f"{request_id}/result", "device.result", {"id": request_id, "_meta": self._meta(scope.tool_call_id, scope.tool, "read", "")}, timeout=FS_TIMEOUT)
            else:
                await self.journal.settle(request_id, entries.LOST, outcome="the terminal came back without knowing it")
                raise RemoteLost(LOST_OUTCOME)
        except LinkLost:
            raise self._pause(scope, request_id, method, entries.UNKNOWN) from None
        except TimeoutError:
            await link.close(CLOSE_UNRESPONSIVE, "the terminal stopped answering")
            raise self._pause(scope, request_id, method, entries.UNKNOWN) from None
        except RemoteError as exc:
            if exc.code == NOT_FOUND and held == "done":
                # It forgot the answer meanwhile (it keeps them fifteen minutes after a drop).
                await self.journal.settle(request_id, entries.LOST, outcome="the terminal no longer had its answer")
                raise RemoteLost(LOST_OUTCOME) from None
            await self._failed(request_id, exc)
            raise self._mapped(exc) from None
        finally:
            if sink is not None:
                sink.flush()
        await self.journal.settle(request_id, entries.DONE, outcome=self._outcome(method, result), response=result, keep=True)
        return result

    async def _outside_call(self, method: str, params: dict[str, Any], *, intent: str, title: str, timeout: float) -> Any:
        """A request of the host's own (a notes file read at a run's start, an attachment put in place):
        audited like the rest, never parked, failing as an ``OSError`` when the terminal is away."""
        request_id = f"{self.session_id}:host-{secrets.token_hex(6)}:1"
        link = self.hub.link_of(self.device_id)
        if link is None:
            raise ConnectionError(OFFLINE)
        await self.journal.record(request_id, session_id=self.session_id, tool_call_id="", seq=1, device_id=self.device_id, tool="Daedalus", method=method, intent=intent, title=title, state=entries.SENT)
        try:
            result = await link.request(request_id, method, {**params, "_meta": self._meta("", "Daedalus", intent, title)}, timeout=timeout)
        except (LinkLost, TimeoutError):
            await self.journal.settle(request_id, entries.LOST, outcome="the terminal went away")
            raise ConnectionError(OFFLINE) from None
        except RemoteError as exc:
            await self._failed(request_id, exc, keep=False)
            raise self._mapped(exc) from None
        await self.journal.settle(request_id, entries.DONE, outcome=self._outcome(method, result))
        return result

    def _replayed(self, entry: entries.Entry) -> Any:
        """The answer a request already had, for its repeat: the result, or the same error again."""
        response = entry.response
        if entry.state == entries.DONE:
            return response
        error = response.get("error") if isinstance(response, dict) else None
        error = error if isinstance(error, dict) else {}
        raise self._mapped(RemoteError(int(error.get("code") or 0), str(error.get("message") or "the terminal refused"), error.get("data")))

    async def _failed(self, request_id: str, exc: RemoteError, *, keep: bool = True) -> None:
        refused = exc.code == FORBIDDEN and exc.reason in REFUSAL_REASONS
        state = entries.REFUSED if refused else entries.FAILED
        outcome = f"{exc.reason or exc.code}: {exc.message}"
        await self.journal.settle(request_id, state, outcome=outcome, response={"error": {"code": exc.code, "message": exc.message, "data": exc.data}}, keep=keep)
        if refused:
            logger.info("session %s: the operator's terminal refused %s (%s)", self.session_id, request_id, exc.reason)

    @staticmethod
    def _mapped(exc: RemoteError) -> Exception:
        """A terminal's error in the terms the file tools already handle."""
        if exc.code == FORBIDDEN and exc.reason in REFUSAL_REASONS:
            return ToolPolicyDenied(refusal_text(exc))
        if exc.code == NOT_FOUND:
            return FileNotFoundError(exc.message)
        if exc.code == FORBIDDEN:
            return PermissionError(exc.message)
        return exc

    async def _cancelled(self, link: DeviceLink, request_id: str) -> None:
        """The turn was stopped while the request ran: the terminal is told to end it."""
        await self.journal.settle(request_id, entries.CANCELLED, outcome="stopped by the operator")
        await link.notify("request.cancel", {"id": request_id})

    @staticmethod
    def _outcome(method: str, result: Any) -> str:
        if not isinstance(result, dict):
            return "ok"
        if method == "exec.run":
            return f"exit {result.get('exit_code')}" + (" timed out" if result.get("timed_out") else "")
        if method == "fs.read":
            return f"{len(str(result.get('data_b64') or '')) * 3 // 4} bytes at {result.get('offset', 0)}"
        if method == "fs.write":
            return f"size {result.get('size')}"
        return "ok"

    def _sink(self, tool_call_id: str) -> _OutputSink | None:
        if self.on_output is None:
            return None
        return _OutputSink(lambda text: self.on_output(self.session_id, tool_call_id, text) if self.on_output is not None else None)


class _OutputSink:
    """A running command's output, gathered into pieces worth an event: a stream of single bytes
    would be one event each on every open screen."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buffer: list[str] = []
        self._size = 0
        self._last = time.monotonic()

    def __call__(self, text: str) -> None:
        self._buffer.append(text)
        self._size += len(text)
        now = time.monotonic()
        if self._size >= OUTPUT_FLUSH_BYTES or now - self._last >= OUTPUT_FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if self._buffer:
            text, self._buffer, self._size = "".join(self._buffer), [], 0
            self._last = time.monotonic()
            try:
                self._emit(text)
            except Exception:  # noqa: BLE001 — showing the output is a courtesy; the command runs regardless
                logger.debug("could not pass on a command's output", exc_info=True)


class DeviceFS(ShellFS):
    """The files of a remote conversation, on the operator's machine.

    Native ``fs.*`` calls where the client serves them (``device.hello`` lists them under
    ``capabilities.fs``), the inherited shell commands where it does not. Every shell command a file
    tool runs here is a read and says so (``intent: read``), so the client's guard does not ask the
    operator before a file is merely looked at."""

    backend: DeviceExecBackend

    def __init__(self, backend: DeviceExecBackend, *, timeout: float = FS_TIMEOUT) -> None:
        super().__init__(backend, timeout=timeout)
        self._intent = "read"

    def _native(self, name: str) -> bool:
        link = self.backend.hub.link_of(self.backend.device_id)
        if link is None:
            # Away: assume the calls every client serves, so the request is journaled and the call parks
            # on it; the fallback would park on a shell command instead, which says less of what it was.
            return name in ("stat", "list", "read", "write", "mkdir")
        return name in link.hello.fs_methods()

    async def _run(self, command: str) -> ExecOutcome:
        return await self.backend.run(command, cwd=None, env=None, timeout=self.timeout, intent=self._intent)

    async def _fs(self, method: str, params: dict[str, Any], *, intent: str = "read", title: str = "") -> Any:
        return await self.backend.call(f"fs.{method}", params, intent=intent, title=title or str(params.get("path") or ""), timeout=self.timeout)

    async def _stat(self, path: Path) -> dict[str, Any]:
        result = await self._fs("stat", {"path": str(path)})
        return result if isinstance(result, dict) else {}

    async def _read_all(self, path: Path, limit: int) -> bytes:
        stat = await self._stat(path)
        if not stat.get("exists"):
            raise FileNotFoundError(f"no such file: {path}")
        if stat.get("type") not in (None, "file"):
            kind = "a directory" if stat.get("type") == "dir" else "not a regular file"
            raise NotRegularFile(f"{path} is {kind}; only a regular file can be read")
        size = int(stat.get("size") or 0)
        if size > limit:
            raise FileTooLarge(f"{path} is {size} bytes, more than the {limit} read here")
        data = bytearray()
        while True:
            result = await self._fs("read", {"path": str(path), "offset": len(data), "max": READ_PIECE})
            piece = base64.b64decode(str(result.get("data_b64") or "")) if isinstance(result, dict) else b""
            data += piece
            if len(data) > limit:
                raise FileTooLarge(f"{path} is more than the {limit} bytes read here")
            if not isinstance(result, dict) or result.get("eof") or not piece:
                return bytes(data)

    async def read_bytes(self, path: Path, *, limit: int) -> bytes:
        if not self._native("read"):
            return await super().read_bytes(path, limit=limit)
        try:
            return await self._read_all(path, limit)
        except FileTooLarge as exc:
            raise ValueError(str(exc)) from None

    async def read_text(self, path: Path, *, limit: int | None = None) -> str:
        if not self._native("read"):
            return await super().read_text(path, limit=limit)
        data = await self._read_all(path, TEXT_MAX_BYTES if limit is None else limit)
        if b"\x00" in data[:8192]:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary file")
        return data.decode("utf-8")

    async def read_window(self, path: Path, *, start: int, max_lines: int, max_chars: int, line_chars: int) -> TextWindow:
        if not self._native("read"):
            return await super().read_window(path, start=start, max_lines=max_lines, max_chars=max_chars, line_chars=line_chars)
        try:
            data = await self._read_all(path, TEXT_MAX_BYTES)
        except FileTooLarge:
            # Larger than a whole read is worth: the shell cuts the window over there instead.
            return await super().read_window(path, start=start, max_lines=max_lines, max_chars=max_chars, line_chars=line_chars)
        return window_of_bytes(data, start=start, max_lines=max_lines, max_chars=max_chars, line_chars=line_chars)

    async def write_bytes(self, path: Path, data: bytes) -> None:
        if not self._native("write"):
            self._intent = "write"
            try:
                await super().write_bytes(path, data)
            finally:
                self._intent = "read"
            return
        pieces = [data[i : i + WRITE_PIECE] for i in range(0, len(data), WRITE_PIECE)] or [b""]
        offset = 0
        for index, piece in enumerate(pieces):
            await self._fs(
                "write",
                {"path": str(path), "offset": offset, "data_b64": base64.b64encode(piece).decode("ascii"), "final": index == len(pieces) - 1},
                intent="write",
            )
            offset += len(piece)

    async def write_text(self, path: Path, content: str) -> None:
        await self.write_bytes(path, content.encode("utf-8"))

    async def exists(self, path: Path) -> bool:
        if not self._native("stat"):
            return await super().exists(path)
        return bool((await self._stat(path)).get("exists"))

    async def is_dir(self, path: Path) -> bool:
        if not self._native("stat"):
            return await super().is_dir(path)
        return (await self._stat(path)).get("type") == "dir"

    async def is_file(self, path: Path) -> bool:
        if not self._native("stat"):
            return await super().is_file(path)
        return (await self._stat(path)).get("type") == "file"

    async def size(self, path: Path) -> int:
        if not self._native("stat"):
            return await super().size(path)
        return int((await self._stat(path)).get("size") or 0)

    async def listdir(self, path: Path) -> list[str]:
        if not self._native("list"):
            return await super().listdir(path)
        result = await self._fs("list", {"path": str(path), "limit": 5000})
        rows = result.get("entries") if isinstance(result, dict) else None
        return sorted(str(row.get("name")) for row in rows or [] if isinstance(row, dict) and row.get("name"))

    async def find(self, root: Path, pattern: str, limit: int) -> list[str]:
        if not self._native("find"):
            return await super().find(root, pattern, limit)
        result = await self._fs("find", {"path": str(root), "pattern": pattern, "limit": limit}, title=pattern)
        found = result.get("paths") if isinstance(result, dict) else None
        return sorted(str(item) for item in found or [])[:limit]

    async def search(self, root: Path, pattern: str, *, glob: str | None, case_insensitive: bool, limit: int) -> tuple[int, str, str]:
        if not self._native("search"):
            return await super().search(root, pattern, glob=glob, case_insensitive=case_insensitive, limit=limit)
        params: dict[str, Any] = {"path": str(root), "pattern": pattern, "case_insensitive": case_insensitive, "limit": limit}
        if glob:
            params["glob"] = glob
        result = await self._fs("search", params, title=pattern)
        lines = result.get("lines") if isinstance(result, dict) else None
        text = "\n".join(str(line) for line in lines or [])
        return (0 if text else 1), text + ("\n" if text else ""), ""


__all__ = ["LOST_OUTCOME", "NO_TURN", "OFFLINE", "REFUSALS", "DeviceExecBackend", "DeviceFS", "RemoteLost", "refusal_text"]
