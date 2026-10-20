"""Codex as a staff member: its TUI in a terminal, driven through the app server it talks to.

Measured against Codex 0.155.1 in a throwaway home, and the whole launch again on 0.160.0; what the
app server and the TUI said is in ``tests/support/fake_cli/recorded/codex`` and the fake Codex replays
it. What matters here:

- **Two processes.** ``codex app-server --listen unix://<dial dir>/codex.sock`` runs as the launch's
  companion terminal, and the TUI attaches to it with ``codex --remote unix://… resume <thread>``.
  The host reaches the same server through the daemon's ``net.dial``: it speaks JSON-RPC over a
  WebSocket on that socket (a bare JSON line is met with a closed connection). From 0.157 Codex binds
  the socket itself under ``/tmp/codex-daemon-<uid>/`` and leaves a link at the path it was given
  (measured on 0.160.0); the daemon follows such a link to a private socket of its own user, and an
  older daemon's refusal of it ends the launch at once with its reason.
- **The host starts the thread.** Notifications about a thread's turns and items go only to the
  clients subscribed to it — the one that started it and those that resumed it — while every client
  hears ``thread/started`` and status changes. A thread nobody has written to yet cannot be resumed
  ("no rollout found"), neither by the TUI nor by this client, so a TUI-started thread would stay
  mute to the host. So the host starts the thread with the member's developer instructions, writes
  one developer item into it (which creates its rollout), and only then names the thread in a
  launch file; the TUI's terminal waits for that file and then attaches with ``resume``. Every
  prompt the host sends appears in the TUI, and whatever the operator types there reaches the host.
- **Acknowledgement by id.** ``turn/start`` and ``turn/steer`` carry ``clientUserMessageId``, and the
  ``userMessage`` item that follows echoes it as ``clientId``.
- **Steer** is ``turn/steer`` with the running turn's id; a turn that just ended refuses it ("no
  active turn to steer") and the message starts a turn instead.
- **Requests.** Approvals and questions are server requests to every client subscribed to the thread
  when the request is sent (Codex's ``app-server/src/outgoing_message.rs``, the recipients from the
  thread's subscribed connections); the first answer wins and the others hear
  ``serverRequest/resolved``. The host is subscribed from the thread's start, so it receives every
  one the TUI does. The exception is Codex's experimental user-verification elicitation, which goes
  to one verification-capable connection only: when the thread says it waits on an approval and no
  request reached the host, the operator is told to answer in the terminal.
- **Model and effort** are named on every thread the host starts or resumes: the member's own, else
  the operator's Codex configuration (``config/read``). Left to Codex, a member ran on its bundled
  default, which 0.161 changed to a new model at "low".
- **Remote control** is off for every staff server, by Codex's
  ``CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED``: a server started without a choice takes the
  one stored for the Codex home, and an operator who paired the phone app would have every staff
  server enrol with the relay. The variable changes no stored choice; Codex has no setting for it.
- **Switches.** ``check_for_update_on_startup=false`` is the TUI's own setting (the update prompt
  appeared with it only on the server); the rate-limit model nudge is a dialog too and is switched off
  the same way. The MCP servers Codex starts get a filtered environment, so the team bridge's
  variables are passed through by name; ``default_tools_approval_mode="approve"`` keeps its tools from
  asking, and ``tool_timeout_sec`` is set above the longest hold. The ``daedalus-team`` skill is a
  launch file, added with ``skills/extraRoots/set`` (verified in ``skills/list``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daedalus.harness import register
from daedalus.harness.capabilities import capabilities
from daedalus.harness.contract import (
    DIAL_DIR,
    LAUNCH_DIR,
    Answer,
    Catalog,
    CheckResult,
    CheckStep,
    CompanionSpec,
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
    ToolUse,
    Turn,
    TurnUsage,
    UpdateResult,
    skill_files,
)
from daedalus.harness.streams import StreamClosed, WebSocket
from daedalus.harness.team import SKILL_NAME
from daedalus.harness.tools import tooling
from daedalus.terminals.model import Forbidden, InvalidRequest, StaleLaunch, Unsupported

logger = logging.getLogger(__name__)

SOCKET = "codex.sock"
THREAD_FILE = "codex-thread"
"""The launch file that names the thread; the TUI's terminal waits for it before attaching."""
SKILLS_DIR = "skills"
SKILL_FILE = f"{SKILLS_DIR}/{SKILL_NAME}/SKILL.md"
TUI_SETTINGS = ("-c", "check_for_update_on_startup=false", "-c", "notice.hide_rate_limit_model_nudge=true")
WAIT_THEN_ATTACH = 'f="$1"; s="$2"; shift 2; until [ -s "$f" ]; do sleep 0.2; done; exec codex --remote "$s" "$@" resume "$(cat "$f")"'
"""The TUI's terminal: wait for the host to name the thread, then attach to it (``$1`` the file,
``$2`` the socket, the rest the TUI's own settings). A shell rather than a later terminal, so the
member's terminal exists from the start and the operator sees it waiting."""
TEAM_ENV = ("DAEDALUS_HOOK_URL", "DAEDALUS_HOOK_TOKEN", "DAEDALUS_LAUNCH_ID", "DAEDALUS_PTYD_BIN", "DAEDALUS_TERMINAL_ID")
"""What the team bridge needs from the launch's environment. Codex starts its MCP servers with a
filtered environment, so each is passed through by name."""
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
NO_REMOTE_CONTROL = {"CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1"}
LEVELS = {"ask": ("workspace-write", "untrusted"), "edits": ("workspace-write", "on-request"), "all": ("danger-full-access", "never")}
"""The project's autonomy as Codex's sandbox and approval policy. ``untrusted`` asks before anything
that is not known to be safe, which is what "ask" means for Claude as well."""


def start_modes(permission_mode: str, permission_level: str) -> tuple[str, str]:
    """The sandbox and the approval policy Codex starts with. A function of its own because the
    member's Details say from it how a setting is kept, and a copy of the rule would drift."""
    sandbox, approval = LEVELS[permission_level]
    if permission_mode in SANDBOXES:
        sandbox = permission_mode
        if sandbox == "danger-full-access":
            approval = "never"
    return sandbox, approval


