"""Claude Code as a staff member: its interactive TUI in a terminal, driven through its hooks.

Everything here was measured against Claude Code 2.1.282 in a throwaway configuration; the
recordings (hook payloads, screens, a transcript) are in ``tests/support/fake_cli/recorded/claude``
and the fake CLI replays their shapes. What matters to the adapter:

- **Hooks, per launch.** A ``--settings`` overlay in the launch directory adds command hooks that run
  ``"$DAEDALUS_PTYD_BIN" hook <Event>``: the daemon's own binary posts the payload to the launch's
  listener and prints the host's reply, which is how a held hook is answered. Settings lists merge,
  so the operator's own hooks keep working. No hook fires before the first-run screens and the
  folder-trust question are passed: ``SessionStart`` arriving is the proof the CLI is ready.
- **Trust.** "Quick safety check: Is this a project you created or one you trust?" is answered on
  screen by the readiness gate: its rows are unnumbered and "No, exit" is highlighted, so the gate
  moves down and confirms only once "Yes, I trust this folder" is the highlighted row. The first-run
  screens (the theme, "Select login method") are never answered: they mean the CLI was not set up
  or not signed in here, which the operator settles.
- **Permissions.** ``PermissionRequest`` is held at the listener for an answer from the app or the
  orchestrator, and Claude draws its dialog meanwhile, so the operator can answer there too. The
  held reply decides with ``decision.behavior``; when the hold has gone, the answer is typed into
  the dialog — after the dialog is on screen, because the hook arrives about a quarter of a second
  before the dialog is drawn and keys typed at once land in the composer. The request names no tool
  use, so the ``PreToolUse`` just before it (same tool, same input) gives its id. An "always" for an
  MCP tool rides on the held reply as ``updatedPermissions`` (an ``addRules`` for the session, the
  shape the 2.1.283 bundle validates), naming the tool or its whole server (``mcp__<server>``); the
  same rules are written into every later launch's ``permissions.allow``, which is where they last.
- **Questions.** ``AskUserQuestion`` is answered structurally: a ``PreToolUse`` hook held for it
  replies ``allow`` with ``updatedInput.answers``, and the dialog never opens. When the hold has
  gone, the dialog is answered with the option's digit.
- **Messages.** Pasted, then Enter. ``UserPromptSubmit`` names the prompt, and fires the moment a
  message is queued in a busy TUI, so it acknowledges a steer as well. The composer is the ``❯`` line
  between the two rules above the footer; a paste over 800 characters or four lines shows there as
  ``[Pasted text #N]``, and is submitted — to the hook and to the transcript — wrapped in
  ``<pasted_content id="…">`` tags, which :func:`unpasted` takes off again.
- **Turns.** Every turn opens with ``UserPromptSubmit`` (a message, a queued steer, a background
  task's notification) and closes with ``Stop``. Tool hooks between a ``Stop`` and the next prompt
  are not a turn: Claude fires some seconds after a turn has ended, and taking them for work kept a
  finished member "working" for as long as nobody looked, with its next task held behind the turn.
- **The idle screen.** The composer drawn with no spinner working above it. The footer's
  "? for shortcuts" is not proof either way: a status line of the operator's own or a permission mode
  replaces it, and then an idle Claude was never recognised from its screen at all.
- **Interrupt.** One Esc (two open the rewind menu). No hook says the turn stopped; the screen's
  "Interrupted" line does, and the adapter reports it.
- **The prompt after variadic options.** ``--add-dir`` and ``--mcp-config`` take every following
  word, so the first prompt comes after ``--``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
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
    ToolUse,
    Turn,
    TurnUsage,
    UpdateResult,
    standing_rule,
)
from daedalus.harness.team import SKILL_PATH
from daedalus.harness.tools import tooling

logger = logging.getLogger(__name__)

HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PermissionDenied", "PostToolUse",
    "PostToolUseFailure", "Stop", "StopFailure", "SubagentStart", "SubagentStop", "Notification", "PreCompact",
    "PostCompact", "SessionEnd",
)
HOOK_TIMEOUT_S = 10
"""A hook that is not held answers at once; this bounds one whose listener has gone."""
HOLD_SLACK_S = 30
"""What a held hook's own timeout exceeds its hold by, so Claude never kills the hook first."""
MCP_TIMEOUT_SLACK_MS = 60_000
"""What Claude's timeout on a team tool call exceeds the longest hold by."""
TEAM_TOOLS = ("mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator")
PERMISSION_MODES = {"default": "manual", "manual": "manual", "acceptEdits": "acceptEdits", "auto": "auto", "plan": "plan", "dontAsk": "dontAsk", "bypassPermissions": "bypassPermissions"}
"""The staff store keeps ``default``, the name hooks report; the flag calls the same mode ``manual``."""
LEVEL_MODES = {"ask": "manual", "edits": "acceptEdits", "all": "bypassPermissions"}
DIALOG_WAIT_S = 10.0
"""How long an answer typed into a dialog waits for the dialog to be drawn."""
DIALOG_CONFIRM_S = 5.0
INTERRUPT_CONFIRM_S = 10.0
TRANSCRIPT_MAX_BYTES = 32 << 20
"""A transcript is read whole to be parsed; a session longer than this is read from its end."""

