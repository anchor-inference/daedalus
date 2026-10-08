"""The adapter's two ports, ``TerminalPort`` and ``EnvironmentPort``, over one daemon connection.

The staff runtime implements them over the terminals service (its audit, its cap, its database);
these go straight to a daemon's socket, which is all an adapter test needs: a real CLI (or a fake
one) in a real terminal behind the real protocol, with nothing of the host in between. They also
show the mapping from the ports to the daemon's methods in one place.

``Rig`` puts the pieces together for a test: a ``LivePtyd`` with the fake CLIs on its ``PATH`` in
a temporary home, a connected client, the event stream, and helpers to register a launch and start
a terminal in it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from daedalus.harness.contract import Environment, EnvironmentUnavailable, ExecResult, HookPost, ProgramNotFound
from daedalus.terminals import wire
from daedalus.terminals.client import PtydClient, Unavailable
from daedalus.terminals.sidechannels import ByteStream
from tests.support import fake_cli
from tests.support.live_ptyd import LivePtyd


class PtydTerminalPort:
    """``TerminalPort`` for one terminal: every write names its origin, as the host's writes do."""

    def __init__(self, client: PtydClient, terminal_id: str, *, env: Environment = "container", actor: str = "adapter", launch_id: str = "", hooks: asyncio.Queue[HookPost | None] | None = None) -> None:
        self.client = client
        self._id = terminal_id
        self._env = env
        self.actor = actor
        self.launch_id = launch_id
        self._hooks: asyncio.Queue[HookPost | None] = hooks if hooks is not None else asyncio.Queue()

    @property
    def id(self) -> str:
        return self._id

    @property
    def env(self) -> Environment:
        return self._env

    async def write(self, *, text: str | None = None, paste: str | None = None, keys: list[str] | None = None, note: str = "", wait_keyboard: bool = True) -> None:
        params: dict[str, Any] = {"id": self._id, "origin": {"kind": "agent", "actor": self.actor, "launch_id": self.launch_id}, "wait": "keyboard" if wait_keyboard else "none"}
        if text is not None:
            params["text"] = text
        elif paste is not None:
            params["paste"] = paste
        elif keys is not None:
            params["keys"] = keys
        await self.client.call("terminal.write", params, timeout=60)

    async def screen(self, *, scrollback: int = 0) -> str:
        result = await self.client.call("terminal.read_screen", {"id": self._id, "format": "text", "scrollback": scrollback})
        lines = list(result.get("lines") or [])
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    async def modes(self) -> Mapping[str, bool]:
        info = await self.client.call("terminal.get", {"id": self._id})
        return {str(k): bool(v) for k, v in (info.get("modes") or {}).items()}

    async def wait_for(self, *, regex: str | None = None, idle_ms: int | None = None, timeout: float) -> bool:
        params: dict[str, Any] = {"id": self._id, "timeout_ms": int(timeout * 1000)}
        if regex is not None:
            params["regex"] = regex
        if idle_ms is not None:
            params["idle_ms"] = idle_ms
        result = await self.client.call("terminal.wait_for", params, timeout=timeout + 10)
        return result.get("matched") in ("regex", "idle")

    async def hooks(self) -> AsyncIterator[HookPost]:
        """The launch's hook posts, as ``Rig`` routes them here; the team's included, since no
        runtime stands between an adapter test and the daemon."""
        while True:
            post = await self._hooks.get()
            if post is None:
                self._hooks.put_nowait(None)
                return
            yield post

    async def reply(self, reply_id: str, body: Any) -> bool:
        try:
            await self.client.call("hooks.reply", {"reply_id": reply_id, "status": 200, "body": body, "launch_id": self.launch_id})
        except Exception:  # noqa: BLE001 — the daemon's refusal is "nothing waits any more"
            return False
        return True

    async def put_file(self, name: str, data: bytes, *, replace: bool = False) -> str:
        params: dict[str, Any] = {"launch_id": self.launch_id, "name": name, "data": base64.b64encode(data).decode()}
        if replace:
            params["replace"] = True
        result = await self.client.call("hooks.put_file", params)
        return str(result["path"])

    async def dial(self, target: str) -> ByteStream:
        result = await self.client.call("net.dial", {"target": target, "launch_id": self.launch_id})
        return ByteStream(self.client.channel(int(result["channel"])), env=self._env, launch_id=self.launch_id, target=target)


class PtydEnvironmentPort:
    """``EnvironmentPort`` for the daemon's environment."""

    def __init__(self, client: PtydClient, *, env: Environment = "container", home: str = "") -> None:
        self.client = client
        self._env = env
        self._home = home

    @property
    def name(self) -> Environment:
        return self._env

    @property
    def home(self) -> str:
        return self._home

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float = 30.0) -> ExecResult:
        params: dict[str, Any] = {"argv": list(argv), "timeout_ms": int(timeout * 1000)}
        if cwd:
            params["cwd"] = cwd
        if env:
            params["env"] = dict(env)
        try:
            result = await self.client.call("exec.run", params, timeout=timeout + 15)
        except wire.RpcError as exc:
            if exc.code == wire.NOT_FOUND:
                raise ProgramNotFound(exc.message) from None
            raise
        except Unavailable as exc:
            raise EnvironmentUnavailable(str(exc)) from None
        return ExecResult(
            exit_code=int(result["exit_code"]), stdout=str(result["stdout"]), stderr=str(result["stderr"]), timed_out=bool(result["timed_out"]), path=str(result.get("path") or ""),
        )

    async def read(self, path: str, *, offset: int = 0, limit: int = 1 << 20) -> bytes:
        out = bytearray()
        while len(out) < limit:
            result = await self.client.call("fs.read", {"path": path, "offset": offset + len(out), "max": min(limit - len(out), 640 << 10)})
            out += base64.b64decode(result["data_b64"])
            if result.get("eof") or not result["data_b64"]:
                break
        return bytes(out)

    async def stat(self, path: str) -> Mapping[str, Any] | None:
        result = await self.client.call("fs.stat", {"path": path})
        return result if result.get("exists") else None

    async def list(self, path: str) -> list[str]:
        result = await self.client.call("fs.list", {"path": path})
        return [str(entry["name"]) for entry in result.get("entries") or []]


