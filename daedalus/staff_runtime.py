"""The seam between a project's team and whatever runs each member.

A staff member is run by Daedalus itself or by one of the command-line agents. Everything above
this seam — assignment, the launch queue, requests, statuses, the orchestrator's tools and the app —
is one code path; below it, one :class:`StaffRuntime` per executor starts a session, delivers what
is said to it and reports back. A runtime never writes the staff tables itself: every change of state
goes through :class:`TeamIngress`, which writes the row and publishes the event together, so a
status on the screen and a status in the database cannot disagree.

The value types are frozen and carry everything a runtime needs, composed by the host: a runtime
does not look anything up. :class:`FakeStaffRuntime` stands in for a real one in tests.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from daedalus.host.worktrees import Worktree
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.stores.staff import Staff, StaffSession

MessageMode = Literal["now", "after_turn", "interrupt"]
"""When a message goes in; see ``daedalus.harness.contract.SendMode``."""
Origin = Literal["orchestrator", "operator"]
ReceiptState = Literal["queued", "written", "submitted", "acknowledged", "failed"]
PermissionLevel = Literal["ask", "edits", "all"]


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether a runtime can start a member in an environment now, and why not when it cannot."""

    ok: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BoardTask:
    """The task a session is started for, as the board holds it: its four-part brief included."""

    id: str
    title: str
    status: str
    priority: int = 3
    objective: str = ""
    deliverable: str = ""
    boundaries: str = ""
    done_when: str = ""
    project_id: str | None = None
    folder_id: str | None = None
    assignee_staff_id: str | None = None
    branch: str | None = None
    depends_on: tuple[str, ...] = ()
    sent_back: str = ""
    """The operator's note when the work was sent back from review; the next session starts from it."""
    returned: str = ""
    """The orchestrator's note when it checked the work and returned it; the next session starts from it."""

    def missing(self) -> list[str]:
        """The brief's fields still empty; a task is not handed to anyone until all four are written."""
        return [name for name in ("objective", "deliverable", "boundaries", "done_when") if not getattr(self, name).strip()]


@dataclass(frozen=True, slots=True)
class LiveSession:
    """A member and its one live session: what every call into a runtime is about."""

    staff: Staff
    session: StaffSession

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def session_id(self) -> str | None:
        """The Daedalus session a Daedalus member works in; ``None`` for a command-line member."""
        return self.session.session_id

    @property
    def terminal_id(self) -> str | None:
        return self.session.terminal_id

    @property
    def cli_session_id(self) -> str | None:
        return self.session.cli_session_id


@dataclass(frozen=True, slots=True)
class StartRequest:
    """Everything a runtime needs to start one session, composed by the host.

    ``brief_text`` is the standing brief (persona or agent, instructions, notes, the rules of the
    team); ``first_message`` is the task itself. ``cwd`` is where the session works: the worktree's
    working directory when there is one (``Worktree.cwd``, which is a sub-folder of the worktree when
    the project folder is a sub-folder of its repository), the folder otherwise.
    """

    staff: Staff
    project: Project
    folder: ProjectFolder
    cwd: Path
    worktree: Worktree | None
    task: BoardTask | None
    first_message: str
    brief_text: str
    staff_session_id: str
    env: str
    model: str
    effort: str
    agent: str
    permission_level: PermissionLevel
    """From the project's autonomy: ``ask`` asks for everything, ``edits`` lets edits through, ``all`` lets everything through."""
    permission_mode: str
    """The staff member's own override of the command-line agent's mode, empty for its default."""
    team_url: str
    team_token: str
    """For the team server a command-line member reports through; minted per launch, kept only hashed."""
    predecessor: LiveSession | None = None
    origin: Origin = "operator"
    first_message_id: str = ""
    """The staff message the first message is recorded as, so its receipt can be reported."""
    allow_rules: tuple[str, ...] = ()
    """The operator's standing grants to the member, as the CLI's allow rules, for a CLI that keeps them."""


@dataclass(frozen=True, slots=True)
class Started:
    terminal_id: str | None
    cli_session_id: str | None
    transcript_ref: str | None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    id: str
    text: str
    mode: MessageMode
    origin: Origin


@dataclass(frozen=True, slots=True)
class Receipt:
    """How far a message got. A later state arrives through :meth:`TeamIngress.message_state`.

    ``degraded_to`` names the mode a runtime used instead of the one asked for, when its executor
    cannot do that one (``now`` that waits for the turn to end, ``now`` that interrupts the turn first).
    """

    state: ReceiptState
    error: str = ""
    degraded_to: MessageMode | None = None


@dataclass(frozen=True, slots=True)
class AskRef:
    id: str
    kind: Literal["question", "permission"]
    request_ref: str
    """What the runtime needs to deliver the answer: the Daedalus tool-call id or approval key, or the command-line agent's own request id."""


