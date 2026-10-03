"""The side channels of the terminal daemons: what a harness adapter reaches besides a terminal.

Running a program for its output (a version check, a CLI's own updater), reading the files a CLI
keeps its transcripts in, a byte stream to the socket or loopback port a launch registered, and the
launch itself: the per-launch directory of overlay files, the hook URL and token its terminals are
given, and the hook posts that come back as events, a held one waiting for this host's reply.

The daemon enforces the walls — the program list, the file roots and deny list, the registered
ports — and this side keeps them fed: the roots are the project folders of each environment plus
the transcript directories adapters name, pushed on every connection and whenever they change.
Everything that acts (a program run, a launch registered or ended, a port opened, a stream dialled,
a hook answered) is written to the terminal audit.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from daedalus.terminals import wire
from daedalus.terminals.client import Channel, PtydClient, Unavailable
from daedalus.terminals.endpoint import absolute_on
from daedalus.terminals.model import (
    Conflict,
    EnvUnavailable,
    ExecResult,
    FileChunk,
    Forbidden,
    HookEvent,
    InvalidRequest,
    Launch,
    LaunchSpec,
    NotFound,
    StaleLaunch,
    TerminalError,
    TerminalEvent,
    TimedOut,
    Unsupported,
)

if TYPE_CHECKING:
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

STREAM_QUEUE_BYTES = 1 << 20
"""What a dialled stream holds unread before it is closed as too slow: a consumer that stopped
reading must not make the connection hold megabytes for it."""
STREAM_WRITE_CHUNK = 256 << 10
HOOK_QUEUE_EVENTS = 1000
"""Hook events kept for a launch nobody is reading yet. A launch whose consumer is gone keeps no
more than this; the oldest are dropped first, with a warning."""
ENDED_REMEMBERED = 1000
AUDIT_TEXT_BYTES = 4096


def side_failure(exc: wire.RpcError, what: str) -> TerminalError:
    """A daemon's refusal of a side-channel call, in the service's terms, with the daemon's words."""
    code = exc.code
    text = f"{what}: {exc.message}"
    if code == wire.FORBIDDEN:
        return Forbidden(text)
    if code == wire.NOT_FOUND:
        return NotFound(text)
    if code == wire.STALE_LAUNCH:
        return StaleLaunch(text)
    if code == wire.LIMIT:
        return Conflict(text, reason="limit")
    if code == wire.TIMEOUT:
        return TimedOut(text)
    if code in (wire.UNSUPPORTED, wire.METHOD_NOT_FOUND):
        return Unsupported(f"{what}: not available in this terminal service ({exc.message})")
    if code == wire.INVALID_PARAMS:
        return InvalidRequest(text)
    return TerminalError(f"{text} ({code})")


class ByteStream:
    """A ``net_dial`` stream: bytes to and from a program's socket, over a channel of the daemon's
    connection. ``read`` returns ``b""`` once either side has closed it; ``reason`` says why."""

    def __init__(self, channel: Channel, *, env: str, launch_id: str, target: str) -> None:
        self.channel = channel
        self.env = env
        self.launch_id = launch_id
        self.target = target
        channel.limit = STREAM_QUEUE_BYTES

    @property
    def closed(self) -> bool:
        return self.channel.closed

    @property
    def reason(self) -> str:
        return self.channel.reason

    async def read(self) -> bytes:
        return await self.channel.recv() or b""

    async def write(self, data: bytes) -> None:
        try:
            for start in range(0, len(data), STREAM_WRITE_CHUNK):
                await self.channel.send(data[start : start + STREAM_WRITE_CHUNK])
        except Unavailable as exc:
            raise EnvUnavailable(f"the stream to {self.target} is closed: {exc.detail}", env=self.env, reason=exc.reason) from None

    async def close(self) -> None:
        await self.channel.close()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        while chunk := await self.read():
            yield chunk


class _HookQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[HookEvent | None] = asyncio.Queue()
        self.dropped = 0


