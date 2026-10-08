"""The staff runtime of a command-line agent: one CLI, run as a staff member inside a terminal.

The team decides who works on what, keeps the rows and runs the launch queue; this runs the CLI. For
each staff session it registers a launch with the terminal daemon (the overlay files, the ports, the
hook token), starts the CLI's terminals as an agent's — so a launch at the machine's cap waits for a
place instead of being refused — takes the CLI through its readiness gate, and turns what the CLI
does into the one status machine's transitions. Every change it sees goes to the team's ingress,
which writes the row and publishes the event; nothing here writes a staff table.

Three sources feed a session, all into one serialised ``_apply``: the adapter's ``events`` (the CLI's
hooks or its structured channel, normalised), the daemon's terminal events (the CLI or its companion
exiting), and a quiet timer that reads the screen when a working session has said nothing for a
while. The team tools' posts (``Report``, ``AskOrchestrator``) arrive on the same hook listener and
are answered here, since they mean the same for every CLI.

A restarted host finds its CLIs still running — the daemon owns them — and ``reconcile`` takes each
open launch up again: the adapter re-attaches, the daemon replays the hook posts made meanwhile, and
the status continues from the row.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from daedalus.config import HarnessConfig
from daedalus.extensions.watch_egress import watch_message_guard
from daedalus.harness import team as protocol
from daedalus.harness.contract import (
    DIAL_DIR,
    LAUNCH_DIR,
    Answer,
    EnvironmentUnavailable,
    EventKind,
    ExecResult,
    HarnessAdapter,
    HookPost,
    Launch,
    LaunchPlan,
    LaunchSpec,
    ProgramNotFound,
    ScreenClass,
    StaffEvent,
    ToolSetSpec,
    Turn,
)
from daedalus.harness.delivery import DeliveryWorker, Pending, normalised, pending_of, same_prompt
from daedalus.harness.env import terminal_environment
from daedalus.harness.state import OpenRequest, StaffState, StateContext, next_state
from daedalus.staff_runtime import (
    AskRef,
    Availability,
    Decision,
    LiveSession,
    OutgoingMessage,
    ReadPage,
    ReadRequest,
    Receipt,
    Started,
    StartRequest,
    TeamIngress,
    UsageSnapshot,
)
from daedalus.stores.harness import HarnessStore
from daedalus.stores.resource_profiles import bind_launch_in, for_staff_in, observe_in
from daedalus.terminals.model import EnvUnavailable, NotFound, Origin, Owner, TerminalError, TerminalEvent, TerminalSpec
from daedalus.terminals.model import LaunchSpec as DaemonLaunch
from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)


class ToolSet(Protocol):
    """A set of Daedalus's own tools a CLI reaches through ``ptyd tools-mcp`` (the browser's): what the
    launch is given, and how one call is answered for a live session.

    ``ask`` is the runtime's way to put a question to the operator for the session and wait for the
    answer: ``(key, tool, summary) -> True / False``, or ``None`` when the hold ran out first. The
    question stays open; a later yes lets that one call through once when it is made again."""

    def spec(self, hold_ms: int) -> ToolSetSpec: ...

    async def call(self, live: LiveSession, tool: str, arguments: dict[str, Any], *, ask: Callable[[str, str, str], Awaitable[bool | None]], launch_id: str, cwd: str) -> tuple[str, bool]: ...

LAUNCH_COLS = 120
LAUNCH_ROWS = 36
"""The size a CLI's terminal starts at. A TUI lays itself out once for the size it sees first, and
the operator attaching later takes the size over anyway; this one fits a CLI's dialogs unwrapped."""
GATE_ANSWERS_MAX = 4
"""Dialogs the readiness gate answers in one launch (folder trust, then perhaps a mode warning).
More than that is a CLI asking something the adapter misreads, and typing on would be guessing."""
GATE_REPEAT_S = 3.0
"""A dialog still on screen, unchanged, this long after its keys were typed is answered again: a TUI
that draws its dialog before it reads the keyboard drops keys typed in that moment (the real Claude
Code's trust question did). Counted against ``GATE_ANSWERS_MAX``, so a CLI that ignores them fails."""
HOLD_MARGIN_MS = 30_000
"""What a launch's longest hold exceeds the ask hold by, so the listener never cuts a question short."""
READ_TRANSCRIPT_TURNS = 400
DIFF_TIMEOUT = 30.0
SCREEN_TAIL_CHARS = 1200
"""How much of a stuck screen the failure keeps: enough to see the dialog, not a page of scrollback."""
EXIT_SCREEN_LINES = 8
EXIT_SCREEN_CHARS = 400
"""How much of the screen a CLI that failed leaves in its session's end: the error it printed, not
the frame around it."""
EXIT_SCREEN_TIMEOUT = 3.0
CALLS_REMEMBERED = 256
"""Team call ids a session keeps for spotting a repeat; a replay is of the latest posts."""
PORT_ATTEMPTS = 3
"""Plans tried for a CLI that serves on a port of its own before its start fails: each chooses a port
at random in the range, so a taken one is usually followed by a free one."""

Lookup = Callable[[str], Awaitable[LiveSession | None]]


class _PortTaken(Exception):
    """The port a plan chose already has a listener; the launch is undone and planned again."""

    def __init__(self, port: int) -> None:
        super().__init__(f"port {port} is taken")
        self.port = port


def _settled_reply(ask: Any) -> dict[str, Any]:
    """What a replayed team question is told when the question it repeats was already settled: the
    answer it got, in the words the first post was given, or why it was withdrawn."""
    resolution = ask.resolution or {}
    if resolution.get("closed"):
        return {"text": f"This question was withdrawn: {resolution['closed']}", "error": True}
    allow = resolution.get("allow")
    # The option, the typed answer and the note beside it, all of them: the note was once left out
    # here, and a replayed question heard the option without the operator's words.
    parts = (", ".join(str(s) for s in resolution.get("selected") or []), str(resolution.get("text") or "").strip(), str(resolution.get("note") or "").strip())
    words = " — ".join(part for part in dict.fromkeys(parts) if part)
    return {"text": words or ("yes" if allow else "no" if allow is False else "")}


def _said(lines: list[str]) -> str:
    """The last lines of a screen that say something, on one line. Printable characters only: the
    words are stored as one line of text, and a store that refused them would leave the session
    unended. A line with no word on it is a frame."""
    kept = ["".join(c for c in line if c.isprintable()).strip() for line in lines]
    said = " / ".join([line for line in kept if any(c.isalnum() for c in line)][-EXIT_SCREEN_LINES:])
    return "…" + said[-EXIT_SCREEN_CHARS:] if len(said) > EXIT_SCREEN_CHARS else said


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _substitute(value: str, directory: str, dial_dir: str = "") -> str:
    return value.replace(LAUNCH_DIR, directory).replace(DIAL_DIR, dial_dir)


def port_range(text: str) -> tuple[int, int]:
    low, _, high = text.partition("-")
    return int(low), int(high or low)


def _seconds_since(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


class RuntimeTerminal:
    """``TerminalPort`` over the terminals service: every write carries the launch and is audited
    there, and waits for the human's quiet time. The hook posts come from the runtime, which keeps
    the team's posts for itself."""

    def __init__(self, terminals: Terminals, terminal_id: str, env: str, *, actor: str, launch_id: str, hooks: asyncio.Queue[HookPost | None]) -> None:
        self.terminals = terminals
        self._id = terminal_id
        self._env = env
        self.actor = actor
        self.launch_id = launch_id
        self._hooks = hooks

    @property
    def id(self) -> str:
        return self._id

    @property
    def env(self) -> Any:
        return self._env

    async def write(self, *, text: str | None = None, paste: str | None = None, keys: list[str] | None = None, note: str = "", wait_keyboard: bool = True) -> None:
        """``wait_keyboard`` false only for the operator's own message: it does not wait for the
        operator to stop typing, while anyone else's words wait for a person to finish."""
        await self.terminals.write(self._id, text=text, paste=paste, keys=keys, origin=Origin(self.actor, self.launch_id, note), wait_keyboard=wait_keyboard)

    async def screen(self, *, scrollback: int = 0) -> str:
        result = await self.terminals.read_screen(self._id, format="text", scrollback=scrollback)
        lines = [str(line) for line in result.get("lines") or []]
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    async def modes(self) -> Mapping[str, bool]:
        # The daemon brackets a paste by itself when the program asked for it; the one mode the
        # host's view carries is the alternate screen.
        view = await self.terminals.get(self._id)
        live = view.get("live") or {}
        return {"alt_screen": bool(live.get("alt_screen"))}

    async def wait_for(self, *, regex: str | None = None, idle_ms: int | None = None, timeout: float) -> bool:
        result = await self.terminals.wait_for(self._id, regex=regex, idle_ms=idle_ms, timeout=timeout)
        return result.get("matched") in ("regex", "idle")

    async def hooks(self) -> AsyncIterator[HookPost]:
        while True:
            post = await self._hooks.get()
            if post is None:
                self._hooks.put_nowait(None)  # the end stays visible to a reader that comes back
                return
            yield post

    async def reply(self, reply_id: str, body: Any) -> bool:
        try:
            await self.terminals.reply_hook(self._env, reply_id, body=body, launch_id=self.launch_id, actor=self.actor)
        except NotFound:
            return False
        return True

    async def put_file(self, name: str, data: bytes) -> str:
        return await self.terminals.put_launch_file(self._env, self.launch_id, name, data, actor=self.actor)

    async def dial(self, target: str) -> Any:
        return await self.terminals.net_dial(self._env, target, self.launch_id, actor=self.actor)


class RuntimeEnvironment:
    """``EnvironmentPort`` over the terminals service's side channels of one environment: the one
    implementation, for the staff runtime and the harness manager alike, so every program run and
    file read is audited and fenced by the daemon's lists the same way."""

    def __init__(self, terminals: Terminals, env: str, *, home: str = "", actor: str = "agent:harness",
                 resources: dict[str, Any] | None = None) -> None:
        self.terminals = terminals
        self._env = env
        self._home = home
        self.actor = actor
        self.resources = resources

    @property
    def name(self) -> Any:
        return self._env

    @property
    def home(self) -> str:
        """As the environment's daemon reported it; asked again while unknown, because a daemon that
        was not connected when the port was made reports it once it is."""
        if not self._home:
            self._home = next((s.home for s in self.terminals.environments() if s.env == self._env), "")
        return self._home

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float = 30.0) -> ExecResult:
        try:
            result = await self.terminals.exec_run(self._env, list(argv), cwd=cwd, env_vars=dict(env) if env else None,
                                                   timeout=timeout, actor=self.actor, resources=self.resources)
        except NotFound as exc:
            # "Not installed" and "the environment is down" are different answers to a version check;
            # the contract names them so a caller never has to know the terminals' error classes.
            raise ProgramNotFound(exc.message) from None
        except EnvUnavailable as exc:
            raise EnvironmentUnavailable(exc.message) from None
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr, timed_out=result.timed_out, path=result.path)

    async def read(self, path: str, *, offset: int = 0, limit: int = 1 << 20) -> bytes:
        out = bytearray()
        while len(out) < limit:
            chunk = await self.terminals.fs_read(self._env, path, offset=offset + len(out), max_bytes=min(limit - len(out), 512 << 10))
            out += chunk.data
            if chunk.eof or not chunk.data:
                break
        return bytes(out)

    async def stat(self, path: str) -> Mapping[str, Any] | None:
        try:
            result = await self.terminals.fs_stat(self._env, path)
        except NotFound:
            return None
        return result if result.get("exists") else None

    async def list(self, path: str) -> list[str]:
        result = await self.terminals.fs_list(self._env, path)
        return [str(entry["name"]) for entry in result.get("entries") or []]