class Rig:
    """A daemon with fake CLIs, a client and its events, in a temporary home. Use as
    ``async with Rig() as rig:``; everything it started is stopped and removed at the end.

    ``extra_env`` reaches every program the daemon starts (``FAKE_CLI_TIME_SCALE``,
    ``FAKE_CLI_FAULTS``…); ``log`` is the fakes' ``FAKE_CLI_LOG``.
    """

    def __init__(self, *, extra_env: Mapping[str, str] | None = None, input_idle_ms: int = 10_000, launch_grace_s: float = 30.0, ptyd_bin: Path | None = None) -> None:
        # Short, because a unix socket path is limited to 108 bytes and dial sockets live below it.
        self.root = Path(tempfile.mkdtemp(prefix="ptyd-"))
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.work = self.root / "work"
        self.log = self.root / "fake-cli.jsonl"
        for directory in (self.home, self.work):
            directory.mkdir()
        fake_cli.install(self.bin, ptyd=ptyd_bin)
        env = {"FAKE_CLI_LOG": str(self.log), "FAKE_CLI_TIME_SCALE": "0.05", **(extra_env or {})}
        self.ptyd = LivePtyd(self.root / "run", home=self.home, bin_dir=self.bin, base_env=env, input_idle_ms=input_idle_ms, launch_grace_s=launch_grace_s)
        self.events: list[dict[str, Any]] = []
        self._event = asyncio.Event()
        self._launch_hooks: dict[str, list[asyncio.Queue[HookPost | None]]] = {}
        self.client = PtydClient("container", self.root / "run", on_notification=self._on_event)
        self.env_port = PtydEnvironmentPort(self.client, home=str(self.home))

    async def __aenter__(self) -> Rig:
        await self.ptyd.start()
        self.ptyd.allow_root(self.root)
        await self.client.connect()
        await self.client.call("events.subscribe", {"after_seq": 0})
        return self

    async def __aexit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()
        await self.ptyd.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    async def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "event":
            self.events.append(params)
            data = params.get("data") or {}
            if params.get("type") == "hook":
                post = HookPost(name=str(data.get("name") or ""), body=data.get("body"), at=str(params.get("at") or ""), reply_id=data.get("reply_id"), hold_ms=int(data.get("hold_ms") or 0))
                for queue in self._launch_hooks.get(str(data.get("launch_id") or ""), []):
                    queue.put_nowait(post)
            elif params.get("type") == "launch.ended":
                for queue in self._launch_hooks.pop(str(data.get("launch_id") or ""), []):
                    queue.put_nowait(None)
            self._event.set()
            self._event = asyncio.Event()

    async def event(self, kind: str, *, where: Mapping[str, Any] | None = None, timeout: float = 30.0, after: int = 0) -> dict[str, Any]:
        """The first event of ``kind`` (past index ``after``) whose ``data`` has the ``where`` values."""
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events[after:]:
                    data = event.get("data") or {}
                    if event["type"] == kind and all(data.get(k) == v for k, v in (where or {}).items()):
                        return event
                await self._event.wait()

    def hooks(self, name: str | None = None) -> list[dict[str, Any]]:
        """Hook events so far, oldest first, optionally of one hook name."""
        return [e["data"] for e in self.events if e["type"] == "hook" and (name is None or e["data"].get("name") == name)]

    async def register(self, *, files: Mapping[str, bytes] | None = None, ports: list[int] | None = None, hold_max_ms: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"launch_id": "l" + secrets.token_hex(6), "files": {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}}
        if ports:
            params["ports"] = ports
        if hold_max_ms:
            params["hold_max_ms"] = hold_max_ms
        result: dict[str, Any] = await self.client.call("hooks.register_launch", params)
        return result

    async def spawn(self, argv: list[str], *, cwd: Path | str | None = None, env: Mapping[str, str] | None = None, launch_id: str = "", cols: int = 100, rows: int = 30) -> PtydTerminalPort:
        terminal_id = "t" + secrets.token_hex(6)
        params: dict[str, Any] = {"id": terminal_id, "argv": argv, "cwd": str(cwd or self.work), "cols": cols, "rows": rows, "env": dict(env or {})}
        if launch_id:
            params["launch_id"] = launch_id
        hooks: asyncio.Queue[HookPost | None] = asyncio.Queue()
        if launch_id:
            self._launch_hooks.setdefault(launch_id, []).append(hooks)
        await self.client.call("terminal.create", params)
        return PtydTerminalPort(self.client, terminal_id, launch_id=launch_id, hooks=hooks)

    async def screen_until(self, term: PtydTerminalPort, regex: str, *, timeout: float = 30.0) -> str:
        """The screen once ``regex`` is on it; fails with the screen as it was."""
        if not await term.wait_for(regex=regex, timeout=timeout):
            raise AssertionError(f"{regex!r} never appeared; the screen:\n{await term.screen()}")
        return await term.screen()


__all__ = ["PtydEnvironmentPort", "PtydTerminalPort", "Rig"]