TRUST_MARKERS = ("Quick safety check", "Accessing workspace")
DIALOG_MARKERS = (
    "Do you want to proceed?",
    "Quick safety check",
    "Select login method",
    "Choose the text style",
    "Bypass Permissions mode",
    "Enter to confirm · Esc to cancel",
    "Esc to cancel · Tab to amend",
    "Enter to select",
    "Type something.",
)
BUSY_HINT = "esc to interrupt"
_WORKING = re.compile(r"^\s*[·✢✳✶✻✽]\s+\S.*(…|Retrying in)")
"""The spinner line of a turn at work ("✶ Brewing… (12s · ↓ 1k tokens)", "✻ Connection refused … Retrying
in 3s"); a finished turn leaves "✻ Brewed for 3m 5s", with no ellipsis."""
PASTE_MARKER = "[Pasted text #"
_PASTED_OPEN = re.compile(r'<pasted_content id="[^"]*">\n?')
_PASTED_CLOSE = re.compile(r'\n?</pasted_content(?: id="[^"]*")?>')
_RULE = re.compile(r"^\s*─{8,}")


def unpasted(prompt: str) -> str:
    """A prompt as it was sent, without the tags Claude wraps a collapsed paste in.

    Claude Code 2.1.282 submits a paste it showed as ``[Pasted text #N +K lines]`` as
    ``\\n\\n<pasted_content id="777e">\\n…\\n</pasted_content id="777e">\\n``, in the
    ``UserPromptSubmit`` hook and in the transcript alike. Matched as it came, no brief of four lines
    or more was ever acknowledged: the delivery compares the start of what it pasted with the start
    of what the CLI reports, and the report started with the tag. Every next-task brief was marked
    "not delivered" while the member worked on it."""
    return _PASTED_CLOSE.sub("", _PASTED_OPEN.sub("", prompt)).strip()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def composer(screen: str) -> str | None:
    """The text in Claude's composer: the ``❯`` line and its continuations between the last two
    rules of the screen, or None when no composer is drawn (a dialog, a screen still starting)."""
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if _RULE.match(line)]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    body = lines[top + 1 : bottom]
    if not body or not body[0].lstrip().startswith("❯"):
        return None
    first = body[0].lstrip()[1:].strip()
    rest = [line.strip() for line in body[1:]]
    return "\n".join([first, *rest]).strip()


def _tail(lines: str, count: int = 12) -> str:
    return "\n".join(lines.splitlines()[-count:])


def _highlighted(screen: str) -> str:
    """The highlighted row of a dialog: the text after ``❯`` on the last line that has one."""
    for line in reversed(screen.splitlines()):
        stripped = line.strip()
        if stripped.startswith("❯"):
            return re.sub(r"^\d+\.\s*", "", stripped[1:].strip())
    return ""


def summary_of(tool: str, tool_input: Mapping[str, Any]) -> str:
    """One line saying what a tool call would do: the command, the file, the question."""
    for key in ("command", "file_path", "path", "url", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:300]
    description = tool_input.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()[:300]
    return tool


@dataclass(eq=False)
class _Request:
    kind: str
    """``permission`` or ``question``."""
    tool: str
    tool_input: dict[str, Any]
    reply_id: str | None
    summary: str
    questions: list[dict[str, Any]] = field(default_factory=list)
    settled: bool = False