@dataclass(eq=False)
class CliSession:
    """What the runtime holds for one live staff session while its CLI runs."""

    staff_session_id: str
    launch: Launch
    term: RuntimeTerminal
    env_port: RuntimeEnvironment
    cwd: str
    companions: dict[str, str] = field(default_factory=dict)
    """Companion terminal id → its role."""
    plan: LaunchPlan | None = None
    """The plan it was launched with; ``None`` for a session taken up after a restart."""
    first_message_id: str = ""
    first_prompt_pending: bool = False
    first_acknowledged: bool = False
    transcript_ref: str = ""
    open: dict[str, OpenRequest] = field(default_factory=dict)
    """Requests the CLI has open, by the reference the adapter gave them."""
    hooks: asyncio.Queue[HookPost | None] = field(default_factory=asyncio.Queue)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once the runtime has let the session go, whichever way it ended."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    last_signal: float = 0.0
    quiet_checked: float = 0.0
    idle_checked: float = 0.0
    stopping: bool = False
    finished: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    """Set by every event applied, so a message waiting for its window looks again at once."""
    worker: DeliveryWorker | None = None
    reported: bool = False
    """A Report arrived in the current turn; a turn that ends without one is reported for it."""
    turn_ended_waiting: bool = False
    """The turn ended while a request was still open, and nothing has started since: when that
    request is answered the CLI is idle, and the answer waits for it as a message. Without this the
    session went back to ``working`` and the message waited for a turn that had already ended."""
    resend_first: bool = False
    """After a restart during the start: the first message still has to go by channel once ready."""
    start_failure: str = ""
    """What the adapter reported as failed before the CLI was ready. The gate stops on it rather than
    replacing it with its own "not ready" when its timeout comes."""
    channel: dict[str, str] = field(default_factory=dict)
    """What the host last heard on each channel of the launch: ``hook``, ``team`` (a call),
    ``hello`` (the team tools loaded), and the team tools' state (``connected`` or ``missing``)."""
    calls: dict[str, tuple[str, Any]] = field(default_factory=dict)
    """Team calls by their call id: a post seen twice is answered as the first was, never acted on twice."""
    asked: dict[str, str] = field(default_factory=dict)
    """Open team questions by their text, to the request's reference: asked again, it is the same request."""
    held_for: dict[str, list[str]] = field(default_factory=dict)
    """A team request's reference to the later held posts that asked it again, newest last."""
    pending_after_restart: list[Pending] = field(default_factory=list)
    tool_asks: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    """A tool call's question to the operator, by its reference, while the call still waits."""
    tool_grants: dict[str, bool] = field(default_factory=dict)
    """Answers to tool questions that came after their call gave up waiting, by the question's key:
    the same call made again takes its answer once, as a policy grant is taken by a session."""


