"""Grok Build as a staff member: its interactive TUI in a terminal, driven through its hooks.

Measured against Grok Build 1.0.41 in a throwaway home, its model calls answered by a local stub
(the recordings are in ``tests/support/fake_cli/recorded/grok``). What the adapter rests on:

- **Everything per launch, in an agent file.** Grok's configuration overlay cannot carry hooks or MCP
  servers, but an agent definition given with ``--agent <file>`` can, and for the session itself, not
  only for subagents: the file in the launch directory declares command hooks that run
  ``"$DAEDALUS_PTYD_BIN" hook <Event>`` and the ``daedalus_team`` MCP server (``mcpServers`` as a
  list of ``{name, command, args, env}``; a map is not read, and the whole file is then ignored).
  Its body extends Grok's own prompt ("promptMode": "extend"). Nothing goes into ``~/.grok``.
- **The team block** goes in with ``--rules``, which Grok puts in its system prompt as the human's
  rules. The ``daedalus-team`` skill is the agent file's body.
- **Team tools** are deferred by Grok behind ``search_tool``/``use_tool``; ``--allow
  'MCPTool(daedalus_team__*)'`` lets them run without asking. Grok's own ``ask_user_question`` is
  removed (``--disallowed-tools``): a staff member asks its orchestrator, never the terminal.
- **Hooks.** Payloads are camelCase, with Claude's names beside them. ``UserPromptSubmit`` names the
  prompt and fires when a prompt starts — for a message queued behind a running turn, only when that
  turn is over. ``Stop`` with ``reason: end_turn`` ends a turn and carries ``lastAssistantMessage``;
  a second ``Stop`` with ``reason: shutdown`` and no ``promptId`` follows ``SessionEnd`` and is not a
  turn. A cancelled turn fires ``StopCancelled`` (``user_interrupt``, ``permission_rejected``,
  ``permission_cancelled``).
- **Permissions.** ``PreToolUse`` names the tool and its input; ``Notification permission_prompt``
  follows within milliseconds, only when the dialog is really waiting. The dialog's rows are numbered
  and a digit chooses and confirms: 1 is "always-approve mode" for everything (never chosen here),
  2 "Yes, proceed", 3 "No, reject", 4 "Never allow". Rejecting ends the turn. The launch pre-selects
  "Yes, proceed" (``GROK_DEFAULT_SELECTED_PERMISSION=allow_once``) instead of Grok's default.
- **Interrupt** is Ctrl+C; Esc only says to press Ctrl+C. Enter while busy queues the message after
  the turn, so a steer is an interrupt followed by the message.
- **The operator's Claude hooks** in ``~/.claude/settings.json`` run in every Grok session unless
  ``GROK_CLAUDE_HOOKS_ENABLED=false``, which the launch sets.
- **The transcript** is ``updates.jsonl`` in the session's directory, whose path every hook but the
  first carries as ``transcriptPath``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    ToolSetSpec,
    ToolUse,
    Turn,
    TurnUsage,
    UpdateResult,
)
from daedalus.harness.tools import tooling

logger = logging.getLogger(__name__)

AGENT_FILE = "daedalus-staff.md"
AGENT_NAME = "daedalus-staff"
HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied",
    "Stop", "StopFailure", "StopCancelled", "Notification", "SessionEnd",
)
HOOK_TIMEOUT_S = 10
TEAM_RULE = "MCPTool(daedalus_team__*)"
PERMISSION_MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
LEVEL_MODES = {"ask": "default", "edits": "acceptEdits", "all": "bypassPermissions"}
EFFORTS = ("low", "medium", "high")
DIALOG_WAIT_S = 10.0
DIALOG_CONFIRM_S = 5.0
TRANSCRIPT_MAX_BYTES = 32 << 20
SIGN_IN = "Approve in your browser to finish signing in."
DIALOG_MARKERS = ("Tab:next option", "Yes, proceed", "No, reject", SIGN_IN, "Waiting for approval...")
BUSY_MARKERS = ("Ctrl+c:cancel", "Responding…", "Thinking…")
GHOST_HINT = "accept suggestion"
ALLOW_ROW, DENY_ROW = ("2", "Yes, proceed"), ("3", "No, reject")
_ROW = re.compile(r"^\s*┃?\s*(\d)\s+\((?:●|○)\)\s+(.*?)\s*$")


def start_mode(permission_mode: str, permission_level: str) -> str:
    """The mode Grok starts in: the member's own when it has one, else the project's autonomy; the
    member's Details say from it how a setting is kept."""
    if permission_mode in PERMISSION_MODES:
        return permission_mode
    return LEVEL_MODES[permission_level]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def agent_file(skill_text: str, env: Mapping[str, str], tool_sets: Sequence[ToolSetSpec] = ()) -> str:
    """The launch's agent definition: the hooks, the team's MCP server, and the skill as its body.

    The front matter is YAML written as JSON, one key per line: YAML reads it, and so does anything
    that reads JSON (the fake CLI). ``mcpServers`` must be a list — Grok ignores the whole file when
    it is a map (measured)."""
    hooks = {
        event: [{"hooks": [{"type": "command", "command": f'"$DAEDALUS_PTYD_BIN" hook {event}', "timeout": HOOK_TIMEOUT_S}]}]
        for event in HOOK_EVENTS
    }
    server = {
        "name": "daedalus_team",
        # The daemon's path is known only once the launch is registered, after this file is written;
        # the shell reads it from the launch's environment.
        "command": "sh",
        "args": ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp'],
        "env": [{"name": name, "value": value} for name, value in env.items()],
    }
    body = skill_text.split("\n---\n", 1)[1].strip() if skill_text.startswith("---\n") and "\n---\n" in skill_text else skill_text.strip()
    lines = [
        "---",
        f"name: {AGENT_NAME}",
        'description: "A staff member of a Daedalus project, working under its orchestrator."',
        f"hooks: {json.dumps(hooks)}",
        f"mcpServers: {json.dumps([server, *(_set_server(t) for t in tool_sets)])}",
        "---",
        "",
        body or "You are a staff member of a Daedalus project. Report to your orchestrator with the Report tool.",
        "",
    ]
    return "\n".join(lines)


