"""The values the terminals service takes and gives: owners, specifications, views and errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ENVS = ("container", "host")
OWNER_KINDS = ("session", "staff", "project", "free")
STATUSES = ("running", "exited", "lost")


@dataclass(frozen=True, slots=True)
class Owner:
    kind: str
    """``session`` · ``staff`` · ``project`` · ``free``"""
    id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in OWNER_KINDS:
            raise InvalidRequest(f"an owner is one of {', '.join(OWNER_KINDS)}, not {self.kind!r}")
        if (self.kind == "free") != (not self.id):
            raise InvalidRequest("a free terminal has no owner id, and every other owner has one")


@dataclass(frozen=True, slots=True)
class Origin:
    """Who writes into a terminal: always an agent, named, because the audit and the daemon's journal
    must say whose words they were. Human input never comes this way; it arrives on an attachment."""

    actor: str
    launch_id: str = ""
    note: str = ""


@dataclass(slots=True)
class TerminalSpec:
    env: str
    owner: Owner
    project_id: str | None = None
    cwd: str | None = None
    """Absolute, in the environment's filesystem. ``None`` = the owner's default place."""
    argv: list[str] | None = None
    """Run exactly, never typed into a shell. ``None`` = the environment's login shell."""
    env_vars: dict[str, str] = field(default_factory=dict)
    strip_env: list[str] = field(default_factory=list)
    title: str = ""
    sandbox: bool = False
    cols: int = 80
    rows: int = 24
    profile: str = "shell"
    """``shell`` or ``harness:<name>``: what kind of cost this terminal is, for the load estimate."""
    launch_id: str = ""
    log_to_disk: bool = False
    created_by: str = "operator"
    """``operator`` or ``agent:<actor>``. An agent's launch waits for a place under the cap; the
    operator's goes past it once confirmed."""
    resources: dict[str, Any] | None = None
    """An exact host-approved attempt scope and immutable ceilings; the daemon chooses the handle."""


@dataclass(frozen=True, slots=True)
class EnvStatus:
    env: str
    available: bool
    reason: str = ""
    """A code: ``not_configured``, ``connecting``, ``not_installed``, ``not_running``, ``refused``,
    ``unreachable``, ``permission_denied``, ``protocol_mismatch``; empty when available."""
    detail: str = ""
    version: str = ""
    sandbox: str = ""
    """``ok`` when the environment's daemon can run a terminal in the sandbox; otherwise why not, in
    the daemon's words; empty while the environment is unavailable."""
    containment: dict[str, Any] = field(default_factory=dict)
    shell: str = ""
    home: str = ""
    port_range: str = ""
    public_host: str = ""
    preview_poll_ms: int = 3000
    running: int = 0
    image_version: str = ""
    """The daemon this installation's image carries, where the host can know it (the container
    environment of a compose install); empty elsewhere."""
    update_available: bool = False
    """The running daemon is not the one in the image: recreating the service would update it, and
    end its terminals."""

    def view(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
            "version": self.version,
            "sandbox": self.sandbox,
            "containment": self.containment,
            "shell": self.shell,
            "home": self.home,
            "port_range": self.port_range,
            "public_host": self.public_host,
            "preview_poll_ms": self.preview_poll_ms,
            "running": self.running,
            "image_version": self.image_version,
            "update_available": self.update_available,
        }


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """The bytes reached the terminal. Never that the program read them, let alone acted on them."""

    bytes: int
    seq_before: int
    queued_ms: int
    delivered_at: str


@dataclass(frozen=True, slots=True)
class OutputChunk:
    from_seq: int
    to_seq: int
    head_seq: int
    gap: bool
    data: str | bytes


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    """An event of one environment's daemon, in the daemon's own total order (``seq``)."""

    env: str
    seq: int
    type: str
    terminal_id: str | None
    at: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecResult:
    """A program run for its output, not in a terminal. ``exit_code`` is -1 when a signal ended it."""

    exit_code: int
    signal: str
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_ms: int
    path: str = ""
    """What ``PATH`` resolved the program to, as the daemon found it."""