class SideChannels:
    """The side-channel half of the terminals service; ``Terminals`` is the one class that has it."""

    db: Database

    def _init_side(self) -> None:
        self._hook_queues: dict[str, _HookQueue] = {}
        self._ended_launches: OrderedDict[str, None] = OrderedDict()
        self._launch_terminals: dict[str, str] = {}
        self._extra_roots: dict[str, dict[str, builtins.list[str]]] = {}
        self._roots_sent: dict[str, tuple[str, ...] | None] = {}

    # Provided by the service.
    def _client(self, env: str) -> PtydClient:
        raise NotImplementedError

    async def audit(self, terminal_id: str, env: str, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    async def _side_call(self, env: str, method: str, params: dict[str, Any], *, what: str, timeout: float = 10.0) -> Any:
        client = self._client(env)
        try:
            return await client.call(method, params, timeout=timeout)
        except Unavailable as exc:
            raise EnvUnavailable(f"{what}: the {env} terminal service is not available: {exc.detail}", env=env, reason=exc.reason) from None
        except wire.RpcError as exc:
            raise side_failure(exc, what) from None

    # -- programs ---------------------------------------------------------------------------

    async def exec_run(
        self,
        env: str,
        argv: builtins.list[str],
        *,
        cwd: str | None = None,
        env_vars: dict[str, str] | None = None,
        timeout: float = 60.0,
        stdin: bytes | None = None,
        max_output: int | None = None,
        actor: str = "system",
        resources: dict[str, Any] | None = None,
    ) -> ExecResult:
        """Run a program of the daemon's list in ``env`` (not in a terminal) and return its output.

        A program that fails is a result with its exit code, not an error; the errors are the ones
        of the call (not on the list, not found, the environment gone).
        """
        if not argv:
            raise InvalidRequest("a program needs an argv")
        params: dict[str, Any] = {"argv": list(argv), "timeout_ms": int(timeout * 1000)}
        if cwd:
            params["cwd"] = cwd
        if env_vars:
            params["env"] = dict(env_vars)
        if stdin is not None:
            params["stdin_b64"] = base64.b64encode(stdin).decode()
        if max_output is not None:
            params["max_output"] = max_output
        if resources is not None:
            params["resources"] = {"scope": resources["scope"],
                                   "expected_instance": resources["expected_instance"]}
        detail: dict[str, Any] = {"argv": [a[:200] for a in argv[:32]], "cwd": cwd}
        try:
            result = await self._side_call(env, "exec.run", params, what=f"running {argv[0]}", timeout=timeout + 15)
        except TerminalError as exc:
            await self.audit("", env, actor, "exec", {**detail, "error": exc.message})
            raise
        out = ExecResult(
            exit_code=int(result.get("exit_code", -1)),
            signal=str(result.get("signal") or ""),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            truncated=bool(result.get("truncated")),
            timed_out=bool(result.get("timed_out")),
            duration_ms=int(result.get("duration_ms") or 0),
            path=str(result.get("path") or ""),
        )
        await self.audit("", env, actor, "exec", {**detail, "exit_code": out.exit_code, "signal": out.signal, "timed_out": out.timed_out, "duration_ms": out.duration_ms})
        return out

    # -- files ------------------------------------------------------------------------------

    async def fs_stat(self, env: str, path: str, *, as_root: bool = False) -> dict[str, Any]:
        """``{exists, type, size, mtime, mode, file_id}``; a missing file under a root is ``{exists: false}``.

        ``as_root`` asks about a folder that is about to become a root — one being added to a project —
        and so is under none yet. The daemon then holds the path to the rules a root is held to (not
        the filesystem's root, not a folder that holds the home directory, nothing on the deny list)
        and adds ``writable``.
        """
        params: dict[str, Any] = {"path": path}
        if as_root:
            params["as_root"] = True
        result: dict[str, Any] = await self._side_call(env, "fs.stat", params, what=f"looking at {path}")
        return result

    async def fs_mkdir(self, env: str, path: str, *, actor: str = "system") -> dict[str, Any]:
        """Make a folder that is about to become a root, with its missing parents; the answer is
        ``fs_stat(as_root=True)``'s plus ``created``. An existing folder is left as it is. This is the
        one write the side channels make, so it is audited like a program run."""
        try:
            result: dict[str, Any] = await self._side_call(env, "fs.mkdir", {"path": path}, what=f"making {path}")
        except TerminalError as exc:
            await self.audit("", env, actor, "mkdir", {"path": path, "error": exc.message})
            raise
        await self.audit("", env, actor, "mkdir", {"path": path, "created": bool(result.get("created"))})
        return result

    async def fs_write(self, env: str, path: str, data: bytes, *, offset: int = 0, actor: str = "system") -> dict[str, Any]:
        """Write ``data`` at ``offset`` into a file of a staff member's inbox (``<folder>/.agents/inbox/…``);
        ``{size, created}``. Offset 0 creates the file and is refused when it
        exists; a later offset continues it. Nothing outside an inbox can be written, whatever the roots,
        and every call is audited with the bytes' hash."""
        detail: dict[str, Any] = {"path": path, "offset": offset, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        params = {"path": path, "offset": offset, "data_b64": base64.b64encode(data).decode()}
        try:
            result: dict[str, Any] = await self._side_call(env, "fs.write", params, what=f"writing {path}", timeout=30.0)
        except TerminalError as exc:
            await self.audit("", env, actor, "write", {**detail, "error": exc.message})
            raise
        await self.audit("", env, actor, "write", {**detail, "size": int(result.get("size") or 0)})
        return result

    async def fs_list(self, env: str, path: str, *, glob: str | None = None, sort: str = "name", limit: int = 1000) -> dict[str, Any]:
        """``{entries: [{name, type, size, mtime}], truncated}``; files on the deny list are left out."""
        params: dict[str, Any] = {"path": path, "sort": sort, "limit": limit}
        if glob:
            params["glob"] = glob
        result: dict[str, Any] = await self._side_call(env, "fs.list", params, what=f"listing {path}")
        return result

    async def fs_read(self, env: str, path: str, *, offset: int = 0, max_bytes: int = 64 << 10) -> FileChunk:
        """Up to ``max_bytes`` at ``offset``. One reply carries at most about 640 KiB; ``eof`` says
        whether the end was reached, and a longer read continues at ``next_offset``."""
        result = await self._side_call(env, "fs.read", {"path": path, "offset": offset, "max": max_bytes}, what=f"reading {path}")
        data = base64.b64decode(result.get("data_b64") or "")
        return FileChunk(data=data, offset=offset, next_offset=offset + len(data), size=int(result.get("size") or 0), eof=bool(result.get("eof")), file_id=str(result.get("file_id") or ""))

    async def fs_tail(self, env: str, path: str, *, from_offset: int, max_bytes: int = 64 << 10, follow: float = 0.0, file_id: str = "") -> FileChunk:
        """What the file holds past ``from_offset``, waiting up to ``follow`` seconds (at most 60) for
        something to arrive. Pass back the last ``file_id``: a replaced or shrunk file comes back
        ``rotated``, read from its start."""
        params: dict[str, Any] = {"path": path, "from_offset": from_offset, "max": max_bytes, "follow_ms": int(follow * 1000)}
        if file_id:
            params["file_id"] = file_id
        result = await self._side_call(env, "fs.tail", params, what=f"following {path}", timeout=follow + 15)
        data = base64.b64decode(result.get("data_b64") or "")
        rotated = bool(result.get("rotated"))
        next_offset = int(result.get("next_offset") or 0)
        return FileChunk(data=data, offset=next_offset - len(data), next_offset=next_offset, size=int(result.get("size") or 0), rotated=rotated, file_id=str(result.get("file_id") or ""))

    def set_extra_roots(self, env: str, name: str, paths: builtins.list[str]) -> None:
        """Roots a subsystem needs besides the project folders — a CLI's transcript directory — under
        its own name, so each adapter replaces only its own. They reach the daemon within seconds."""
        mine = self._extra_roots.setdefault(env, {})
        if paths:
            mine[name] = sorted(set(paths))
        else:
            mine.pop(name, None)

    async def _roots(self, env: str) -> tuple[str, ...]:
        rows = await self.db.fetchall("SELECT path FROM project_folders WHERE env = ?", (env,))
        roots = {str(r["path"]) for r in rows if absolute_on(env, str(r["path"]))}
        for paths in self._extra_roots.get(env, {}).values():
            roots.update(paths)
        return tuple(sorted(roots))

    async def _push_roots(self, env: str, client: PtydClient, *, force: bool = False) -> None:
        """Send the environment's roots when they changed since last sent (always, after a connect:
        a restarted daemon starts with none)."""
        roots = await self._roots(env)
        if not force and self._roots_sent.get(env) == roots:
            return
        # Recorded as sent whatever the answer, so a daemon that refuses is not asked every two
        # seconds; the next connection sends them again.
        self._roots_sent[env] = roots
        try:
            result = await client.call("fs.set_roots", {"roots": list(roots)})
        except wire.RpcError as exc:
            if exc.code != wire.METHOD_NOT_FOUND:  # an older daemon has no side channels to feed
                logger.warning("terminal service %s did not take its file roots: %s", env, exc.message)
            return
        except Unavailable as exc:
            logger.info("terminal service %s: file roots not sent: %s", env, exc.detail)
            return
        for refused in (result or {}).get("refused") or []:
            logger.warning("terminal service %s refused the root %s: %s", env, refused.get("root"), refused.get("reason"))

    # -- streams ----------------------------------------------------------------------------

    async def net_allow(self, env: str, launch_id: str, port: int, *, actor: str = "system") -> None:
        """Let ``net_dial`` reach a loopback port for a launch — a CLI's HTTP server it was told to
        start there. Only registered ports: natively the same loopback holds this host's own API."""
        await self._side_call(env, "net.allow", {"launch_id": launch_id, "port": port}, what=f"opening port {port}")
        await self.audit(self._launch_terminals.get(launch_id, ""), env, actor, "net_allow", {"launch_id": launch_id, "port": port})

    async def net_dial(self, env: str, target: str, launch_id: str, *, actor: str = "system") -> ByteStream:
        """A byte stream to ``unix:<name>`` in the launch's dial directory or ``tcp:127.0.0.1:<port>``
        it registered."""
        result = await self._side_call(env, "net.dial", {"target": target, "launch_id": launch_id}, what=f"dialling {target}")
        channel = self._client(env).channel(int(result["channel"]))
        await self.audit(self._launch_terminals.get(launch_id, ""), env, actor, "dial", {"launch_id": launch_id, "target": target})
        return ByteStream(channel, env=env, launch_id=launch_id, target=target)

    # -- launches and hooks -----------------------------------------------------------------

    async def register_launch(self, env: str, launch: LaunchSpec, *, actor: str = "system") -> Launch:
        """Register a launch: its files are written, its ports opened, its token made. Create its
        terminal with ``TerminalSpec.launch_id`` so the terminal is given the launch's environment."""
        params: dict[str, Any] = {"files": {name: base64.b64encode(data).decode() for name, data in launch.files.items()}}
        if launch.launch_id:
            params["launch_id"] = launch.launch_id
        if launch.terminal_id:
            params["terminal_id"] = launch.terminal_id
        if launch.ports:
            params["ports"] = list(launch.ports)
        if launch.hold_max_ms:
            params["hold_max_ms"] = launch.hold_max_ms
        if launch.ttl_s:
            params["ttl_s"] = launch.ttl_s
        result = await self._side_call(env, "hooks.register_launch", params, what="registering the launch")
        launch_id = str(result["launch_id"])
        # The queue exists from now on, so no hook the launch posts before a consumer asks is lost.
        self._hook_queues.setdefault(launch_id, _HookQueue())
        if launch.terminal_id:
            self._launch_terminals[launch_id] = launch.terminal_id
        files = {name: hashlib.sha256(data).hexdigest() for name, data in launch.files.items()}
        await self.audit(launch.terminal_id, env, actor, "launch", {"launch_id": launch_id, "files": files, "ports": list(launch.ports)})
        return Launch(
            env=env,
            launch_id=launch_id,
            hook_url=str(result.get("hook_url") or ""),
            hook_token=str(result.get("hook_token") or ""),
            dir=str(result.get("dir") or ""),
            dial_dir=str(result.get("dial_dir") or ""),
            env_vars={str(k): str(v) for k, v in (result.get("env") or {}).items()},
            files=[str(f) for f in result.get("files") or []],
        )

    async def put_launch_file(self, env: str, launch_id: str, name: str, data: bytes, *, actor: str = "system") -> str:
        """Add a file to an open launch's directory and return its path: a message too long to type,
        which the CLI is told to read. The daemon refuses a name that is a path or already exists."""
        detail: dict[str, Any] = {"launch_id": launch_id, "name": name, "sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
        terminal_id = self._launch_terminals.get(launch_id, "")
        try:
            result = await self._side_call(env, "hooks.put_file", {"launch_id": launch_id, "name": name, "data": base64.b64encode(data).decode()}, what="adding a file to the launch")
        except TerminalError as exc:
            await self.audit(terminal_id, env, actor, "launch_file", {**detail, "error": exc.message})
            raise
        await self.audit(terminal_id, env, actor, "launch_file", detail)
        return str((result or {}).get("path") or "")

    async def unregister_launch(self, env: str, launch_id: str, *, actor: str = "system") -> bool:
        """End a launch now: its files go, its streams close, its held posts are answered 410."""
        result = await self._side_call(env, "hooks.unregister_launch", {"launch_id": launch_id}, what="ending the launch")
        removed = bool((result or {}).get("removed"))
        self._end_launch(launch_id)
        await self.audit(self._launch_terminals.pop(launch_id, ""), env, actor, "unlaunch", {"launch_id": launch_id, "removed": removed})
        return removed

    async def hook_events(self, launch_id: str) -> AsyncIterator[HookEvent]:
        """The launch's hook posts, in order, until it ends. One consumer per launch: two would share
        the posts between them. Posts made before the first call are kept for it."""
        if launch_id in self._ended_launches and launch_id not in self._hook_queues:
            return
        hooks = self._hook_queues.setdefault(launch_id, _HookQueue())
        while True:
            item = await hooks.queue.get()
            if item is None:
                hooks.queue.put_nowait(None)  # a second consumer sees the end as well
                return
            yield item

    async def reply_hook(self, env: str, reply_id: str, status: int = 200, body: bytes | str | dict[str, Any] | builtins.list[Any] | None = None, *, launch_id: str = "", actor: str = "system") -> None:
        """Answer a held post. Raises ``NotFound`` when nothing waits any more (it timed out, or its
        CLI went away): the answer then reached nobody, and the caller must know."""
        params: dict[str, Any] = {"reply_id": reply_id, "status": status}
        if launch_id:
            params["launch_id"] = launch_id
        if isinstance(body, bytes):
            params["body"] = body.decode("utf-8", "replace")
        elif body is not None:
            params["body"] = body
        text = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
        detail: dict[str, Any] = {"launch_id": launch_id, "reply_id": reply_id, "status": status, "body": text[:AUDIT_TEXT_BYTES].decode("utf-8", "replace"), "sha256": hashlib.sha256(text).hexdigest()}
        terminal_id = self._launch_terminals.get(launch_id, "")
        try:
            await self._side_call(env, "hooks.reply", params, what="answering the hook")
        except TerminalError as exc:
            await self.audit(terminal_id, env, actor, "hook_reply", {**detail, "error": exc.message})
            raise
        await self.audit(terminal_id, env, actor, "hook_reply", detail)

    def _end_launch(self, launch_id: str) -> None:
        hooks = self._hook_queues.pop(launch_id, None)
        if hooks is not None:
            hooks.queue.put_nowait(None)
        self._ended_launches[launch_id] = None
        while len(self._ended_launches) > ENDED_REMEMBERED:
            self._ended_launches.popitem(last=False)

    def _on_side_event(self, event: TerminalEvent) -> None:
        """Route the daemon's launch events: hook posts to their launch's queue, the end of a launch
        to the end of its queue."""
        data = event.data
        if event.type == "hook":
            launch_id = str(data.get("launch_id") or "")
            if not launch_id or launch_id in self._ended_launches:
                return
            reply_id = data.get("reply_id")
            hook = HookEvent(
                env=event.env,
                seq=event.seq,
                at=event.at,
                launch_id=launch_id,
                terminal_id=str(data.get("terminal_id") or "") or event.terminal_id,
                name=str(data.get("name") or ""),
                body=data.get("body"),
                reply_id=str(reply_id) if reply_id else None,
                hold_ms=int(data.get("hold_ms") or 0),
                truncated=bool(data.get("truncated")),
                size=int(data.get("size") or 0),
            )
            hooks = self._hook_queues.setdefault(launch_id, _HookQueue())
            if hooks.queue.qsize() >= HOOK_QUEUE_EVENTS:
                hooks.queue.get_nowait()
                hooks.dropped += 1
                if hooks.dropped in (1, 100, 1000) or hooks.dropped % 10000 == 0:
                    logger.warning("launch %s: %d hook events dropped; nobody reads them", launch_id, hooks.dropped)
            hooks.queue.put_nowait(hook)
        elif event.type == "launch.ended":
            launch_id = str(data.get("launch_id") or "")
            if launch_id:
                self._end_launch(launch_id)
                self._launch_terminals.pop(launch_id, None)
        elif event.type == "terminal.created" and event.terminal_id and data.get("launch_id"):
            self._launch_terminals.setdefault(str(data["launch_id"]), event.terminal_id)


__all__ = ["ByteStream", "SideChannels", "side_failure"]