class CliStaffRuntime:
    """:class:`daedalus.staff_runtime.StaffRuntime` for one command-line agent, over its adapter.

    ``lookup`` is the team's view of a live session (``None`` once it has ended), read afresh for
    every change, because the team moves a session too — an answer sets it working, a reader sets a
    finished turn seen — and the machine must start from where the row is, not where this left it.
    """

    def __init__(
        self,
        adapter: HarnessAdapter,
        *,
        terminals: Terminals,
        store: HarnessStore,
        ingress: TeamIngress,
        lookup: Lookup,
        config: Callable[[], HarnessConfig],
        clock: Callable[[], float] = time.monotonic,
        blocker: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        self.adapter = adapter
        self.kind = adapter.name
        self.terminals = terminals
        self.store = store
        self.ingress = ingress
        self.lookup = lookup
        self.config = config
        self.clock = clock
        self.blocker = blocker
        """The harness manager's word on ``(env, harness)``: why no member may start on it now (it is
        being updated, its last self-check failed, its major is not supported), or empty."""
        self.sessions: dict[str, CliSession] = {}
        self.tool_sets: dict[str, ToolSet] = {}
        """Daedalus's tools beyond the team's that every launch offers (``browser``), set by the
        subsystem that has them; each becomes an MCP entry and its calls arrive as ``tools`` posts."""
        self._by_terminal: dict[str, CliSession] = {}
        self._unsubscribe = terminals.subscribe(self._on_terminal_event)
        self._background: set[asyncio.Task[Any]] = set()
        self.taken_up = False
        """Whether ``reconcile`` has run: until then a live session this runtime does not hold may
        still be one the previous host left, about to be attached, rather than one that is gone."""

    def close(self) -> None:
        """Let go of the sessions without touching them: the CLIs keep running in the daemon for the
        next host to take up. What a host does when it shuts down."""
        self._unsubscribe()
        for session in list(self.sessions.values()):
            for task in session.tasks:
                task.cancel()
        for task in list(self._background):
            task.cancel()
        self.sessions.clear()
        self._by_terminal.clear()

    def _spawn(self, awaitable: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(awaitable)
        task.set_name(name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    @staticmethod
    def _actor(staff_session_id: str) -> str:
        return f"staff:{staff_session_id}"

    # -- availability ----------------------------------------------------------------------------

    async def available(self, env: str) -> Availability:
        label = self.adapter.capabilities.label
        status = next((e for e in self.terminals.environments() if e.env == env), None)
        if status is None:
            return Availability(False, f"there is no {env} environment")
        if not status.available:
            return Availability(False, f"the {env} terminal service is not available: {status.detail or status.reason}")
        row = await self.store.catalog_row(env, self.kind)
        if row is not None and row.checked_at and not row.installed:
            return Availability(False, f"{label} is not installed in the {env} environment")
        if row is not None and row.logged_in == "no":
            return Availability(False, f"{label} is not signed in in the {env} environment")
        if self.blocker is not None:
            reason = await self.blocker(env, self.kind)
            if reason:
                return Availability(False, reason)
        return Availability(True)

    # -- starting --------------------------------------------------------------------------------

    async def start(self, req: StartRequest) -> Started:
        return await self._launch(req, resume_ref="")

    async def resume(self, req: StartRequest, prior: LiveSession) -> Started:
        """The same CLI session in a new launch, when the CLI can take one up again; a fresh one told
        what ended otherwise."""
        return await self._launch(req, resume_ref=prior.cli_session_id or "")

    def _spec(self, req: StartRequest, launch_id: str, resume_ref: str) -> LaunchSpec:
        cfg = self.config()
        facts = protocol.TeamFacts(
            staff_name=req.staff.name,
            project_name=req.project.name,
            role=req.staff.role,
            task_id=req.task.id if req.task is not None else "",
            task_title=req.task.title if req.task is not None else "",
            branch=req.worktree.branch if req.worktree is not None else "",
        )
        block = protocol.mandatory_block(facts) + (f"\n\n{req.brief_text.strip()}" if req.brief_text.strip() else "")
        # The protocol's first line leads the first prompt too: a CLI that reads its system channel
        # late, or a model that skims it, meets it again where the task begins.
        first = f"{protocol.first_line(facts)}\n\n{req.first_message}" if req.first_message else None
        return LaunchSpec(
            harness=self.kind,
            env=req.env,  # type: ignore[arg-type]
            cwd=str(req.cwd),
            launch_id=launch_id,
            first_prompt=first,
            brief_text=req.brief_text,
            model=req.model,
            effort=req.effort,
            agent=req.agent,
            permission_mode=req.permission_mode,
            permission_level=req.permission_level,
            session_ref=resume_ref,
            title=f"{req.staff.name} · {req.task.title}" if req.task is not None else req.staff.name,
            team_block=block,
            team_skill=protocol.skill_markdown(facts),
            ask_hold_ms=cfg.ask_hold_s * 1000,
            report_hold_ms=cfg.report_hold_s * 1000,
            permission_hold_ms=cfg.permission_hold_s * 1000,
            port_range=port_range(cfg.opencode_port_range),
            tool_sets=tuple(tools.spec(self._tools_hold_ms()) for tools in self.tool_sets.values()),
            allow_rules=req.allow_rules,
        )

    def _tools_hold_ms(self) -> int:
        """How long a tool call waits for the host: past a question to the operator, which it may be."""
        return self.config().permission_hold_s * 1000 + HOLD_MARGIN_MS

    async def _launch(self, req: StartRequest, *, resume_ref: str) -> Started:
        """One launch, planned again while the port the plan chose is taken (``PORT_ATTEMPTS``)."""
        last = _PortTaken(0)
        for attempt in range(1, PORT_ATTEMPTS + 1):
            try:
                return await self._launch_once(req, resume_ref=resume_ref)
            except _PortTaken as taken:
                logger.info("%s: port %d is taken (attempt %d of %d); planning another", self.kind, taken.port, attempt, PORT_ATTEMPTS)
                last = taken
        raise RuntimeError(f"{self.adapter.capabilities.label} found its port taken {PORT_ATTEMPTS} times (last {last.port}); free ports in harness.opencode_port_range or widen it")

    async def _taken_port(self, env: str, launch_id: str, ports: tuple[int, ...], actor: str) -> int | None:
        """A port the plan means the CLI to listen on that something already answers on. The CLI
        would fail to start on it after its whole launch; asking first costs one dial."""
        for port in ports:
            try:
                stream = await self.terminals.net_dial(env, f"tcp:127.0.0.1:{port}", launch_id, actor=actor)
            except Exception:  # noqa: BLE001 — refused, which is what a free port says
                continue
            with contextlib.suppress(Exception):
                await stream.close()
            return port
        return None

    async def _launch_once(self, req: StartRequest, *, resume_ref: str) -> Started:
        actor = self._actor(req.staff_session_id)
        launch_id = "l" + secrets.token_hex(8)
        spec = self._spec(req, launch_id, resume_ref)
        plan = self.adapter.resume_plan(spec, resume_ref) if resume_ref else self.adapter.launch_plan(spec)
        from daedalus.harness.observation import observe  # Lazy: observe imports the launch contract used by adapters.
        effective = observe(self.adapter, spec, plan)
        row = await self.store.catalog_row(req.env, self.kind)
        logger.info(
            "%s launch %s version=%s requested=(%s, %s, %s, %s) effective=(%s, %s, %s, %s)",
            self.kind, launch_id, row.installed_version if row is not None else "unknown",
            effective.requested_model or "default", effective.requested_tools,
            effective.requested_permission, bool(effective.requested_resume),
            effective.model or "default", effective.tools, effective.permission, bool(effective.resume),
        )
        record = Launch(
            launch_id=launch_id,
            staff_session_id=req.staff_session_id,
            harness=self.kind,
            env=req.env,  # type: ignore[arg-type]
            terminal_id=None,
            companion_terminal_id=None,
            launch_dir="",
            session_ref=plan.session_ref,
            harness_version=row.installed_version if row is not None else "",
            started_at=_now(),
            adapter_state=plan.adapter_state,
        )
        # Written before the daemon hears of it: a launch the database does not know is one a
        # restarted host could never take up or end.
        await self.store.open_launch(record)
        if req.resources is not None:
            async with self.store.db.transaction() as conn:
                resources = await bind_launch_in(conn, attempt_id=req.resources["attempt_id"],
                                                 host_generation=req.resources["host_generation"],
                                                 launch_id=launch_id)
            req = replace(req, resources=resources)
        cfg = self.config()
        hold = max([cfg.ask_hold_s * 1000 + HOLD_MARGIN_MS, cfg.permission_hold_s * 1000 + HOLD_MARGIN_MS, *plan.hooks.hold_ms.values(), *(t.hold_ms + HOLD_MARGIN_MS for t in spec.tool_sets)])
        registered = False
        created: list[str] = []
        try:
            daemon_launch = await self.terminals.register_launch(req.env, DaemonLaunch(launch_id=launch_id, files=dict(plan.files), ports=list(plan.ports), hold_max_ms=hold), actor=f"agent:{actor}")
            registered = True
            if plan.ports and (taken := await self._taken_port(req.env, launch_id, plan.ports, f"agent:{actor}")) is not None:
                raise _PortTaken(taken)
            directory, dials = daemon_launch.dir, daemon_launch.dial_dir
            cwd = _substitute(plan.cwd, directory, dials) or str(req.cwd)
            title = f"{req.staff.name} · {req.task.title}" if req.task is not None else req.staff.name
            companions: dict[str, str] = {}
            for companion in plan.companions:
                view = await self._create(req, actor, launch_id, [_substitute(a, directory, dials) for a in companion.argv], {k: _substitute(v, directory, dials) for k, v in companion.env.items()}, cwd, f"{title} · {companion.role}")
                created.append(view["id"])
                companions[view["id"]] = companion.role
                if companion.ready_pattern and not (await self.terminals.wait_for(view["id"], regex=companion.ready_pattern, timeout=cfg.ready_timeout_s)).get("matched") == "regex":
                    raise RuntimeError(f"the {companion.role} of {self.adapter.capabilities.label} did not start within {cfg.ready_timeout_s:g} s")
            view = await self._create(req, actor, launch_id, [_substitute(a, directory, dials) for a in plan.argv], {k: _substitute(v, directory, dials) for k, v in plan.env.items()}, cwd, title)
            created.append(view["id"])
            launch = await self.store.update_launch(launch_id, terminal_id=view["id"], companion_terminal_id=next(iter(companions), None), launch_dir=directory)
        except BaseException:
            with contextlib.suppress(Exception):
                for terminal_id in created:
                    await self.terminals.kill(terminal_id, actor=f"agent:{actor}")
                if registered:
                    await self.terminals.unregister_launch(req.env, launch_id, actor=f"agent:{actor}")
                await self.store.end_launch(launch_id)
            raise
        hooks: asyncio.Queue[HookPost | None] = asyncio.Queue()
        session = CliSession(
            staff_session_id=req.staff_session_id,
            launch=launch,
            term=RuntimeTerminal(self.terminals, view["id"], req.env, actor=actor, launch_id=launch_id, hooks=hooks),
            env_port=RuntimeEnvironment(self.terminals, req.env, actor=f"agent:{actor}",
                                        resources=req.resources),
            cwd=cwd,
            companions=companions,
            plan=plan,
            first_message_id=req.first_message_id,
            first_prompt_pending=bool(plan.first_prompt),
            transcript_ref=plan.transcript_hint or "",
            hooks=hooks,
            last_signal=self.clock(),
        )
        self._run(session)
        return Started(terminal_id=view["id"], cli_session_id=plan.session_ref or None, transcript_ref=plan.transcript_hint)

    async def _create(self, req: StartRequest, actor: str, launch_id: str, argv: list[str], env: dict[str, str], cwd: str, title: str) -> dict[str, Any]:
        environment = terminal_environment(env, capabilities=self.adapter.capabilities)
        spec = TerminalSpec(
            env=req.env,
            owner=Owner("staff", req.staff.id),
            project_id=req.project.id,
            cwd=cwd,
            argv=argv,
            env_vars=environment.set,
            strip_env=list(environment.strip),
            title=title,
            cols=LAUNCH_COLS,
            rows=LAUNCH_ROWS,
            profile=f"harness:{self.kind}",
            launch_id=launch_id,
            # An agent's launch: at the machine's cap it waits in the service's line for a place.
            created_by=f"agent:{actor}",
            resources=req.resources,
            sandbox=req.resources is not None,
        )
        view: dict[str, Any] = dict(await self.terminals.create(spec))
        if req.resources is not None:
            evidence = dict(view.get("containment") or {})
            evidence["scope"] = req.resources["scope"]
            evidence["daemon_instance"] = view.get("daemon_instance")
            async with self.store.db.transaction() as conn:
                await observe_in(conn, attempt_id=req.resources["scope"]["attempt_id"],
                                 launch_id=launch_id, kind="spawn", evidence=evidence,
                                 terminal_id=view["id"])
        return view

    def _run(self, session: CliSession, *, gate: bool = True) -> None:
        self.sessions[session.staff_session_id] = session
        self._by_terminal[session.term.id] = session
        for terminal_id in session.companions:
            self._by_terminal[terminal_id] = session
        name = f"harness-{self.kind}-{session.staff_session_id}"
        session.worker = DeliveryWorker(self.adapter, session, self.lookup, self.ingress, self.store,
                                        self.config, self._in_transcript,
                                        lambda message_id: watch_message_guard(
                                            self.store.db, message_id, session.staff_session_id))
        session.tasks = [
            asyncio.create_task(self._pump_hooks(session), name=f"{name}-hooks"),
            asyncio.create_task(self._consume(session), name=f"{name}-events"),
            asyncio.create_task(self._watch_quiet(session), name=f"{name}-quiet"),
            session.worker.start(),
        ]
        if self.adapter.capabilities.team_tools != "none":
            session.tasks.append(asyncio.create_task(self._watch_team_tools(session), name=f"{name}-team"))
        if gate:
            session.tasks.append(asyncio.create_task(self._gate(session), name=f"{name}-gate"))

    # -- the readiness gate ----------------------------------------------------------------------

    async def _gate(self, session: CliSession) -> None:
        """Read the screen until the CLI says it is ready, answering the dialogs the adapter
        recognises, and hand over to ``after_spawn``. Every key typed here is one the adapter chose
        for a screen it recognised; a screen it does not recognise is waited on, never guessed at."""
        cfg = self.config()
        deadline = self.clock() + cfg.ready_timeout_s
        answered = 0
        last_answered = ""
        answered_at = 0.0
        screen = ""
        try:
            await self._companions_gone(session)
            while not session.ready.is_set():
                if session.exited.is_set() or session.finished or session.start_failure:
                    return
                screen = await session.term.screen()
                step = self.adapter.readiness(screen)
                if step.action == "fail":
                    if "sign in" in step.reason.lower() or "signed in" in step.reason.lower():
                        phase_event = getattr(self.ingress, "phase_event", None)
                        if phase_event is not None and (live := await self.lookup(session.staff_session_id)) is not None:
                            await phase_event(live, "auth_required")
                    await self._failed(session, step.reason or "the command-line agent will not start", screen)
                    return
                if step.action == "keys" and step.keys and (screen != last_answered or self.clock() - answered_at >= GATE_REPEAT_S):
                    if answered >= GATE_ANSWERS_MAX:
                        await self._failed(session, "it kept asking questions before it was ready", screen)
                        return
                    await session.term.write(keys=list(step.keys), note=f"readiness: {step.reason or 'dialog'}")
                    answered += 1
                    last_answered = screen
                    answered_at = self.clock()
                if self.clock() >= deadline:
                    await self._failed(session, f"not ready after {cfg.ready_timeout_s:g} s" + await self._companions_said(session), screen)
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(session.ready.wait(), cfg.ready_poll_ms / 1000)
            if session.plan is not None:
                await self.adapter.after_spawn(session.term, session.launch, session.plan)
            elif session.resend_first:
                await self._resend_first(session)
        except asyncio.CancelledError:
            raise
        except TerminalError as exc:
            if not session.exited.is_set():
                await self._failed(session, f"the terminal could not be read: {exc.message}", screen)
        except Exception as exc:  # noqa: BLE001 — a broken gate is this session's failure, shown on it
            logger.exception("the readiness gate of %s failed", session.staff_session_id)
            await self._failed(session, f"the readiness gate failed: {exc}", screen)

    async def _companions_said(self, session: CliSession, only: str = "") -> str:
        """What the companions' screens last showed, as a clause for a failure. A Codex member that
        never got ready showed an empty screen, its TUI still waiting for the thread, while the reason
        was on the app server's screen, which nobody was shown."""
        said = ""
        for terminal_id, role in session.companions.items():
            if only and terminal_id != only:
                continue
            try:
                async with asyncio.timeout(EXIT_SCREEN_TIMEOUT):
                    result = await self.terminals.read_screen(terminal_id, format="text", scrollback=EXIT_SCREEN_LINES)
            except Exception:  # noqa: BLE001 — the failure stands without the companion's words
                logger.info("the %s screen of %s could not be read", role, session.staff_session_id, exc_info=True)
                continue
            words = _said([str(line) for line in result.get("lines") or []])
            said += f"; the {role}'s screen last showed: {words}" if words else f"; the {role}'s screen is empty"
        return said

    async def _failed(self, session: CliSession, reason: str, screen: str) -> None:
        tail = screen[-SCREEN_TAIL_CHARS:].strip()
        await self._apply(session, StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": reason, "screen": tail}, launch_id=session.launch.launch_id))

    # -- what the CLI does -----------------------------------------------------------------------

    async def _pump_hooks(self, session: CliSession) -> None:
        """The launch's hook posts in order: the team's are answered here, the rest go to the adapter."""
        try:
            async for hook in self.terminals.hook_events(session.launch.launch_id):
                post = HookPost(name=hook.name, body=hook.body, at=hook.at, reply_id=hook.reply_id, hold_ms=hook.hold_ms)
                session.channel["team" if hook.name == "team" else "tools" if hook.name == "tools" else "hook"] = hook.at or _now()
                if hook.name == "tools":
                    # A tool call may wait minutes for the operator: never in the way of the next hook.
                    self._spawn(self._tools(session, post), f"tools-{session.staff_session_id}")
                elif hook.name == "team":
                    try:
                        await self._team(session, post)
                    except Exception:  # noqa: BLE001 — one bad team post must not stop the CLI's hooks
                        logger.exception("a team post of %s failed", session.staff_session_id)
                else:
                    session.hooks.put_nowait(post)
        finally:
            session.hooks.put_nowait(None)

    async def _consume(self, session: CliSession) -> None:
        try:
            async for event in self.adapter.events(session.term, session.launch):
                await self._apply(session, event)
                if session.finished:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the adapter's failure is the session's error, not the host's
            logger.exception("the events of %s stopped", session.staff_session_id)
            if not session.finished:
                await self._apply(session, StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": f"its events stopped: {exc}"}, launch_id=session.launch.launch_id))

    async def _on_terminal_event(self, event: TerminalEvent) -> None:
        if event.type != "terminal.exited" or not event.terminal_id:
            return
        session = self._by_terminal.get(event.terminal_id)
        if session is None or session.finished:
            return
        code = event.data.get("exit_code")
        if event.terminal_id == session.term.id:
            session.exited.set()
            if not session.stopping:
                await self._apply(session, StaffEvent(EventKind.PROCESS_EXITED, event.at, {"exit_code": code}, launch_id=session.launch.launch_id))
            return
        await self._companion_exited(session, event.terminal_id, code, event.at)

    async def _companion_exited(self, session: CliSession, terminal_id: str, code: Any, at: str) -> None:
        role = session.companions.get(terminal_id, "companion")
        if not session.stopping:
            # Without its companion the CLI has no channel to report through: an error the operator
            # sees, not a silence that looks like thinking. What it printed as it went is the why.
            ended = f" with code {code}" if isinstance(code, int) and code != 0 else ""
            said = await self._companions_said(session, only=terminal_id)
            await self._apply(session, StaffEvent(EventKind.TURN_FAILED, at, {"failure": f"side channel lost: the {role} exited{ended}{said}"}, launch_id=session.launch.launch_id))

    async def _companions_gone(self, session: CliSession) -> None:
        """A companion that died before the session was registered: its exit event found no session
        to fail, and the launch then waited its whole readiness timeout for a server that was gone."""
        for terminal_id in session.companions:
            with contextlib.suppress(TerminalError):
                view = await self.terminals.get(terminal_id)
                if view.get("status") == "exited" and not session.start_failure:
                    await self._companion_exited(session, terminal_id, view.get("exit_code"), str(view.get("exited_at") or _now()))

    async def _apply(self, session: CliSession, event: StaffEvent) -> None:
        """One event through the status machine, and what it changed to the ingress. Serialised per
        session, so the machine always starts from the state the previous event left."""
        async with session.lock:
            if session.finished:
                return
            live = await self.lookup(session.staff_session_id)
            if live is None:
                await self._finish(session, "", end=False)
                return
            await self._apply_locked(session, live, event)

    async def _apply_locked(self, session: CliSession, live: LiveSession, event: StaffEvent) -> None:
        kind = event.kind
        session.changed.set()
        if kind in (EventKind.READY, EventKind.PROMPT_ACKNOWLEDGED, EventKind.TURN_STARTED, EventKind.TOOL_STARTED, EventKind.PERMISSION_REQUESTED, EventKind.QUESTION_ASKED, EventKind.TURN_COMPLETED):
            session.ready.set()
        if kind is EventKind.TURN_FAILED and not session.ready.is_set() and not session.start_failure:
            session.start_failure = str(event.payload.get("failure") or "failed")
        if kind in (EventKind.TURN_CANCELLED, EventKind.TURN_FAILED):
            # A turn that ended any other way leaves nothing for the next one to count. The flag is
            # cleared at a turn's end, never at its start: a team call is answered as it arrives,
            # while the CLI's own events queue behind the adapter, so a Report made early in a turn
            # (pi's, measured) is often seen before the prompt that started the turn.
            session.reported = False
        if kind is EventKind.TRANSCRIPT:
            await self._located(session, live, event)
        if kind is EventKind.PROMPT_ACKNOWLEDGED:
            await self._acknowledged(session, event)
        if kind in (EventKind.PERMISSION_REQUESTED, EventKind.QUESTION_ASKED) and event.native_id:
            summary = str(event.payload.get("summary") or event.payload.get("text") or "")
            session.open[event.native_id] = OpenRequest("permission" if kind is EventKind.PERMISSION_REQUESTED else "question", summary[:200])
        withdrawn: list[str] = []
        if kind is EventKind.REQUEST_RESOLVED:
            # From the adapter it names no ``via``: the CLI saw the request settled where the team did
            # not, on its own screen. The team's own answers come back through ``answer`` with theirs.
            if session.open.pop(event.native_id, None) is not None and (event.payload.get("via") or "terminal") == "terminal":
                await self.ingress.resolved(live, event.native_id, by="operator", via="terminal")
            self._forget_question(session, event.native_id)
        elif kind in (EventKind.TURN_CANCELLED, EventKind.SESSION_ENDED, EventKind.PROCESS_EXITED):
            withdrawn = list(session.open)
            session.open.clear()
            for ref in withdrawn:
                self._forget_question(session, ref)
        try:
            current = StaffState(live.session.status)
        except ValueError:
            current = StaffState.WORKING
        if kind is EventKind.TURN_COMPLETED and session.open:
            session.turn_ended_waiting = True
        elif kind in (EventKind.PROMPT_ACKNOWLEDGED, EventKind.TURN_STARTED, EventKind.TOOL_STARTED):
            session.turn_ended_waiting = False
        context = StateContext(first_prompt_pending=session.first_prompt_pending, open_requests=tuple(session.open.values()), waiting_for=live.session.waiting_for, turn_ended=session.turn_ended_waiting)
        step = next_state(current, event, context)
        phase_event = getattr(self.ingress, "phase_event", None)
        if phase_event is not None and step.signal:
            await phase_event(live, kind.value,
                              meaningful_output=kind is EventKind.TURN_COMPLETED and bool(event.payload.get("last_message")))
        if kind in (EventKind.PROMPT_ACKNOWLEDGED, EventKind.TURN_STARTED, EventKind.TOOL_STARTED):
            session.first_prompt_pending = False
        if step.signal:
            session.last_signal = self.clock()
        for ref in withdrawn:
            if kind is EventKind.TURN_CANCELLED:
                await self.ingress.resolved(live, ref, by="system", via="withdrawn")
        if kind is EventKind.PERMISSION_REQUESTED and event.native_id:
            await self.ingress.permission(live, event.native_id, str(event.payload.get("tool") or ""), str(event.payload.get("summary") or ""))
        elif kind is EventKind.QUESTION_ASKED and event.native_id:
            options = [str(o) for o in event.payload.get("options") or []]
            await self.ingress.question(live, event.native_id, str(event.payload.get("text") or event.payload.get("summary") or ""), options, call_id=str(event.payload.get("call_id") or "") or None)
            if step.state is StaffState.PERMISSION:
                # The question is recorded, but the permission still holds the process.
                await self.ingress.status(live, StaffState.PERMISSION.value, step.waiting_for)
        elif step.state is StaffState.EXITED:
            await self._finish(session, await self._exit_reason(session, event), end=True, live=live)
            return
        elif step.changed:
            detail = "read from the screen" if step.inferred else ""
            if step.state is StaffState.ERROR:
                # The store keeps words only for the waiting statuses, so the failure travels as the
                # detail, with the screen it was stuck on when there is one.
                screen = str(event.payload.get("screen") or "")
                detail = step.waiting_for + (f"\n\n{screen}" if screen else "")
            await self.ingress.status(live, step.state.value, step.waiting_for, detail=detail)
        elif step.signal:
            await self.ingress.signal(live)
        if kind is EventKind.TURN_COMPLETED and step.state is StaffState.TURN_DONE_UNSEEN:
            self._spawn(self._record_usage(session), f"harness-usage-{session.staff_session_id}")
            if not session.reported:
                await self._implicit_report(live, str(event.payload.get("last_message") or ""))
            session.reported = False
        if step.reconcile:
            self._spawn(self._reconcile_screen(session), f"harness-reconcile-{session.staff_session_id}")

    @staticmethod
    def _forget_question(session: CliSession, ref: str) -> None:
        for text, known in list(session.asked.items()):
            if known == ref:
                del session.asked[text]
        session.held_for.pop(ref, None)

    def _ending(self, event: StaffEvent) -> str:
        label = self.adapter.capabilities.label
        if event.kind is EventKind.SESSION_ENDED:
            return f"{label} ended its session"
        code = event.payload.get("exit_code")
        return f"{label} exited" + (f" with code {code}" if isinstance(code, int) and code != 0 else "")

    async def _exit_reason(self, session: CliSession, event: StaffEvent) -> str:
        """Why the CLI went, with what its screen last said when it failed.

        "exited with code 1" was all a member that died a third of a second after its start left
        behind: it wrote no turn record, its terminal was gone by the time anyone looked, and the
        orchestrator hired a second helper to find out what had happened. What the CLI printed as it
        failed — a refused model, a missing login — is read here, while the terminal still holds it,
        and kept as the session's end reason, which the event and ``ReadStaff`` both show.
        """
        reason = self._ending(event)
        code = event.payload.get("exit_code")
        if event.kind is EventKind.SESSION_ENDED or not isinstance(code, int) or code == 0:
            return reason
        try:
            async with asyncio.timeout(EXIT_SCREEN_TIMEOUT):
                screen = await session.term.screen(scrollback=EXIT_SCREEN_LINES)
        except Exception:  # noqa: BLE001 — the reason stands without the screen
            logger.info("the screen of %s could not be read after it exited", session.staff_session_id, exc_info=True)
            return reason
        said = _said(screen.splitlines())
        return f"{reason}; its screen last showed: {said}" if said else reason

    async def _located(self, session: CliSession, live: LiveSession, event: StaffEvent) -> None:
        ref = str(event.payload.get("ref") or "")
        cli_session = str(event.payload.get("session_ref") or "")
        if ref and ref != session.transcript_ref:
            session.transcript_ref = ref
        if cli_session and cli_session != session.launch.session_ref:
            session.launch = await self.store.update_launch(session.launch.launch_id, session_ref=cli_session)
        if (ref and ref != live.session.transcript_ref) or (cli_session and cli_session != live.session.cli_session_id):
            await self.ingress.located(live, cli_session_id=cli_session or None, transcript_ref=ref or None)

    async def _acknowledged(self, session: CliSession, event: StaffEvent) -> None:
        message_id = str(event.payload.get("message_id") or "")
        if session.worker is not None and session.worker.acknowledge(str(event.payload.get("prompt") or ""), message_id):
            return  # the worker reports the message it was delivering
        prompt = str(event.payload.get("prompt") or "")
        first = session.plan.first_prompt if session.plan is not None else None
        if not message_id and session.first_message_id and not session.first_acknowledged and (session.first_prompt_pending or (first and same_prompt(first, prompt))):
            # The first prompt went on the command line, or by a channel ahead of any message: the CLI
            # taking a prompt before anything else was sent, or taking that very text, is that one. A
            # channel's turn begins before its prompt is echoed, so "pending" alone is not enough.
            message_id = session.first_message_id
        if message_id and message_id == session.first_message_id:
            session.first_acknowledged = True
        if message_id:
            await self.ingress.message_state(message_id, "acknowledged")

    async def _record_usage(self, session: CliSession) -> None:
        live = await self.lookup(session.staff_session_id)
        if live is None:
            return
        try:
            snapshot = await self.usage(live)
        except Exception:  # noqa: BLE001 — spend is reported when it can be read, and a turn is not failed for it
            logger.info("the usage of %s could not be read", session.staff_session_id, exc_info=True)
            return
        if snapshot is not None:
            await self.ingress.usage(live, snapshot)

    async def _implicit_report(self, live: LiveSession, last_message: str) -> None:
        """A turn that ended without a Report still reaches the orchestrator: the end of its last
        message, marked as no report, and as needing input when it ends by asking something. Nothing
        is answered for the member; the orchestrator decides."""
        kind = "needs_input" if protocol.looks_like_question(last_message) else "turn_done"
        words = protocol.excerpt(last_message)
        text = f"{words} ({protocol.NO_REPORT})" if words else f"The turn ended without a message ({protocol.NO_REPORT})."
        try:
            await self.ingress.implicit_report(live, kind, text)
        except Exception:  # noqa: BLE001 — the status already says the turn ended; this is its gloss
            logger.warning("the implicit report of %s was not made", live.id, exc_info=True)

    async def _in_transcript(self, session: CliSession, text: str) -> bool:
        """Whether the CLI's own transcript has the message as a prompt it took."""
        if not session.transcript_ref:
            return False
        wanted = normalised(text)[:200]
        try:
            turns = await self.adapter.transcript(session.env_port, session.transcript_ref)
        except Exception:  # noqa: BLE001 — unreadable is "not found", which never resends anything
            logger.info("the transcript of %s could not be read", session.staff_session_id, exc_info=True)
            return False
        return any(t.role in ("user", "orchestrator") and wanted and normalised(t.text).startswith(wanted) for t in turns[-READ_TRANSCRIPT_TURNS:])

    async def _watch_team_tools(self, session: CliSession) -> None:
        """The team tools must say they loaded within a while of the CLI being ready. If they do not,
        the member's card says so and the orchestrator hears it once; the member works on, with its
        status still coming from hooks and the screen, and messages still reaching it."""
        if session.plan is None:
            # Taken up after a restart: the tools said hello to the host before this one, when the
            # CLI started, and do not say it again. Watching for it here marked every such member
            # "team tools not connected" thirty seconds after each deploy, while it went on calling
            # them. What is known of it is on record (``last_team_call``) or comes with its next call.
            return
        await session.ready.wait()
        wait = self.config().team_hello_s
        deadline = self.clock() + wait
        while self.clock() < deadline:
            if session.channel.get("hello") or session.channel.get("team_tools") == "connected":
                return
            await asyncio.sleep(min(1.0, wait / 10))
        if session.channel.get("hello") or session.channel.get("team_tools") == "connected" or session.finished:
            return
        session.channel["team_tools"] = "missing"
        live = await self.lookup(session.staff_session_id)
        if live is not None:
            await self.ingress.channel(live, "missing", f"no team tools {wait:g} s after {self.adapter.capabilities.label} was ready: its reports and questions cannot arrive")

    async def _tools_connected(self, session: CliSession, why: str) -> None:
        """The team tools are known to work; a member shown without them hears it corrected."""
        was = session.channel.get("team_tools")
        session.channel["team_tools"] = "connected"
        if was == "missing":
            live = await self.lookup(session.staff_session_id)
            if live is not None:
                await self.ingress.channel(live, "connected", why)

    def channel(self, live: LiveSession) -> dict[str, Any]:
        """What the host last heard on each of the session's channels, for the staff view."""
        session = self.sessions.get(live.id)
        if session is None:
            return {}
        return {
            "team_tools": session.channel.get("team_tools") or ("connected" if session.channel.get("hello") or session.channel.get("team") else "waiting"),
            "last_hook_at": session.channel.get("hook") or None,
            "last_team_call_at": session.channel.get("team") or None,
            "team_hello_at": session.channel.get("hello") or None,
            "queued_messages": session.worker.size() if session.worker is not None else 0,
        }

    # -- silence ---------------------------------------------------------------------------------

    async def _watch_quiet(self, session: CliSession) -> None:
        """A working session that has said nothing and printed nothing for ``no_signal_after_s`` has
        its screen read. Only the screen reconcile may then call the turn over; what it cannot tell
        is shown as silence, never as a failure."""
        while not session.finished:
            cfg = self.config()
            await asyncio.sleep(max(0.05, min(5.0, cfg.no_signal_after_s / 10)))
            try:
                await self._check_quiet(session, cfg)
            except asyncio.CancelledError:
                raise
            except TerminalError as exc:
                logger.info("the quiet check of %s could not read the terminal: %s", session.staff_session_id, exc.message)
            except Exception:  # noqa: BLE001 — the watch goes on; the next check may succeed
                logger.exception("the quiet check of %s failed", session.staff_session_id)

    async def _check_quiet(self, session: CliSession, cfg: HarnessConfig) -> None:
        live = await self.lookup(session.staff_session_id)
        if live is None or live.session.status not in (StaffState.WORKING.value, StaffState.NO_SIGNAL.value):
            return
        if await self._settled_idle(session, cfg):
            return
        if live.session.status == StaffState.WORKING.value and not await self.ingress.expects_signal(live):
            # Working without a task still being worked (handed in for review, done, or none at all):
            # its quiet is nobody's alarm. A session already shown silent is still read below, since
            # the screen is how it finds its way back to a finished turn.
            return
        now = self.clock()
        if now - max(session.last_signal, session.quiet_checked) < cfg.no_signal_after_s:
            return
        view = await self.terminals.get(session.term.id)
        printed = _seconds_since(view.get("last_output_at"))
        if printed is not None and printed < cfg.no_signal_after_s:
            session.quiet_checked = now - printed
            return
        session.quiet_checked = now
        if live.session.status == StaffState.WORKING.value:
            await self._apply(session, StaffEvent(EventKind.QUIET, _now(), {"after_s": cfg.no_signal_after_s}, launch_id=session.launch.launch_id))
        else:
            await self._reconcile_screen(session)

    async def _settled_idle(self, session: CliSession, cfg: HarnessConfig) -> bool:
        """Whether a session the host holds as working turned out to sit at an idle prompt, and was
        settled so. Read once per ``idle_settle_s`` without a signal, whatever the terminal prints and
        whether or not a task is expected of it; only an idle verdict acts, the rest is left to the
        silence check."""
        now = self.clock()
        if now - max(session.last_signal, session.idle_checked) < cfg.idle_settle_s:
            return False
        session.idle_checked = now
        if await self._screen_verdict(session) is not ScreenClass.IDLE_COMPOSER:
            return False
        logger.info(
            "%s shows an idle prompt %.0f s after its last signal while its turn was held as running: the turn end was missed, and it is taken from the screen",
            session.staff_session_id,
            now - session.last_signal,
        )
        await self._apply(session, StaffEvent(EventKind.RECONCILED, _now(), {"screen": ScreenClass.IDLE_COMPOSER.value}, launch_id=session.launch.launch_id))
        return True

    async def _reconcile_screen(self, session: CliSession) -> None:
        """Two readings ``reconcile_gap_ms`` apart. A turn end is inferred only from two identical
        screens that both show an idle composer: one reading can catch a TUI between two frames."""
        verdict = await self._screen_verdict(session)
        await self._apply(session, StaffEvent(EventKind.RECONCILED, _now(), {"screen": verdict.value}, launch_id=session.launch.launch_id))

    async def _screen_verdict(self, session: CliSession) -> ScreenClass:
        gap = self.config().reconcile_gap_ms / 1000
        first = await session.term.screen()
        await asyncio.sleep(gap)
        second = await session.term.screen()
        one, two = self.adapter.classify_screen(first), self.adapter.classify_screen(second)
        if one is ScreenClass.IDLE_COMPOSER:
            return ScreenClass.IDLE_COMPOSER if two is ScreenClass.IDLE_COMPOSER and first == second else ScreenClass.UNKNOWN
        return one if one == two else ScreenClass.UNKNOWN

    async def settle_idle(self, statuses: tuple[str, ...] = (StaffState.WORKING.value, StaffState.NO_SIGNAL.value)) -> int:
        """Read the screen of every held session whose row says ``statuses`` and settle the ones
        whose CLI sits idle at its prompt; returns how many were settled.

        The quiet watch only reads a screen after ``no_signal_after_s`` with no output, and only moves
        a session when the verdict changes something. A row left ``no_signal`` by an earlier host, or
        by a check that ran before a fix, therefore stayed grey for hours in front of an idle CLI —
        three members sat "silent 164 min" with their tasks long done. This is the one-shot that
        settles such rows, at start and from the team's ticker. Only an idle verdict is acted on: a
        busy or unreadable screen is left to the quiet watch, which knows how long it has been quiet.
        """
        settled = 0
        for session in list(self.sessions.values()):
            if session.finished or session.stopping:
                continue
            live = await self.lookup(session.staff_session_id)
            if live is None or live.session.status not in statuses:
                continue
            try:
                verdict = await self._screen_verdict(session)
            except TerminalError as exc:
                logger.info("the screen of %s could not be read to settle it: %s", session.staff_session_id, exc.message)
                continue
            if verdict is not ScreenClass.IDLE_COMPOSER:
                continue
            await self._apply(session, StaffEvent(EventKind.RECONCILED, _now(), {"screen": verdict.value}, launch_id=session.launch.launch_id))
            after = await self.lookup(session.staff_session_id)
            settled += after is not None and after.session.status != live.session.status
        return settled

    # -- the team tools --------------------------------------------------------------------------

    async def _team(self, session: CliSession, post: HookPost) -> None:
        """``Report`` and ``AskOrchestrator`` from the team bridge, and its word that the tools loaded.

        A report is answered with what the team made of it (a refusal as an error the worker reads);
        a question is held open and answered when someone answers it, with the ask carrying the held
        post's id as its reference. Every call carries a call id: a post seen again — a replay after
        the host restarted — is answered as the first one was and acted on once. A question asked
        again while the first is open (the CLI retried, the model asked twice) is the same request,
        and its answer goes to the newest post that still waits.
        """
        body = post.body if isinstance(post.body, dict) else {}
        tool = str(body.get("tool") or "")
        if tool == "hello":
            session.channel["hello"] = post.at or _now()
            await self._tools_connected(session, "the team tools loaded late")
            return
        # A real call is better proof than the hello: the tools are loaded and the model uses them.
        await self._tools_connected(session, "the team tools answered a call")
        live = await self.lookup(session.staff_session_id)
        if live is None:
            if post.reply_id:
                await session.term.reply(post.reply_id, {"text": "this session is no longer connected to its team", "error": True})
            return
        call_id = str(body.get("call_id") or "")
        seen = session.calls.get(call_id) if call_id else None
        if tool == "report":
            session.reported = True
            try:
                evidence = [e for e in body.get("evidence") or [] if isinstance(e, dict)]
                acknowledged = [str(a) for a in body.get("acknowledged") or []]
                told = await self.ingress.report(
                    live, str(body.get("kind") or ""), str(body.get("note") or ""), [str(a) for a in body.get("artifacts") or []], str(body.get("remember") or "") or None,
                    call_id=call_id or None, evidence=evidence or None, acknowledged=acknowledged or None,
                    operator_steps=body.get("operator_steps") if isinstance(body.get("operator_steps"), dict) else None,
                )
                reply: dict[str, Any] = {"text": told}
            except (PermissionError, ValueError, RuntimeError) as exc:
                reply = {"text": str(exc), "error": True}
            self._remember_call(session, call_id, ("report", reply))
            await self._apply(session, StaffEvent(EventKind.ACTIVITY, post.at, {"team": "report"}, launch_id=session.launch.launch_id))
            if post.reply_id:
                await session.term.reply(post.reply_id, reply)
        elif tool == "ask":
            question = str(body.get("question") or "").strip()
            context = str(body.get("context") or "").strip()
            text = question + (f"\n\nContext: {context}" if context else "")
            earlier = seen[1] if seen is not None else session.asked.get(normalised(question))
            if earlier and earlier in session.open:
                # The same question again: no second request; the answer goes to this post too.
                if post.reply_id:
                    session.held_for.setdefault(earlier, []).append(post.reply_id)
                self._remember_call(session, call_id, ("ask", earlier))
                return
            before = await self.ingress.asked(live, call_id) if call_id else None
            if before is not None:
                # The same call again after its question was settled, or after a host restart forgot
                # the calls it had seen: a replay, never a second question. An open one takes this
                # post as the one to answer; a settled one is answered at once with what it got.
                self._remember_call(session, call_id, ("ask", before.request_ref))
                if before.open:
                    if post.reply_id:
                        session.held_for.setdefault(before.request_ref, []).append(post.reply_id)
                elif post.reply_id:
                    await session.term.reply(post.reply_id, _settled_reply(before))
                return
            # The reference names the held post, so the answer finds its way back even after the
            # host restarted; an ask nobody holds gets a reference of its own and goes as a message.
            ref = f"team:{post.reply_id}" if post.reply_id else f"team-message:{uuid.uuid4().hex[:12]}"
            session.asked[normalised(question)] = ref
            self._remember_call(session, call_id, ("ask", ref))
            await self._apply(session, StaffEvent(EventKind.QUESTION_ASKED, post.at, {"summary": question, "text": text, "options": [str(o) for o in body.get("options") or []], "call_id": call_id}, native_id=ref, launch_id=session.launch.launch_id))
        elif post.reply_id:
            await session.term.reply(post.reply_id, {"text": f"the team has no tool {tool!r}", "error": True})

    # -- Daedalus's other tools (the browser's) -------------------------------------------------------

    async def _tools(self, session: CliSession, post: HookPost) -> None:
        """One call of a tool set through ``ptyd tools-mcp``, answered on its held post.

        The set runs the call as a Daedalus session's tool would run it — the same code, policy and
        audit — for the live session of the launch. A call seen twice (a replay after the host
        restarted) is answered as the first one was and run once."""
        body = post.body if isinstance(post.body, dict) else {}
        tool = str(body.get("tool") or "")
        name = str(body.get("set") or "")
        if tool == "hello":
            session.channel[f"tools:{name}"] = post.at or _now()
            return
        reply: dict[str, Any]
        try:
            call_id = str(body.get("call_id") or "")
            seen = session.calls.get(call_id) if call_id else None
            if seen is not None and seen[0] == "tools":
                if post.reply_id:
                    await session.term.reply(post.reply_id, seen[1])
                return
            tools = self.tool_sets.get(name)
            live = await self.lookup(session.staff_session_id)
            arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
            if live is None:
                reply = {"text": "this session is no longer connected to its team; nothing was done", "error": True}
            elif tools is None:
                reply = {"text": f"the {name or 'unnamed'} tools are not available in this installation", "error": True}
            else:

                async def ask(key: str, what: str, summary: str, *, route: str | None = "operator") -> bool | None:
                    return await self._ask_tool(session, live, key, what, summary, route=route)

                text, failed = await tools.call(live, tool, dict(arguments or {}), ask=ask, launch_id=session.launch.launch_id, cwd=session.cwd)
                reply = {"text": text, "error": bool(failed)}
            self._remember_call(session, call_id, ("tools", reply))
        except Exception as exc:  # noqa: BLE001 — a tool's failure is the call's answer, not the runtime's end
            logger.exception("a %s tool call of %s failed", name, session.staff_session_id)
            reply = {"text": f"the tool failed: {exc}", "error": True}
        if post.reply_id:
            await session.term.reply(post.reply_id, reply)

    async def _ask_tool(self, session: CliSession, live: LiveSession, key: str, tool: str, summary: str, *, route: str | None = "operator") -> bool | None:
        """Put a tool call's question on the member's request list and wait for it up to the
        permission hold.

        The operator's by default: what a browser buys, sends or deletes is theirs whatever the
        project's autonomy gives the orchestrator. ``route=None`` lets the autonomy decide, which is
        how the browser asks to open an address. An answer that arrives after the call gave up is kept
        for the same call made again, once, and the member is told of it."""
        if key in session.tool_grants:
            return session.tool_grants.pop(key)
        ref = f"tools:{key}"
        future = session.tool_asks.get(ref)
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            session.tool_asks[ref] = future
            await self.ingress.permission(live, ref, tool, summary, route=route, risk="elevated" if route == "operator" else "routine")
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.config().permission_hold_s)
        except TimeoutError:
            # Nobody waits on it now. Left in place, a late answer resolved this abandoned future and
            # was lost: the member was neither told nor let through when it made the call again.
            if session.tool_asks.get(ref) is future:
                del session.tool_asks[ref]
            return None

    @staticmethod
    def _remember_call(session: CliSession, call_id: str, what: tuple[str, Any]) -> None:
        if not call_id:
            return
        session.calls[call_id] = what
        while len(session.calls) > CALLS_REMEMBERED:
            session.calls.pop(next(iter(session.calls)))

    # -- the team's calls ------------------------------------------------------------------------

    def _session(self, live: LiveSession) -> CliSession:
        session = self.sessions.get(live.id)
        if session is None or session.finished:
            raise RuntimeError(f"{live.staff.name}'s {self.adapter.capabilities.label} is not running here")
        return session

    def continuity(self, live: LiveSession) -> str:
        """Whether a live session can take its member's next task as a message: ``live`` when this
        runtime holds it, ready and running; ``attaching`` while it may yet be taken up (a host that
        has not finished its reconcile, a CLI still at its readiness gate); ``gone`` otherwise, and
        the member is then launched afresh."""
        session = self.sessions.get(live.id)
        if session is None:
            return "gone" if self.taken_up else "attaching"
        if session.finished or session.stopping or session.exited.is_set():
            return "gone"
        if live.session.status == StaffState.STARTING.value:
            # Still at its readiness gate. Not ``ready``: a session taken up after a restart never
            # sees that event again, and it is as ready as it was when the host went.
            return "attaching"
        return "live"

    async def send(self, live: LiveSession, msg: OutgoingMessage) -> Receipt:
        """Queue a message for the session's delivery worker and say so. The worker reports every
        later state (``written``, ``submitted``, ``acknowledged`` or ``failed``) through the ingress;
        a timing this CLI cannot keep is named in ``degraded_to`` at once."""
        session = self._session(live)
        assert session.worker is not None
        text = msg.text if msg.origin == "operator" else f"[orchestrator] {msg.text}"
        degraded = session.worker.degraded(msg.mode)
        session.worker.put(Pending(msg.id, text, msg.mode, msg.origin, degraded_to=degraded))
        return Receipt("queued", degraded_to=degraded or None)  # type: ignore[arg-type]

    async def interrupt(self, live: LiveSession) -> None:
        await self.adapter.interrupt(self._session(live).term)

    async def answer(self, live: LiveSession, ask: AskRef, decision: Decision) -> None:
        """Deliver an answer the team has already recorded. A team question is answered on its held
        post, or — once the hold has expired and the worker was told to carry on — as a message; a
        CLI's own request through the adapter, which must confirm it or the delivery fails."""
        session = self._session(live)
        ref = ask.request_ref
        # The operator's own words are kept apart from the option they pressed: a note written
        # beside a chosen option once replaced the option in a team answer, and never reached a
        # command-line member at all, whose answer carried only the option's label.
        note = (decision.text or "").strip()
        chosen = ", ".join(decision.selected)
        text = " — ".join(part for part in dict.fromkeys((chosen, note)) if part)
        if ref.startswith("tools:"):
            # A tool call's question: the call waiting on it takes the answer; one that gave up has
            # it kept for the same call made again.
            waiting = session.tool_asks.pop(ref, None)
            allowed = bool(decision.allow)
            if waiting is not None and not waiting.done():
                waiting.set_result(allowed)
            else:
                if allowed:
                    session.tool_grants[ref.removeprefix("tools:")] = True
                # The call that asked gave up waiting and told the member to stop; the answer is news.
                assert session.worker is not None
                words = "it was granted: make the same call again, once" if allowed else "it was refused: do not try it again"
                session.worker.put(Pending("", f"[the {decision.by} answered your request to act in the browser] {words}", "after_turn", "system"))
        elif ref.startswith(("team:", "team-message:")):
            words = text or ("yes" if decision.allow else "no" if decision.allow is False else "")
            posts = [*reversed(session.held_for.get(ref, [])), *([ref.removeprefix("team:")] if ref.startswith("team:") else [])]
            held = False
            for reply_id in posts:
                # The newest post that still waits takes the answer; the others ended on their own.
                if await session.term.reply(reply_id, {"text": words}):
                    held = True
                    break
            if not held:
                assert session.worker is not None
                session.worker.put(Pending("", f"[the {decision.by} answers your question] {words}", "after_turn", "system"))
        else:
            if ask.kind == "permission":
                always = "allow_always_server" if decision.server else "allow_always"
                choice = (always if decision.always else "allow_once") if decision.allow else "deny_with_note" if text else "deny"
            else:
                choice = decision.selected[0] if decision.selected else "text"
            if not await self.adapter.answer(session.term, ref, Answer(choice, note=note)):
                raise RuntimeError(f"the answer could not be confirmed in {self.adapter.capabilities.label}; answer it in the terminal")
        await self._apply(session, StaffEvent(EventKind.REQUEST_RESOLVED, _now(), {"via": decision.by}, native_id=ref, launch_id=session.launch.launch_id))

    async def read(self, live: LiveSession, req: ReadRequest) -> ReadPage:
        if req.what == "screen":
            session = self._session(live)
            return _clip(await session.term.screen(scrollback=200), req.max_chars, None)
        if req.what == "diff":
            return await self._diff(live, req)
        ref = self._transcript_ref(live)
        if not ref:
            # Said, not raised: the session id once stood in for the missing path, the adapter
            # refused it as not absolute, and ReadStaff failed twice on a member that had died before
            # its first turn, when that was the very thing the orchestrator needed to learn.
            label = self.adapter.capabilities.label
            if live.session.live:
                return ReadPage(f"no turn record yet: {label} has not begun its first turn", req.cursor, False)
            return ReadPage(f"no turn record: the session ended before its first turn ({live.session.end_reason or live.session.status})", req.cursor, False)
        try:
            turns = await self._turns(live)
        except Exception as exc:  # noqa: BLE001 — a record that cannot be read is an answer, not a failure of the tool
            logger.info("the turn record of %s could not be read", live.id, exc_info=True)
            return ReadPage(f"the turn record at {ref} could not be read: {getattr(exc, 'message', None) or exc}", req.cursor, False)
        after = int(req.cursor) if req.cursor and req.cursor.isdigit() else -1
        fresh = [t for t in turns if t.index > after]
        cursor = str(max(t.index for t in fresh)) if fresh else req.cursor
        if req.what == "last":
            for turn in reversed(fresh):
                if turn.role == "assistant" and turn.text.strip():
                    return _clip(turn.text.strip(), req.max_chars, cursor)
            return ReadPage("(no reply yet)", cursor, False)
        return _clip(_render(fresh, max(1, req.turns)), req.max_chars, cursor)

    async def turns(self, live: LiveSession) -> list[Turn]:
        """The session's turns from its CLI's own transcript, for the staff view."""
        return await self._turns(live)

    def _transcript_ref(self, live: LiveSession) -> str:
        """Where the CLI keeps the session's turns — a file, or OpenCode's own session — once the CLI
        said so; empty before that. The CLI's session id is not one: it stood in for a record never
        written, and an adapter that reads a file refused it as a path that is not absolute."""
        session = self.sessions.get(live.id)
        return (session.transcript_ref if session is not None else "") or live.session.transcript_ref or ""

    async def _turns(self, live: LiveSession) -> list[Turn]:
        session = self.sessions.get(live.id)
        ref = self._transcript_ref(live)
        if not ref:
            return []
        if session is not None:
            port = session.env_port
        else:
            async with self.store.db.transaction() as conn:
                resources = await for_staff_in(conn, live.id)
            port = RuntimeEnvironment(self.terminals, await self._env_of(live),
                                      actor=f"agent:{self._actor(live.id)}", resources=resources)
        turns = await self.adapter.transcript(port, ref)
        return turns[-READ_TRANSCRIPT_TURNS:]

    async def _env_of(self, live: LiveSession) -> str:
        launch = await self.store.open_launch_for(live.id)
        return launch.env if launch is not None else "container"

    async def _diff(self, live: LiveSession, req: ReadRequest) -> ReadPage:
        session = self.sessions.get(live.id)
        cwd = live.session.worktree_path or (session.cwd if session is not None else "")
        if not cwd:
            return ReadPage("no diff: the session's folder is not known here", None, False)
        env = session.launch.env if session is not None else await self._env_of(live)
        base = live.session.base_ref or "HEAD"
        port = RuntimeEnvironment(self.terminals, env, actor=f"agent:{self._actor(live.id)}")
        try:
            stat = await port.run(["git", "-C", cwd, "diff", "--stat", base], timeout=DIFF_TIMEOUT)
            patch = await port.run(["git", "-C", cwd, "diff", base], timeout=DIFF_TIMEOUT)
            untracked = await port.run(["git", "-C", cwd, "ls-files", "--others", "--exclude-standard"], timeout=DIFF_TIMEOUT)
        except TerminalError as exc:
            return ReadPage(f"no diff: {exc.message}", None, False)
        except (ProgramNotFound, EnvironmentUnavailable) as exc:
            return ReadPage(f"no diff: {exc}", None, False)
        if stat.exit_code != 0:
            return ReadPage(f"no diff: {stat.stderr.strip() or 'git failed'}", None, False)
        text = (stat.stdout.strip() or "(no changes against the base)") + (f"\n\nnew files not yet added:\n{untracked.stdout.strip()}" if untracked.stdout.strip() else "") + (f"\n\n{patch.stdout}" if patch.stdout.strip() else "")
        return _clip(text, req.max_chars, None, keep="head")

    async def usage(self, live: LiveSession) -> UsageSnapshot | None:
        """What the session's turns cost, from the CLI's own transcript. A CLI on a subscription
        reports tokens; the cost, when it gives one, is what the same use would cost metered."""
        every = await self._turns(live)
        turns = [t for t in every if t.usage is not None]
        if not turns:
            return None
        spent = [t.usage for t in turns if t.usage is not None]
        costs = [u.cost_usd for u in spent if u.cost_usd is not None]
        last = spent[-1]
        return UsageSnapshot(
            input_tokens=sum(u.input_tokens + u.cache_read_tokens for u in spent),
            output_tokens=sum(u.output_tokens for u in spent),
            cost_usd=sum(costs) if costs else None,
            window_used_pct=None,
            source="subscription",
            context_tokens=last.context_tokens or None,
            context_window=last.context_window or None,
            windows=next((u.windows for u in reversed(spent) if u.windows), ()),
            replies=sum(1 for t in every if t.role == "assistant"),
            at=turns[-1].ended_at,
        )

    # -- stopping --------------------------------------------------------------------------------

    async def stop(self, live: LiveSession) -> None:
        """Ask the CLI to exit, kill its terminal after the grace, and end the launch. The team ends
        the session row; the worktree is the team's as well."""
        session = self.sessions.get(live.id)
        if session is None:
            launch = await self.store.open_launch_for(live.id)
            if launch is not None:
                await self._end_launch(launch, [t for t in (launch.terminal_id, launch.companion_terminal_id) if t], actor=f"agent:{self._actor(live.id)}")
            return
        session.stopping = True
        grace = self.config().stop_grace_s
        if not session.exited.is_set():
            try:
                await self.adapter.stop(session.term)
            except Exception:  # noqa: BLE001 — a CLI that will not be asked is killed instead
                logger.info("%s did not take the request to exit", live.staff.name, exc_info=True)
            # The CLI's own goodbye (a session-end hook) may let the session go before the terminal's
            # exit is reported; either ends the wait.
            waits = [asyncio.ensure_future(session.exited.wait()), asyncio.ensure_future(session.done.wait())]
            try:
                await asyncio.wait(waits, timeout=grace, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
        async with session.lock:
            await self._finish(session, "stopped", end=False)

    async def _finish(self, session: CliSession, reason: str, *, end: bool, live: LiveSession | None = None) -> None:
        """Let a session go: its tasks, its terminals and its launch. ``end`` reports to the team
        that the CLI ended by itself; a stop or a session the team already ended reports nothing."""
        if session.finished:
            return
        session.finished = True
        session.done.set()
        current = asyncio.current_task()
        for task in session.tasks:
            if task is not current:
                task.cancel()
        self.sessions.pop(session.staff_session_id, None)
        for terminal_id in (session.term.id, *session.companions):
            self._by_terminal.pop(terminal_id, None)
        if end and not session.stopping and live is not None:
            await self.ingress.ended(live, reason)
        await self._end_launch(session.launch, [session.term.id, *session.companions], actor=f"agent:{session.term.actor}", grace=self.config().stop_grace_s if end else 0.0)

    async def _end_launch(self, launch: Launch, terminals: list[str], *, actor: str, grace: float = 0.0) -> None:
        """Kill what still runs, end the launch at the daemon and in the store. ``grace`` is for a CLI
        that said goodbye and is on its way out: killed in the middle of it, it leaves half a
        transcript and an exit code that looks like a crash."""
        deadline = self.clock() + grace
        for terminal_id in terminals:
            try:
                view = await self.terminals.get(terminal_id)
                while view["status"] == "running" and self.clock() < deadline:
                    await asyncio.sleep(0.1)
                    view = await self.terminals.get(terminal_id)
                if view["status"] == "running":
                    await self.terminals.kill(terminal_id, actor=actor)
            except TerminalError as exc:
                logger.info("terminal %s of launch %s not ended: %s", terminal_id, launch.launch_id, exc.message)
        try:
            await self.terminals.unregister_launch(launch.env, launch.launch_id, actor=actor)
        except TerminalError as exc:
            # A daemon that restarted has forgotten the launch already; one that is away forgets it
            # when its terminal goes. The row is ended either way.
            logger.info("launch %s not unregistered: %s", launch.launch_id, exc.message)
        await self.store.end_launch(launch.launch_id)

    # -- after a restart -------------------------------------------------------------------------

    async def reconcile(self, *, wait: float = 60.0) -> int:
        """Take up the launches a previous host left open; returns how many run on.

        A launch whose session has ended is ended too, its terminals with it. One whose terminal is
        gone ended while no host was watching, and its session ends with that reason. The rest are
        attached: the adapter re-dials what it needs, the daemon replays the hook posts made while the
        host was away, and the machine goes on from the status in the row.
        """
        try:
            return await self._reconcile(wait=wait)
        finally:
            self.taken_up = True

    async def _reconcile(self, *, wait: float) -> int:
        attached = 0
        for launch in await self.store.open_launches():
            if launch.harness != self.kind or launch.staff_session_id in self.sessions:
                continue
            if not await self.terminals.wait_available(launch.env, timeout=wait):
                logger.warning("launch %s left as it is: the %s terminal service is not available", launch.launch_id, launch.env)
                continue
            actor = self._actor(launch.staff_session_id)
            live = await self.lookup(launch.staff_session_id)
            terminals = [t for t in (launch.terminal_id, launch.companion_terminal_id) if t]
            if live is None:
                await self._end_launch(launch, terminals, actor=f"agent:{actor}")
                # The host's daemon observer can close ownership before this runtime reconnects.
                # Read that exact historical session only to finish its exit reason, never attach it.
                proof = await self.store.db.fetchone(
                    "SELECT 1 FROM runtime_exit_observations WHERE staff_session_id = ?"
                    " AND runtime_kind = 'cli' AND provider_session_ref = ? LIMIT 1",
                    (launch.staff_session_id, f"terminal:{launch.terminal_id}"),
                )
                if proof is not None:
                    from daedalus.stores.staff import StaffStore  # Lazy: recovery needs only historical projection.

                    staff = StaffStore(self.store.db)
                    prior = await staff.session(launch.staff_session_id)
                    member = await staff.get(prior.staff_id) if prior is not None else None
                    if (prior is not None and member is not None and prior.ended_at
                            and not prior.end_reason and prior.terminal_id == launch.terminal_id):
                        await self.ingress.ended(LiveSession(member, prior),
                                                f"{self.adapter.capabilities.label} ended while the host was away")
                continue
            running = False
            if launch.terminal_id:
                with contextlib.suppress(NotFound):
                    running = (await self.terminals.get(launch.terminal_id))["status"] == "running"
            if not running:
                await self._end_launch(launch, terminals, actor=f"agent:{actor}")
                await self.ingress.ended(live, f"{self.adapter.capabilities.label} ended while the host was away")
                continue
            assert launch.terminal_id is not None
            view = await self.terminals.get(launch.terminal_id)
            async with self.store.db.transaction() as conn:
                resources = await for_staff_in(conn, launch.staff_session_id)
            hooks: asyncio.Queue[HookPost | None] = asyncio.Queue()
            session = CliSession(
                staff_session_id=launch.staff_session_id,
                launch=launch,
                term=RuntimeTerminal(self.terminals, launch.terminal_id, launch.env, actor=actor, launch_id=launch.launch_id, hooks=hooks),
                env_port=RuntimeEnvironment(self.terminals, launch.env, actor=f"agent:{actor}",
                                            resources=resources),
                cwd=str(view.get("cwd") or ""),
                companions={launch.companion_terminal_id: "companion"} if launch.companion_terminal_id else {},
                transcript_ref=live.session.transcript_ref or "",
                hooks=hooks,
                last_signal=self.clock(),
            )
            called = await self.store.last_team_call(launch.launch_id)
            if called:
                session.channel["team"] = called
                session.channel["team_tools"] = "connected"
            await self.adapter.attach(session.term, launch)
            starting = live.session.status == StaffState.STARTING.value
            await self._take_up_messages(session, live, starting=starting)
            # A session still starting when the host went has its gate run again: the dialog it was
            # stuck on may still be on screen, and reading a screen twice is harmless.
            self._run(session, gate=starting)
            if session.worker is not None:
                for pending in session.pending_after_restart:
                    session.worker.put(pending)
                session.pending_after_restart.clear()
            attached += 1
        return attached

    async def _take_up_messages(self, session: CliSession, live: LiveSession, *, starting: bool) -> None:
        """The messages a previous host left on their way. A queued one is delivered as if just sent;
        one already written or submitted is looked for, never typed again. The first message of a
        session still starting is its first prompt: on the command line the CLI submits it itself,
        and its acknowledgement is still to come; through a channel, it goes once the CLI is ready
        again — the plan that would have sent it is gone with the previous host."""
        messages = await self.ingress.messages_of(live)
        if not messages:
            return
        first = messages[0]
        for message in messages:
            if message.state in ("acknowledged", "failed"):
                continue
            if message is first and starting:
                session.first_message_id = message.id
                if self.adapter.capabilities.first_prompt == "channel":
                    session.resend_first = True
                else:
                    session.first_prompt_pending = True
                continue
            session.pending_after_restart.append(pending_of(message, first=message is first))

    async def _resend_first(self, session: CliSession) -> None:
        live = await self.lookup(session.staff_session_id)
        if live is None or session.worker is None:
            return
        message = next((m for m in await self.ingress.messages_of(live) if m.id == session.first_message_id), None)
        if message is None or message.state in ("acknowledged", "failed"):
            return
        session.resend_first = False
        session.worker.put(Pending(message.id, message.text, "after_turn", message.origin))


def _render(turns: list[Turn], count: int) -> str:
    """The last ``count`` turns: what started each, the replies in full, every tool as one line."""
    starts = [i for i, t in enumerate(turns) if t.role in ("user", "orchestrator")]
    begin = starts[-count] if len(starts) >= count else 0
    lines: list[str] = []
    for turn in turns[begin:]:
        if turn.role in ("user", "orchestrator"):
            text = " ".join(turn.text.split())
            if text:
                lines.append(f"» {text[:300]}")
            continue
        lines.extend(f"· {tool.name} {tool.summary[:160]}" for tool in turn.tools)
        if turn.text.strip():
            lines.append(turn.text.strip())
    return "\n".join(lines) or "(nothing yet)"


def _clip(text: str, limit: int, cursor: str | None, *, keep: str = "tail") -> ReadPage:
    if len(text) <= limit:
        return ReadPage(text, cursor, False)
    return ReadPage(text[:limit] if keep == "head" else text[-limit:], cursor, True)


def install_runtimes(
    adapters: Mapping[str, Callable[[], HarnessAdapter]],
    runtimes: dict[str, Any],
    *,
    terminals: Terminals,
    store: HarnessStore,
    ingress: TeamIngress,
    lookup: Lookup,
    config: Callable[[], HarnessConfig],
    blocker: Callable[[str, str], Awaitable[str]] | None = None,
) -> list[CliStaffRuntime]:
    """One runtime per registered adapter, put into the team's ``runtimes`` under the harness name."""
    made = []
    for name, factory in adapters.items():
        runtime = CliStaffRuntime(factory(), terminals=terminals, store=store, ingress=ingress, lookup=lookup, config=config, blocker=blocker)
        runtimes[name] = runtime
        made.append(runtime)
    return made


__all__ = ["CliSession", "CliStaffRuntime", "RuntimeEnvironment", "RuntimeTerminal", "install_runtimes"]
