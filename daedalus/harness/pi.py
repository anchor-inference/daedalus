"""pi as a staff member: its interactive TUI in a terminal, driven through a bridge extension.

pi has no hooks, no MCP and no server of its own, but it loads TypeScript extensions without a build
step, and an extension sees the whole session. So the channel is one file, ``assets/pi_bridge.ts``,
written into the launch directory and loaded with ``-e``. Measured against pi 0.84.2 in a throwaway
home, with its model calls answered by a local stub (the recordings are in
``tests/support/fake_cli/recorded/pi``):

- **Events** arrive as posts to the launch's listener under the name ``pi``: ``ready`` (the session
  id and file, the model), ``input`` (a prompt taken in, with the id it was sent under), then
  ``agent_start``, ``tool_start``/``tool_end``, ``agent_end`` and ``agent_settled`` with the run's last
  words and how it stopped (``stop``, ``aborted``, ``error``), and ``session_end``. Only
  ``agent_settled`` ends a turn: after ``agent_end`` pi may still retry or take a queued follow-up,
  and a follow-up sent while busy runs inside the same run.
- **Messages** go over ``$DAEDALUS_DIAL_DIR/pi.sock``, which the daemon lets the host dial: idle, a
  message starts a turn; busy, a steer goes in after the running tool calls and anything else
  follows the turn. ``input`` fires for a steer the moment it is queued, so it acknowledges both.
  An abort puts a steer that had not gone in yet back into pi's composer, unsent.
- **Team tools** are the bridge's own ``Report`` and ``AskOrchestrator``, on the wire contract of the
  daemon's ``team-mcp``; the bridge says hello on the team channel when the session starts.
- **The team block** goes in with ``--append-system-prompt <file>`` (pi appends the file's contents)
  and the ``daedalus-team`` skill with ``--skill``; both were seen in the system prompt pi sends.
- **Trust.** pi asks whether to trust a folder with ``.pi`` settings; the bridge answers it for the
  process. A trust question on screen therefore means the bridge did not load.
- **No model.** Without a signed-in provider pi still starts and says ``ready`` with no model, takes
  the prompt, and prints an error without running a turn: the adapter reports that as the failure.
- **Permissions and questions:** none, by design. pi runs every tool it is given.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from daedalus.harness import register
from daedalus.harness.capabilities import capabilities
from daedalus.harness.contract import (
    LAUNCH_DIR,
    Answer,
    Catalog,
    CheckResult,
    CheckStep,
    Delivery,
    EnvironmentPort,
    EventKind,
    HookPost,
    HookSpec,
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
    ToolUse,
    Turn,
    TurnUsage,
    UpdateResult,
)
from daedalus.harness.tools import tooling

logger = logging.getLogger(__name__)

BRIDGE_FILE = "pi_bridge.ts"
SYSTEM_FILE = "system.md"
SKILL_FILE = "skills/daedalus-team/SKILL.md"
SOCKET = "unix:pi.sock"
HOOK_SOURCE = "pi"
THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
"""``--thinking`` as pi 0.84 lists it; an effort pi does not know is left out rather than refused."""
BRIDGE_TIMEOUT_S = 10.0
"""An operation on the bridge socket is answered at once; this bounds a pi that stopped reading."""
TRANSCRIPT_MAX_BYTES = 32 << 20
NO_MODEL = "pi has no model it can use here (no provider is signed in): sign in from the Harnesses screen or in this terminal"
DIALOG_MARKERS = ("↑↓ navigate", "enter select", "Trust project folder?")
BUSY_MARKER = "Working..."
_RULE_CHAR = "─"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def bridge_source() -> bytes:
    """The bridge extension as shipped with the host; written into every launch unchanged."""
    return resources.files("daedalus.harness").joinpath("assets", BRIDGE_FILE).read_bytes()


def _rules(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if len(line.strip()) >= 8 and set(line.strip()) == {_RULE_CHAR}]


def composer(screen: str) -> str | None:
    """The text in pi's editor: the lines between the last two rules, above the footer's two lines
    (the folder, the context meter and the model). None when no editor is drawn."""
    lines = screen.splitlines()
    rules = _rules(lines)
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    return "\n".join(line.strip() for line in lines[top + 1 : bottom]).strip()


@dataclass(eq=False)
class _Launch:
    queue: asyncio.Queue[StaffEvent | None] = field(default_factory=asyncio.Queue)
    no_model: bool = False
    """pi said it was ready without a model: every prompt it takes fails without a turn."""


@register
class PiAdapter:
    """:class:`daedalus.harness.contract.HarnessAdapter` for pi. See the module's description."""

    name = "pi"
    capabilities = capabilities("pi")

    def __init__(self) -> None:
        self.tooling = tooling("pi")
        self._launches: dict[str, _Launch] = {}

    # -- the manager's half, through the CLI's tooling ------------------------------------------

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return await self.tooling.installed(env)

    async def latest(self, env: EnvironmentPort) -> str:
        """Asked of npm by the harness manager, which has the fetcher."""
        return ""

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        """``pi update self`` (``pi update --help``: "self works as alias to pi", pi only, not its
        packages). The manager updates an npm install through npm instead, to keep its prefix."""
        before = await self.installed(env)
        result = await env.run(["pi", "update", "self"], timeout=600)
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

    # -- launching ------------------------------------------------------------------------------

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan:
        return self._plan(spec, str(uuid.uuid4()))

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        """``--session-id`` takes up the session when it exists and makes it when it does not, so a
        resume is the same launch with the old id."""
        return self._plan(spec, ref)

    def _plan(self, spec: LaunchSpec, session: str) -> LaunchPlan:
        spec.validate_permissions(())
        files: dict[str, bytes] = {BRIDGE_FILE: bridge_source()}
        argv: list[str] = ["pi", "--session-id", session, "-e", f"{LAUNCH_DIR}/{BRIDGE_FILE}"]
        if spec.team_block:
            files[SYSTEM_FILE] = spec.team_block.encode()
            argv += ["--append-system-prompt", f"{LAUNCH_DIR}/{SYSTEM_FILE}"]
        if spec.team_skill:
            files[SKILL_FILE] = spec.team_skill.encode()
            argv += ["--skill", f"{LAUNCH_DIR}/{SKILL_FILE}"]
        if spec.title:
            argv += ["--name", spec.title[:120]]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort in THINKING:
            argv += ["--thinking", spec.effort]
        prompt = spec.first_prompt
        if prompt:
            # A message is one argument; the runtime starts every first prompt with its "[team]" line,
            # so it never begins with "@", which pi would read as a file to attach.
            argv.append(prompt)
        env = {"DAEDALUS_ASK_HOLD_MS": str(spec.ask_hold_ms), "DAEDALUS_REPORT_HOLD_MS": str(spec.report_hold_ms)}
        if spec.tool_sets:
            # pi has no MCP: its bridge reads each set's file from the launch and registers the tools.
            files.update({tools.path: tools.file for tools in spec.tool_sets})
            env["DAEDALUS_TOOL_SETS"] = ",".join(tools.name for tools in spec.tool_sets)
            env["DAEDALUS_TOOLS_HOLD_MS"] = str(max(tools.hold_ms for tools in spec.tool_sets))
        return LaunchPlan(argv=tuple(argv), env=env, cwd=spec.cwd, files=files, session_ref=session, first_prompt=prompt, first_prompt_via="argv", hooks=HookSpec(sources=(HOOK_SOURCE,)))

    def readiness(self, screen: str) -> ReadyStep:
        if "Trust project folder?" in screen:
            return ReadyStep("fail", reason="pi asks whether to trust the folder, which its bridge extension answers: the bridge did not load")
        if "No models available" in screen:
            return ReadyStep("fail", reason=NO_MODEL)
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        """Nothing to hand over: the first prompt is on the command line."""

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        """Nothing to re-dial: every operation opens the bridge socket afresh."""
        self._launches.setdefault(term.id, _Launch())

    # -- what it does ---------------------------------------------------------------------------

    def _state(self, term: TerminalPort) -> _Launch:
        return self._launches.setdefault(term.id, _Launch())

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        state = self._state(term)

        async def pump() -> None:
            try:
                async for post in term.hooks():
                    for event in self._map(state, post):
                        state.queue.put_nowait(event)
            finally:
                state.queue.put_nowait(None)

        feeder = asyncio.create_task(pump(), name=f"pi-events-{term.id}")
        try:
            while True:
                item = await state.queue.get()
                if item is None:
                    return
                yield item
        finally:
            feeder.cancel()
            self._launches.pop(term.id, None)

    def _map(self, state: _Launch, post: HookPost) -> list[StaffEvent]:
        body: Mapping[str, Any] = post.body if isinstance(post.body, dict) else {}
        if post.name != HOOK_SOURCE:
            return [StaffEvent(EventKind.ACTIVITY, post.at or _now(), {"hook": post.name})]
        at = str(body.get("at") or post.at or _now())
        event = str(body.get("event") or "")
        if event == "ready":
            located = StaffEvent(EventKind.TRANSCRIPT, at, {"ref": str(body.get("sessionFile") or ""), "session_ref": str(body.get("sessionId") or "")})
            # Measured: with no provider signed in pi reports its model as "unknown/unknown".
            model = str(body.get("model") or "")
            state.no_model = not model or model.split("/", 1)[0] == "unknown"
            if state.no_model:
                return [located, StaffEvent(EventKind.READY, at, {"source": str(body.get("reason") or "")}), StaffEvent(EventKind.TURN_FAILED, at, {"failure": NO_MODEL})]
            return [located, StaffEvent(EventKind.READY, at, {"source": str(body.get("reason") or "")})]
        if event == "input":
            acknowledged = StaffEvent(EventKind.PROMPT_ACKNOWLEDGED, at, {"prompt": str(body.get("text") or ""), "message_id": str(body.get("id") or ""), "source": str(body.get("source") or "")})
            if state.no_model:
                # pi takes the prompt and prints its error; no run starts, so no turn will end.
                return [acknowledged, StaffEvent(EventKind.TURN_FAILED, at, {"failure": NO_MODEL})]
            return [acknowledged]
        if event == "agent_start":
            return [StaffEvent(EventKind.TURN_STARTED, at)]
        if event == "tool_start":
            return [StaffEvent(EventKind.TOOL_STARTED, at, {"tool": str(body.get("toolName") or "")}, native_id=str(body.get("toolCallId") or ""))]
        if event == "tool_end":
            return [StaffEvent(EventKind.TOOL_FINISHED, at, {"tool": str(body.get("toolName") or ""), "ok": not body.get("isError")}, native_id=str(body.get("toolCallId") or ""))]
        if event == "agent_settled":
            stop = str(body.get("stopReason") or "")
            if stop == "aborted":
                return [StaffEvent(EventKind.TURN_CANCELLED, at, {"via": "abort"})]
            if stop == "error":
                return [StaffEvent(EventKind.TURN_FAILED, at, {"failure": str(body.get("errorMessage") or "the turn failed")[:500]})]
            return [StaffEvent(EventKind.TURN_COMPLETED, at, {"last_message": str(body.get("lastMessage") or "")})]
        if event == "session_end":
            return [StaffEvent(EventKind.SESSION_ENDED, at, {"reason": str(body.get("reason") or "")})]
        if event == "bridge_error":
            # The socket did not listen: nothing can be sent to this member, which is a failure the
            # operator must see, not a silence.
            return [StaffEvent(EventKind.TURN_FAILED, at, {"failure": f"pi's bridge cannot take messages: {str(body.get('error') or '')[:300]}"})]
        # agent_end and anything newer: the member is alive and busy, nothing more.
        return [StaffEvent(EventKind.ACTIVITY, at, {"pi": event})]

    async def _bridge(self, term: TerminalPort, request: Mapping[str, Any]) -> dict[str, Any]:
        """One operation on the bridge socket: a line out, a line back."""
        stream = await term.dial(SOCKET)
        try:
            await stream.write((json.dumps(request) + "\n").encode())
            buffer = b""
            async with asyncio.timeout(BRIDGE_TIMEOUT_S):
                while b"\n" not in buffer:
                    chunk = await stream.read()
                    if not chunk:
                        break
                    buffer += chunk
        finally:
            with contextlib.suppress(Exception):
                await stream.close()
        line = buffer.split(b"\n", 1)[0]
        reply = json.loads(line) if line.strip() else {}
        if not isinstance(reply, dict):
            raise ValueError("the bridge answered with something other than an object")
        return reply

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        """Hand a message to pi through the bridge. It is ``submitted`` once pi has it; the ``input``
        event that follows, naming the same id, acknowledges it."""
        request: dict[str, Any] = {"op": "send", "id": message_id, "text": text}
        if mode == "now":
            request["deliverAs"] = "steer"
        try:
            reply = await self._bridge(term, request)
        except Exception as exc:  # noqa: BLE001 — a bridge that cannot be reached is this message's failure
            return Delivery(message_id, "failed", via="bridge", error=f"pi's bridge could not be reached: {exc}"[:500])
        if not reply.get("ok"):
            return Delivery(message_id, "failed", via="bridge", error=str(reply.get("error") or "pi's bridge refused the message")[:500])
        return Delivery(message_id, "submitted", via="bridge", client_ref=message_id)

    async def interrupt(self, term: TerminalPort) -> None:
        """pi's own abort; ``agent_settled`` with ``aborted`` confirms it as the turn's end."""
        await self._bridge(term, {"op": "abort"})

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        """pi asks nothing: it has no permission requests and no question tool."""
        return False

    async def stop(self, term: TerminalPort) -> None:
        """pi's graceful exit through the bridge, which pi holds back until it is idle. Nothing is
        typed: whatever the screen shows is left alone, and the runtime ends the terminal after its
        grace if pi does not go."""
        with contextlib.suppress(Exception):
            await self._bridge(term, {"op": "shutdown"})

    # -- the screen -----------------------------------------------------------------------------

    def classify_screen(self, text: str) -> ScreenClass:
        lines = text.splitlines()
        bottom = "\n".join(lines[-30:])
        if any(marker in bottom for marker in DIALOG_MARKERS):
            return ScreenClass.DIALOG
        rules = _rules(lines)
        if len(rules) < 2:
            return ScreenClass.UNKNOWN
        above = "\n".join(lines[max(0, rules[-2] - 4) : rules[-2]])
        return ScreenClass.BUSY if BUSY_MARKER in above else ScreenClass.IDLE_COMPOSER

    def composer_holds(self, screen: str, text: str) -> bool:
        held = composer(screen)
        tail = "".join(text.split())[-40:]
        return bool(held) and bool(tail) and tail in "".join(held.split())

    # -- the transcript -------------------------------------------------------------------------

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        stat = await env.stat(ref)
        size = int((stat or {}).get("size") or 0)
        offset = max(0, size - TRANSCRIPT_MAX_BYTES)
        raw = await env.read(ref, offset=offset, limit=TRANSCRIPT_MAX_BYTES)
        text = raw.decode("utf-8", "replace")
        if offset:
            text = text.split("\n", 1)[1] if "\n" in text else ""
        return parse_transcript(text)[since:]


