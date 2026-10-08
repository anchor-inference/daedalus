"""The contract every command-line harness adapter keeps, and the values that cross it.

An adapter knows one CLI: how to launch it inside a terminal, how to tell when it is ready, busy,
asking or done, how to hand it a message and prove it arrived, and where it keeps its transcript.
It knows nothing of staff, projects, queues or the database. The staff runtime for command-line
staff wraps an adapter, feeds its events through the state machine (``state.py``) and reports to the
orchestrator's ingress; the harness manager uses the version, catalog and sign-in half.

The adapter never touches a terminal or an environment directly. It is handed two narrow ports,
``TerminalPort`` for the one terminal it drives and ``EnvironmentPort`` for the environment the CLI
lives in, both implemented over the terminal daemon's connection. That keeps an adapter testable
against a fake daemon, and keeps every write to a terminal on the one audited path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from daedalus.harness.capabilities import Capabilities

Environment = Literal["container", "host"]
SendMode = Literal["now", "after_turn", "interrupt"]
"""When a message goes in: ``now`` into the running turn (a steer), ``after_turn`` once the turn has
ended, ``interrupt`` after stopping the turn. Named by timing because that is the one thing a sender
chooses; how a CLI manages each is its capability's ``steer``."""
DeliveryState = Literal["queued", "written", "submitted", "acknowledged", "failed"]

ANSWER_CHOICES = ("allow_once", "allow_always", "allow_always_server", "deny", "deny_with_note")
"""What a permission answer can be, whatever words the CLI's dialog uses for it. A question is
answered with one of its own options by label, or with free text. ``allow_always_server`` is
"always" for every tool of the MCP server the asked tool belongs to (see ``standing_rule``)."""

StandingScope = Literal["tool", "server"]


def mcp_server(tool: str) -> str:
    """The MCP server of a tool named ``mcp__<server>__<tool>``, or empty for any other tool.

    Split the way Claude Code splits it: the server is what lies between the first and the second
    ``__``, and the tool is the rest, which may itself hold ``__``. A server name never does, since
    the CLI folds every character outside ``[A-Za-z0-9_-]`` of it to one underscore."""
    prefix, _, rest = tool.partition("__")
    server, sep, name = rest.partition("__")
    return server if prefix == "mcp" and server and sep and name else ""


def standing_rule(tool: str, scope: StandingScope) -> str | None:
    """The allow rule an "Always" for ``tool`` leaves behind, or ``None`` when it leaves none.

    Only an MCP tool gets one. ``mcp__<server>__<tool>`` names the one tool and ``mcp__<server>``
    every tool of that server (Claude Code 2.1: a rule whose tool part is absent matches the whole
    server). A built-in tool gets none: a bare ``Bash`` or ``Write`` rule would let through every
    command or every file, far more than the one call the operator looked at, so its "Always" stays
    what the CLI's own dialog offers."""
    server = mcp_server(tool)
    if not server:
        return None
    return f"mcp__{server}" if scope == "server" else tool


class ScreenClass(StrEnum):
    """What the terminal's screen shows, as the adapter reads it. Only ever a fallback: the screen is
    read to find readiness, a dialog, or a turn end that no structured signal reported."""

    IDLE_COMPOSER = "idle_composer"
    BUSY = "busy"
    DIALOG = "dialog"
    UNKNOWN = "unknown"


class EventKind(StrEnum):
    """The normalised events an adapter emits. Only the state machine turns them into a status."""

    READY = "ready"
    PROMPT_ACKNOWLEDGED = "prompt_acknowledged"
    TURN_STARTED = "turn_started"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    ACTIVITY = "activity"
    PERMISSION_REQUESTED = "permission_requested"
    QUESTION_ASKED = "question_asked"
    REQUEST_RESOLVED = "request_resolved"
    TURN_COMPLETED = "turn_completed"
    TURN_CANCELLED = "turn_cancelled"
    TURN_FAILED = "turn_failed"
    SEEN = "seen"
    SESSION_ENDED = "session_ended"
    PROCESS_EXITED = "process_exited"
    QUIET = "quiet"
    RECONCILED = "reconciled"
    NOTIFICATION = "notification"
    USAGE = "usage"
    TRANSCRIPT = "transcript"