def _set_server(tools: ToolSetSpec) -> dict[str, Any]:
    """One of Daedalus's other tool sets (the browser's) as an entry of the agent file's list."""
    return {"name": tools.server, "command": "sh", "args": ["-c", tools.command()], "env": [{"name": "DAEDALUS_TOOLS_HOLD_MS", "value": str(tools.hold_ms)}]}


def _box_text(line: str) -> str:
    """The text inside one line of Grok's composer box: ``│ ❯ text │`` or ``│   more │``."""
    inner = line.strip()
    inner = inner[1:] if inner.startswith("│") else inner
    inner = inner[:-1] if inner.endswith("│") else inner
    inner = inner.rstrip()
    # A scrollbar cell the box draws at its right edge when the text is long.
    inner = re.sub(r"\s[▁▂▃▄▅▆▇█]$", "", inner)
    return inner.strip()


def composer(screen: str) -> str | None:
    """The text in Grok's composer: the lines of the last box drawn with ``╭``/``╰`` whose first row
    starts with ``❯``. A suggestion Grok offers there (the footer then reads "Tab/→:accept
    suggestion") is not text anyone typed: it counts as empty. None when no composer is drawn."""
    lines = screen.splitlines()
    tops = [i for i, line in enumerate(lines) if line.lstrip().startswith("╭")]
    for top in reversed(tops):
        bottom = next((i for i in range(top + 1, len(lines)) if lines[i].lstrip().startswith("╰")), None)
        if bottom is None or bottom == top + 1:
            continue
        first = _box_text(lines[top + 1])
        if not first.startswith("❯"):
            continue
        footer = "\n".join(lines[bottom + 1 : bottom + 3])
        if GHOST_HINT in footer:
            return ""
        rows = [first[1:].strip(), *(_box_text(line) for line in lines[top + 2 : bottom])]
        return "\n".join(rows).strip()
    return None