@dataclass(frozen=True, slots=True)
class FileChunk:
    """Bytes of a file read through the daemon. ``next_offset`` is where the next read starts; after a
    ``rotated`` tail it counts from the start of the new file. ``file_id`` is passed back to the next
    tail so a replaced file is noticed even when it is already longer than the old one."""

    data: bytes
    offset: int
    next_offset: int
    size: int
    eof: bool = False
    rotated: bool = False
    file_id: str = ""


@dataclass(slots=True)
class LaunchSpec:
    """A launch of a CLI: what its terminals are given to talk back through.

    ``files`` are written into the launch's own directory inside the environment (its settings
    overlay, an MCP entry, a large prompt), ``ports`` are the loopback ports ``net_dial`` may reach
    for it, and ``hold_max_ms`` is the longest one of its hook posts may wait for ``reply_hook``.
    """

    launch_id: str = ""
    """Empty: the daemon picks one."""
    terminal_id: str = ""
    """The terminal its hook events are tagged with, when known before the terminal is created."""
    files: dict[str, bytes] = field(default_factory=dict)
    ports: list[int] = field(default_factory=list)
    hold_max_ms: int = 0
    ttl_s: int = 0
    """How long it waits for its first terminal before it is dropped; 0 = the daemon's default."""


@dataclass(frozen=True, slots=True)
class Launch:
    env: str
    launch_id: str
    hook_url: str
    hook_token: str
    dir: str
    """Where ``files`` were written, in the environment's filesystem."""
    dial_dir: str
    """Where the launch's programs put the unix sockets ``net_dial("unix:<name>")`` reaches."""
    env_vars: dict[str, str]
    """What every terminal of the launch is given; also what a hand-written MCP entry passes on."""
    files: list[str]


@dataclass(frozen=True, slots=True)
class HookEvent:
    """A hook post of a launch, in the daemon's order among its terminal events.

    ``body`` is the post's JSON, or its text when it is not JSON; ``truncated`` when long strings in
    it were shortened to keep the event bounded (keys and ids are kept). ``reply_id`` is set when the
    post waits for ``reply_hook``.
    """

    env: str
    seq: int
    at: str
    launch_id: str
    terminal_id: str | None
    name: str
    body: Any
    reply_id: str | None = None
    hold_ms: int = 0
    truncated: bool = False
    size: int = 0


class TerminalError(Exception):
    """Base of what the service refuses; the API maps each kind to a status."""

    status = 500
    code = "error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(TerminalError):
    status, code = 404, "not_found"


class InvalidRequest(TerminalError):
    status, code = 400, "invalid"


class Conflict(TerminalError):
    status, code = 409, "conflict"


class OverCap(TerminalError):
    """The machine already runs as many terminals as the cap allows."""

    status, code = 409, "over_cap"


class Forbidden(TerminalError):
    """Outside what the daemon allows: a path off its roots or on its deny list, a program not on its
    list, a port no launch registered."""

    status, code = 403, "forbidden"


class StaleLaunch(TerminalError):
    """The launch is not registered, or it has ended."""

    status, code = 410, "stale_launch"


class EnvUnavailable(TerminalError):
    status, code = 503, "unavailable"


class Unsupported(TerminalError):
    status, code = 501, "unsupported"


class TimedOut(TerminalError):
    status, code = 504, "timeout"


class LiveTerminals(TerminalError):
    """Recreating the daemon would end running terminals, and the operator has not said yes to that."""

    status, code = 409, "live_terminals"


class NoRebuilder(TerminalError):
    """Nothing on this installation can recreate the terminals service; the operator runs the command."""

    status, code = 503, "no_rebuilder"


__all__ = [
    "ENVS",
    "OWNER_KINDS",
    "STATUSES",
    "Conflict",
    "EnvStatus",
    "EnvUnavailable",
    "ExecResult",
    "FileChunk",
    "Forbidden",
    "HookEvent",
    "InvalidRequest",
    "Launch",
    "LaunchSpec",
    "LiveTerminals",
    "NoRebuilder",
    "NotFound",
    "Origin",
    "OutputChunk",
    "OverCap",
    "Owner",
    "StaleLaunch",
    "TerminalError",
    "TerminalEvent",
    "TerminalSpec",
    "TimedOut",
    "Unsupported",
    "WriteReceipt",
]