@dataclass(frozen=True, slots=True)
class StaffEvent:
    """One thing a CLI did, in the adapter's words normalised.

    The payload keys the state machine reads: ``summary`` (what a permission or question is about),
    ``failure`` (why a turn failed), ``screen`` (a ``ScreenClass`` value for ``reconciled``),
    ``exit_code``. Anything else is for the log and the feed.
    """

    kind: EventKind
    at: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    native_id: str = ""
    """The CLI's own handle for what the event is about: a tool-use id, a JSON-RPC id, a request id."""
    launch_id: str = ""


@dataclass(frozen=True, slots=True)
class InstallInfo:
    installed: bool
    path: str = ""
    version: str = ""
    method: str = ""
    """How it was installed (``native``, ``npm``, ``installer``), which decides how it is updated."""
    detail: str = ""
    """Why it counts as not installed, when a binary of that name is something else."""


@dataclass(frozen=True, slots=True)
class UpdateResult:
    ok: bool
    previous: str
    version: str
    output: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class CheckStep:
    name: str
    """``version``, ``supported``, ``signin`` (the manager's own), then the adapter's: ``launch``,
    ``hook``, ``ready``, ``deliver``, ``reply``, ``exit``."""
    ok: bool
    detail: str = ""
    duration_ms: int = 0
    skipped: bool = False
    """The step could not be run here (no adapter for the CLI yet, the model turn switched off). A
    skipped step is not a passed one: the screen says which were skipped, so a check that proved
    less than it could is never shown as a full pass."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    steps: tuple[CheckStep, ...]
    version: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class AgentEntry:
    name: str
    source: str
    """``project``, ``user`` or ``builtin``: where the definition lives."""
    description: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class Catalog:
    agents: tuple[AgentEntry, ...] = ()
    models: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    """Permission modes, or sandbox and approval choices, by the names the CLI's own flags take."""
    profiles: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoginState:
    state: Literal["yes", "no", "unknown"]
    detail: str = ""
    """What the CLI said, without secrets: an account name or a plan, never a token."""


@dataclass(frozen=True, slots=True)
class HookSpec:
    """The hooks a launch listens for. ``hold_ms`` keeps the request open at the daemon's ingress
    until the host replies, for hooks whose answer is the decision (a permission, a team question)."""

    sources: tuple[str, ...] = ()
    hold_ms: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompanionSpec:
    """A second process the TUI needs, run in a terminal of its own before the TUI starts."""

    role: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    ready_pattern: str = ""
    """A regular expression on the companion's output that says it is listening."""


LAUNCH_DIR = "{launch_dir}"
"""Stands for the launch's private directory in ``LaunchPlan.argv`` and ``env`` values. The daemon
creates the directory when the launch is registered, so the adapter cannot know its path; the
runtime substitutes it before the terminal is created."""

DIAL_DIR = "{dial_dir}"
"""Stands for the launch's dial directory, where a program the launch starts puts the unix socket the
host reaches through ``TerminalPort.dial``. Substituted like ``LAUNCH_DIR``: the daemon makes it with
the launch, beside the launch directory rather than inside it."""


