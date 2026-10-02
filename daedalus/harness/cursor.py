"""Cursor Agent as staff through its documented ACP JSON-RPC stream.

ACP supplies turns, tool calls and permission requests. A launch-local bridge owns Cursor's stdio,
renders a simple terminal for the operator and exposes a socket that the host can reconnect to.
Cursor's ACP mode can edit workspace files without requesting approval, so only an explicit `agent`
mode gets write access; an unset mode starts in read-only `ask` mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daedalus.harness import cursor_bridge, register
from daedalus.harness.capabilities import capabilities
from daedalus.harness.contract import (
    DIAL_DIR,
    LAUNCH_DIR,
    Answer,
    Catalog,
    CheckResult,
    CheckStep,
    Delivery,
    EnvironmentPort,
    EventKind,
    InstallInfo,
    Launch,
    LaunchPlan,
    LaunchSpec,
    LoginState,
    ReadyStep,
    ScreenClass,
    SendMode,
    StaffEvent,
    TerminalPort,
    Turn,
    UpdateResult,
)
from daedalus.harness.tools import tooling

SOCKET = "cursor.sock"
BRIDGE = "cursor_bridge.py"
CONFIG = "cursor-launch.json"
TRANSCRIPT = "cursor-turns.jsonl"
CONNECT_S = 60.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(eq=False)
class _Connection:
    socket: Any = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    queue: asyncio.Queue[StaffEvent | None] = field(default_factory=asyncio.Queue)
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    next_id: int = 0
    session_id: str = ""
    transcript: str = ""
    stopping: bool = False


@register
class CursorAdapter:
    """Run one Cursor ACP session under the staff runtime's terminal and delivery contract."""

    name = "cursor"
    capabilities = capabilities("cursor")

    def __init__(self) -> None:
        self.tooling = tooling("cursor")
        self._connections: dict[str, _Connection] = {}

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return await self.tooling.installed(env)

    async def latest(self, env: EnvironmentPort) -> str:
        return (await self.installed(env)).version

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        before = await self.installed(env)
        result = await env.run(["cursor-agent", "update"], timeout=600)
        after = await self.installed(env)
        ok = result.exit_code == 0 and not result.timed_out
        return UpdateResult(ok, before.version, after.version, output=result.stdout[-4000:], error="" if ok else (result.stderr or result.stdout)[-2000:])

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        info = await self.installed(env)
        return CheckResult(info.installed, (CheckStep("version", info.installed, info.version or info.detail), CheckStep("session", True, "run by the harness manager", skipped=True)), info.version, 0)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        return await self.tooling.catalog(env, cwd)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        return await self.tooling.login_state(env)

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan:
        return self._plan(spec, "")

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        return self._plan(spec, ref)

    def _plan(self, spec: LaunchSpec, resume: str) -> LaunchPlan:
        mode = spec.permission_mode if spec.permission_mode in ("ask", "plan", "agent") else "ask"
        # ACP asks for permission on some calls, but file edits can proceed without an approval
        # request. Only the explicit full-access mode may therefore enter Cursor's agent mode.
        deny = ["Write(**)", "Shell(*)"] if mode != "agent" else []
        cursor_config = {
            "version": 1,
            "editor": {"vimMode": False},
            "permissions": {"allow": ["Mcp(daedalus_team:*)"], "deny": deny},
            "approvalMode": "allowlist",
            "attribution": {"attributeCommitsToAgent": False, "attributePRsToAgent": False},
        }
        team_env = [
            {"name": "DAEDALUS_ASK_HOLD_MS", "value": str(spec.ask_hold_ms)},
            {"name": "DAEDALUS_REPORT_HOLD_MS", "value": str(spec.report_hold_ms)},
        ]
        servers = [{"name": "daedalus_team", "command": "sh", "args": ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp'], "env": team_env}]
        for tools in spec.tool_sets:
            servers.append({"name": tools.server, "command": "sh", "args": ["-c", tools.command()], "env": [{"name": "DAEDALUS_TOOLS_HOLD_MS", "value": str(tools.hold_ms)}]})
            for name in tools.read_only:
                cursor_config["permissions"]["allow"].append(f"Mcp({tools.server}:{name})")
        skill = spec.team_skill.split("\n---\n", 1)[1].strip() if spec.team_skill.startswith("---\n") and "\n---\n" in spec.team_skill else spec.team_skill.strip()
        prompt = spec.first_prompt
        if prompt and not resume:
            context = "\n\n".join(item for item in (spec.team_block, skill) if item)
            if context:
                prompt = f"{context}\n\n{prompt}"
        launch_config = {
            "launch_dir": LAUNCH_DIR,
            "cwd": spec.cwd,
            "mode": mode,
            "model": spec.model,
            "sandbox": "enabled",
            "resume": resume,
            "mcp_servers": servers,
        }
        files = {
            BRIDGE: Path(cursor_bridge.__file__).read_bytes(),
            CONFIG: json.dumps(launch_config).encode(),
            "cursor-config/cli-config.json": json.dumps(cursor_config).encode(),
            **{tools.path: tools.file for tools in spec.tool_sets},
        }
        return LaunchPlan(
            argv=("python3", f"{LAUNCH_DIR}/{BRIDGE}", f"{LAUNCH_DIR}/{CONFIG}", f"{DIAL_DIR}/{SOCKET}"),
            env={}, cwd=spec.cwd, files=files,
            session_ref=resume, first_prompt=prompt, first_prompt_via="channel",
        )

    def readiness(self, screen: str) -> ReadyStep:
        if "Cursor Agent could not start:" in screen:
            return ReadyStep("fail", reason=screen.rsplit("Cursor Agent could not start:", 1)[-1].splitlines()[0][:300])
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        if plan.first_prompt:
            await self.send(term, "", plan.first_prompt, "after_turn")

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        self._connections.setdefault(term.id, _Connection())

    def _state(self, term: TerminalPort) -> _Connection:
        return self._connections.setdefault(term.id, _Connection())

    async def _connect(self, term: TerminalPort) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_S
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                return await term.dial(f"unix:{SOCKET}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                await asyncio.sleep(0.2)
        raise ConnectionError(f"Cursor ACP bridge did not answer: {last}")

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        state = self._state(term)

        async def start() -> None:
            try:
                state.socket = await self._connect(term)
                await self._read(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.queue.put_nowait(StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": f"Cursor ACP bridge: {exc}"}))
            finally:
                state.queue.put_nowait(None)

        task = asyncio.create_task(start(), name=f"cursor-acp-{term.id}")
        try:
            while True:
                event = await state.queue.get()
                if event is None:
                    return
                yield event
        finally:
            task.cancel()
            if state.socket:
                with contextlib.suppress(Exception):
                    await state.socket.close()
            self._connections.pop(term.id, None)

    async def _read(self, state: _Connection) -> None:
        buffer = b""
        while state.socket and (chunk := await state.socket.read()):
            buffer += chunk
            if len(buffer) > 1 << 20:
                raise ValueError("Cursor ACP bridge sent a line longer than 1 MiB")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("type") == "hello":
                    state.session_id = str(message.get("session_id") or "")
                    state.transcript = str(message.get("transcript") or "")
                    state.queue.put_nowait(StaffEvent(EventKind.TRANSCRIPT, _now(), {"ref": state.transcript, "session_ref": state.session_id}))
                    state.ready.set()
                    state.queue.put_nowait(StaffEvent(EventKind.READY, _now(), {"session_ref": state.session_id}))
                    if message.get("busy"):
                        state.queue.put_nowait(StaffEvent(EventKind.TURN_STARTED, _now(), {"reconnected": True}))
                elif message.get("type") == "response":
                    future = state.pending.get(message.get("id"))
                    if future and not future.done():
                        future.set_result(message.get("result") or {})
                elif message.get("type") == "event":
                    try:
                        kind = EventKind(message.get("kind"))
                    except ValueError:
                        continue
                    state.queue.put_nowait(StaffEvent(kind, _now(), message.get("payload") or {}, str(message.get("native_id") or "")))
        for future in state.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Cursor ACP bridge closed"))

    async def _call(self, state: _Connection, method: str, **params: Any) -> dict[str, Any]:
        await asyncio.wait_for(state.ready.wait(), CONNECT_S)
        if not state.socket:
            raise ConnectionError("Cursor ACP bridge is not connected")
        state.next_id += 1
        ident = state.next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        state.pending[ident] = future
        try:
            await state.socket.write((json.dumps({"id": ident, "method": method, **params}, ensure_ascii=False) + "\n").encode())
            return await asyncio.wait_for(future, 15)
        finally:
            state.pending.pop(ident, None)

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        result = await self._call(self._state(term), "send", text=text, message_id=message_id)
        return Delivery(message_id, "submitted" if result.get("ok") else "failed", via="acp", error=str(result.get("error") or ""))

    async def interrupt(self, term: TerminalPort) -> None:
        await self._call(self._state(term), "interrupt")

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        result = await self._call(self._state(term), "answer", ref=request_ref, choice=answer.choice, said=answer.said)
        return bool(result.get("ok"))

    async def stop(self, term: TerminalPort) -> None:
        state = self._state(term)
        state.stopping = True
        with contextlib.suppress(Exception):
            await self._call(state, "stop")

    def classify_screen(self, text: str) -> ScreenClass:
        tail = text[-1000:]
        if "Approval requested:" in tail and "cursor>" not in tail.split("Approval requested:")[-1]:
            return ScreenClass.DIALOG
        if "Cursor Agent · working" in tail and "cursor>" not in tail.split("Cursor Agent · working")[-1]:
            return ScreenClass.BUSY
        if "cursor>" in tail:
            return ScreenClass.IDLE_COMPOSER
        return ScreenClass.UNKNOWN

    def composer_holds(self, screen: str, text: str) -> bool:
        return False  # staff delivery goes through ACP, never through terminal keystrokes

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        stat = await env.stat(ref)
        size = min(int((stat or {}).get("size") or 0), 32 << 20)
        raw = await env.read(ref, offset=0, limit=size)
        turns = []
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("role") in ("user", "assistant"):
                turns.append(Turn(len(turns), item["role"], str(item.get("text") or "")))
        return turns[since:]