PERMANENT_REFUSALS = (Forbidden, InvalidRequest, StaleLaunch, Unsupported)
"""What the daemon answers a dial with that dialling again will not change: a missing socket and a
refused connection are a server still starting; these are not."""
OLD_DAEMON = "the terminal daemon predates Codex 0.157's link at its socket and must be updated"
"""Said with a refused link, so the operator knows the fix is the daemon's, not Codex's."""
CONNECT_S = 60.0
"""How long the host keeps dialling the app server's socket while the companion starts."""
CALL_TIMEOUT_S = 30.0
ANSWER_CONFIRM_S = 5.0
WAITING_GRACE_S = 1.5
"""How long the thread may say it waits on an approval before the host, having received no request,
tells the operator to answer in the terminal."""
TIMEOUT_SLACK_S = 60
OUTAGES = frozenset({"usageLimitExceeded", "rateLimitExceeded", "serverOverloaded"})
"""The ``codexErrorInfo`` tags that say the account or the service failed, not the work: the turn is
driven again once the wait is over, as the host does for its own providers."""
QUOTAS = frozenset({"usageLimitExceeded", "rateLimitExceeded"})
"""Of those, the ones a rate-limit window's reset ends."""
TRANSCRIPT_MAX_BYTES = 32 << 20
DIALOG_MARKERS = (
    "Update available!",
    "Press enter to continue",
    "Approaching rate limits",
    "Trust this folder?",
    "Would you like to run the following command",
    "Would you like to make the following edits",
    "Would you like to grant these permissions?",
    "Do you want to approve network access to",
    "Would you like to send input to",
    " needs your approval.",
    "Allow this request",
)
"""What Codex 0.163 draws when it waits for a choice: the update and rate-limit prompts, the folder
trust question (``tui/src/onboarding/trust_directory.rs``), the approval overlay's titles
(``tui/src/bottom_pane/approval_overlay.rs``) and an MCP server's request
(``mcp_server_elicitation.rs``). The trust question once read "Do you trust the contents of this
directory", and three of the approval titles were never here, so ``stop`` typed into them."""
BUSY_HINT = " to interrupt)"
"""The end of the busy line, "Working (0s • esc to interrupt)". The key is drawn from the operator's
key map since 0.163, so the line is matched without it."""
CONNECT_FAILED = "failed to connect to remote app server"
"""What the TUI prints when it cannot reach the app server, wrapped as "Error: failed to connect to
remote app server" around the client's own "… at `<url>`: <cause>" (``tui/src/lib.rs``,
``app-server-client/src/remote.rs``). The words once matched here were the fake's invention, so a TUI
that never attached went unnoticed until its terminal exited."""
DISMISS_ATTEMPTS = 3
INJECTED = (
    "<environment_context", "<recommended_plugins", "<user_instructions", "# AGENTS.md",
    "<skills_instructions", "<permissions", "<turn_aborted", "<user_action", "<subagent_notification",
    "<codex_internal_context", "<goal_context", "<collaboration_mode", "<multi_agent", "<model_switch",
    "<apps_instructions", "<codex_apps", "<external_", "<skill>", "<plugins_instructions",
    "<user_shell_command", "<realtime_delegation", "<hook_prompt", "Warning: apply_patch was requested via ",
    "Warning: The maximum number of unified exec processes you can keep open is",
    "Warning: Your account was flagged for potentially high-risk cyber activity",
)
"""The starts of the messages Codex writes into the conversation as the user's for its model to read:
the environment, the instructions files, skills and plugins, an interrupted turn, a helper agent's
notice, a shell command typed with ``!``, a goal, a hook's prompt, its own warnings. None is anything
anyone said. From its ``CONTEXTUAL_USER_FRAGMENT_MATCHERS`` (``core/src/context/contextual_user_message.rs``)
and the fragments they match, plus the older names earlier rollouts carry.

The terminal daemon's session import reads the same rollouts and keeps the same list
(``codexNoise`` in ``ptyd/internal/sidechan/sessions/codex.go``); a test holds the two equal. The
host's copy had fallen to six entries, and a member interrupted by the orchestrator showed a "user"
turn ``<turn_aborted>…`` that nobody typed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _toml(value: Any) -> str:
    """A value as an inline TOML literal. JSON's string escapes are TOML's basic-string escapes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ",".join(_toml(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{k}={_toml(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"no TOML for {type(value).__name__}")


class CodexError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(eq=False)
class _Request:
    rpc_id: Any
    method: str
    params: dict[str, Any]
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    ours: bool = False


@dataclass(eq=False)
class _Launch:
    """One launch's connection to its app server and what it has seen."""

    queue: asyncio.Queue[StaffEvent | None] = field(default_factory=asyncio.Queue)
    socket: WebSocket | None = None
    pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict)
    next_id: int = 0
    thread_id: str = ""
    turn_id: str = ""
    requests: dict[str, _Request] = field(default_factory=dict)
    client_ids: dict[str, str] = field(default_factory=dict)
    """``clientUserMessageId`` → the staff message it carries (empty for one with no row)."""
    last_message: str = ""
    failure: str = ""
    waiting_ref: str = ""
    stopping: bool = False
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    rate_limits: dict[str, Any] = field(default_factory=dict)
    """The account's rate-limit windows as ``account/rateLimits/updated`` last gave them."""


@dataclass(frozen=True, slots=True)
class _Thread:
    """What the host starts or resumes the thread with, kept from the plan for ``events``."""

    cwd: str
    model: str
    effort: str
    sandbox: str
    approval: str
    instructions: str
    resume: str


@register
class CodexAdapter:
    """:class:`daedalus.harness.contract.HarnessAdapter` for Codex. See the module's description."""

    name = "codex"
    capabilities = capabilities("codex")

    def __init__(self) -> None:
        self.tooling = tooling("codex")
        self._planned: dict[str, _Thread] = {}
        self._launches: dict[str, _Launch] = {}

    # -- the manager's half ---------------------------------------------------------------------

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return await self.tooling.installed(env)

    async def latest(self, env: EnvironmentPort) -> str:
        return ""

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        before = await self.installed(env)
        result = await env.run(["codex", "update"], timeout=600)
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
        return self._plan(spec, "")

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        return self._plan(spec, ref)

    @staticmethod
    def _modes(spec: LaunchSpec) -> tuple[str, str]:
        spec.validate_permissions(SANDBOXES)
        return start_modes(spec.permission_mode, spec.permission_level)

    def _plan(self, spec: LaunchSpec, resume: str) -> LaunchPlan:
        sandbox, approval = self._modes(spec)
        self._planned[spec.launch_id] = _Thread(spec.cwd, spec.model, spec.effort, sandbox, approval, spec.team_block, resume)
        socket = f"unix://{DIAL_DIR}/{SOCKET}"
        team = {
            "command": "sh",
            "args": ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp'],
            "env_vars": list(TEAM_ENV),
            "env": {"DAEDALUS_ASK_HOLD_MS": str(spec.ask_hold_ms), "DAEDALUS_REPORT_HOLD_MS": str(spec.report_hold_ms)},
            "tool_timeout_sec": max(spec.ask_hold_ms, spec.report_hold_ms) // 1000 + TIMEOUT_SLACK_S,
            "default_tools_approval_mode": "approve",
        }
        sets: list[str] = []
        for tools in spec.tool_sets:
            # The launch directory is passed through too: the set's tools are read from a file there.
            # No approval mode is set: whether Codex asks before one of them is its own policy's call,
            # and the host asks the operator about what is sensitive in any case.
            entry = {"command": "sh", "args": ["-c", tools.command()], "env_vars": [*TEAM_ENV, "DAEDALUS_LAUNCH_DIR"], "env": {"DAEDALUS_TOOLS_HOLD_MS": str(tools.hold_ms)}, "tool_timeout_sec": tools.hold_ms // 1000 + TIMEOUT_SLACK_S}
            sets += ["-c", f"mcp_servers.{tools.server}={_toml(entry)}"]
        server = (
            "codex", "app-server", "--listen", socket,
            "-c", f"mcp_servers.daedalus_team={_toml(team)}",
            *sets,
            "-c", f"projects.{_toml(spec.cwd)}.trust_level=\"trusted\"",
            "-c", "check_for_update_on_startup=false",
        )
        files: dict[str, bytes] = {tools.path: tools.file for tools in spec.tool_sets}
        if spec.team_skill:
            files[SKILL_FILE] = spec.team_skill.encode()
        files.update(skill_files(spec.skills, SKILLS_DIR))
        argv = ("sh", "-c", WAIT_THEN_ATTACH, "codex-tui", f"{LAUNCH_DIR}/{THREAD_FILE}", socket, *TUI_SETTINGS)
        return LaunchPlan(
            argv=argv,
            env={},
            cwd=spec.cwd,
            files=files,
            companions=(CompanionSpec("app-server", server, env=NO_REMOTE_CONTROL),),
            session_ref=resume,
            first_prompt=spec.first_prompt,
            first_prompt_via="channel",
        )

    def readiness(self, screen: str) -> ReadyStep:
        if "Failed to resume session" in screen:
            return ReadyStep("fail", reason="Codex could not attach to its session: " + _last_line_with(screen, "Failed"))
        if CONNECT_FAILED in screen.lower():
            return ReadyStep("fail", reason="Codex could not attach to its session: " + _last_line_with(screen, CONNECT_FAILED, any_case=True))
        if "Sign in with ChatGPT" in screen or "Not logged in" in screen:
            return ReadyStep("fail", reason="Codex is not signed in here: sign in from the Harnesses screen or in this terminal")
        if "Update available!" in screen:
            row = _highlighted(screen)
            if row.startswith("Skip") and "until" not in row:
                return ReadyStep("keys", ("Enter",), "update prompt: skipped")
            if row.startswith("Update now"):
                return ReadyStep("keys", ("Down",), "update prompt: to the row that skips it")
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        """The first prompt goes by ``turn/start`` once the thread is there; its ``userMessage`` item
        acknowledges it."""
        if plan.first_prompt:
            await self.send(term, "", plan.first_prompt, "after_turn")

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        self._launches.setdefault(term.id, _Launch())

    # -- the app server -------------------------------------------------------------------------

    def _state(self, term: TerminalPort) -> _Launch:
        return self._launches.setdefault(term.id, _Launch())

    async def _call(self, state: _Launch, method: str, params: Mapping[str, Any], *, timeout: float = CALL_TIMEOUT_S) -> Any:
        if state.socket is None:
            raise CodexError(-1, "not connected to Codex's app server")
        state.next_id += 1
        ident = state.next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        state.pending[ident] = future
        try:
            await state.socket.send_text(json.dumps({"id": ident, "method": method, "params": dict(params)}))
            answer = await asyncio.wait_for(future, timeout)
        finally:
            state.pending.pop(ident, None)
        if isinstance(answer, dict) and "error" in answer:
            error = answer["error"] or {}
            raise CodexError(int(error.get("code") or -1), str(error.get("message") or "the app server refused"))
        return answer.get("result") if isinstance(answer, dict) else None

    async def _respond(self, state: _Launch, rpc_id: Any, result: Any = None, error: Mapping[str, Any] | None = None) -> None:
        if state.socket is None:
            return
        message: dict[str, Any] = {"id": rpc_id}
        if error is not None:
            message["error"] = dict(error)
        else:
            message["result"] = result
        await state.socket.send_text(json.dumps(message))

    async def _connect(self, term: TerminalPort) -> WebSocket:
        """Dial the companion's socket, again and again while it starts: the app server says nothing
        when it listens, so the socket answering is the sign.

        A refusal that no wait mends ends the dialling at once, in the daemon's words. Codex 0.157
        began leaving a link where its socket used to be, a daemon that would not follow it refused
        every dial, and the host went on dialling for a minute while the readiness gate gave up at
        thirty seconds with "not ready" over an empty screen, the reason never shown."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_S
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                return await WebSocket.connect(await term.dial(f"unix:{SOCKET}"))
            except asyncio.CancelledError:
                raise
            except PERMANENT_REFUSALS:
                raise
            except Exception as exc:  # noqa: BLE001 — not listening yet; the next try may find it
                last = exc
                await asyncio.sleep(0.2)
        raise ConnectionError(f"Codex's app server did not answer within {CONNECT_S:g} s: {last}")

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        state = self._state(term)
        planned = self._planned.pop(launch.launch_id, None)

        async def until_launch_ends() -> None:
            async for _ in term.hooks():
                pass  # Codex posts no hooks; the stream ending is the launch ending
            state.queue.put_nowait(None)

        async def start() -> None:
            try:
                await self._open(term, state, launch, planned)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the session's failure, shown on it
                logger.info("codex launch %s did not start: %s", launch.launch_id, exc)
                failure = f"Codex's app server: {exc}"
                if isinstance(exc, Forbidden) and f"{SOCKET} is not a socket" in str(exc):
                    failure += f"; {OLD_DAEMON}"
                state.queue.put_nowait(StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": failure}))

        state.tasks = [asyncio.create_task(until_launch_ends(), name=f"codex-end-{term.id}"), asyncio.create_task(start(), name=f"codex-start-{term.id}")]
        try:
            while True:
                event = await state.queue.get()
                if event is None:
                    return
                yield event
        finally:
            for task in state.tasks:
                task.cancel()
            if state.socket is not None:
                with contextlib.suppress(Exception):
                    await state.socket.close()
            self._launches.pop(term.id, None)

    async def _open(self, term: TerminalPort, state: _Launch, launch: Launch, planned: _Thread | None) -> None:
        state.socket = await self._connect(term)
        state.tasks.append(asyncio.create_task(self._read(state), name=f"codex-read-{term.id}"))
        await self._call(state, "initialize", {"clientInfo": {"name": "daedalus", "title": "Daedalus", "version": "1"}})
        await state.socket.send_text(json.dumps({"method": "initialized"}))
        if launch.launch_dir:
            with contextlib.suppress(CodexError, TimeoutError):
                await self._call(state, "skills/extraRoots/set", {"extraRoots": [f"{launch.launch_dir.rstrip('/')}/{SKILLS_DIR}"]})
        resume = launch.session_ref or (planned.resume if planned is not None else "")
        settings: dict[str, Any] = {}
        if planned is not None:
            settings = {"cwd": planned.cwd, "sandbox": planned.sandbox, "approvalPolicy": planned.approval}
            model, effort = await self._model_and_effort(state, planned)
            if model:
                settings["model"] = model
            if planned.instructions:
                settings["developerInstructions"] = planned.instructions
            if effort:
                settings["config"] = {"model_reasoning_effort": effort}
        if resume:
            result = await self._call(state, "thread/resume", {"threadId": resume, "excludeTurns": True, **settings})
        elif planned is None:
            raise ConnectionError("this launch was never started here, and names no thread to take up")
        else:
            result = await self._call(state, "thread/start", {**settings, "serviceName": "daedalus"})
        thread = (result or {}).get("thread") or {}
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        # A resumed thread can carry another session's history and tools. The requested id is not
        # proof that the app server attached to it, so require the id in the server's own reply.
        if not isinstance(thread_id, str) or not thread_id or (resume and thread_id != resume):
            raise ConnectionError("the app server did not confirm the requested thread")
        state.thread_id = thread_id
        if not resume:
            # A thread nobody has written to has no rollout, and without one neither the TUI nor a
            # restarted host can take it up; one developer item makes it.
            await self._call(state, "thread/inject_items", {"threadId": state.thread_id, "items": [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "This session was started by Daedalus for a staff member; the operator may watch it in a terminal."}]}]})
        status = (thread.get("status") or {}).get("type")
        if status == "active":
            state.turn_id = await self._active_turn(state)
        state.queue.put_nowait(StaffEvent(EventKind.TRANSCRIPT, _now(), {"ref": str(thread.get("path") or ""), "session_ref": state.thread_id}))
        with contextlib.suppress(Exception):
            # Already there after a host restart: the TUI attached long ago.
            await term.put_file(THREAD_FILE, state.thread_id.encode())
        state.connected.set()
        await self._tui_attached(term)
        state.queue.put_nowait(StaffEvent(EventKind.READY, _now(), {"thread": state.thread_id}))

    async def _model_and_effort(self, state: _Launch, planned: _Thread) -> tuple[str, str]:
        """The member's model and effort, each completed from the operator's Codex configuration as
        the server reads it for the member's folder. Empty only where neither names one, and then
        Codex's own default stands, which is logged: nothing in Daedalus is a better guess."""
        model, effort = planned.model, planned.effort
        if model and effort:
            return model, effort
        configured: Mapping[str, Any] = {}
        with contextlib.suppress(CodexError, TimeoutError):
            answer = await self._call(state, "config/read", {"cwd": planned.cwd})
            found = (answer or {}).get("config") if isinstance(answer, dict) else None
            configured = found if isinstance(found, dict) else {}
        model = model or str(configured.get("model") or "")
        effort = effort or str(configured.get("model_reasoning_effort") or "")
        if not model or not effort:
            logger.warning("a codex member names no %s and the operator's codex configuration none either; codex's own default applies",
                           "model or effort" if not model and not effort else "model" if not model else "effort")
        return model, effort

    async def _tui_attached(self, term: TerminalPort) -> None:
        """Ready means the TUI shows the thread, not only that the server has it: a request sent
        before the TUI subscribed never reaches its screen, so the operator could not answer it there.
        Until then the readiness gate still reads the screen and answers what it knows (an update
        prompt). A TUI that never attaches does not hold the member back: the host works through the
        server either way, and a TUI that failed says so on its screen, which the gate reports."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_S
        while loop.time() < deadline:
            with contextlib.suppress(Exception):
                if self.classify_screen(await term.screen()) in (ScreenClass.IDLE_COMPOSER, ScreenClass.BUSY):
                    return
            await asyncio.sleep(0.2)
        logger.info("the codex TUI of %s did not show its thread within %g s", term.id, CONNECT_S)

    async def _active_turn(self, state: _Launch) -> str:
        with contextlib.suppress(CodexError, TimeoutError):
            page = await self._call(state, "thread/turns/list", {"threadId": state.thread_id, "limit": 1, "sortDirection": "desc"})
            for turn in (page or {}).get("data") or []:
                if turn.get("status") == "inProgress":
                    return str(turn.get("id") or "")
        return ""

    async def _read(self, state: _Launch) -> None:
        """Read the app server until the connection ends, then release every call still waiting and,
        unless the host is stopping the member, say the side channel is lost.

        Both happen however the reading ends. An exception other than a closed stream once ended the
        reader without a word: the member looked busy, and every call waiting on an answer waited
        out its whole timeout."""
        assert state.socket is not None
        why = "the connection to Codex's app server closed"
        cancelled = False
        try:
            while (text := await state.socket.receive()) is not None:
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await self._handle(state, message)
        except (StreamClosed, ConnectionError):
            pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001 — the channel is gone either way; the reason is the member's error
            logger.warning("reading codex's app server failed", exc_info=True)
            why = f"reading Codex's app server failed: {exc}"
        finally:
            for future in state.pending.values():
                if not future.done():
                    future.set_result({"error": {"code": -1, "message": why}})
            if not state.stopping and not cancelled:
                state.queue.put_nowait(StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": f"side channel lost: {why}"}))

    async def _handle(self, state: _Launch, message: dict[str, Any]) -> None:
        """One message from the app server. One the adapter cannot read is logged and skipped: a
        single malformed notification must not cost the member its channel."""
        method = message.get("method")
        params = message.get("params")
        try:
            if method is not None and params is not None and not isinstance(params, dict):
                raise TypeError(f"params of {method} are {type(params).__name__}, not an object")
            if method is not None and "id" in message:
                await self._server_request(state, message)
            elif method is not None:
                self._notification(state, str(method), params or {})
            else:
                future = state.pending.get(message.get("id"))  # type: ignore[arg-type]
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — see the description
            logger.warning("a %s message from codex's app server could not be handled; skipped", method or "reply", exc_info=True)
            if method is not None and "id" in message:
                # Unanswered, the request would hold the turn for good.
                with contextlib.suppress(Exception):
                    await self._respond(state, message["id"], error={"code": -32603, "message": f"{method} could not be read by this client"})

    def _put(self, state: _Launch, kind: EventKind, payload: Mapping[str, Any] | None = None, native_id: str = "") -> None:
        if kind is EventKind.ACTIVITY and not state.turn_id:
            # Between turns the server still talks (the thread going idle, token counts, its helper
            # threads); none of it is work, and "activity" would take a finished turn back to working.
            return
        state.queue.put_nowait(StaffEvent(kind, _now(), dict(payload or {}), native_id=native_id))

    async def _server_request(self, state: _Launch, message: dict[str, Any]) -> None:
        method = str(message["method"])
        params = message.get("params") or {}
        if params.get("threadId") not in (None, state.thread_id):
            await self._respond(state, message["id"], error={"code": -32601, "message": "not this client's thread"})
            return
        ref = f"codex-{message['id']}"
        if method == "item/commandExecution/requestApproval":
            summary = " ".join(str(params.get("command") or params.get("reason") or "a command").split())[:300]
            state.requests[ref] = _Request(message["id"], method, params)
            self._put(state, EventKind.PERMISSION_REQUESTED, {"tool": "command", "summary": summary, "options": ["allow_once", "allow_always", "deny"]}, ref)
        elif method in ("item/fileChange/requestApproval", "item/permissions/requestApproval"):
            what = "file changes" if "fileChange" in method else "more permissions"
            summary = str(params.get("reason") or what)[:300]
            state.requests[ref] = _Request(message["id"], method, params)
            self._put(state, EventKind.PERMISSION_REQUESTED, {"tool": "edit" if "fileChange" in method else "permissions", "summary": summary, "options": ["allow_once", "allow_always", "deny"]}, ref)
        elif method == "item/tool/requestUserInput":
            questions = [q for q in params.get("questions") or [] if isinstance(q, dict)]
            first = questions[0] if questions else {}
            text = "\n".join(str(q.get("question") or "") for q in questions).strip() or "Codex asks a question"
            options = [str(o.get("label") or "") for o in first.get("options") or [] if isinstance(o, dict)]
            state.requests[ref] = _Request(message["id"], method, params)
            self._put(state, EventKind.QUESTION_ASKED, {"tool": "question", "summary": text[:200], "text": text, "options": options}, ref)
        elif method == "mcpServer/elicitation/request":
            form = _form(params)
            text = str(params.get("message") or "An MCP server asks for input")
            if form.kind == "url":
                text += f"\n\n{params.get('url') or ''}".rstrip()
            elif form.kind == "terminal":
                text += "\n\nThis form can only be filled in Codex's terminal; here it can only be declined."
            state.requests[ref] = _Request(message["id"], method, params)
            self._put(state, EventKind.QUESTION_ASKED, {"tool": "elicitation", "summary": text[:200], "text": text, "options": list(form.options)}, ref)
        else:
            await self._respond(state, message["id"], error={"code": -32601, "message": f"{method} is not handled by this client"})

    def _notification(self, state: _Launch, method: str, params: dict[str, Any]) -> None:
        thread = params.get("threadId") or ((params.get("thread") or {}).get("id") if isinstance(params.get("thread"), dict) else None)
        if thread and state.thread_id and thread != state.thread_id:
            return  # the server's helper threads (titles, memories), and anyone else's
        if method == "turn/started":
            state.turn_id = str((params.get("turn") or {}).get("id") or "")
            state.failure = ""
            state.last_message = ""
            self._put(state, EventKind.TURN_STARTED, {"turn": state.turn_id})
        elif method in ("item/started", "item/completed"):
            item = params.get("item")
            self._item(state, method == "item/started", item if isinstance(item, dict) else {})
        elif method == "error":
            if not params.get("willRetry"):
                error = params.get("error")
                state.failure = str(error.get("message") or "") if isinstance(error, dict) else str(error or "")
        elif method == "turn/completed":
            self._turn_completed(state, params.get("turn") or {})
        elif method == "serverRequest/resolved":
            ref = f"codex-{params.get('requestId')}"
            request = state.requests.pop(ref, None)
            if request is not None:
                request.answered.set()
                if not request.ours:
                    self._put(state, EventKind.REQUEST_RESOLVED, {}, ref)
        elif method == "thread/status/changed":
            self._status(state, params.get("status") or {})
        elif method == "account/rateLimits/updated":
            limits = params.get("rateLimits")
            state.rate_limits = limits if isinstance(limits, dict) else {}
            windows = [state.rate_limits.get(name) for name in ("primary", "secondary")]
            readings = [[int(w.get("windowDurationMins") or 0), float(w["usedPercent"])] for w in windows if isinstance(w, dict) and isinstance(w.get("usedPercent"), int | float)]
            if readings:
                self._put(state, EventKind.USAGE, {"windows": readings})
        elif method == "thread/tokenUsage/updated":
            self._token_usage(state, params.get("tokenUsage"))
        else:
            self._put(state, EventKind.ACTIVITY, {"method": method})

    def _token_usage(self, state: _Launch, usage: Any) -> None:
        """The thread's spend after a model call, as the board shows it: the whole prompt sent so
        far (the cached part included, as the transcript counts it), the output, the cached part,
        and the last call's prompt against the window. Codex sends one after every call of a turn;
        read only from the rollout after the turn, the numbers stood still while the member worked."""
        if not isinstance(usage, dict):
            return
        total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
        last = usage.get("last") if isinstance(usage.get("last"), dict) else {}
        assert isinstance(total, dict) and isinstance(last, dict)
        reading = {
            "input_tokens": int(total.get("inputTokens") or 0),
            "output_tokens": int(total.get("outputTokens") or 0),
            "cache_read_tokens": int(total.get("cachedInputTokens") or 0),
            "context_tokens": int(last.get("inputTokens") or 0),
            "context_window": int(usage.get("modelContextWindow") or 0),
        }
        if any(reading.values()):
            self._put(state, EventKind.USAGE, reading)

    def _item(self, state: _Launch, started: bool, item: Mapping[str, Any]) -> None:
        kind = str(item.get("type") or "")
        if kind == "userMessage":
            if not started:
                return
            text = "".join(str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict))
            client = str(item.get("clientId") or "")
            payload: dict[str, Any] = {"prompt": text}
            if client in state.client_ids:
                payload["message_id"] = state.client_ids.pop(client)
            self._put(state, EventKind.PROMPT_ACKNOWLEDGED, payload)
        elif kind == "agentMessage":
            if not started and str(item.get("text") or "").strip():
                state.last_message = str(item["text"])
            self._put(state, EventKind.ACTIVITY, {"item": kind})
        elif kind in ("commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "webSearch", "collabAgentToolCall"):
            tool = str(item.get("tool") or item.get("command") or kind) if kind != "commandExecution" else "command"
            if started:
                self._put(state, EventKind.TOOL_STARTED, {"tool": tool}, str(item.get("id") or ""))
            else:
                finished: dict[str, Any] = {"tool": tool, "ok": item.get("status") == "completed"}
                if kind == "fileChange" and finished["ok"]:
                    # A patch names every file it touched in its changes, each with its own path.
                    edited = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict) and c.get("path")]
                    if edited:
                        finished["edited"] = edited
                self._put(state, EventKind.TOOL_FINISHED, finished, str(item.get("id") or ""))
        else:
            self._put(state, EventKind.ACTIVITY, {"item": kind})

    def _turn_completed(self, state: _Launch, turn: Mapping[str, Any]) -> None:
        status = str(turn.get("status") or "completed")
        state.turn_id = ""
        # A turn that ended has nothing waiting any more, whoever settled it.
        for ref, request in list(state.requests.items()):
            request.answered.set()
            self._put(state, EventKind.REQUEST_RESOLVED, {}, ref)
        state.requests.clear()
        if state.waiting_ref:
            self._put(state, EventKind.REQUEST_RESOLVED, {}, state.waiting_ref)
            state.waiting_ref = ""
        if status == "interrupted":
            self._put(state, EventKind.TURN_CANCELLED, {"via": "turn/interrupt"})
        elif status == "failed":
            error = turn.get("error") or {}
            failure = str(error.get("message") or state.failure or "the turn failed") if isinstance(error, dict) else state.failure or "the turn failed"
            code = _error_tag(error.get("codexErrorInfo")) if isinstance(error, dict) else ""
            payload: dict[str, Any] = {"failure": failure, "code": code}
            if code in OUTAGES:
                payload["outage"] = True
                resets = _resets_at(state.rate_limits) if code in QUOTAS else ""
                if resets:
                    payload["resets_at"] = resets
            self._put(state, EventKind.TURN_FAILED, payload)
        else:
            self._put(state, EventKind.TURN_COMPLETED, {"last_message": state.last_message})

    def _status(self, state: _Launch, status: Mapping[str, Any]) -> None:
        flags = [str(f) for f in status.get("activeFlags") or []] if status.get("type") == "active" else []
        if "waitingOnApproval" in flags or "waitingOnUserInput" in flags:
            if not state.requests and not state.waiting_ref:
                state.tasks.append(asyncio.ensure_future(self._waiting_without_request(state, "waitingOnApproval" in flags)))
        elif state.waiting_ref:
            self._put(state, EventKind.REQUEST_RESOLVED, {}, state.waiting_ref)
            state.waiting_ref = ""
        self._put(state, EventKind.ACTIVITY, {"status": status.get("type")})

    async def _waiting_without_request(self, state: _Launch, approval: bool) -> None:
        """The thread waits on a decision the host was not asked for: another client got it. The
        operator answers it in the terminal, and the orchestrator is told so."""
        await asyncio.sleep(WAITING_GRACE_S)
        if state.requests or state.waiting_ref or not state.turn_id:
            return
        state.waiting_ref = f"codex-terminal-{secrets.token_hex(4)}"
        kind = EventKind.PERMISSION_REQUESTED if approval else EventKind.QUESTION_ASKED
        text = "Codex waits for an answer in its terminal"
        self._put(state, kind, {"tool": "terminal", "summary": text, "text": text, "options": []}, state.waiting_ref)

    # -- what it does ---------------------------------------------------------------------------

    async def _ready(self, term: TerminalPort) -> _Launch:
        state = self._state(term)
        await asyncio.wait_for(state.connected.wait(), CONNECT_S)
        return state

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        state = await self._ready(term)
        client = message_id or f"d{secrets.token_hex(6)}"
        state.client_ids[client] = message_id
        words = [{"type": "text", "text": text}]
        if mode == "now" and state.turn_id:
            try:
                await self._call(state, "turn/steer", {"threadId": state.thread_id, "expectedTurnId": state.turn_id, "input": words, "clientUserMessageId": client})
                return Delivery(message_id, "submitted", via="turn/steer", client_ref=client)
            except CodexError as exc:
                # The turn ended between the look and the steer: the message starts the next one.
                logger.info("codex steer refused (%s); starting a turn", exc.message)
        try:
            await self._call(state, "turn/start", {"threadId": state.thread_id, "input": words, "clientUserMessageId": client})
        except CodexError as exc:
            state.client_ids.pop(client, None)
            return Delivery(message_id, "failed", via="turn/start", error=exc.message)
        return Delivery(message_id, "submitted", via="turn/start", client_ref=client)

    async def interrupt(self, term: TerminalPort) -> None:
        state = await self._ready(term)
        if state.turn_id:
            with contextlib.suppress(CodexError):
                await self._call(state, "turn/interrupt", {"threadId": state.thread_id, "turnId": state.turn_id})

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        state = self._state(term)
        request = state.requests.get(request_ref)
        if request is None:
            return False
        allowed = answer.choice.startswith("allow")
        if request.method == "item/commandExecution/requestApproval":
            result: Any = {"decision": "acceptForSession" if answer.choice == "allow_always" else "accept" if allowed else "decline"}
        elif request.method == "item/fileChange/requestApproval":
            result = {"decision": "acceptForSession" if answer.choice == "allow_always" else "accept" if allowed else "decline"}
        elif request.method == "item/permissions/requestApproval":
            granted = request.params.get("permissions") if allowed else {}
            result = {"permissions": granted or {}, "scope": "session" if answer.choice == "allow_always" else "turn"}
        elif request.method == "item/tool/requestUserInput":
            chosen = answer.said
            result = {"answers": {str(q.get("id") or ""): {"answers": [chosen]} for q in request.params.get("questions") or [] if isinstance(q, dict)}}
        else:
            result = _form(request.params).answer(answer)
        request.ours = True
        await self._respond(state, request.rpc_id, result)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(request.answered.wait(), ANSWER_CONFIRM_S)
        return request.answered.is_set()

    async def stop(self, term: TerminalPort) -> None:
        state = self._state(term)
        state.stopping = True
        if state.turn_id:
            with contextlib.suppress(Exception):
                await self._call(state, "turn/interrupt", {"threadId": state.thread_id, "turnId": state.turn_id}, timeout=5)
        # Never type into a dialog: Enter chooses its highlighted row, which on an approval is "Yes,
        # proceed". Esc is the dialog's own way out (an approval's "No", an MCP request's cancel); a
        # dialog that will not go is left for the runtime to end with the terminal after its grace.
        for _ in range(DISMISS_ATTEMPTS):
            if self.classify_screen(await term.screen()) is not ScreenClass.DIALOG:
                break
            await term.write(keys=["Esc"])
            await asyncio.sleep(0.3)
        else:
            return
        await term.write(text="/exit")
        await asyncio.sleep(0.3)
        await term.write(keys=["Enter"])

    # -- the screen -----------------------------------------------------------------------------

    def classify_screen(self, text: str) -> ScreenClass:
        lines = [line for line in text.splitlines() if line.strip()]
        bottom = "\n".join(lines[-24:])
        if any(marker in bottom for marker in DIALOG_MARKERS):
            return ScreenClass.DIALOG
        tail = lines[-6:]
        if any(BUSY_HINT in line for line in tail):
            return ScreenClass.BUSY
        if any(line.lstrip().startswith("› ") or line.strip() == "›" for line in tail):
            return ScreenClass.IDLE_COMPOSER
        return ScreenClass.UNKNOWN

    def composer_holds(self, screen: str, text: str) -> bool:
        return False  # nothing is typed into Codex's composer; messages go by the app server

    def composer_text(self, screen: str) -> str | None:
        return None  # nothing is typed into Codex's composer; messages go by the app server

    # -- the transcript -------------------------------------------------------------------------

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        """Read from the thread's rollout file, the one record that outlives the app server. Its
        format is Codex's own and not a published interface: the reader keeps to messages, tool
        calls and token counts, and skips what it does not know."""
        stat = await env.stat(ref)
        size = int((stat or {}).get("size") or 0)
        offset = max(0, size - TRANSCRIPT_MAX_BYTES)
        raw = await env.read(ref, offset=offset, limit=TRANSCRIPT_MAX_BYTES)
        text = raw.decode("utf-8", "replace")
        if offset:
            text = text.split("\n", 1)[1] if "\n" in text else ""
        return parse_rollout(text)[since:]


def _error_tag(info: Any) -> str:
    """A ``codexErrorInfo`` as one name: a string as it is, an object by its one key
    (``{"httpConnectionFailed": {"httpStatusCode": 502}}``). Stringified whole, an object was a
    Python dict's repr, which nothing could match."""
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and len(info) == 1:
        return str(next(iter(info)))
    return ""


def _resets_at(limits: Mapping[str, Any]) -> str:
    """When the exhausted rate-limit window resets, as an ISO time; empty when no window is full or
    none says. Codex gives ``resetsAt`` in seconds since the epoch; of two full windows the later
    reset is the one that ends the wait."""
    resets = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if isinstance(window, dict) and float(window.get("usedPercent") or 0) >= 100 and isinstance(window.get("resetsAt"), int | float):
            resets.append(float(window["resetsAt"]))
    return datetime.fromtimestamp(max(resets), UTC).isoformat() if resets else ""


ACCEPTING = ("accept", "allow_once", "allow_always")
DECLINED = {"action": "decline", "content": None}


@dataclass(frozen=True, slots=True)
class _Form:
    """What an MCP elicitation asks for, as far as a question to the team can carry it.

    ``kind`` is ``url`` (a page to open; accepting carries no content), ``empty`` (a form with no
    fields), ``choice`` and ``text`` (one field, filled with the option chosen or the words
    written), or ``terminal`` (anything more, which only Codex's own form can fill). An accept with
    no content once answered every form, and a server that requires its fields was handed none."""

    kind: str
    name: str = ""
    """The one field's name, for ``choice`` and ``text``."""
    values: Mapping[str, str] = field(default_factory=dict)
    """For ``choice``: each option's label to the value it stands for."""

    @property
    def options(self) -> tuple[str, ...]:
        if self.kind in ("url", "empty"):
            return ("accept", "decline")
        return (*self.values, "decline")

    def answer(self, answer: Answer) -> dict[str, Any]:
        if answer.choice in ("decline", "deny", "deny_with_note"):
            return DECLINED
        if self.kind == "url" and answer.choice in ACCEPTING:
            return {"action": "accept", "content": None}
        if self.kind == "empty" and answer.choice in ACCEPTING:
            return {"action": "accept", "content": {}}
        if self.kind == "choice" and answer.choice in self.values:
            return {"action": "accept", "content": {self.name: self.values[answer.choice]}}
        if self.kind == "text" and answer.choice not in ACCEPTING and answer.said.strip():
            return {"action": "accept", "content": {self.name: answer.said.strip()}}
        return DECLINED


def _form(params: Mapping[str, Any]) -> _Form:
    if params.get("mode") == "url":
        return _Form("url")
    schema = params.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return _Form("empty") if isinstance(properties, dict) else _Form("terminal")
    if len(properties) != 1:
        return _Form("terminal")
    [(name, spec)] = properties.items()
    if not isinstance(spec, dict):
        return _Form("terminal")
    labelled = [(str(o.get("title") or o.get("const")), str(o.get("const"))) for o in spec.get("oneOf") or [] if isinstance(o, dict) and o.get("const") is not None]
    if not labelled and isinstance(spec.get("enum"), list):
        names = spec.get("enumNames") if isinstance(spec.get("enumNames"), list) else []
        labelled = [(str(names[i]) if i < len(names) else str(v), str(v)) for i, v in enumerate(spec["enum"])]
    if labelled:
        return _Form("choice", str(name), dict(labelled))
    if spec.get("type") == "string":
        return _Form("text", str(name))
    return _Form("terminal")


def _highlighted(screen: str) -> str:
    for line in reversed(screen.splitlines()):
        stripped = line.strip()
        if stripped.startswith("›") and re.match(r"›\s*\d+\.", stripped):
            return re.sub(r"^›\s*\d+\.\s*", "", stripped)
    return ""


def _last_line_with(screen: str, word: str, *, any_case: bool = False) -> str:
    for line in reversed(screen.splitlines()):
        if word in (line.lower() if any_case else line):
            return line.strip()[:300]
    return ""


def parse_rollout(text: str) -> list[Turn]:
    """Codex's rollout JSON lines as turns: user and assistant messages, the tool calls between them,
    and the token count reported at each step. Developer messages, the context Codex injects as user
    messages (environment, instructions, plugins) and every other record are skipped."""
    turns: list[Turn] = []
    current: dict[str, Any] | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            usage = current["usage"]
            if current["text"] or current["tools"]:
                spent = TurnUsage(*usage, context_tokens=current.get("context", 0), context_window=current.get("window", 0), windows=current.get("windows", ())) if any(usage) else None
                turns.append(Turn(len(turns), "assistant", "\n".join(current["text"]).strip(), tuple(current["tools"]), current["at"], current["end"], spent))
            current = None

    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        at = str(record.get("timestamp") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        kind = record.get("type")
        if kind == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            words = "".join(str(c.get("text") or "") for c in payload.get("content") or [] if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")).strip()
            if role == "user":
                if not words or words.startswith(INJECTED):
                    continue
                close()
                turns.append(Turn(len(turns), "orchestrator" if words.startswith("[orchestrator]") else "user", words, started_at=at, ended_at=at))
            elif role == "assistant" and words:
                current = current or {"text": [], "tools": [], "at": at, "end": at, "usage": [0, 0, 0]}
                current["text"].append(words)
                current["end"] = at
            continue
        if kind == "response_item" and payload.get("type") in ("function_call", "local_shell_call", "custom_tool_call"):
            current = current or {"text": [], "tools": [], "at": at, "end": at, "usage": [0, 0, 0]}
            name = str(payload.get("name") or payload.get("type") or "tool")
            summary = name
            arguments = payload.get("arguments") or payload.get("input") or payload.get("action")
            with contextlib.suppress(ValueError, TypeError):
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                if isinstance(parsed, dict):
                    command = parsed.get("command") or parsed.get("cmd")
                    summary = " ".join(command) if isinstance(command, list) else str(command or parsed.get("path") or name)
            current["tools"].append(ToolUse(name, " ".join(summary.split())[:300]))
            current["end"] = at
            continue
        if kind == "event_msg" and payload.get("type") == "token_count" and current is not None:
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            assert isinstance(info, dict)
            last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            assert isinstance(last, dict)
            cached = int(last.get("cached_input_tokens") or 0)
            current["usage"][0] += max(0, int(last.get("input_tokens") or 0) - cached)
            current["usage"][1] += int(last.get("output_tokens") or 0)
            current["usage"][2] += cached
            # The call's input already includes its cached part: it is the whole prompt, the fill.
            current["context"] = int(last.get("input_tokens") or 0)
            current["window"] = int(info.get("model_context_window") or 0)
            limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
            assert isinstance(limits, dict)
            windows = [limits.get(name) for name in ("primary", "secondary")]
            current["windows"] = tuple((int(w.get("window_minutes") or 0), float(w.get("used_percent") or 0.0)) for w in windows if isinstance(w, dict) and w.get("used_percent") is not None)
            continue
        if kind == "event_msg" and payload.get("type") in ("task_complete", "turn_aborted"):
            close()
    close()
    return turns


__all__ = ["CodexAdapter", "parse_rollout"]