@dataclass(frozen=True, slots=True)
class ToolSetSpec:
    """A set of Daedalus's own tools a launch offers its CLI through ``ptyd tools-mcp --set <name>``.

    The host describes the tools in a launch file (``tools/<name>.json``) and answers every call from
    the launch's hook listener, so the CLI's copy of a tool runs through the same code, policy and
    audit as a Daedalus session's. An adapter only places the MCP entry and its CLI's permission rule
    for the tools that only read (``read_only``)."""

    name: str
    """The set, as ``tools-mcp`` takes it and the launch file is named: ``browser``."""
    server: str
    """The MCP server's name, which is how the CLI names the tools: ``daedalus_browser``."""
    file: bytes
    """The launch file's content."""
    hold_ms: int
    """How long one call may wait for the host; the CLI's own timeout on a tool call goes above it."""
    read_only: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return f"tools/{self.name}.json"

    def command(self) -> str:
        """The shell line that starts the server; the daemon's path is known only from the launch."""
        return f'exec "$DAEDALUS_PTYD_BIN" tools-mcp --set {self.name}'


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything an adapter needs to plan a launch, composed by the host.

    The host composes the brief and the first message (persona, instructions, notes, task), so the
    adapter never reads the board or the staff tables; it only decides how this CLI is given them.
    """

    harness: str
    env: Environment
    cwd: str
    launch_id: str
    first_prompt: str | None
    brief_text: str = ""
    model: str = ""
    effort: str = ""
    agent: str = ""
    permission_mode: str = ""
    permission_level: Literal["ask", "edits", "all"] = "ask"
    """What the project's autonomy lets through without asking, for the adapter to map onto the
    CLI's own mode when the member has no ``permission_mode`` of its own."""
    team_url: str = ""
    team_token: str = ""
    """Left empty by the runtime: the team tools post to the launch's hook listener, which a CLI
    reaches from either environment, while the host's own team route is loopback of the host's
    container and unreachable from the terminals container."""
    session_ref: str = ""
    """The CLI session to resume; empty for a new one."""
    title: str = ""
    """The session's display name (``Ada · Menu page``), for a CLI that shows one."""
    team_block: str = ""
    """The mandatory lines about the team (``harness.team.mandatory_block``) followed by the standing
    brief, for the CLI's system-level channel. Composed by the runtime; the adapter only places it."""
    team_skill: str = ""
    """The ``daedalus-team`` skill's text, for a CLI that loads skills; the adapter writes it where
    its CLI finds skills in the launch directory, never into the project folder."""
    ask_hold_ms: int = 300_000
    """How long ``AskOrchestrator`` is held for an answer; a CLI's own timeout on a tool call must be
    set above it, or the CLI cuts the question short."""
    report_hold_ms: int = 15_000
    permission_hold_ms: int = 300_000
    """How long a CLI's permission or question hook may be held for an answer given elsewhere."""
    port_range: tuple[int, int] = (18300, 18399)
    """Loopback ports a CLI that serves on one (OpenCode) may take, ``harness.opencode_port_range``."""
    tool_sets: tuple[ToolSetSpec, ...] = ()
    """Daedalus's tools beyond the team's that this launch offers (the browser's), each through an
    MCP entry of its own."""
    allow_rules: tuple[str, ...] = ()
    """The standing grants the operator gave this member ("Always"), as the CLI's allow rules
    (``standing_rule``). Written into every launch, so a grant outlives the session it was given in."""

    def validate_permissions(self, modes: Collection[str]) -> None:
        """Reject a permission setting the adapter cannot map before reserving launch resources."""
        if self.permission_level not in ("ask", "edits", "all"):
            raise ValueError(f"unsupported permission level {self.permission_level!r}")
        if self.permission_mode and self.permission_mode not in modes:
            # An unknown explicit mode used to fall back to the project's autonomy, which could
            # turn a misspelled read-only request into full access without another decision.
            raise ValueError(f"unsupported {self.harness} permission mode {self.permission_mode!r}")


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    files: Mapping[str, bytes] = field(default_factory=dict)
    """Overlay files by name, written by the daemon into the launch directory and removed with it —
    never into the CLI's own configuration."""
    ports: tuple[int, ...] = ()
    """Loopback ports the launch listens on, which the daemon lets the host dial."""
    companions: tuple[CompanionSpec, ...] = ()
    session_ref: str = ""
    """The CLI session id chosen before the launch, where the CLI allows choosing it."""
    transcript_hint: str | None = None
    first_prompt: str | None = None
    first_prompt_via: Literal["argv", "channel"] = "argv"
    hooks: HookSpec = field(default_factory=HookSpec)
    adapter_state: str = field(default="", repr=False)
    """What the adapter needs to take the launch up again after the host restarted, when that is
    more than the launch row says (OpenCode's port and its server's password). Kept in the launch row
    until the launch ends and blanked then; never logged and never shown, since it may hold a secret."""


@dataclass(frozen=True, slots=True)
class Launch:
    """A launch as the host remembers it, so a restarted host can attach to a CLI still running."""

    launch_id: str
    staff_session_id: str
    harness: str
    env: Environment
    terminal_id: str | None
    companion_terminal_id: str | None
    launch_dir: str
    session_ref: str
    harness_version: str
    started_at: str
    ended_at: str | None = None
    adapter_state: str = field(default="", repr=False)
    """The plan's ``adapter_state``, for the adapter's ``events`` after a host restart; empty once ended."""


@dataclass(frozen=True, slots=True)
class HookPost:
    """One post to the launch's hook listener, as the adapter's ``events`` reads it.

    ``name`` is the last part of the hook's path (a Claude event name, ``grok``, ``pi``); ``reply_id``
    is set when the post is held for an answer, which ``TerminalPort.reply`` gives. The team tools'
    posts (``name == "team"``) never reach an adapter: the runtime answers those itself.
    """

    name: str
    body: Any
    at: str = ""
    reply_id: str | None = None
    hold_ms: int = 0