@dataclass(eq=False)
class _Launch:
    """What the adapter keeps for one running launch."""

    queue: asyncio.Queue[HookPost | StaffEvent | None] = field(default_factory=asyncio.Queue)
    tool_uses: dict[str, str] = field(default_factory=dict)
    """A PreToolUse's tool and input to its tool-use id: the permission request that follows names neither."""
    requests: dict[str, _Request] = field(default_factory=dict)
    count: int = 0
    turn_open: bool = True
    """A prompt opened a turn and no ``Stop`` has closed it. True at the start: the first prompt may
    have gone on the command line, and a launch taken up after a restart may be mid-turn."""


@register
class ClaudeCodeAdapter:
    """:class:`daedalus.harness.contract.HarnessAdapter` for Claude Code. See the module's description."""

    name = "claude"
    capabilities = capabilities("claude")

    def __init__(self) -> None:
        self.tooling = tooling("claude")
        self._launches: dict[str, _Launch] = {}

    # -- the manager's half, through the CLI's tooling ------------------------------------------

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return await self.tooling.installed(env)

    async def latest(self, env: EnvironmentPort) -> str:
        """Asked of the release feed by the harness manager, which has the fetcher; the CLI itself
        does not say."""
        return ""

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        before = await self.installed(env)
        result = await env.run(["claude", "update"], timeout=600)
        after = await self.installed(env)
        ok = result.exit_code == 0 and not result.timed_out
        return UpdateResult(ok, before.version, after.version, output=result.stdout[-4000:], error="" if ok else (result.stderr or result.stdout)[-2000:])

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        """The session check needs a terminal and a launch, which the harness extension gives
        (``harness.selfcheck``); without them only the version can be asked."""
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
        if spec.permission_mode:
            return PERMISSION_MODES.get(spec.permission_mode, "manual")
        return LEVEL_MODES.get(spec.permission_level, "manual")

    def _plan(self, spec: LaunchSpec, session: str, *, resume: bool) -> LaunchPlan:
        mode = self._mode(spec)
        permission_hold = max(0, spec.permission_hold_ms)
        hooks: dict[str, list[dict[str, Any]]] = {}
        for event in HOOK_EVENTS:
            command = f'"$DAEDALUS_PTYD_BIN" hook {event}'
            timeout = HOOK_TIMEOUT_S
            if event == "PermissionRequest" and permission_hold:
                command += f" --wait-ms {permission_hold}"
                timeout = permission_hold // 1000 + HOLD_SLACK_S
            hooks[event] = [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
        if permission_hold:
            # A question to the operator is answered through this held hook's reply; every other
            # tool's PreToolUse goes by the entry above and is never held.
            hooks["PreToolUse"].append({"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": f'"$DAEDALUS_PTYD_BIN" hook PreToolUse --wait-ms {permission_hold}', "timeout": permission_hold // 1000 + HOLD_SLACK_S}]})
        # The team's tools and the tools of each set that only read are Claude's to run unasked; the
        # others ask by the member's mode, and what the host judges sensitive is asked again there.
        allow = [*TEAM_TOOLS, *(f"mcp__{tools.server}__{name}" for tools in spec.tool_sets for name in tools.read_only)]
        # The operator's standing grants to this member, given by "Always" in an earlier session.
        allow += [rule for rule in spec.allow_rules if rule not in allow]
        settings: dict[str, Any] = {"hooks": hooks, "permissions": {"allow": allow}}
        if mode == "bypassPermissions":
            # The overlay's switch is honoured (measured): the warning would otherwise stop the launch.
            settings["skipDangerousModePermissionPrompt"] = True
        mcp = {
            "mcpServers": {
                "daedalus_team": {
                    # The daemon's path is known only once the launch is registered, after these
                    # files are written; the shell reads it from the launch's environment.
                    "command": "sh",
                    "args": ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp'],
                    "env": {"DAEDALUS_ASK_HOLD_MS": str(spec.ask_hold_ms), "DAEDALUS_REPORT_HOLD_MS": str(spec.report_hold_ms)},
                }
            }
        }
        for tools in spec.tool_sets:
            mcp["mcpServers"][tools.server] = {"command": "sh", "args": ["-c", tools.command()], "env": {"DAEDALUS_TOOLS_HOLD_MS": str(tools.hold_ms)}}
        files: dict[str, bytes] = {"settings.json": json.dumps(settings, indent=1).encode(), "mcp.json": json.dumps(mcp, indent=1).encode()}
        files.update({tools.path: tools.file for tools in spec.tool_sets})
        argv: list[str] = ["claude", "--resume" if resume else "--session-id", session, "--settings", f"{LAUNCH_DIR}/settings.json"]
        if spec.team_block:
            files["system.md"] = spec.team_block.encode()
            argv += ["--append-system-prompt-file", f"{LAUNCH_DIR}/system.md"]
        if spec.team_skill:
            files[SKILL_PATH] = spec.team_skill.encode()
        if spec.title:
            argv += ["--name", spec.title[:120]]
        if spec.agent and spec.agent.lower() != "default":
            # "default" is no agent of Claude Code's: passed on, it ends the launch with "--agent
            # 'default' not found". Members hired with it before the hire refused it still start.
            argv += ["--agent", spec.agent]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        argv += ["--permission-mode", mode]
        # Variadic options last, and the prompt after "--": each would take the prompt as one more
        # directory or configuration otherwise.
        argv += ["--mcp-config", f"{LAUNCH_DIR}/mcp.json", "--add-dir", LAUNCH_DIR]
        prompt = spec.first_prompt
        if prompt:
            argv += ["--", prompt]
        env = {"MCP_TOOL_TIMEOUT": str(max(spec.ask_hold_ms, spec.report_hold_ms, *(t.hold_ms for t in spec.tool_sets)) + MCP_TIMEOUT_SLACK_MS)}
        return LaunchPlan(
            argv=tuple(argv),
            env=env,
            cwd=spec.cwd,
            files=files,
            session_ref=session,
            first_prompt=prompt,
            first_prompt_via="argv",
            hooks=HookSpec(sources=HOOK_EVENTS, hold_ms={"PermissionRequest": permission_hold, "PreToolUse": permission_hold} if permission_hold else {}),
        )

    def readiness(self, screen: str) -> ReadyStep:
        if "Select login method" in screen:
            return ReadyStep("fail", reason="Claude Code is not signed in here (it shows its sign-in screen): sign in from the Harnesses screen or in this terminal")
        if "Choose the text style" in screen:
            return ReadyStep("fail", reason="Claude Code has not finished its first run here: open this terminal and finish it once")
        if any(marker in screen for marker in TRUST_MARKERS):
            row = _highlighted(screen)
            if row.startswith("Yes, I trust this folder"):
                return ReadyStep("keys", ("Enter",), "folder trust")
            if row.startswith("No, exit"):
                return ReadyStep("keys", ("Down",), "folder trust: to the row that trusts it")
            return ReadyStep()
        if "Bypass Permissions mode" in screen and ("No, exit" in screen or "Yes, I accept" in screen):
            return ReadyStep("fail", reason="Claude Code asks to confirm bypass-permissions mode, which is not answered for it")
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        """Nothing to hand over: the first prompt is on the command line, and Claude submits it itself."""

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

        feeder = asyncio.create_task(pump(), name=f"claude-hooks-{term.id}")
        try:
            while True:
                item = await state.queue.get()
                if item is None:
                    return
                if isinstance(item, StaffEvent):
                    yield item
                    continue
                for event in self._map(term, state, item):
                    yield event
                if item.name in ("PostToolUse", "PostToolUseFailure", "PermissionDenied", "Stop", "StopFailure", "SessionEnd"):
                    await self._release(term, state, item)
        finally:
            feeder.cancel()
            self._launches.pop(term.id, None)

    def _map(self, term: TerminalPort, state: _Launch, post: HookPost) -> list[StaffEvent]:
        body = post.body if isinstance(post.body, dict) else {}
        at = post.at or _now()
        name = post.name
        tool = str(body.get("tool_name") or "")
        raw_input = body.get("tool_input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        tool_id = str(body.get("tool_use_id") or "")
        if name == "SessionStart":
            return [StaffEvent(EventKind.TRANSCRIPT, at, {"ref": body.get("transcript_path") or "", "session_ref": body.get("session_id") or ""}), StaffEvent(EventKind.READY, at, {"source": body.get("source") or ""})]
        if name == "UserPromptSubmit":
            if state is not None:
                state.turn_open = True
            prompt = unpasted(str(body.get("prompt") or ""))
            if prompt.lstrip().startswith("<task-notification>"):
                # A background task of the CLI finished and it takes that up by itself: a turn, but
                # no one's message.
                return [StaffEvent(EventKind.TURN_STARTED, at, {"source": "task-notification"})]
            return [StaffEvent(EventKind.PROMPT_ACKNOWLEDGED, at, {"prompt": prompt})]
        if name == "PreToolUse":
            if tool == "AskUserQuestion":
                if post.reply_id is None:
                    return []  # the held entry for this tool carries it
                questions = [q for q in tool_input.get("questions") or [] if isinstance(q, dict)]
                first = questions[0] if questions else {}
                text = "\n".join(str(q.get("question") or "") for q in questions).strip() or "Claude Code asks a question"
                options = [str(o.get("label") or "") for o in first.get("options") or [] if isinstance(o, dict)]
                ref = tool_id or f"question-{uuid.uuid4().hex[:8]}"
                state.requests[ref] = _Request("question", tool, dict(tool_input), post.reply_id, text[:200], questions)
                return [StaffEvent(EventKind.QUESTION_ASKED, at, {"tool": tool, "summary": text[:200], "text": text, "options": options}, native_id=ref)]
            if tool_id:
                state.tool_uses[self._key(tool, tool_input)] = tool_id
            if not state.turn_open:
                return [StaffEvent(EventKind.ACTIVITY, at, {"hook": name, "tool": tool, "between_turns": True})]
            return [StaffEvent(EventKind.TOOL_STARTED, at, {"tool": tool}, native_id=tool_id)]
        if name == "PermissionRequest":
            state.count += 1
            ref = state.tool_uses.pop(self._key(tool, tool_input), "") or f"permission-{state.count}"
            summary = summary_of(tool, tool_input)
            state.requests[ref] = _Request("permission", tool, dict(tool_input), post.reply_id, summary)
            return [StaffEvent(EventKind.PERMISSION_REQUESTED, at, {"tool": tool, "summary": summary, "options": ["allow_once", "deny"]}, native_id=ref)]
        if name in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            events = [StaffEvent(EventKind.REQUEST_RESOLVED, at, {}, native_id=tool_id)] if tool_id in state.requests else []
            if not state.turn_open:
                return [*events, StaffEvent(EventKind.ACTIVITY, at, {"hook": name, "tool": tool, "between_turns": True})]
            return [*events, StaffEvent(EventKind.TOOL_FINISHED, at, {"tool": tool, "ok": name == "PostToolUse"}, native_id=tool_id)]
        if name in ("Stop", "StopFailure") and state is not None:
            state.turn_open = False
        if name == "Stop":
            # A turn that ended has nothing waiting any more, whoever settled it.
            events = [StaffEvent(EventKind.REQUEST_RESOLVED, at, {}, native_id=ref) for ref in state.requests]
            return [*events, StaffEvent(EventKind.TURN_COMPLETED, at, {"last_message": str(body.get("last_assistant_message") or "")})]
        if name == "StopFailure":
            failure = str(body.get("error") or "the turn failed")
            return [StaffEvent(EventKind.TURN_FAILED, at, {"failure": failure.replace("_", " ")})]
        if name == "SessionEnd":
            return [StaffEvent(EventKind.SESSION_ENDED, at, {"reason": body.get("reason") or ""})]
        if name == "Notification":
            # Never a status: "needs your permission" follows the hook that already said so, and
            # "waiting for your input" is a timer on an empty prompt.
            return [StaffEvent(EventKind.NOTIFICATION, at, {"type": body.get("notification_type") or "", "message": str(body.get("message") or "")[:200]})]
        return [StaffEvent(EventKind.ACTIVITY, at, {"hook": name})]

    @staticmethod
    def _key(tool: str, tool_input: Mapping[str, Any]) -> str:
        return tool + "\x00" + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)

    async def _release(self, term: TerminalPort, state: _Launch, post: HookPost) -> None:
        """A request settled elsewhere lets its held hook go at once. Claude has already moved on;
        a hook left running would keep its spinner up until the hold ends."""
        body = post.body if isinstance(post.body, dict) else {}
        tool_id = str(body.get("tool_use_id") or "")
        refs = list(state.requests) if post.name in ("Stop", "StopFailure", "SessionEnd") else [tool_id] if tool_id in state.requests else []
        for ref in refs:
            request = state.requests.pop(ref, None)
            if request is not None and request.reply_id and not request.settled:
                with contextlib.suppress(Exception):
                    await term.reply(request.reply_id, None)

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        """Messages are pasted by the runtime's delivery worker, as for every CLI written to by
        paste; this is the plain form of it, for a caller without one."""
        await term.write(paste=text)
        await asyncio.sleep((self.capabilities.paste.burst_guard_ms if self.capabilities.paste else 0) / 1000 + 0.3)
        await term.write(keys=["Enter"])
        return Delivery(message_id, "submitted", via="paste")

    async def interrupt(self, term: TerminalPort) -> None:
        """One Esc — a second would open the rewind menu — and the turn's end reported once the
        screen says it was interrupted, since no hook does."""
        before = (await term.screen()).count("Interrupted")
        await term.write(keys=["Esc"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INTERRUPT_CONFIRM_S
        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            if (await term.screen()).count("Interrupted") > before:
                self._state(term).queue.put_nowait(StaffEvent(EventKind.TURN_CANCELLED, _now(), {"via": "esc"}))
                return

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        state = self._state(term)
        request = state.requests.get(request_ref)
        if request is None:
            return False
        if request.kind == "permission":
            allowed = answer.choice.startswith("allow")
            decision: dict[str, Any] = {"behavior": "allow"} if allowed else {"behavior": "deny", "message": answer.note or "The operator declined this."}
            rule = standing_rule(request.tool, "server" if answer.choice == "allow_always_server" else "tool") if answer.choice.startswith("allow_always") else None
            if rule is not None:
                # Held for this session only: "localSettings" would write the rule into the project
                # folder, which is the operator's repository, not the member's. The host keeps the
                # grant and writes it into every later launch itself.
                decision["updatedPermissions"] = [{"type": "addRules", "rules": [{"toolName": rule}], "behavior": "allow", "destination": "session"}]
            # A built-in tool's "always" is a plain allow on the held hook: its only rule would be the
            # whole tool (every command, every file), and an allow that asks again is the safe side.
            body: Any = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
            if request.reply_id and await term.reply(request.reply_id, body):
                request.settled = True
                return True
            # In the dialog "always" is its second row, for the one tool; a server-wide grant reaches
            # this session no further, and the next launch carries it from the host's list.
            return await self._keys_for_permission(term, request, allowed, always=answer.choice.startswith("allow_always"))
        labels = [str(o.get("label") or "") for q in request.questions[:1] for o in q.get("options") or [] if isinstance(o, dict)]
        chosen = answer.said
        answers = {str(q.get("question") or ""): chosen for q in request.questions}
        body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {**request.tool_input, "answers": answers}}}
        if request.reply_id and await term.reply(request.reply_id, body):
            request.settled = True
            return True
        if chosen not in labels:
            # Free text, or an option with the operator's words beside it, goes only through the
            # hook: pressing the option's key would deliver the choice and silently drop the words.
            return False  # the dialog's own text box is the operator's
        return await self._keys_for_dialog(term, re.escape(str(request.questions[0].get("question") or "")[:40]), str(labels.index(chosen) + 1), request)

    async def _keys_for_permission(self, term: TerminalPort, request: _Request, allowed: bool, *, always: bool = False) -> bool:
        # The dialog's second row is its "and don't ask again" (the recorded dialog: 1. Yes, 2. Yes, and
        # always allow …, 3. No). Not every dialog has that row, and in a two-row one "2" is No, so
        # "always" presses 2 only when the screen shows it as a Yes.
        def key(screen: str) -> str:
            if not allowed:
                return "3" if re.search(r"3\.\s*No", screen) else "2" if re.search(r"2\.\s*No", screen) else "3"
            return "2" if always and re.search(r"2\.\s*Yes", screen) else "1"

        return await self._keys_for_dialog(term, r"Do you want to proceed\?", key, request, must_show=request.summary[:40])

    async def _keys_for_dialog(self, term: TerminalPort, regex: str, key: str | Callable[[str], str], request: _Request, *, must_show: str = "") -> bool:
        """Type a dialog's digit — once the dialog is on screen and is the one asked about. ``key`` may
        be chosen from the screen, for a dialog whose rows differ from one request to another."""
        if not await term.wait_for(regex=regex, timeout=DIALOG_WAIT_S):
            return False
        screen = await term.screen()
        if must_show and _squeezed(must_show) not in _squeezed(screen):
            return False
        await term.write(text=key(screen) if callable(key) else key)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DIALOG_CONFIRM_S
        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            if not re.search(regex, await term.screen()):
                request.settled = True
                return True
        return False

    async def stop(self, term: TerminalPort) -> None:
        screen = await term.screen()
        if self.classify_screen(screen) is ScreenClass.DIALOG:
            return  # typing would answer the dialog; the runtime ends the terminal after its grace
        await term.write(text="/exit")
        await asyncio.sleep(0.3)
        await term.write(keys=["Enter"])

    # -- the screen -----------------------------------------------------------------------------

    def classify_screen(self, text: str) -> ScreenClass:
        bottom = _tail(text, 24)
        if any(marker in bottom for marker in DIALOG_MARKERS):
            return ScreenClass.DIALOG
        lines = text.splitlines()
        rules = [i for i, line in enumerate(lines) if _RULE.match(line)]
        # The spinner sits a few lines above the composer's top rule (a recap or a queued message may
        # come between), the busy hint in it or in the footer.
        near = "\n".join(lines[max(0, rules[-2] - 8) :]) if len(rules) >= 2 else _tail(text, 3)
        if BUSY_HINT in near or any(_WORKING.match(line) for line in near.splitlines()):
            return ScreenClass.BUSY
        if composer(text) is not None:
            return ScreenClass.IDLE_COMPOSER
        return ScreenClass.UNKNOWN

    def composer_holds(self, screen: str, text: str) -> bool:
        held = composer(screen)
        if not held:
            return False
        if PASTE_MARKER in held:
            return True
        tail = _squeezed(text)[-40:]
        return bool(tail) and tail in _squeezed(held)

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


def parse_transcript(text: str) -> list[Turn]:
    """Claude's JSON-lines transcript as turns. The records a reader needs are ``user`` (a prompt, a
    tool's result, an interruption) and ``assistant`` (one record per content block, several
    sharing one message and its usage); every other type — attachments, modes, titles, queue
    operations, snapshots — is skipped, and an unreadable line with it."""
    turns: list[Turn] = []
    current: dict[str, Any] | None = None
    counted: set[str] = set()
    tools_by_id: dict[str, int] = {}

    def close() -> None:
        nonlocal current
        if current is not None:
            usage = current["usage"]
            turns.append(Turn(len(turns), "assistant", "\n".join(current["text"]).strip(), tuple(current["tools"]), current["at"], current["end"], TurnUsage(*usage, context_tokens=current["context"]) if any(usage) else None))
            current = None

    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        raw_message = record.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        at = str(record.get("timestamp") or "")
        content = message.get("content")
        if kind == "assistant":
            ident = str(message.get("id") or "")
            if current is None:
                current = {"text": [], "tools": [], "at": at, "end": at, "usage": [0, 0, 0], "context": 0}
            current["end"] = at
            raw_usage = message.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            if usage and ident not in counted:
                counted.add(ident)
                current["usage"][0] += int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                current["usage"][1] += int(usage.get("output_tokens") or 0)
                current["usage"][2] += int(usage.get("cache_read_input_tokens") or 0)
                # Each call's prompt is the whole context it was sent, so the last one is the fill.
                current["context"] = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    current["text"].append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    raw_block_input = block.get("input")
                    block_input: dict[str, Any] = raw_block_input if isinstance(raw_block_input, dict) else {}
                    tools_by_id[str(block.get("id") or "")] = len(current["tools"])
                    current["tools"].append(ToolUse(str(block.get("name") or ""), summary_of(str(block.get("name") or ""), block_input)))
            continue
        if kind != "user":
            continue
        if isinstance(content, str):
            words = unpasted(content)
            if not words or words.startswith(("<task-notification>", "<command-name>", "<local-command")):
                continue
            close()
            role = "orchestrator" if words.startswith("[orchestrator]") else "user"
            turns.append(Turn(len(turns), role, words, started_at=at, ended_at=at))  # type: ignore[arg-type]
            continue
        results = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results and current is not None:
            for result in results:
                index = tools_by_id.get(str(result.get("tool_use_id") or ""))
                if index is not None and index < len(current["tools"]):
                    used = current["tools"][index]
                    current["tools"][index] = ToolUse(used.name, used.summary, not bool(result.get("is_error")))
            continue
        words = unpasted("\n".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"))
        if words:
            close()
            turns.append(Turn(len(turns), "system" if words.startswith("[Request interrupted") else "user", words, started_at=at, ended_at=at))
    close()
    return turns


__all__ = ["ClaudeCodeAdapter", "composer", "parse_transcript", "summary_of", "unpasted"]