def _rows(screen: str) -> dict[str, str]:
    """The permission dialog's numbered rows: digit → label."""
    found: dict[str, str] = {}
    for line in screen.splitlines():
        match = _ROW.match(line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def _summary(tool: str, tool_input: Mapping[str, Any]) -> str:
    for key in ("command", "file_path", "path", "target_file", "url", "query", "pattern", "tool_name", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:300]
    return tool


@dataclass(eq=False)
class _Request:
    tool: str
    summary: str
    settled: bool = False


@dataclass(eq=False)
class _Launch:
    queue: asyncio.Queue[HookPost | StaffEvent | None] = field(default_factory=asyncio.Queue)
    tools: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    """The latest PreToolUse per tool-use id: the permission prompt that follows names no tool."""
    last_tool: str = ""
    requests: dict[str, _Request] = field(default_factory=dict)
    transcript: str = ""
    count: int = 0


@register
class GrokAdapter:
    """:class:`daedalus.harness.contract.HarnessAdapter` for Grok Build. See the module's description."""

    name = "grok"
    capabilities = capabilities("grok")

    def __init__(self) -> None:
        self.tooling = tooling("grok")
        self._launches: dict[str, _Launch] = {}

    # -- the manager's half, through the CLI's tooling ------------------------------------------

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return await self.tooling.installed(env)

    async def latest(self, env: EnvironmentPort) -> str:
        """Asked of Grok itself by the harness manager (``grok update --check --json``)."""
        return ""

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        before = await self.installed(env)
        result = await env.run(["grok", "update"], timeout=600)
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
        return self._plan(spec, str(uuid.uuid4()), resume=False)

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        return self._plan(spec, ref, resume=True)

    def _mode(self, spec: LaunchSpec) -> str:
        spec.validate_permissions(PERMISSION_MODES)
        return start_mode(spec.permission_mode, spec.permission_level)

    def _plan(self, spec: LaunchSpec, session: str, *, resume: bool) -> LaunchPlan:
        holds = {"DAEDALUS_ASK_HOLD_MS": str(spec.ask_hold_ms), "DAEDALUS_REPORT_HOLD_MS": str(spec.report_hold_ms)}
        files = {AGENT_FILE: agent_file(spec.team_skill, holds, spec.tool_sets).encode(), **{tools.path: tools.file for tools in spec.tool_sets}}
        # "-s" names a new session and is refused for one that exists; "-r" takes one up.
        argv: list[str] = ["grok", "--cwd", spec.cwd, "-r" if resume else "-s", session, "--trust", "--agent", f"{LAUNCH_DIR}/{AGENT_FILE}"]
        if spec.team_block:
            argv += ["--rules", spec.team_block]
        argv += ["--allow", TEAM_RULE]
        # The tools of each set that only read run unasked; the rest ask by the member's mode.
        for tools in spec.tool_sets:
            for name in tools.read_only:
                argv += ["--allow", f"MCPTool({tools.server}__{name})"]
        argv += ["--disallowed-tools", "ask_user_question"]
        if spec.model:
            argv += ["-m", spec.model]
        argv += ["--permission-mode", self._mode(spec)]
        if spec.effort in EFFORTS:
            argv += ["--effort", spec.effort]
        prompt = spec.first_prompt
        if prompt:
            argv += ["--", prompt]
        env = {
            **holds,
            # Grok's own default pre-selects the row that approves everything for good.
            "GROK_DEFAULT_SELECTED_PERMISSION": "allow_once",
            # The operator's Claude hooks are theirs, not a staff member's (measured: they run otherwise).
            "GROK_CLAUDE_HOOKS_ENABLED": "false",
        }
        return LaunchPlan(argv=tuple(argv), env=env, cwd=spec.cwd, files=files, session_ref=session, first_prompt=prompt, first_prompt_via="argv", hooks=HookSpec(sources=HOOK_EVENTS))

    def readiness(self, screen: str) -> ReadyStep:
        if SIGN_IN in screen or "Waiting for approval..." in screen:
            return ReadyStep("fail", reason="Grok Build is not signed in here (it started its sign-in): sign in from the Harnesses screen or in this terminal")
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        """Nothing to hand over: the first prompt is on the command line."""

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        self._launches.setdefault(term.id, _Launch())

    # -- what it does ---------------------------------------------------------------------------

    def _state(self, term: TerminalPort) -> _Launch:
        return self._launches.setdefault(term.id, _Launch())

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        state = self._state(term)

        async def pump() -> None:
            try:
                async for post in term.hooks():
                    state.queue.put_nowait(post)
            finally:
                state.queue.put_nowait(None)

        feeder = asyncio.create_task(pump(), name=f"grok-hooks-{term.id}")
        try:
            while True:
                item = await state.queue.get()
                if item is None:
                    return
                if isinstance(item, StaffEvent):
                    yield item
                    continue
                for event in self._map(state, item):
                    yield event
        finally:
            feeder.cancel()
            self._launches.pop(term.id, None)

    def _map(self, state: _Launch, post: HookPost) -> list[StaffEvent]:
        body: Mapping[str, Any] = post.body if isinstance(post.body, dict) else {}
        at = post.at or _now()
        name = post.name
        events: list[StaffEvent] = []
        path = str(body.get("transcriptPath") or "")
        if path and path != state.transcript:
            state.transcript = path
            events.append(StaffEvent(EventKind.TRANSCRIPT, at, {"ref": path, "session_ref": str(body.get("sessionId") or "")}))
        if body.get("subagentType"):
            # A subagent's own tools and turns are its parent's activity, not the member's turn.
            return [*events, StaffEvent(EventKind.ACTIVITY, at, {"hook": name, "subagent": str(body["subagentType"])})]
        tool = str(body.get("toolName") or "")
        raw_input = body.get("toolInput")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        tool_id = str(body.get("toolUseId") or "")
        if name == "SessionStart":
            return [*events, StaffEvent(EventKind.TRANSCRIPT, at, {"ref": state.transcript, "session_ref": str(body.get("sessionId") or "")}), StaffEvent(EventKind.READY, at, {"source": str(body.get("source") or "")})]
        if name == "UserPromptSubmit":
            return [*events, StaffEvent(EventKind.PROMPT_ACKNOWLEDGED, at, {"prompt": str(body.get("prompt") or ""), "turn": str(body.get("promptId") or "")})]
        if name == "PreToolUse":
            ref = tool_id or f"tool-{uuid.uuid4().hex[:8]}"
            state.tools[ref] = (tool, dict(tool_input))
            state.last_tool = ref
            while len(state.tools) > 64:
                state.tools.pop(next(iter(state.tools)))
            return [*events, StaffEvent(EventKind.TOOL_STARTED, at, {"tool": tool}, native_id=ref)]
        if name == "Notification":
            kind = str(body.get("notificationType") or "")
            if kind == "permission_prompt" and state.last_tool and state.last_tool not in state.requests:
                # Grok says a permission dialog waits only while it really does; the tool it is for
                # is the one whose PreToolUse came just before.
                ref = state.last_tool
                asked_tool, asked_input = state.tools.get(ref, ("", {}))
                summary = _summary(asked_tool, asked_input)
                state.requests[ref] = _Request(asked_tool, summary)
                return [*events, StaffEvent(EventKind.PERMISSION_REQUESTED, at, {"tool": asked_tool, "summary": summary, "options": ["allow_once", "deny"]}, native_id=ref)]
            # Never a status: "idle", "task complete" and the like report a state, not a request.
            return [*events, StaffEvent(EventKind.NOTIFICATION, at, {"type": kind, "message": str(body.get("message") or "")[:200]})]
        if name in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            resolved = [StaffEvent(EventKind.REQUEST_RESOLVED, at, {}, native_id=tool_id)] if state.requests.pop(tool_id, None) is not None else []
            return [*events, *resolved, StaffEvent(EventKind.TOOL_FINISHED, at, {"tool": tool, "ok": name == "PostToolUse"}, native_id=tool_id)]
        if name == "Stop":
            if body.get("reason") != "end_turn" or not body.get("promptId"):
                # The session's own last word as it shuts down, after SessionEnd: not a turn.
                return events
            return [*events, *self._settle(state, at), StaffEvent(EventKind.TURN_COMPLETED, at, {"last_message": str(body.get("lastAssistantMessage") or "")})]
        if name == "StopCancelled":
            return [*events, *self._settle(state, at), StaffEvent(EventKind.TURN_CANCELLED, at, {"via": str(body.get("reason") or "")})]
        if name == "StopFailure":
            failure = str(body.get("error") or body.get("reason") or "the turn failed")
            return [*events, *self._settle(state, at), StaffEvent(EventKind.TURN_FAILED, at, {"failure": failure.replace("_", " ")})]
        if name == "SessionEnd":
            return [*events, StaffEvent(EventKind.SESSION_ENDED, at, {"reason": str(body.get("reason") or "")})]
        return [*events, StaffEvent(EventKind.ACTIVITY, at, {"hook": name})]

    @staticmethod
    def _settle(state: _Launch, at: str) -> list[StaffEvent]:
        """A turn that ended has nothing waiting any more, whoever settled it."""
        refs = list(state.requests)
        state.requests.clear()
        return [StaffEvent(EventKind.REQUEST_RESOLVED, at, {}, native_id=ref) for ref in refs]

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        """Messages are pasted by the runtime's delivery worker; this is the plain form of it, for a
        caller without one (the self-check)."""
        await term.write(paste=text)
        await asyncio.sleep((self.capabilities.paste.burst_guard_ms if self.capabilities.paste else 0) / 1000 + 0.3)
        await term.write(keys=["Enter"])
        return Delivery(message_id, "submitted", via="paste")

    async def interrupt(self, term: TerminalPort) -> None:
        """One Ctrl+C: Esc does not stop a turn in Grok. ``StopCancelled`` confirms it."""
        await term.write(keys=["C-c"])

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        """The dialog's digit, once the dialog is on screen, names the command asked about and has the
        rows where they are expected. "Always" is answered as once: the dialog's only standing
        approval is the one for everything."""
        request = self._state(term).requests.get(request_ref)
        if request is None:
            return False
        digit, label = ALLOW_ROW if answer.choice.startswith("allow") else DENY_ROW
        if not await term.wait_for(regex=re.escape(ALLOW_ROW[1]), timeout=DIALOG_WAIT_S):
            return False
        screen = await term.screen()
        squeezed = "".join(screen.split())
        if request.summary and "".join(request.summary[:40].split()) not in squeezed:
            return False
        if not _rows(screen).get(digit, "").startswith(label):
            return False
        await term.write(text=digit)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DIALOG_CONFIRM_S
        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            if ALLOW_ROW[1] not in await term.screen():
                request.settled = True
                return True
        return False

    async def stop(self, term: TerminalPort) -> None:
        screen = await term.screen()
        kind = self.classify_screen(screen)
        if kind is ScreenClass.DIALOG:
            return  # typing would answer the dialog; the runtime ends the terminal after its grace
        if kind is ScreenClass.BUSY:
            await term.write(keys=["C-c"])
            await asyncio.sleep(1.0)
        await term.write(paste="/exit")
        await asyncio.sleep(0.5)
        await term.write(keys=["Enter"])

    # -- the screen -----------------------------------------------------------------------------

    def classify_screen(self, text: str) -> ScreenClass:
        lines = text.splitlines()
        bottom = "\n".join(lines[-24:])
        if any(marker in bottom for marker in DIALOG_MARKERS):
            return ScreenClass.DIALOG
        if composer(text) is None:
            return ScreenClass.UNKNOWN
        tail = "\n".join(lines[-8:])
        if any(marker in tail for marker in BUSY_MARKERS):
            return ScreenClass.BUSY
        return ScreenClass.IDLE_COMPOSER

    def composer_holds(self, screen: str, text: str) -> bool:
        held = composer(screen)
        if not held:
            return False
        if "[Pasted:" in held:
            return True
        tail = "".join(text.split())[-40:]
        return bool(tail) and tail in "".join(held.split())

    # -- the transcript -------------------------------------------------------------------------

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        stat = await env.stat(ref)
        size = int((stat or {}).get("size") or 0)
        offset = max(0, size - TRANSCRIPT_MAX_BYTES)
        raw = await env.read(ref, offset=offset, limit=TRANSCRIPT_MAX_BYTES)
        text = raw.decode("utf-8", "replace")
        if offset:
            text = text.split("\n", 1)[1] if "\n" in text else ""
        usage: list[dict[str, Any]] = []
        folder = ref.rsplit("/", 1)[0]
        with contextlib.suppress(Exception):
            if await env.stat(f"{folder}/usage.json"):
                data = json.loads(await env.read(f"{folder}/usage.json", limit=4 << 20))
                usage = [t for t in data.get("turns") or [] if isinstance(t, dict)]
        return parse_transcript(text, usage)[since:]


def parse_transcript(text: str, usage: list[dict[str, Any]] | None = None) -> list[Turn]:
    """Grok's ``updates.jsonl`` as turns. Each line is an ACP session update
    (``{"method": "_x.ai/session/update", "params": {"update": {...}}}``): ``user_message_chunk`` a
    prompt, ``agent_message_chunk`` the reply in pieces, ``tool_call``/``tool_call_update`` a tool and
    how it ended, ``turn_completed`` the end with its ``stop_reason``. Hook runs and anything else are
    skipped. ``usage`` is ``usage.json``'s per-turn list, laid on the replies in order."""
    turns: list[Turn] = []
    current: dict[str, Any] | None = None
    tools_by_id: dict[str, int] = {}
    ended = 0
    spent = usage or []

    def close(at: str) -> None:
        nonlocal current
        if current is not None:
            turns.append(Turn(len(turns), "assistant", "".join(current["text"]).strip(), tuple(current["tools"]), current["at"], at or current["at"], current.get("usage")))
            current = None

    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        params = record.get("params") if isinstance(record.get("params"), dict) else record
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        kind = update.get("sessionUpdate")
        stamp = record.get("timestamp")
        at = datetime.fromtimestamp(stamp, UTC).isoformat() if isinstance(stamp, int | float) else str(stamp or "")
        content = update.get("content")
        words = str(content.get("text") or "") if isinstance(content, dict) else ""
        if kind == "user_message_chunk":
            close(at)
            if words.strip():
                role = "orchestrator" if words.lstrip().startswith("[orchestrator]") else "user"
                turns.append(Turn(len(turns), role, words.strip(), started_at=at, ended_at=at))  # type: ignore[arg-type]
            continue
        if kind in ("agent_message_chunk", "tool_call", "tool_call_update") and current is None:
            current = {"text": [], "tools": [], "at": at}
        if kind == "agent_message_chunk" and current is not None:
            current["text"].append(words)
        elif kind == "tool_call" and current is not None:
            raw_input = update.get("rawInput")
            name = str(update.get("title") or "")
            tools_by_id[str(update.get("toolCallId") or "")] = len(current["tools"])
            current["tools"].append(ToolUse(name, _summary(name, raw_input if isinstance(raw_input, dict) else {})))
        elif kind == "tool_call_update" and current is not None:
            index = tools_by_id.get(str(update.get("toolCallId") or ""))
            status = update.get("status")
            if index is not None and status in ("completed", "failed"):
                used = current["tools"][index]
                current["tools"][index] = ToolUse(used.name, used.summary, status == "completed")
        elif kind == "turn_completed":
            if ended < len(spent) and current is not None:
                row = spent[ended]
                current["usage"] = TurnUsage(int(row.get("inputTokens") or 0) + int(row.get("cacheCreationTokens") or 0), int(row.get("outputTokens") or 0), int(row.get("cachedReadTokens") or 0))
            ended += 1
            cancelled = update.get("stop_reason") == "cancelled"
            close(at)
            if cancelled:
                turns.append(Turn(len(turns), "system", "[interrupted]", started_at=at, ended_at=at))
    close("")
    return turns


__all__ = ["GrokAdapter", "agent_file", "composer", "parse_transcript"]