@dataclass(frozen=True, slots=True)
class ReadyStep:
    """What the readiness gate does about the screen it just read, as the adapter judges it.

    ``wait``: nothing recognised yet, or the CLI is still drawing. ``keys``: a dialog the launch
    answers (the folder-trust question), with the keys that answer it — only after the adapter has
    checked that the highlighted row is the one those keys choose. ``fail``: the CLI will not get
    ready by itself (a sign-in screen), and ``reason`` says why in the operator's words.
    """

    action: Literal["wait", "keys", "fail"] = "wait"
    keys: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Answer:
    """A decision on a pending request, in the harness's normalised terms."""

    choice: str
    """One of ``ANSWER_CHOICES`` for a permission; an option's label or ``text`` for a question."""
    note: str = ""

    @property
    def said(self) -> str:
        """A question's answer as the asker is handed it: the option chosen and the operator's words
        beside it, whole. Once only the option's label went to the command-line agents, and a note
        written next to a chosen option never reached the member at all."""
        if self.choice == "text":
            return self.note
        note = self.note.strip()
        return f"{self.choice} — {note}" if note and note != self.choice else self.choice


@dataclass(frozen=True, slots=True)
class Delivery:
    """How far a message got, as the adapter knows it. A write to the terminal is ``written`` and
    never more: only the CLI saying it took the prompt makes it ``acknowledged``."""

    message_id: str
    state: DeliveryState
    via: str = ""
    """``paste``, ``pointer`` or the structured channel's name."""
    degraded_to: Literal["", "after_turn", "interrupt"] = ""
    client_ref: str = ""
    """The id the CLI echoes back for this message, where it takes one, so a restarted host can find it."""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ToolUse:
    name: str
    summary: str
    ok: bool | None = None


@dataclass(frozen=True, slots=True)
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    context_tokens: int = 0
    """The prompt of the turn's last model call, cached part included: how full the context was
    when the turn ended. The sums above cannot say it, since a turn of ten calls counts its history
    ten times. Zero where the CLI's transcript does not give each call's own count."""
    context_window: int = 0
    """The window the CLI itself reported for its model; zero when it did not. Never guessed from
    the model's name: a wrong denominator makes a meter that lies."""
    windows: tuple[tuple[int, float], ...] = ()
    """The subscription's rate-limit windows as the CLI last reported them, each as its length in
    minutes and the share used in percent (Codex writes them beside every token count)."""


@dataclass(frozen=True, slots=True)
class Turn:
    """One turn of a transcript, read from the CLI's own store, never from the screen."""

    index: int
    role: Literal["user", "assistant", "orchestrator", "system"]
    text: str
    tools: tuple[ToolUse, ...] = ()
    started_at: str = ""
    ended_at: str = ""
    usage: TurnUsage | None = None


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    path: str = ""
    """The program ``PATH`` resolved ``argv[0]`` to, where the environment says: how a CLI was
    installed is read from where it lives."""


class ProgramNotFound(LookupError):
    """``EnvironmentPort.run`` found no program of that name: the CLI is not installed there."""


class EnvironmentUnavailable(RuntimeError):
    """The environment cannot be reached at all (its terminal daemon is down or not installed), which
    says nothing about whether a CLI is installed in it."""


class ByteStreamPort(Protocol):
    """Bytes to and from a program's socket. ``read`` returns ``b""`` once either side closed it."""

    async def read(self) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


class TerminalPort(Protocol):
    """The one terminal an adapter drives. Every write goes through the daemon's writer queue and the
    audit, and waits for the human's quiet time unless the caller is the human."""

    @property
    def id(self) -> str: ...

    @property
    def env(self) -> Environment: ...

    async def write(self, *, text: str | None = None, paste: str | None = None, keys: list[str] | None = None) -> None: ...

    async def screen(self, *, scrollback: int = 0) -> str: ...

    async def modes(self) -> Mapping[str, bool]:
        """The terminal modes of interest, by name (``bracketed_paste``, ``alt_screen``)."""
        ...

    async def wait_for(self, *, regex: str | None = None, idle_ms: int | None = None, timeout: float) -> bool: ...

    def hooks(self) -> AsyncIterator[HookPost]:
        """The launch's hook posts in the daemon's order, ending when the launch ends. One reader:
        the adapter's ``events``."""
        ...

    async def reply(self, reply_id: str, body: Any) -> bool:
        """Answer a held post; false when nothing waits any more (the hold expired, the CLI went)."""
        ...

    async def put_file(self, name: str, data: bytes, *, replace: bool = False) -> str:
        """Add a file to the launch directory (a message too long to type) and return its path. An
        existing name is refused unless ``replace``, which swaps the file whole."""
        ...

    async def dial(self, target: str) -> ByteStreamPort:
        """A byte stream to ``unix:<name>`` in the launch's dial directory or to
        ``tcp:127.0.0.1:<port>`` among the launch's ports: a CLI's own server, reached through the
        daemon, which is the one way in from the host's side in both environments."""
        ...


