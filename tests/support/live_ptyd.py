"""The terminal daemon's test double with real processes in it.

``FakePtyd`` scripts its terminals; ``LivePtyd`` runs them. It speaks the same socket protocol (the
framing, the handshake, JSON-RPC, event subscription) and adds what an adapter test needs from a
daemon that actually hosts a CLI:

- ``terminal.create`` spawns the argv in a pseudo-terminal, in a session and process group of its
  own, with the environment the daemon's contract gives a program (the inherited environment
  filtered, the terminal settings, the launch's variables, the caller's on top). The output goes to
  a ring addressed by absolute offset and into a ``FakeScreen``; mode and title changes become
  events, and the program's exit becomes ``terminal.exited``.
- ``terminal.write`` with the daemon's payloads (``text``, ``paste`` — bracketed only when the
  program turned mode 2004 on, line feeds as carriage returns otherwise — ``keys`` and
  ``bytes_b64``) and its arbitration: an agent write waits until the human has been quiet for
  ``input_idle_ms`` and holds no keyboard grant, and fails with 1006 when a human holds it past the
  timeout. Every write is kept with its origin (``writes`` on the terminal). ``type_as_human`` is
  the human's side.
- ``terminal.read_screen`` (text), ``terminal.wait_for`` (regex on the screen or the output, idle,
  exit), ``terminal.read_output``, ``terminal.keyboard``, ``terminal.resize``, ``terminal.signal``
  and ``terminal.kill`` (hang-up, grace, kill, to the whole group).
- The side channels: ``exec.run`` (a program of the allowlist, not in a terminal, killed as a group
  on timeout), ``fs.set_roots``/``stat``/``list``/``read``/``tail`` over real files under the roots
  and never on the deny list, ``fs.browse`` of folders anywhere but the denied and the daemon's own, ``net.allow``/``net.dial`` to a unix socket in the launch's dial
  directory or a registered loopback port, relayed over a channel, and ``hooks.register_launch``
  (file names of a few plain parts)/``put_file``/``unregister_launch``/``reply`` with a real hook listener on ``127.0.0.1:0``: token checked,
  unknown launch 410, a post held for ``wait_ms`` (or ``daedalus_hold_ms`` in the body) until the
  host replies or the hold expires with an empty 204, published as a ``hook`` event in the one
  event order.

The shapes follow ``docs/architecture/terminals.md`` and the side-channel contract of the terminals
plan. Browser attachments are not streamed here: ``FakePtyd``'s scripted channels serve the gateway's
tests, and an adapter never attaches. Processes are started without ``fork`` from this (threaded) test process; the fake CLIs take
their terminal as controlling terminal themselves, and a resize signals the process group directly.
Everything is killed on ``stop``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import fnmatch
import json
import os
import re
import secrets
import shutil
import signal as signals
import stat
import struct
import termios
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.support.fake_ptyd import FakePtyd, FakeTerminal, _RpcFail, browse_folders, stamp, write_inbox
from tests.support.fake_screen import FakeScreen

RING_BYTES = 8 << 20
READ_RAW_MAX = 512 << 10
READ_TEXT_MAX = 768 << 10
HOOK_BODY_MAX = 1 << 20
HOOK_HOLD_MAX_MS = 600_000
EXEC_ALLOW = frozenset({"claude", "codex", "opencode", "pi", "grok", "cursor-agent", "npm", "npx", "node", "git", "uname", "bash"})
FS_DENY = (
    "**/.claude/.credentials.json", "**/.codex/auth.json", "**/.grok/auth.json", "**/.local/share/opencode/auth.json",
    "**/.pi/agent/auth.json", "**/.ssh/**", "**/.gnupg/**", "**/.config/gh/hosts.yml", "**/.netrc", "**/.git-credentials",
    "**/.docker/config.json",
)
STRIP = ("DAEDALUS_PTYD_", "DAEDALUS_TERMINAL_ID", "DAEDALUS_LAUNCH_", "DAEDALUS_HOOK_", "DAEDALUS_DIAL_DIR", "TERM_PROGRAM", "VSCODE_",
         "TMUX", "STY", "WINDOW", "KITTY_", "ITERM_", "WT_SESSION", "CLAUDE")
KEEP = frozenset({"CLAUDE_CONFIG_DIR"})
"""Kept although it starts with ``CLAUDE``: where a host's Claude keeps its sign-in. The contract
says so; the real daemon does not do it yet."""
PLAIN_KEYS = {
    "Enter": "\r", "Tab": "\t", "S-Tab": "\x1b[Z", "Esc": "\x1b", "Backspace": "\x7f", "PgUp": "\x1b[5~", "PgDn": "\x1b[6~",
    "Delete": "\x1b[3~", "Insert": "\x1b[2~",
}
CURSOR_KEYS = {"Up": "A", "Down": "B", "Right": "C", "Left": "D", "Home": "H", "End": "F"}
_ESCAPES = re.compile(rb"\x1b\[[0-9;?<=>]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_X][^\x1b]*\x1b\\|\x1b[()*+].|\x1b[@-Z\\-_]")


def encode_keys(keys: list[str], app_cursor: bool) -> bytes:
    out = bytearray()
    for key in keys:
        out += encode_key(key, app_cursor)
    return bytes(out)


def encode_key(key: str, app_cursor: bool) -> bytes:
    if key in PLAIN_KEYS:
        return PLAIN_KEYS[key].encode()
    if key in CURSOR_KEYS:
        return ("\x1bO" if app_cursor else "\x1b[").encode() + CURSOR_KEYS[key].encode()
    if key.startswith("C-") and len(key) == 3:
        c = key[2]
        if "a" <= c <= "z":
            return bytes([ord(c) - ord("a") + 1])
        if "@" <= c <= "_":
            return bytes([ord(c) - ord("@")])
    if key.startswith("M-") and len(key) > 2:
        inner = key[2:]
        return b"\x1b" + (encode_key(inner, app_cursor) if len(inner) > 1 else inner.encode())
    if re.fullmatch(r"F([1-9]|1[0-2])", key):
        n = int(key[1:])
        return {1: b"\x1bOP", 2: b"\x1bOQ", 3: b"\x1bOR", 4: b"\x1bOS"}.get(n) or f"\x1b[{[15, 17, 18, 19, 20, 21, 23, 24][n - 5]}~".encode()
    raise _RpcFail(-32602, f"unknown key {key!r}")


def strip_escapes(data: bytes) -> str:
    text = _ESCAPES.sub(b"", data).decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def paste_bytes(text: str, bracketed: bool) -> bytes:
    if bracketed:
        clean = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
        return b"\x1b[200~" + clean.encode() + b"\x1b[201~"
    return text.replace("\r\n", "\r").replace("\n", "\r").encode()


def _default_signals() -> None:
    """In the child, before exec: the hang-up and the interrupt back to their default action.

    What the real daemon does with `DefaultSignalsForChildren`, and the double did not: a signal
    ignored in the process that starts a program stays ignored across exec. A test run started under
    `nohup` (or from a script's background) handed an ignored SIGHUP to every terminal it opened, so
    the hang-up that ends a terminal ended nothing, the grace ran out, and the kill reported SIGKILL.
    It looked like a flake under load because such runs were the long, backgrounded ones."""
    for number in (signals.SIGHUP, signals.SIGINT):
        signals.signal(number, signals.SIG_DFL)


@dataclass
class LiveTerminal(FakeTerminal):
    master: int = -1
    process: asyncio.subprocess.Process | None = None
    screen: FakeScreen = field(default_factory=FakeScreen)
    base: int = 0
    """Offset of the first byte still in ``output``: the ring drops from the front."""
    env: dict[str, str] = field(default_factory=dict)
    launch_id: str = ""
    input_idle_ms: int = 10_000
    last_output: float = 0.0
    last_human: float = 0.0
    keyboard_owner: str = "auto"
    keyboard_until: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    modes_seen: int = 0
    title_seen: str = ""
    last_output_at: str | None = None
    last_input_at: str | None = None
    last_human_input_at: str | None = None

    @property
    def head(self) -> int:
        return self.base + len(self.output)

    def pulse(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def human_holds(self) -> bool:
        now = time.monotonic()
        if self.keyboard_until is not None and now >= self.keyboard_until:
            self.keyboard_owner, self.keyboard_until = "auto", None
        if self.keyboard_owner == "human":
            return True
        if self.keyboard_owner == "agent":
            return False
        return bool(self.last_human) and (now - self.last_human) * 1000 < self.input_idle_ms

    def info(self, preview_rows: int = 0) -> dict[str, Any]:
        out = super().info(preview_rows)
        modes = self.screen.modes
        until = None
        if self.keyboard_until is not None:
            until = int((time.time() + self.keyboard_until - time.monotonic()) * 1000)
        out.update({
            "modes": {"alt_screen": modes["alt_screen"], "bracketed_paste": modes["bracketed_paste"], "mouse": False, "app_cursor": modes["app_cursor"]},
            "keyboard": {"owner": self.keyboard_owner, "until": until}, "launch_id": self.launch_id, "output_seq": self.head,
            "last_output_at": self.last_output_at, "last_input_at": self.last_input_at, "last_human_input_at": self.last_human_input_at,
            "title": self.screen.title or self.title,
        })
        if preview_rows:
            out["preview"] = self.screen.lines()[-preview_rows:]
        return out


@dataclass
class LiveLaunch:
    launch_id: str
    token: str
    dir: Path
    dial_dir: Path
    terminal_id: str
    hold_max_ms: int
    ports: set[int] = field(default_factory=set)
    held: dict[str, asyncio.Future[tuple[int, bytes, str]]] = field(default_factory=dict)
    streams: set[int] = field(default_factory=set)
    posts: list[dict[str, Any]] = field(default_factory=list)
    """Every post the listener accepted: ``{name, body, status}``; for tests that look from the CLI's side."""


class LivePtyd(FakePtyd):
    def __init__(
        self,
        run_dir: Path,
        *,
        home: Path,
        bin_dir: Path | None = None,
        env: str = "container",
        base_env: dict[str, str] | None = None,
        input_idle_ms: int = 10_000,
        launch_grace_s: float = 30.0,
        exec_allow: set[str] | None = None,
    ) -> None:
        super().__init__(run_dir, env=env)
        self.home = str(home)
        self.bin_dir = bin_dir
        self.state_dir = run_dir / "state"
        self.input_idle_ms = input_idle_ms
        self.launch_grace_s = launch_grace_s
        self.exec_allow = set(exec_allow or EXEC_ALLOW)
        path = f"{bin_dir}{os.pathsep}/usr/local/bin:/usr/bin:/bin" if bin_dir else "/usr/local/bin:/usr/bin:/bin"
        self.base_env: dict[str, str] = {"PATH": path, "HOME": self.home, "LANG": "C.UTF-8", "USER": "operator", "SHELL": "/bin/sh", **(base_env or {})}
        """What the daemon inherited, before the contract's filtering. Deliberately not this test
        process's environment: that would hand the fakes whatever the machine running the suite has."""
        self.roots: list[str] = []
        self.launches: dict[str, LiveLaunch] = {}
        self.ended_launches: set[str] = set()
        self.replies: list[dict[str, Any]] = []
        self.agent_writes: list[dict[str, Any]] = []
        self.hook_port = 0
        self._hook_server: asyncio.AbstractServer | None = None
        self._streams: dict[int, tuple[asyncio.StreamWriter, asyncio.StreamWriter, str]] = {}
        """channel → (the socket to the program, the host connection it belongs to, launch id)."""
        self._next_channel = 0
        self._send_locks: dict[asyncio.StreamWriter, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self.follow_links = True
        """A link in a dial directory is followed to a private socket of this user, as the daemon
        does; false is the daemon before it did, which refused every link as "not a socket"."""

    # -- lifecycle --------------------------------------------------------------------------------

    async def start(self) -> LivePtyd:
        await super().start()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("launches", "dial"):
            (self.state_dir / sub).mkdir(mode=0o700, exist_ok=True)
        if self._hook_server is None:
            self._hook_server = await asyncio.start_server(self._hook_http, "127.0.0.1", 0, limit=HOOK_BODY_MAX + 65536)
            self.hook_port = self._hook_server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        await self.kill_all()
        for launch_id in list(self.launches):
            self._end_launch(launch_id, "daemon_stopped")
        for sock, _, _ in list(self._streams.values()):
            sock.close()
        self._streams.clear()
        if self._hook_server is not None:
            self._hook_server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._hook_server.wait_closed(), 2)
            self._hook_server = None
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await super().stop()

    async def restart(self) -> None:
        await self.kill_all()
        for launch_id in list(self.launches):
            self._end_launch(launch_id, "daemon_stopped")
        await super().restart()

    async def kill_all(self) -> None:
        for term in list(self.terminals.values()):
            if isinstance(term, LiveTerminal) and term.status == "running":
                await self._kill(term, grace_ms=500)

    def _bg(self, awaitable: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # -- the connection -----------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        self._send_locks[writer] = asyncio.Lock()
        queue: asyncio.Queue[dict[str, Any] | None] | None = None
        pump: asyncio.Task[None] | None = None
        requests: set[asyncio.Task[None]] = set()
        try:
            channel, payload = await self._read(reader)
            if channel != 0 or payload.strip() != self.token.encode():
                await asyncio.sleep(0.05)
                return
            await self._send(writer, {"jsonrpc": "2.0", "method": "hello", "params": {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env}})
            while True:
                channel, payload = await self._read(reader)
                if channel != 0:
                    await self._stream_frame(channel, payload)
                    continue
                message = json.loads(payload)
                method, params = message.get("method"), message.get("params") or {}
                self.calls.append((method, params))
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
                # Requests run concurrently, as the daemon's do: a long poll or a write that waits
                # for the keyboard must not hold up the next call on the same connection.
                request = asyncio.create_task(self._answer(writer, message, method, params))
                requests.add(request)
                request.add_done_callback(requests.discard)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for request in list(requests):
                request.cancel()
            if pump is not None:
                pump.cancel()
            for channel_id, (sock, owner, _) in list(self._streams.items()):
                if owner is writer:
                    sock.close()
                    del self._streams[channel_id]
            self._subscribers = [(w, q) for w, q in self._subscribers if w is not writer]
            self._writers.discard(writer)
            self._send_locks.pop(writer, None)
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _answer(self, writer: asyncio.StreamWriter, message: dict[str, Any], method: str, params: dict[str, Any]) -> None:
        try:
            result = await self._live(method, params, writer)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except _RpcFail as exc:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": exc.code, "message": exc.message}}
        except (KeyError, TypeError, ValueError) as exc:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": f"invalid params: {exc}"}}
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self._send(writer, reply)

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        await self._frame(writer, 0, json.dumps(message).encode())

    async def _frame(self, writer: asyncio.StreamWriter, channel: int, payload: bytes) -> None:
        lock = self._send_locks.get(writer)
        if lock is None:
            lock = self._send_locks[writer] = asyncio.Lock()
        async with lock:
            writer.write(struct.pack(">II", 4 + len(payload), channel) + payload)
            await writer.drain()

    # -- methods ------------------------------------------------------------------------------------

    async def _live(self, method: str, params: dict[str, Any], writer: asyncio.StreamWriter) -> Any:
        if method in self.fail:
            code, text = self.fail.pop(method)
            raise _RpcFail(code, text)
        if method == "daemon.info":
            info = self._handle(method, params)
            info["hooks"] = {"listen": f"127.0.0.1:{self.hook_port}"}
            info["side_channels"] = {"exec_allow": sorted(self.exec_allow), "fs_roots": self.roots, "state_dir": str(self.state_dir)}
            info["capabilities"]["emulator"] = "fake-screen@1"
            return info
        handler = getattr(self, "_m_" + method.replace(".", "_"), None)
        if handler is not None:
            return await handler(params, writer) if method == "net.dial" else await handler(params)
        return self._handle(method, params)

    def _live_term(self, params: dict[str, Any]) -> LiveTerminal:
        term = self._term(params)
        if not isinstance(term, LiveTerminal):
            raise _RpcFail(1007, "a scripted terminal has no process")
        return term

    async def _m_terminal_create(self, params: dict[str, Any]) -> dict[str, Any]:
        ident = str(params["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            raise _RpcFail(-32602, "the id must be 1-64 of A-Z a-z 0-9 - _")
        if ident in self.terminals:
            raise _RpcFail(1004, "id in use")
        if params.get("sandbox"):
            raise _RpcFail(1007, "the sandbox is not available in this build")
        cols, rows = int(params.get("cols") or 80), int(params.get("rows") or 24)
        if not (20 <= cols <= 500 and 4 <= rows <= 300):
            raise _RpcFail(1009, "outside 20x4 ... 500x300")
        launch_id = str(params.get("launch_id") or "")
        launch = self.launches.get(launch_id) if launch_id else None
        if launch_id and launch is None:
            raise _RpcFail(1008, "launch is not registered or has ended")
        cwd = str(params.get("cwd") or self.home)
        fallback = not Path(cwd).is_dir()
        if fallback:
            cwd = self.home
        env = self._environment(ident, params.get("env") or {}, params.get("strip_env") or [], launch,
                                params.get("inherited_env"))
        argv = list(params.get("argv") or [env.get("SHELL", "/bin/sh"), "-l"])
        program = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
        master, slave = os.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        try:
            process = await asyncio.create_subprocess_exec(
                program, *argv[1:], stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, start_new_session=True, close_fds=True,
                preexec_fn=_default_signals,
            )
        except OSError as exc:
            os.close(master)
            raise _RpcFail(1001, f"cannot start {argv[0]}: {exc}") from None
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        term = LiveTerminal(ident, argv, cwd, dict(params.get("labels") or {}), cols, rows, title=str(params.get("title") or ""),
                            master=master, process=process, screen=FakeScreen(cols, rows), env=env, launch_id=launch_id,
                            input_idle_ms=int(params.get("input_idle_ms") or self.input_idle_ms), pid=process.pid)
        self.terminals[ident] = term
        if launch is not None and not launch.terminal_id:
            launch.terminal_id = ident
        asyncio.get_running_loop().add_reader(master, self._readable, term)
        self._bg(self._supervise(term))
        self.emit("terminal.created", ident, {"pid": term.pid, "argv": argv, "cwd": cwd, "labels": term.labels, "launch_id": launch_id})
        return {"id": ident, "pid": term.pid, "cwd": cwd, "cwd_fallback": fallback, "shell": env.get("SHELL", "/bin/sh"), "created_at": term.created_at}

    def _environment(self, ident: str, extra: dict[str, str], strip: list[str], launch: LiveLaunch | None,
                     inherited: list[str] | None = None) -> dict[str, str]:
        def stripped(name: str) -> bool:
            if name in KEEP:
                return False
            if any(name.startswith(prefix) for prefix in STRIP):
                return True
            return any(name.startswith(p[:-1]) if p.endswith("*") else name == p for p in strip)

        # The fake CLI's controls come from the test rig, not the host daemon; keep that fixture
        # channel while modelling the daemon's selection of ordinary inherited variables.
        env = {k: v for k, v in self.base_env.items()
               if not stripped(k) and (inherited is None or k in inherited or k.startswith("FAKE_"))}
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "CLAUDE_CODE_NO_FLICKER": "1", "CLAUDE_CODE_SCROLL_SPEED": "3", "DAEDALUS_TERMINAL_ID": ident, "HOME": self.home})
        effective = next((env[k] for k in ("LC_ALL", "LC_CTYPE", "LANG") if env.get(k)), "")
        if "utf-8" not in effective.lower() and "utf8" not in effective.lower():
            env.pop("LC_ALL", None)
            env.pop("LC_CTYPE", None)
            env["LANG"] = "C.UTF-8"
        if launch is not None:
            env.update(self._launch_env(launch))
        env.update({str(k): str(v) for k, v in extra.items()})
        return env

    def _readable(self, term: LiveTerminal) -> None:
        try:
            data = os.read(term.master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(term.master)
            return
        term.output += data
        if len(term.output) > RING_BYTES:
            drop = len(term.output) - RING_BYTES
            del term.output[:drop]
            term.base += drop
        term.screen.feed(data)
        term.last_output = time.monotonic()
        term.last_output_at = stamp()
        if term.screen.mode_changes != term.modes_seen:
            term.modes_seen = term.screen.mode_changes
            modes = term.screen.modes
            self.emit("terminal.mode", term.id, {"alt_screen": modes["alt_screen"], "bracketed_paste": modes["bracketed_paste"], "mouse": False, "app_cursor": modes["app_cursor"]})
        if term.screen.title != term.title_seen:
            term.title_seen = term.screen.title
            self.emit("terminal.title", term.id, {"title": term.screen.title, "seq": term.head})
        term.pulse()

    async def _supervise(self, term: LiveTerminal) -> None:
        assert term.process is not None
        code = await term.process.wait()
        # Take what the program wrote last before the terminal is reported exited.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            before = term.head
            self._readable(term)
            if term.head == before:
                break
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(term.master)
        with contextlib.suppress(OSError):
            os.close(term.master)
        signal_name = None
        if code < 0:
            signal_name = signals.Signals(-code).name
            code = -1
        term.status, term.exit_code, term.exit_signal, term.exited_at = "exited", code, signal_name, stamp()
        term.pulse()
        self.emit("terminal.exited", term.id, {"exit_code": code, "signal": signal_name, "seq": term.head})
        for launch in list(self.launches.values()):
            if launch.terminal_id == term.id:
                self._bg(self._end_launch_later(launch.launch_id))

    async def _end_launch_later(self, launch_id: str) -> None:
        await asyncio.sleep(self.launch_grace_s)
        if launch_id in self.launches:
            self._end_launch(launch_id, "terminal_exited")

    async def _m_terminal_write(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        if term.status != "running":
            raise _RpcFail(1002, "exited")
        payloads = [k for k in ("text", "paste", "keys", "bytes_b64") if params.get(k) is not None]
        if len(payloads) != 1:
            raise _RpcFail(-32602, "exactly one of text, paste, keys, bytes_b64")
        origin = params.get("origin") or {}
        started = time.monotonic()
        if params.get("wait", "keyboard") == "keyboard":
            timeout = min(int(params.get("timeout_ms") or 30_000), 600_000) / 1000
            while term.human_holds():
                if time.monotonic() - started >= timeout:
                    raise _RpcFail(1006, "a human holds the keyboard")
                await asyncio.sleep(0.01)
        kind = payloads[0]
        if kind == "text":
            data = str(params["text"]).encode()
        elif kind == "paste":
            data = paste_bytes(str(params["paste"]), term.screen.modes["bracketed_paste"])
        elif kind == "keys":
            data = encode_keys([str(k) for k in params["keys"]], term.screen.modes["app_cursor"])
        else:
            data = base64.b64decode(params["bytes_b64"])
        if len(data) > 1 << 20:
            raise _RpcFail(1003, "a write is at most 1 MiB")
        seq_before = term.head
        async with term.write_lock:
            await self._write_pty(term, data)
        record = {"kind": kind, "value": params[kind], "bytes": len(data), "origin": origin, "at": stamp()}
        term.writes.append(record)
        self.agent_writes.append({"terminal_id": term.id, **record})
        term.last_input_at = stamp()
        return {"bytes": len(data), "seq_before": seq_before, "queued_ms": int((time.monotonic() - started) * 1000), "delivered_at": stamp()}

    async def _write_pty(self, term: LiveTerminal, data: bytes) -> None:
        for start in range(0, len(data), 4096):
            chunk = data[start : start + 4096]
            while chunk:
                try:
                    written = os.write(term.master, chunk)
                except BlockingIOError:
                    await asyncio.sleep(0.002)
                    continue
                except OSError as exc:
                    raise _RpcFail(1002, f"the terminal is gone: {exc}") from None
                chunk = chunk[written:]

    async def type_as_human(self, terminal_id: str, data: str | bytes) -> None:
        """What a person typing in the browser does: it goes straight in and takes the keyboard."""
        term = self.terminals[terminal_id]
        assert isinstance(term, LiveTerminal)
        term.last_human = time.monotonic()
        term.last_human_input_at = stamp()
        if term.keyboard_owner == "agent":
            term.keyboard_owner, term.keyboard_until = "auto", None
        async with term.write_lock:
            await self._write_pty(term, data.encode() if isinstance(data, str) else data)

    async def _m_terminal_keyboard(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        owner = str(params["owner"])
        if owner not in ("auto", "human", "agent"):
            raise _RpcFail(-32602, "owner is auto, human or agent")
        ttl = params.get("ttl_ms")
        term.keyboard_owner = owner
        term.keyboard_until = time.monotonic() + int(ttl) / 1000 if ttl and owner != "auto" else None
        until = int((time.time() + int(ttl) / 1000) * 1000) if ttl and owner != "auto" else None
        return {"owner": owner, "until": until}

    async def _m_terminal_resize(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        cols, rows = int(params["cols"]), int(params["rows"])
        if not (20 <= cols <= 500 and 4 <= rows <= 300):
            raise _RpcFail(1009, "outside 20x4 ... 500x300")
        term.cols, term.rows = cols, rows
        term.screen.resize(cols, rows)
        if term.status == "running":
            with contextlib.suppress(OSError):
                fcntl.ioctl(term.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(term.pid, signals.SIGWINCH)
        return {"cols": cols, "rows": rows}

    async def _m_terminal_signal(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        name = str(params["signal"]).upper()
        if name not in ("INT", "TERM", "HUP", "KILL", "QUIT", "TSTP", "CONT", "WINCH", "USR1", "USR2"):
            raise _RpcFail(-32602, f"unknown signal {name}")
        term.signals.append(name)
        number = getattr(signals, "SIG" + name)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if params.get("group", True):
                os.killpg(term.pid, number)
            else:
                os.kill(term.pid, number)
        return {}

    async def _m_terminal_kill(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        if term.status == "running":
            await self._kill(term, grace_ms=min(int(params.get("grace_ms") or 2000), 30_000))
        return {"exit_code": term.exit_code, "signal": term.exit_signal}

    async def _kill(self, term: LiveTerminal, *, grace_ms: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(term.pid, signals.SIGHUP)
        deadline = time.monotonic() + grace_ms / 1000
        while term.status == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if term.status == "running":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(term.pid, signals.SIGKILL)
            while term.status == "running":
                await asyncio.sleep(0.01)
        # The program's own group is gone; anything it left behind in the group goes too.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(term.pid, signals.SIGKILL)

    async def _m_terminal_read_output(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._term(params)
        if not isinstance(term, LiveTerminal):
            return self._handle("terminal.read_output", params)
        since = int(params.get("since_seq") or 0)
        gap = since < term.base
        start = max(since, term.base)
        strip = bool(params.get("strip"))
        cap = min(int(params.get("max_bytes") or (1 << 20)), READ_TEXT_MAX if strip else READ_RAW_MAX)
        chunk = bytes(term.output[start - term.base : start - term.base + cap])
        out: dict[str, Any] = {"from_seq": start, "to_seq": start + len(chunk), "head_seq": term.head, "gap": gap}
        if strip:
            out["data"] = strip_escapes(chunk)
        else:
            out["data_b64"] = base64.b64encode(chunk).decode()
        return out

    async def _m_terminal_read_screen(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        if params.get("format", "text") != "text":
            raise _RpcFail(1007, "the test double reads screens as text only")
        scrollback = min(int(params.get("scrollback") or 0), 10_000)
        lines = term.screen.lines(scrollback=scrollback)
        tail = params.get("tail_rows")
        if tail:
            lines = lines[-int(tail) :]
        x, y = term.screen.cursor
        return {
            "cols": term.cols, "rows": term.rows, "cursor": {"x": x, "y": y, "visible": term.screen.modes["cursor_visible"], "abs_row": len(term.screen.scrollback) + y},
            "alt_screen": term.screen.modes["alt_screen"], "title": term.screen.title, "cwd": term.cwd, "seq": term.head, "lines": lines,
        }

    async def _m_terminal_wait_for(self, params: dict[str, Any]) -> dict[str, Any]:
        term = self._live_term(params)
        pattern = re.compile(params["regex"], re.M) if params.get("regex") else None
        scope = params.get("scope", "screen")
        idle_ms = params.get("idle_ms")
        since = int(params["since_seq"]) if params.get("since_seq") is not None else term.head if scope == "output" else 0
        deadline = time.monotonic() + int(params.get("timeout_ms") or 30_000) / 1000
        while True:
            if pattern is not None:
                haystack = term.screen.text() if scope == "screen" else strip_escapes(bytes(term.output[max(0, since - term.base) :]))
                found = pattern.search(haystack)
                if found:
                    return {"matched": "regex", "seq": term.head, "match": found.group(0)}
            now = time.monotonic()
            if idle_ms is not None and (now - max(term.last_output, 0.0)) * 1000 >= int(idle_ms):
                return {"matched": "idle", "seq": term.head}
            if term.status != "running":
                return {"matched": "exited", "seq": term.head}
            if now >= deadline:
                return {"matched": "timeout", "seq": term.head}
            wait = deadline - now
            if idle_ms is not None:
                wait = min(wait, max(0.005, int(idle_ms) / 1000 - (now - term.last_output)))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(term.changed.wait(), wait)

    # -- programs --------------------------------------------------------------------------------------

    async def _m_exec_run(self, params: dict[str, Any]) -> dict[str, Any]:
        argv = [str(a) for a in params["argv"]]
        if not argv:
            raise _RpcFail(-32602, "an empty argv")
        env = self._environment("", params.get("env") or {}, [], None)
        env.pop("DAEDALUS_TERMINAL_ID", None)
        program = shutil.which(argv[0], path=env.get("PATH"))
        name = Path(program or argv[0]).name
        if name not in self.exec_allow:
            raise _RpcFail(1004, f"{name} is not among the programs exec.run may run")
        if program is None:
            raise _RpcFail(1001, f"{argv[0]} was not found")
        cap = min(int(params.get("max_output") or (4 << 20)), 4 << 20)
        timeout = min(int(params.get("timeout_ms") or 60_000), 1_800_000) / 1000
        stdin = base64.b64decode(params["stdin_b64"]) if params.get("stdin_b64") else None
        cwd = str(params.get("cwd") or self.home)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            program, *argv[1:], cwd=cwd, env=env, start_new_session=True, preexec_fn=_default_signals,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(process.communicate(stdin), timeout)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signals.SIGKILL)
            out, err = await process.communicate()
        code = process.returncode if process.returncode is not None else -1
        signal_name = ""
        if code < 0:
            signal_name, code = signals.Signals(-code).name, -1
        return {
            "exit_code": code, "signal": signal_name, "stdout": out[:cap].decode("utf-8", "replace"), "stderr": err[:cap].decode("utf-8", "replace"),
            "truncated": len(out) > cap or len(err) > cap, "timed_out": timed_out, "duration_ms": int((time.monotonic() - started) * 1000),
            "path": program,
        }

    # -- files -------------------------------------------------------------------------------------------

    def allow_root(self, path: str | Path) -> None:
        """A root the host would send with ``fs.set_roots``: for tests that skip that step."""
        if str(path) not in self.roots:
            self.roots.append(str(Path(path).resolve()))

    def _allowed_path(self, path: str) -> Path:
        target = Path(path)
        if not target.is_absolute():
            raise _RpcFail(-32602, "the path must be absolute")
        real = target.resolve()
        if not any(real == Path(root) or Path(root) in real.parents for root in self.roots):
            raise _RpcFail(1004, f"{path} is not under an allowed root")
        text = str(real)
        for pattern in FS_DENY:
            if fnmatch.fnmatch(text, pattern.replace("**/", "*/")) or fnmatch.fnmatch(text, pattern):
                raise _RpcFail(1004, f"{path} is on the deny list")
        if self.run_dir.resolve() in real.parents or real == self.run_dir.resolve():
            raise _RpcFail(1004, f"{path} is the terminal service's own")
        return real

    @staticmethod
    def _file_id(real: Path) -> str:
        st = real.stat()
        return f"{st.st_dev}:{st.st_ino}"

    async def _m_fs_set_roots(self, params: dict[str, Any]) -> dict[str, Any]:
        wanted = [str(r) for r in params.get("roots") or []]
        refused = [{"root": r, "reason": "is the root of the filesystem" if r == "/" else "is not absolute"} for r in wanted if r == "/" or not r.startswith("/")]
        self.roots = sorted({str(Path(r).resolve()) for r in wanted if r != "/" and r.startswith("/")})
        return {"roots": self.roots, "accepted": self.roots, "refused": refused}

    async def _m_fs_stat(self, params: dict[str, Any]) -> dict[str, Any]:
        real = self._allowed_path(str(params["path"]))
        if not real.exists():
            return {"exists": False, "size": 0}
        st = real.stat()
        return {"exists": True, "type": "dir" if real.is_dir() else "file", "size": st.st_size, "mtime": stamp_of(st.st_mtime), "mode": oct(st.st_mode & 0o777), "file_id": f"{st.st_dev}:{st.st_ino}"}

    async def _m_fs_mkdir(self, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(params["path"]))
        if not path.is_absolute() or str(path) == "/":
            raise _RpcFail(-32602, "a folder to make is an absolute path below the root")
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        st = path.stat()
        return {"exists": True, "type": "dir", "size": st.st_size, "mtime": stamp_of(st.st_mtime), "writable": True, "created": created}

    async def _m_fs_browse(self, params: dict[str, Any]) -> dict[str, Any]:
        """Not held to the roots, as the daemon's: a folder for a new project is under none yet."""
        return browse_folders(params, home=self.home, sealed=[str(self.run_dir)])

    async def _m_fs_write(self, params: dict[str, Any]) -> dict[str, Any]:
        return write_inbox(self._allowed_path, params)

    async def _m_fs_list(self, params: dict[str, Any]) -> dict[str, Any]:
        real = self._allowed_path(str(params["path"]))
        if not real.is_dir():
            raise _RpcFail(1001, f"no directory {params['path']}")
        glob = params.get("glob")
        entries = []
        for child in real.iterdir():
            if glob and not fnmatch.fnmatch(child.name, glob):
                continue
            with contextlib.suppress(_RpcFail, OSError):
                self._allowed_path(str(child))
                st = child.stat()
                entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file", "size": st.st_size, "mtime": stamp_of(st.st_mtime), "_m": st.st_mtime})
        if params.get("sort") == "mtime":
            entries.sort(key=lambda e: e["_m"], reverse=True)
        else:
            entries.sort(key=lambda e: e["name"])
        limit = min(int(params.get("limit") or 1000), 5000)
        for entry in entries:
            entry.pop("_m")
        return {"entries": entries[:limit], "truncated": len(entries) > limit}

    async def _m_fs_read(self, params: dict[str, Any]) -> dict[str, Any]:
        real = self._allowed_path(str(params["path"]))
        if not real.is_file():
            raise _RpcFail(1001, f"no file {params['path']}")
        offset = int(params.get("offset") or 0)
        size = real.stat().st_size
        with real.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(min(int(params.get("max") or 65536), 640 << 10))
        return {"data_b64": base64.b64encode(chunk).decode(), "offset": offset, "size": size, "eof": offset + len(chunk) >= size, "file_id": self._file_id(real)}

    async def _m_fs_tail(self, params: dict[str, Any]) -> dict[str, Any]:
        real = self._allowed_path(str(params["path"]))
        start = int(params.get("from_offset") or 0)
        deadline = time.monotonic() + min(int(params.get("follow_ms") or 0), 60_000) / 1000
        while True:
            if real.is_file():
                file_id = self._file_id(real)
                size = real.stat().st_size
                rotated = start > size or bool(params.get("file_id") and params["file_id"] != file_id)
                begin = 0 if rotated else start
                if size > begin or rotated or time.monotonic() >= deadline:
                    with real.open("rb") as handle:
                        handle.seek(begin)
                        chunk = handle.read(min(int(params.get("max") or 65536), 640 << 10))
                    return {"data_b64": base64.b64encode(chunk).decode(), "next_offset": begin + len(chunk), "size": size, "rotated": rotated, "file_id": file_id}
            elif time.monotonic() >= deadline:
                raise _RpcFail(1001, f"no file {params['path']}")
            await asyncio.sleep(0.02)

    # -- launches and hooks ----------------------------------------------------------------------------

    def _launch_env(self, launch: LiveLaunch) -> dict[str, str]:
        env = {
            "DAEDALUS_LAUNCH_ID": launch.launch_id, "DAEDALUS_HOOK_URL": f"http://127.0.0.1:{self.hook_port}/hook/{launch.launch_id}",
            "DAEDALUS_HOOK_TOKEN": launch.token, "DAEDALUS_DIAL_DIR": str(launch.dial_dir), "DAEDALUS_LAUNCH_DIR": str(launch.dir),
        }
        if self.bin_dir is not None:
            env["DAEDALUS_HOOK_CMD"] = str(self.bin_dir / "hook-post")
            env["DAEDALUS_PTYD_BIN"] = str(self.bin_dir / "ptyd")
        return env

    async def _m_hooks_register_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        launch_id = str(params.get("launch_id") or "l" + secrets.token_hex(8))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", launch_id):
            raise _RpcFail(-32602, "a launch id is 1-64 of A-Z a-z 0-9 - _")
        if launch_id in self.launches or launch_id in self.ended_launches:
            raise _RpcFail(1004, "a launch with this id exists")
        directory = self.state_dir / "launches" / launch_id
        dial = self.state_dir / "dial" / launch_id
        directory.mkdir(mode=0o700, parents=True)
        dial.mkdir(mode=0o700, parents=True)
        names = []
        for name, data in (params.get("files") or {}).items():
            # A few plain parts joined by '/', as the daemon allows (a skill has to sit at a fixed
            # place under a directory the CLI is given); never a way out of the launch directory.
            parts = name.split("/")
            if len(parts) > 6 or any(part in ("", ".", "..") for part in parts):
                raise _RpcFail(-32602, f"a file name is plain parts inside the launch directory: {name!r}")
            path = directory.joinpath(*parts)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data))
            path.chmod(0o600)
            names.append(name)
        hold_max = int(params.get("hold_max_ms") or HOOK_HOLD_MAX_MS)
        launch = LiveLaunch(launch_id, secrets.token_hex(32), directory, dial, str(params.get("terminal_id") or ""), min(hold_max, HOOK_HOLD_MAX_MS), set(int(p) for p in params.get("ports") or []))
        self.launches[launch_id] = launch
        env = self._launch_env(launch)
        return {"launch_id": launch_id, "hook_url": env["DAEDALUS_HOOK_URL"], "hook_token": launch.token, "dir": str(directory), "dial_dir": str(dial), "env": env, "files": sorted(names)}

    async def _m_hooks_put_file(self, params: dict[str, Any]) -> dict[str, Any]:
        launch = self.launches.get(str(params["launch_id"]))
        if launch is None:
            raise _RpcFail(1008, "no such launch")
        name = str(params["name"])
        if "/" in name or name in ("", ".", ".."):
            raise _RpcFail(-32602, f"a file added later is one plain name: {name!r}")
        path = launch.dir / name
        target = launch.dir / f".put-{secrets.token_hex(4)}-{name}" if params.get("replace") else path
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise _RpcFail(-32602, f"file {name} exists") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64decode(params.get("data") or ""))
        if target != path:
            os.replace(target, path)
        return {"path": str(path)}

    async def _m_hooks_unregister_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        launch_id = str(params["launch_id"])
        removed = launch_id in self.launches
        if removed:
            self._end_launch(launch_id, "unregistered")
        return {"removed": removed}

    def _end_launch(self, launch_id: str, reason: str) -> None:
        launch = self.launches.pop(launch_id)
        self.ended_launches.add(launch_id)
        for future in launch.held.values():
            if not future.done():
                future.set_result((410, b"the launch ended", "text/plain"))
        for channel in list(launch.streams):
            entry = self._streams.pop(channel, None)
            if entry is not None:
                entry[0].close()
        shutil.rmtree(launch.dir, ignore_errors=True)
        shutil.rmtree(launch.dial_dir, ignore_errors=True)
        self.emit("launch.ended", launch.terminal_id or None, {"launch_id": launch_id, "reason": reason})

    async def _m_hooks_reply(self, params: dict[str, Any]) -> dict[str, Any]:
        reply_id = str(params["reply_id"])
        for launch in self.launches.values():
            future = launch.held.get(reply_id)
            if future is not None and not future.done() and (not params.get("launch_id") or params["launch_id"] == launch.launch_id):
                body = params.get("body")
                if body is None:
                    raw, kind = b"", ""
                elif isinstance(body, str):
                    raw, kind = body.encode(), "text/plain"
                else:
                    raw, kind = json.dumps(body).encode(), "application/json"
                future.set_result((int(params.get("status") or 200), raw, str(params.get("content_type") or kind)))
                self.replies.append(params)
                return {"delivered": True}
        raise _RpcFail(1001, "no request is waiting for this reply")

    async def _hook_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body, kind = await self._hook_request(reader)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            writer.close()
            return
        reason = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 410: "Gone", 413: "Payload Too Large"}.get(status, "Status")
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
        if kind:
            head += f"Content-Type: {kind}\r\n"
        with contextlib.suppress(ConnectionError):
            writer.write(head.encode() + b"\r\n" + body)
            await writer.drain()
        writer.close()

    async def _hook_request(self, reader: asyncio.StreamReader) -> tuple[int, bytes, str]:
        request_line = (await reader.readline()).decode("latin-1").strip()
        method, _, rest = request_line.partition(" ")
        target = rest.rpartition(" ")[0]
        headers: dict[str, str] = {}
        while (line := (await reader.readline()).decode("latin-1").strip()):
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        if length > HOOK_BODY_MAX:
            return 413, b"the body is larger than a megabyte", "text/plain"
        body = await reader.readexactly(length) if length else b""
        if method != "POST":
            return 405, b"POST only", "text/plain"
        path, _, query = target.partition("?")
        found = re.fullmatch(r"/hook/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9._-]{1,64})", path)
        if found is None:
            return 404, b"not a hook URL", "text/plain"
        launch = self.launches.get(found.group(1))
        if launch is None:
            return 410, b"no such launch", "text/plain"
        if headers.get("authorization", "") != f"Bearer {launch.token}":
            return 401, b"wrong token", "text/plain"
        hold_ms = 0
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key in ("wait_ms", "hold_ms") and value.isdigit():
                hold_ms = int(value)
        try:
            parsed: Any = json.loads(body) if body.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = body.decode("utf-8", "replace")
        if not hold_ms and isinstance(parsed, dict) and isinstance(parsed.get("daedalus_hold_ms"), int):
            hold_ms = parsed["daedalus_hold_ms"]
        hold_ms = min(max(hold_ms, 0), launch.hold_max_ms)
        data: dict[str, Any] = {"launch_id": launch.launch_id, "terminal_id": launch.terminal_id, "name": found.group(2), "body": parsed, "size": len(body)}
        future: asyncio.Future[tuple[int, bytes, str]] | None = None
        if hold_ms:
            reply_id = "r" + secrets.token_hex(8)
            future = asyncio.get_running_loop().create_future()
            launch.held[reply_id] = future
            data["reply_id"], data["hold_ms"] = reply_id, hold_ms
        launch.posts.append({"name": found.group(2), "body": parsed, "hold_ms": hold_ms})
        self.emit("hook", launch.terminal_id or None, data)
        if future is None:
            return 204, b"", ""
        # As the daemon does: a caller that gives up (its own timer is shorter than the hold) closes
        # the connection, and the post stops waiting, so a later reply is refused rather than lost.
        gone = asyncio.ensure_future(reader.read(1))
        try:
            done, _ = await asyncio.wait({future, gone}, timeout=hold_ms / 1000, return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            return 204, b"", ""
        finally:
            gone.cancel()
            launch.held.pop(data["reply_id"], None)

    # -- streams -----------------------------------------------------------------------------------------

    async def _m_net_allow(self, params: dict[str, Any]) -> dict[str, Any]:
        launch = self.launches.get(str(params["launch_id"]))
        if launch is None:
            raise _RpcFail(1008, "no such launch")
        launch.ports.add(int(params["port"]))
        return {}

    async def _m_net_dial(self, params: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
        launch = self.launches.get(str(params["launch_id"]))
        if launch is None:
            raise _RpcFail(1008, "no such launch")
        target = str(params["target"])
        try:
            if target.startswith("unix:"):
                name = target[5:]
                if "/" in name or not name:
                    raise _RpcFail(-32602, "a unix target is a name in the launch's dial directory")
                sock_reader, sock_writer = await asyncio.open_unix_connection(self._dial_path(launch, name))
            elif target.startswith("tcp:127.0.0.1:"):
                port = int(target.rpartition(":")[2])
                if port not in launch.ports:
                    raise _RpcFail(1004, "the port is not registered for this launch")
                sock_reader, sock_writer = await asyncio.open_connection("127.0.0.1", port)
            else:
                raise _RpcFail(-32602, "the target is unix:<name> or tcp:127.0.0.1:<port>")
        except OSError as exc:
            raise _RpcFail(1001, f"nothing answers at {target}: {exc}") from None
        self._next_channel += 1
        channel = self._next_channel
        self._streams[channel] = (sock_writer, writer, launch.launch_id)
        launch.streams.add(channel)
        self._bg(self._relay(channel, sock_reader, writer, launch))
        return {"channel": channel}

    def _dial_path(self, launch: LiveLaunch, name: str) -> str:
        """The socket a ``unix:`` target reaches, by the daemon's rule: a socket in the dial directory,
        or a link there to a socket of this user in a directory no one else may write."""
        path = launch.dial_dir / name
        try:
            entry = os.lstat(path)
        except FileNotFoundError:
            raise _RpcFail(1001, f"no socket {name}") from None
        if stat.S_ISSOCK(entry.st_mode):
            return str(path)
        if not stat.S_ISLNK(entry.st_mode) or not self.follow_links:
            raise _RpcFail(1004, f"forbidden: {name} is not a socket")
        real = os.path.realpath(path)
        try:
            target, parent = os.lstat(real), os.lstat(os.path.dirname(real))
        except FileNotFoundError:
            raise _RpcFail(1001, f"{name} links to no socket yet") from None
        if not stat.S_ISSOCK(target.st_mode) or target.st_uid != os.getuid() or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise _RpcFail(1004, f"forbidden: {name} links to a socket that is not private to this user")
        return real

    async def _relay(self, channel: int, sock_reader: asyncio.StreamReader, host: asyncio.StreamWriter, launch: LiveLaunch) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            while chunk := await sock_reader.read(65536):
                await self._frame(host, channel, chunk)
        launch.streams.discard(channel)
        if self._streams.pop(channel, None) is not None:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self._frame(host, channel, b"")

    async def _stream_frame(self, channel: int, payload: bytes) -> None:
        entry = self._streams.get(channel)
        if entry is None:
            return
        sock, host, _ = entry
        if not payload:
            del self._streams[channel]
            sock.close()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self._frame(host, channel, b"")
            return
        with contextlib.suppress(ConnectionError, OSError):
            sock.write(payload)
            await sock.drain()


def stamp_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


__all__ = ["EXEC_ALLOW", "FS_DENY", "LiveLaunch", "LivePtyd", "LiveTerminal", "encode_keys", "paste_bytes", "strip_escapes"]
