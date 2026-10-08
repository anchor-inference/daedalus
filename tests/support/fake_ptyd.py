"""A terminal daemon in-process: the socket protocol of ``docs/architecture/terminals.md``, with
scripted terminals instead of processes.

It speaks the real framing, handshake, JSON-RPC and event subscription, so the host's client and
service are exercised end to end over a unix socket; what a terminal does is whatever the test says.
Restarting it is a new instance with none of the old terminals, as a restarted daemon is.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FakeTerminal:
    id: str
    argv: list[str]
    cwd: str
    labels: dict[str, str]
    cols: int = 80
    rows: int = 24
    status: str = "running"
    exit_code: int | None = None
    exit_signal: str | None = None
    created_at: str = field(default_factory=stamp)
    exited_at: str | None = None
    output: bytearray = field(default_factory=bytearray)
    writes: list[dict[str, Any]] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    title: str = ""
    preview: list[Any] | None = None
    screen: list[str] | None = None
    """The screen's lines, once a test gives it one; without, ``read_screen`` is unknown to the daemon,
    as it is to a daemon without an emulator."""
    commands: list[dict[str, Any]] | None = None
    """The shell's commands, likewise; without, ``terminal.commands`` says the program reports none."""
    pid: int = field(default_factory=lambda: 1000 + secrets.randbelow(30000))
    sandbox: dict[str, Any] | None = None
    """What ``terminal.create`` was asked to make writable, when it was sandboxed."""

    def info(self, preview_rows: int = 0) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "pid": self.pid, "argv": self.argv, "cwd": self.cwd, "title": self.title, "status": self.status,
            "exit_code": self.exit_code, "exit_signal": self.exit_signal, "created_at": self.created_at, "exited_at": self.exited_at,
            "last_output_at": None, "last_input_at": None, "last_human_input_at": None, "cols": self.cols, "rows": self.rows,
            "size_owner": None, "clients": [], "last_detach_at": None, "keyboard": {"owner": "auto", "until": None},
            "modes": {"alt_screen": False, "bracketed_paste": False, "mouse": False, "app_cursor": False}, "busy": False,
            "last_command": None, "labels": self.labels, "launch_id": "", "sandbox": self.sandbox is not None, "output_seq": len(self.output),
        }
        if preview_rows and self.preview is not None:
            out["preview"] = self.preview[-preview_rows:]
        return out


@dataclass
class FakeChannel:
    """An attachment the host opened: what it sent, in order, and whether it closed it."""

    id: int
    terminal_id: str
    client: dict[str, Any]
    writer: asyncio.StreamWriter
    received: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    closed_by_host: asyncio.Event = field(default_factory=asyncio.Event)
    closed_by_daemon: bool = False

    async def frame(self, timeout: float = 5.0) -> bytes:
        return await asyncio.wait_for(self.received.get(), timeout)