class EnvironmentPort(Protocol):
    """The environment a CLI lives in, through the daemon's allowlisted side channels.

    ``run`` raises ``ProgramNotFound`` when there is no such program and ``EnvironmentUnavailable``
    when the environment cannot be reached; a program that runs and fails is a result, not an error.
    """

    @property
    def name(self) -> Environment: ...

    @property
    def home(self) -> str:
        """The home directory programs in this environment run with, where each CLI keeps its user
        configuration; empty when the environment did not say."""
        ...

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float = 30.0) -> ExecResult: ...

    async def read(self, path: str, *, offset: int = 0, limit: int = 1 << 20) -> bytes: ...

    async def stat(self, path: str) -> Mapping[str, Any] | None: ...

    async def list(self, path: str) -> list[str]: ...


@runtime_checkable
class HarnessAdapter(Protocol):
    """One command-line agent.

    Rules every adapter keeps: the CLI is the terminal's own process; hooks and settings go per
    launch, never into the user's CLI configuration; the CLI's own updater is off; a write is never
    an acknowledgement; no Enter while a permission or a dialog is open; and a notification that the
    CLI is idle or wants input is never taken for a question.
    """

    name: str
    capabilities: Capabilities

    async def installed(self, env: EnvironmentPort) -> InstallInfo: ...

    async def latest(self, env: EnvironmentPort) -> str: ...

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        """Run the CLI's own updater; the manager runs ``self_check`` after it."""
        ...

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        """Launch, see a hook arrive, deliver one line, see it acknowledged, exit."""
        ...

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog: ...

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        """Asked of the CLI itself, never by reading its credential files."""
        ...

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan: ...

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan: ...

    def readiness(self, screen: str) -> ReadyStep:
        """The readiness gate's judgement of one screen. The runtime runs the gate: it reads the
        screen until the CLI's ready signal arrives through ``events``, types what this answers for a
        dialog, and gives up with this reason or after its timeout."""
        ...

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        """Called once the gate saw the CLI ready: hand over the first prompt when it goes by
        channel. Nothing is typed before the CLI proved it is ready."""
        ...

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        """Take up a launch that outlived a host restart: re-dial side channels and re-read the
        transcript's tail, so ``events`` continues where the previous host stopped."""
        ...

    def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]: ...

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery: ...

    async def interrupt(self, term: TerminalPort) -> None: ...

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        """Deliver a decision; true once the CLI confirmed it (the tool ran, was declined, or the
        dialog closed), false when it could not be confirmed and the operator must answer in the
        terminal."""
        ...

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]: ...

    async def stop(self, term: TerminalPort) -> None:
        """Ask the CLI to exit; the runtime kills the terminal after the grace period."""
        ...

    def classify_screen(self, text: str) -> ScreenClass: ...

    def composer_holds(self, screen: str, text: str) -> bool:
        """Whether the composer on ``screen`` holds ``text`` (or this CLI's paste marker for it).
        When unsure, false: a message that is not seen in the composer is not submitted."""
        ...


__all__ = [
    "ANSWER_CHOICES",
    "DIAL_DIR",
    "LAUNCH_DIR",
    "AgentEntry",
    "Answer",
    "ByteStreamPort",
    "Catalog",
    "CheckResult",
    "CheckStep",
    "CompanionSpec",
    "Delivery",
    "DeliveryState",
    "Environment",
    "EnvironmentPort",
    "EnvironmentUnavailable",
    "EventKind",
    "ExecResult",
    "HarnessAdapter",
    "HookPost",
    "HookSpec",
    "InstallInfo",
    "Launch",
    "LaunchPlan",
    "LaunchSpec",
    "LoginState",
    "ProgramNotFound",
    "ReadyStep",
    "ScreenClass",
    "SendMode",
    "StaffEvent",
    "StandingScope",
    "TerminalPort",
    "ToolSetSpec",
    "ToolUse",
    "Turn",
    "TurnUsage",
    "UpdateResult",
    "mcp_server",
    "standing_rule",
]