def _stamp(entry: Mapping[str, Any]) -> str:
    return str(entry.get("timestamp") or "")


def _summary(name: str, arguments: Mapping[str, Any]) -> str:
    for key in ("command", "path", "file_path", "pattern", "question", "note"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:300]
    return name


def parse_transcript(text: str) -> list[Turn]:
    """pi's session file (JSON lines, version 3) as turns. Entries of type ``message`` carry the
    conversation: ``user`` prompts, ``assistant`` messages (text and tool calls, one message per model
    call, each with its own usage), and ``toolResult`` for each call. The other entry types — the
    header, names, model and thinking changes, custom entries — and an unreadable line are skipped."""
    turns: list[Turn] = []
    current: dict[str, Any] | None = None
    tools_by_id: dict[str, int] = {}

    def close() -> None:
        nonlocal current
        if current is not None:
            usage = current["usage"]
            spent = TurnUsage(usage[0], usage[1], usage[2], usage[3] if usage[4] else None) if any(usage[:3]) or usage[4] else None
            turns.append(Turn(len(turns), "assistant", "\n".join(current["text"]).strip(), tuple(current["tools"]), current["at"], current["end"], spent))
            current = None

    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        raw_message = entry.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        role = message.get("role")
        at = _stamp(entry)
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if role == "user":
            words = content.strip() if isinstance(content, str) else "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text").strip()
            if not words:
                continue
            close()
            turns.append(Turn(len(turns), "orchestrator" if words.startswith("[orchestrator]") else "user", words, started_at=at, ended_at=at))
            continue
        if role == "assistant":
            if current is None:
                current = {"text": [], "tools": [], "at": at, "end": at, "usage": [0, 0, 0, 0.0, False]}
            current["end"] = at
            raw_usage = message.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            current["usage"][0] += int(usage.get("input") or 0) + int(usage.get("cacheWrite") or 0)
            current["usage"][1] += int(usage.get("output") or 0)
            current["usage"][2] += int(usage.get("cacheRead") or 0)
            cost = usage.get("cost")
            if isinstance(cost, dict) and isinstance(cost.get("total"), int | float) and cost["total"]:
                current["usage"][3] += float(cost["total"])
                current["usage"][4] = True
            for block in blocks:
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    current["text"].append(str(block["text"]))
                elif block.get("type") == "toolCall":
                    arguments = block.get("arguments")
                    name = str(block.get("name") or "")
                    tools_by_id[str(block.get("id") or "")] = len(current["tools"])
                    current["tools"].append(ToolUse(name, _summary(name, arguments if isinstance(arguments, dict) else {})))
            if message.get("stopReason") == "aborted":
                close()
                turns.append(Turn(len(turns), "system", "[interrupted]", started_at=at, ended_at=at))
            continue
        if role == "toolResult" and current is not None:
            index = tools_by_id.get(str(message.get("toolCallId") or ""))
            if index is not None and index < len(current["tools"]):
                used = current["tools"][index]
                current["tools"][index] = ToolUse(used.name, used.summary, not bool(message.get("isError")))
    close()
    return turns


__all__ = ["PiAdapter", "bridge_source", "composer", "parse_transcript"]