@dataclass(frozen=True, slots=True)
class Decision:
    allow: bool | None
    text: str | None
    selected: list[str]
    by: Origin
    always: bool = False
    """An allow that also covers the same request from now on, where the CLI offers that (Claude's
    "don't ask again", Codex's accept for the session, OpenCode's "always"). A runtime that cannot
    deliver it gives a plain allow: the operator is asked again next time, which is the safe side."""
    server: bool = False
    """With ``always``: the grant covers every tool of the asked tool's MCP server, not the one tool."""


@dataclass(frozen=True, slots=True)
class ReadRequest:
    what: Literal["last", "turns", "screen", "diff"] = "last"
    turns: int = 1
    cursor: str | None = None
    max_chars: int = 4000


@dataclass(frozen=True, slots=True)
class ReadPage:
    text: str
    next_cursor: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    window_used_pct: float | None
    source: Literal["metered", "subscription"]
    context_tokens: int | None = None
    """How full the context was at the end of the last turn, where the CLI's transcript says."""
    context_window: int | None = None
    """The model's window, only when the CLI reported it."""
    windows: tuple[tuple[int, float], ...] = ()
    """The subscription's rate-limit windows (minutes, percent used) the CLI last reported."""
    replies: int = 0
    """The member's replies counted in its transcript."""
    at: str = ""
    """When the turn the snapshot was read after ended: the numbers are as of then, not live."""

    def view(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "window_used_pct": self.window_used_pct,
            "source": self.source,
            "context_tokens": self.context_tokens,
            "context_window": self.context_window,
            "windows": [{"minutes": minutes, "used_pct": used} for minutes, used in self.windows],
            "replies": self.replies,
            "at": self.at,
        }


@runtime_checkable
class StaffRuntime(Protocol):
    """One executor of staff: Daedalus, or one command-line agent.

    ``start`` is called only once the launch queue has admitted the launch; ``stop`` ends the process
    or session and leaves the worktree alone (the host keeps or removes it).
    """

    kind: str

    async def available(self, env: str) -> Availability: ...

    async def start(self, req: StartRequest) -> Started: ...

    async def send(self, live: LiveSession, msg: OutgoingMessage) -> Receipt: ...

    async def interrupt(self, live: LiveSession) -> None: ...

    async def answer(self, live: LiveSession, ask: AskRef, decision: Decision) -> None: ...

    async def read(self, live: LiveSession, req: ReadRequest) -> ReadPage: ...

    async def stop(self, live: LiveSession) -> None: ...

    async def resume(self, req: StartRequest, prior: LiveSession) -> Started: ...

    async def usage(self, live: LiveSession) -> UsageSnapshot | None: ...