class FakePtyd:
    def __init__(self, run_dir: Path, *, env: str = "container", protocol: int = 1) -> None:
        self.run_dir = run_dir
        self.env = env
        self.protocol = protocol
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.terminals: dict[str, FakeTerminal] = {}
        self.events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, tuple[int, str]] = {}
        """Method → the error its next call answers with, once."""
        self.machine: dict[str, Any] = {"mem_total_bytes": 16 << 30, "mem_available_bytes": 8 << 30, "cpus": 8, "cpu_percent": 10.0, "load1": 1.0, "load5": 1.0, "load15": 1.0}
        # ptyd's own process, which holds every emulator: a real daemon reports it with each sample.
        self.daemon: dict[str, Any] = {"pid": 1, "rss_bytes": 30 << 20, "cpu_percent": 0.5}
        self.rss: dict[str, int] = {}
        self.home = "/root"
        self.sandbox = "not available in this build"
        """The sandbox capability: ``ok`` makes ``terminal.create {sandbox}`` succeed, leaving read-only
        every writable folder that does not exist here, as the real daemon does."""
        # Side channels: scripted programs, real files under the roots the host sets, echoing byte
        # streams, and launches whose hook posts a test makes with ``post_hook``.
        self.exec_allow = {"claude", "codex", "opencode", "pi", "grok", "cursor-agent", "npm", "npx", "node", "git", "uname", "bash"}
        self.exec_results: dict[str, dict[str, Any]] = {}
        """Program basename → the exec.run result it gives; unknown ones exit 0 with no output."""
        self.roots: list[str] = []
        self.made: list[str] = []
        """Every folder ``fs.mkdir`` was asked for."""
        self.launches: dict[str, dict[str, Any]] = {}
        self.pending_replies: dict[str, str] = {}
        """reply_id → launch_id of a held post that waits."""
        self.replies: list[dict[str, Any]] = []
        self.dials: dict[int, str] = {}
        """Channel → target of an open stream; what the host writes on one is echoed back."""
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: list[tuple[asyncio.StreamWriter, asyncio.Queue[dict[str, Any] | None]]] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.channels: dict[int, FakeChannel] = {}
        self.attached: asyncio.Queue[FakeChannel] = asyncio.Queue()
        """Every attachment as it is opened, for a test to wait on."""
        self._next_channel = 0

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> FakePtyd:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        sock = self.run_dir / "ptyd.sock"
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(sock), limit=(1 << 20) + 64)
        (self.run_dir / "token").write_text(self.token + "\n")
        (self.run_dir / "endpoint").write_text("unix:ptyd.sock")
        return self

    async def stop(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.run_dir / "endpoint").unlink()
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        self._server = None
        self._subscribers.clear()

    async def restart(self) -> None:
        """A new life: new instance, new token, no terminals, an empty event log."""
        await self.stop()
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.terminals.clear()
        self.events.clear()
        await self.start()

    # -- scripting --------------------------------------------------------------------------

    def emit(self, kind: str, terminal_id: str | None, data: dict[str, Any], *, at: str | None = None) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "at": at or stamp(), "type": kind, "data": data}
        if terminal_id:
            event["terminal_id"] = terminal_id
        self.events.append(event)
        for _, queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def exit(self, terminal_id: str, code: int = 0, signal: str | None = None) -> None:
        term = self.terminals[terminal_id]
        term.status, term.exit_code, term.exit_signal, term.exited_at = "exited", code, signal, stamp()
        self.emit("terminal.exited", terminal_id, {"exit_code": code, "signal": signal, "seq": len(term.output)})

    def post_hook(self, launch_id: str, name: str, body: Any, *, hold_ms: int = 0) -> dict[str, Any]:
        """A CLI of the launch posting a hook; with ``hold_ms`` the event carries a reply id."""
        launch = self.launches[launch_id]
        data: dict[str, Any] = {"launch_id": launch_id, "terminal_id": launch["terminal_id"], "name": name, "body": body, "size": len(json.dumps(body))}
        if hold_ms:
            reply_id = "r" + secrets.token_hex(8)
            self.pending_replies[reply_id] = launch_id
            data["reply_id"], data["hold_ms"] = reply_id, hold_ms
        return self.emit("hook", launch["terminal_id"] or None, data)

    def end_launch(self, launch_id: str, reason: str = "terminal_exited") -> None:
        launch = self.launches.pop(launch_id)
        self.pending_replies = {r: lid for r, lid in self.pending_replies.items() if lid != launch_id}
        self.emit("launch.ended", launch["terminal_id"] or None, {"launch_id": launch_id, "reason": reason})

    def spawn(self, terminal_id: str, *, labels: dict[str, str] | None = None, cwd: str = "/tmp") -> FakeTerminal:
        """A terminal the host did not ask for (or whose row it lost)."""
        term = self.terminals[terminal_id] = FakeTerminal(terminal_id, ["/bin/bash", "-l"], cwd, labels or {})
        self.emit("terminal.created", terminal_id, {"pid": term.pid, "argv": term.argv, "cwd": cwd, "labels": term.labels, "launch_id": ""})
        return term

    # -- the protocol -----------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        queue: asyncio.Queue[dict[str, Any] | None] | None = None
        pump: asyncio.Task[None] | None = None
        try:
            channel, payload = await self._read(reader)
            if channel != 0 or payload.strip() != self.token.encode():
                await asyncio.sleep(0.05)
                return
            await self._send(writer, {"jsonrpc": "2.0", "method": "hello", "params": {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env}})
            while True:
                channel, payload = await self._read(reader)
                if channel != 0:
                    if channel in self.dials:
                        if not payload:
                            del self.dials[channel]
                        await self._frame(writer, channel, payload)  # an echo, or the answering close
                        continue
                    await self._channel_frame(writer, channel, payload)
                    continue
                message = json.loads(payload)
                method, params = message.get("method"), message.get("params") or {}
                self.calls.append((method, params))
                if method == "terminal.attach" and method not in self.fail:
                    await self._attach(writer, message["id"], params)
                    continue
                if method == "events.subscribe":
                    after = int(params.get("after_seq") or 0)
                    resync = after > len(self.events)
                    start = 0 if resync else after
                    queue = asyncio.Queue()
                    for event in self.events[start:]:
                        queue.put_nowait(event)
                    self._subscribers.append((writer, queue))
                    await self._send(writer, {"jsonrpc": "2.0", "id": message["id"], "result": {"instance": self.instance, "from_seq": start + 1, "resync": resync}})
                    pump = asyncio.create_task(self._pump(writer, queue))
                    self._tasks.add(pump)
                    continue
                try:
                    result = self._handle(method, params)
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                except _RpcFail as exc:
                    reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": exc.code, "message": exc.message}}
                await self._send(writer, reply)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if pump is not None:
                pump.cancel()
            self._subscribers = [(w, q) for w, q in self._subscribers if w is not writer]
            self._writers.discard(writer)
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _attach(self, writer: asyncio.StreamWriter, call_id: int, params: dict[str, Any]) -> None:
        term = self.terminals.get(str(params.get("id")))
        if term is None:
            await self._send(writer, {"jsonrpc": "2.0", "id": call_id, "error": {"code": 1001, "message": "no such terminal"}})
            return
        self._next_channel += 1
        channel = FakeChannel(self._next_channel, term.id, dict(params.get("client") or {}), writer)
        self.channels[channel.id] = channel
        await self._send(writer, {"jsonrpc": "2.0", "id": call_id, "result": {"channel": channel.id, "client_id": f"c{channel.id}"}})
        self.attached.put_nowait(channel)

    async def _channel_frame(self, writer: asyncio.StreamWriter, channel_id: int, payload: bytes) -> None:
        channel = self.channels.get(channel_id)
        if channel is None or channel.writer is not writer:
            return
        if payload:
            channel.received.put_nowait(payload)
            return
        channel.closed_by_host.set()
        if not channel.closed_by_daemon:
            channel.closed_by_daemon = True
            await self._frame(writer, channel_id, b"")  # a close is answered with a close

    async def send_channel(self, channel_id: int, payload: bytes) -> None:
        """A frame from the terminal to the attached browser."""
        await self._frame(self.channels[channel_id].writer, channel_id, payload)

    async def close_channel(self, channel_id: int) -> None:
        """The daemon lets go of the client (a detach, or the terminal forgotten)."""
        channel = self.channels[channel_id]
        if not channel.closed_by_daemon:
            channel.closed_by_daemon = True
            await self._frame(channel.writer, channel_id, b"")

    async def _frame(self, writer: asyncio.StreamWriter, channel_id: int, payload: bytes) -> None:
        writer.write(struct.pack(">II", 4 + len(payload), channel_id) + payload)
        await writer.drain()

    async def _pump(self, writer: asyncio.StreamWriter, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        with contextlib.suppress(ConnectionError, asyncio.CancelledError):
            while (event := await queue.get()) is not None:
                await self._send(writer, {"jsonrpc": "2.0", "method": "event", "params": event})

    async def _read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        length, channel = struct.unpack(">II", await reader.readexactly(8))
        return channel, await reader.readexactly(length - 4)

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode()
        writer.write(struct.pack(">II", 4 + len(payload), 0) + payload)
        await writer.drain()

    def _term(self, params: dict[str, Any]) -> FakeTerminal:
        term = self.terminals.get(str(params.get("id")))
        if term is None:
            raise _RpcFail(1001, "no such terminal")
        return term

    def _handle(self, method: str, params: dict[str, Any]) -> Any:
        if method in self.fail:
            code, text = self.fail.pop(method)
            raise _RpcFail(code, text)
        if method == "daemon.info":
            running = sum(1 for t in self.terminals.values() if t.status == "running")
            return {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env, "home": self.home, "shell": "/bin/bash",
                    "capabilities": {"sandbox": self.sandbox, "emulator": "fake@1", "stats": "ok",
                                     "inherited_env_selection": getattr(self, "inheritance_selection", True)}, "hooks": {"listen": ""},
                    "side_channels": {"exec_allow": sorted(self.exec_allow), "fs_roots": self.roots, "state_dir": f"{self.run_dir}-state"},
                    "counts": {"running": running, "exited": len(self.terminals) - running}, "machine": self.machine}
        if method == "events.unsubscribe":
            return {}
        if method == "terminal.create":
            box = params.get("sandbox")
            if box is not None and self.sandbox != "ok":
                raise _RpcFail(1007, f"the sandbox is not available: {self.sandbox}")
            if params["id"] in self.terminals:
                raise _RpcFail(1004, "id in use")
            cwd = params.get("cwd") or self.home
            fallback = not Path(cwd).is_dir()
            term = FakeTerminal(params["id"], params.get("argv") or ["/bin/bash", "-l"], self.home if fallback else cwd, dict(params.get("labels") or {}), params.get("cols") or 80, params.get("rows") or 24, title=params.get("title") or "")
            reply: dict[str, Any] = {}
            if box is not None:
                writable = [p for p in box.get("writable") or [] if Path(p).is_dir()]
                skipped = [{"path": p, "reason": "missing"} for p in box.get("writable") or [] if p not in writable]
                term.sandbox = {"writable": writable, "skipped": skipped}
                reply["sandbox"] = term.sandbox
            self.terminals[term.id] = term
            if params.get("launch_id") and params["launch_id"] not in self.launches:
                del self.terminals[term.id]
                raise _RpcFail(1008, "launch is not registered or has ended")
            self.emit("terminal.created", term.id, {"pid": term.pid, "argv": term.argv, "cwd": term.cwd, "labels": term.labels, "launch_id": params.get("launch_id") or ""})
            integration = "" if params.get("argv") else "bash"
            return {"id": term.id, "pid": term.pid, "cwd": term.cwd, "cwd_fallback": fallback, "shell": "/bin/bash", "shell_integration": integration, "created_at": term.created_at, **reply}
        if method == "terminal.list":
            ids = params.get("ids")
            return {"terminals": [t.info(int(params.get("preview_rows") or 0)) for t in self.terminals.values() if not ids or t.id in ids]}
        if method == "terminal.get":
            return self._term(params).info()
        if method == "terminal.write":
            term = self._term(params)
            if term.status != "running":
                raise _RpcFail(1002, "exited")
            data = params.get("text") or params.get("paste") or " ".join(params.get("keys") or [])
            term.writes.append(params)
            before = len(term.output)
            return {"bytes": len(str(data).encode()), "seq_before": before, "queued_ms": 0, "delivered_at": stamp()}
        if method == "terminal.resize":
            term = self._term(params)
            term.cols, term.rows = params["cols"], params["rows"]
            return {"cols": term.cols, "rows": term.rows}
        if method == "terminal.signal":
            self._term(params).signals.append(params["signal"])
            return {}
        if method == "terminal.kill":
            term = self._term(params)
            if term.status == "running":
                self.exit(term.id, -1, "SIGHUP")
            return {"exit_code": term.exit_code, "signal": term.exit_signal}
        if method == "terminal.forget":
            term = self._term(params)
            if term.status == "running":
                raise _RpcFail(1004, "still running")
            del self.terminals[term.id]
            return {}
        if method == "terminal.read_output":
            term = self._term(params)
            since = int(params.get("since_seq") or 0)
            chunk = bytes(term.output[since:])
            out = {"from_seq": since, "to_seq": len(term.output), "head_seq": len(term.output), "gap": False}
            if params.get("strip"):
                out["data"] = chunk.decode("utf-8", "replace")
            else:
                out["data_b64"] = base64.b64encode(chunk).decode()
            return out
        if method == "terminal.read_screen" and self._term(params).screen is not None:
            term = self._term(params)
            assert term.screen is not None
            return {"cols": term.cols, "rows": term.rows, "cursor": {"x": 0, "y": len(term.screen) - 1, "visible": True, "abs_row": len(term.screen) - 1},
                    "alt_screen": False, "title": term.title, "cwd": term.cwd, "seq": len(term.output), "lines": list(term.screen)}
        if method == "terminal.commands":
            term = self._term(params)
            if term.commands is None:
                raise _RpcFail(1007, "this terminal's program reports no commands: it is not a shell with integration")
            return {"commands": list(term.commands)[-int(params.get("last") or 20):], "busy": False, "shell_integration": "bash"}
        if method == "terminal.stats":
            running = [t for t in self.terminals.values() if t.status == "running"]
            return {"at": stamp(), "supported": True, "machine": self.machine, "daemon": dict(self.daemon),
                    "terminals": [{"id": t.id, "pid": t.pid, "processes": 1, "rss_bytes": self.rss.get(t.id, 50 << 20), "cpu_percent": 2.0} for t in running]}
        side = self._side(method, params)
        if side is not None:
            return side
        raise _RpcFail(-32601, f"method not found: {method}")

    def _under_root(self, path: str) -> Path:
        target = Path(path)
        if not target.is_absolute():
            raise _RpcFail(-32602, "the path must be absolute")
        real = target.resolve()
        if not any(real == Path(r) or Path(r) in real.parents for r in self.roots):
            raise _RpcFail(1004, f"{path} is not under an allowed root")
        return real

    def _as_root(self, path: str) -> Path:
        """The daemon's rule for a folder about to become a root: absolute, and neither the
        filesystem's root nor a folder that holds the home directory."""
        target = Path(path)
        if not target.is_absolute():
            raise _RpcFail(-32602, "the path must be absolute")
        real = target.resolve()
        if real == Path("/") or real == Path(self.home) or real in Path(self.home).parents:
            raise _RpcFail(1004, f"{path} cannot be a folder of a project")
        return real

    def _side(self, method: str, params: dict[str, Any]) -> Any:
        if method == "exec.run":
            name = Path(params["argv"][0]).name
            if name not in self.exec_allow:
                raise _RpcFail(1004, f"{name} is not among the programs exec.run may run")
            result = {"exit_code": 0, "signal": "", "stdout": "", "stderr": "", "truncated": False, "timed_out": False, "duration_ms": 1}
            return {**result, **self.exec_results.get(name, {})}
        if method == "fs.set_roots":
            self.roots = [r for r in params.get("roots") or [] if r != "/"]
            return {"roots": self.roots, "accepted": self.roots, "refused": [{"root": "/", "reason": "is the root of the filesystem"}] if "/" in (params.get("roots") or []) else []}
        if method == "fs.stat" and params.get("as_root"):
            real = self._as_root(params["path"])
            if not real.exists():
                return {"exists": False, "size": 0}
            return {"exists": True, "type": "dir" if real.is_dir() else "file", "size": 0, "mtime": stamp(), "mode": "0755", "writable": os.access(real, os.W_OK)}
        if method == "fs.mkdir":
            real = self._as_root(params["path"])
            if real.exists() and not real.is_dir():
                raise _RpcFail(-32602, f"{params['path']} exists and is not a directory")
            created = not real.exists()
            real.mkdir(parents=True, exist_ok=True)
            self.made.append(str(real))
            return {"exists": True, "type": "dir", "size": 0, "mtime": stamp(), "mode": "0755", "writable": True, "created": created}
        if method == "fs.write":
            return write_inbox(self._under_root, params)
        if method == "fs.stat":
            real = self._under_root(params["path"])
            if not real.exists():
                return {"exists": False, "size": 0}
            st = real.stat()
            return {"exists": True, "type": "dir" if real.is_dir() else "file", "size": st.st_size, "mtime": stamp(), "mode": "0600", "file_id": f"{st.st_dev}:{st.st_ino}"}
        if method == "fs.list":
            real = self._under_root(params["path"])
            names = sorted(p.name for p in real.iterdir())
            return {"entries": [{"name": n, "type": "file", "size": 0, "mtime": stamp()} for n in names], "truncated": False}
        if method in ("fs.read", "fs.tail"):
            real = self._under_root(params["path"])
            if not real.is_file():
                raise _RpcFail(1001, f"no file {params['path']}")
            data = real.read_bytes()
            st = real.stat()
            file_id = f"{st.st_dev}:{st.st_ino}"
            if method == "fs.read":
                offset = int(params.get("offset") or 0)
                chunk = data[offset : offset + int(params.get("max") or 65536)]
                return {"data_b64": base64.b64encode(chunk).decode(), "offset": offset, "size": len(data), "eof": offset + len(chunk) >= len(data), "file_id": file_id}
            start = int(params.get("from_offset") or 0)
            rotated = start > len(data) or bool(params.get("file_id") and params["file_id"] != file_id)
            if rotated:
                start = 0
            chunk = data[start : start + int(params.get("max") or 65536)]
            return {"data_b64": base64.b64encode(chunk).decode(), "next_offset": start + len(chunk), "size": len(data), "rotated": rotated, "file_id": file_id}
        if method == "hooks.register_launch":
            launch_id = params.get("launch_id") or "l" + secrets.token_hex(8)
            if launch_id in self.launches:
                raise _RpcFail(1004, "a launch with this id exists")
            files = {name: base64.b64decode(data) for name, data in (params.get("files") or {}).items()}
            token = secrets.token_hex(32)
            directory = f"/state/launches/{launch_id}"
            self.launches[launch_id] = {"terminal_id": params.get("terminal_id") or "", "files": files, "ports": set(params.get("ports") or []), "token": token}
            env = {"DAEDALUS_LAUNCH_ID": launch_id, "DAEDALUS_HOOK_URL": f"http://127.0.0.1:1/hook/{launch_id}", "DAEDALUS_HOOK_TOKEN": token,
                   "DAEDALUS_HOOK_CMD": "/state/bin/hook-post", "DAEDALUS_PTYD_BIN": "/usr/local/bin/ptyd", "DAEDALUS_DIAL_DIR": f"/state/dial/{launch_id}", "DAEDALUS_LAUNCH_DIR": directory}
            return {"launch_id": launch_id, "hook_url": env["DAEDALUS_HOOK_URL"], "hook_token": token, "dir": directory, "dial_dir": env["DAEDALUS_DIAL_DIR"], "env": env, "files": sorted(files)}
        if method == "hooks.put_file":
            launch = self.launches.get(params["launch_id"])
            if launch is None:
                raise _RpcFail(1008, "no such launch")
            name = str(params["name"])
            if "/" in name or name in ("", ".", "..") or (name in launch["files"] and not params.get("replace")):
                raise _RpcFail(-32602, f"no file may be written as {name!r}")
            launch["files"][name] = base64.b64decode(params.get("data") or "")
            return {"path": f"/state/launches/{params['launch_id']}/{name}"}
        if method == "hooks.unregister_launch":
            removed = params["launch_id"] in self.launches
            if removed:
                self.end_launch(params["launch_id"], "unregistered")
            return {"removed": removed}
        if method == "hooks.reply":
            launch_id = self.pending_replies.get(params["reply_id"])
            if launch_id is None or (params.get("launch_id") and params["launch_id"] != launch_id):
                raise _RpcFail(1001, "no request is waiting for this reply")
            del self.pending_replies[params["reply_id"]]
            self.replies.append(params)
            return {"delivered": True}
        if method == "net.allow":
            if params["launch_id"] not in self.launches:
                raise _RpcFail(1008, "no such launch")
            self.launches[params["launch_id"]]["ports"].add(int(params["port"]))
            return {}
        if method == "net.dial":
            launch = self.launches.get(params["launch_id"])
            if launch is None:
                raise _RpcFail(1008, "no such launch")
            target = str(params["target"])
            if target.startswith("tcp:") and int(target.rpartition(":")[2]) not in launch["ports"]:
                raise _RpcFail(1004, "the port is not registered for this launch")
            self._next_channel += 1
            self.dials[self._next_channel] = target
            return {"channel": self._next_channel}
        return None


class _RpcFail(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def inbox_target(path: str) -> bool:
    """The daemon's rule for ``fs.write``: below the last ``.agents`` of the path, a file of ``inbox/<…>/``."""
    parts = path.split("/")
    last = max((i for i, part in enumerate(parts) if part == ".agents"), default=-1)
    if last < 1:
        return False
    rest = parts[last + 1 :]
    return len(rest) >= 2 and rest[0] == "inbox" and bool(rest[-1])


def write_inbox(real_root_check: Any, params: dict[str, Any]) -> dict[str, Any]:
    """``fs.write`` as the daemon does it, over this machine's files: the inbox rule, the roots
    (``real_root_check`` raises for a path outside them), exclusive creation, continuation by offset."""
    path = str(params["path"])
    if not path.startswith("/"):
        raise _RpcFail(-32602, "the path must be absolute")
    clean = os.path.normpath(path)
    if not inbox_target(clean):
        raise _RpcFail(1004, f"{clean} is not in a staff inbox (<folder>/.agents/inbox/...)")
    data = base64.b64decode(params.get("data_b64") or "")
    offset = int(params.get("offset") or 0)
    if len(data) > 512 << 10 or offset < 0 or offset + len(data) > 50 << 20:
        raise _RpcFail(-32602, "too much for one write or one file")
    parent = Path(clean).parent
    real_root_check(str(parent))
    parent.mkdir(parents=True, exist_ok=True)
    target = parent.resolve() / Path(clean).name
    real_root_check(str(target.parent))
    if not inbox_target(str(target)):
        raise _RpcFail(1004, f"{clean} resolves outside a staff inbox")
    if offset == 0:
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            raise _RpcFail(1004, f"forbidden: the file exists: {clean}") from None
    else:
        fd = os.open(target, os.O_WRONLY | os.O_NOFOLLOW)
        if os.fstat(fd).st_size != offset:
            os.close(fd)
            raise _RpcFail(-32602, f"{clean} does not hold {offset} bytes")
    try:
        os.pwrite(fd, data, offset)
    finally:
        os.close(fd)
    return {"size": offset + len(data), "created": offset == 0}


__all__ = ["FakeChannel", "FakePtyd", "FakeTerminal"]