@runtime_checkable
class TeamIngress(Protocol):
    """Where a runtime reports what happens in a session. Each call writes the staff tables and
    publishes the matching event with the project, the member and the session named."""

    async def status(self, live: LiveSession, status: str, waiting_for: str = "", *, detail: str = "") -> None:
        """``starting · working · turn_done_unseen · idle · error · no_signal``; ``question`` and
        ``permission`` are set by the two calls below, ``exited`` by ending the session."""

    async def permission(self, live: LiveSession, request_ref: str, tool: str, summary: str, *, route: str | None = None, risk: str = "routine") -> str:
        """A permission the executor asks for; routed by the project's autonomy, or to ``route`` when
        the request names who decides it (a browser's sensitive action: the operator). Returns the
        request's id."""

    async def question(self, live: LiveSession, request_ref: str, text: str, options: list[str], *, call_id: str | None = None) -> str:
        """A question the executor's own dialog asks; routed like a permission. Returns the request's id.
        A team call's question keeps its ``call_id``, so the same call seen again finds it (``asked``)."""

    async def asked(self, live: LiveSession, call_id: str) -> Any:
        """The request a team call with this ``call_id`` opened in the session, open or settled, or
        ``None``. It outlives the host: a call replayed after a restart finds what the first one got."""

    async def message_state(self, message_id: str, state: str, error: str = "") -> None:
        """A later receipt for a message: a terminal write is ``written``, never ``acknowledged``."""

    async def report(
        self, live: LiveSession, kind: str, note: str, artifacts: list[str] | None = None, remember: str | None = None, *,
        call_id: str | None = None, evidence: list[dict[str, str]] | None = None, acknowledged: list[str] | None = None,
        operator_steps: dict[str, Any] | None = None,
    ) -> str:
        """``checkpoint · needs_input · stuck · done``; returns what the reporter is told. A report
        with a ``call_id`` already recorded is not made again: it was a replay of the same call.
        ``evidence`` is a done report's word on each check and requirement; ``acknowledged`` the
        requirements the member confirmed."""

    async def ask(self, live: LiveSession, question: str, options: list[str] | None = None, context: str = "") -> str:
        """A question for the orchestrator from the team server; returns the request's id at once."""

    async def usage(self, live: LiveSession, snapshot: UsageSnapshot) -> None:
        """What the session has spent so far, after each turn."""

    async def expects_signal(self, live: LiveSession) -> bool:
        """Whether the session's silence should be looked into: a turn is running on a task that is
        still being worked. The runtime's silence check asks before it reads a quiet screen."""

    async def signal(self, live: LiveSession) -> None:
        """A sign of life from the executor that changes no status. Written at most every few
        seconds; the team's clock shows a session silent when these stop coming."""

    async def resolved(self, live: LiveSession, request_ref: str, *, by: str = "operator", via: str = "terminal") -> bool:
        """A request settled where the team did not see it: answered in the executor's own screen
        (``operator`` via ``terminal``), or withdrawn with the turn that asked it (``system`` via
        ``withdrawn``). True when this closed it; false when it was already answered."""

    async def located(self, live: LiveSession, *, cli_session_id: str | None = None, transcript_ref: str | None = None) -> None:
        """Where the executor keeps the session, once it says so: a command-line agent names its
        session and its transcript only after it has started."""

    async def ended(self, live: LiveSession, reason: str) -> None:
        """The executor ended on its own — the command-line agent exited — rather than being stopped.
        The session ends with that reason and its open requests are withdrawn."""

    async def implicit_report(self, live: LiveSession, kind: str, text: str) -> None:
        """A turn ended without the member reporting: ``turn_done`` with the end of its last message,
        or ``needs_input`` when that message asks something. Published as a report the host made."""

    async def channel(self, live: LiveSession, team_tools: str, detail: str = "") -> None:
        """The member's team tools are ``connected`` or ``missing``: whether its reports and
        questions can reach the team at all."""

    async def messages_of(self, live: LiveSession) -> list[Any]:
        """The session's messages, oldest first, with their receipt states: what a runtime taking a
        session up after a restart still has to deliver or look for."""


@dataclass
class FakeStaffRuntime:
    """A runtime that does nothing but remember what it was asked, for the tests of everything above it.

    ``fail_start`` makes the next starts raise; ``receipt`` is what every ``send`` answers;
    ``availability`` what ``available`` answers.
    """

    kind: str = "fake"
    availability: Availability = field(default_factory=lambda: Availability(True))
    receipt: Receipt = field(default_factory=lambda: Receipt("submitted"))
    fail_start: str = ""
    started: list[StartRequest] = field(default_factory=list)
    sent: list[tuple[str, OutgoingMessage]] = field(default_factory=list)
    answered: list[tuple[str, AskRef, Decision]] = field(default_factory=list)
    interrupted: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    resumed: list[tuple[StartRequest, str]] = field(default_factory=list)
    reads: list[tuple[str, ReadRequest]] = field(default_factory=list)
    page: ReadPage = field(default_factory=lambda: ReadPage("", None, False))
    snapshot: UsageSnapshot | None = None
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    async def available(self, env: str) -> Availability:
        return self.availability

    async def start(self, req: StartRequest) -> Started:
        if self.fail_start:
            raise RuntimeError(self.fail_start)
        self.started.append(req)
        n = next(self._ids)
        return Started(terminal_id=f"term-{n}", cli_session_id=f"cli-{n}", transcript_ref=None)

    async def send(self, live: LiveSession, msg: OutgoingMessage) -> Receipt:
        self.sent.append((live.id, msg))
        return self.receipt

    async def interrupt(self, live: LiveSession) -> None:
        self.interrupted.append(live.id)

    async def answer(self, live: LiveSession, ask: AskRef, decision: Decision) -> None:
        self.answered.append((live.id, ask, decision))

    async def read(self, live: LiveSession, req: ReadRequest) -> ReadPage:
        self.reads.append((live.id, req))
        return self.page

    async def stop(self, live: LiveSession) -> None:
        self.stopped.append(live.id)

    async def resume(self, req: StartRequest, prior: LiveSession) -> Started:
        self.resumed.append((req, prior.id))
        return Started(terminal_id=prior.terminal_id, cli_session_id=prior.cli_session_id, transcript_ref=prior.session.transcript_ref)

    async def usage(self, live: LiveSession) -> UsageSnapshot | None:
        return self.snapshot


__all__ = [
    "AskRef",
    "Availability",
    "BoardTask",
    "Decision",
    "FakeStaffRuntime",
    "LiveSession",
    "OutgoingMessage",
    "ReadPage",
    "ReadRequest",
    "Receipt",
    "StaffRuntime",
    "StartRequest",
    "Started",
    "TeamIngress",
    "UsageSnapshot",
]
