"""What this installation is actually able to do, decided once at startup.

Self-development is not a property of the code but of the installation around it: a server with a
GitHub token and a rebuild channel can open pull requests against its own repositories and replace
itself; a desktop install has a checkout and nothing else; an install with neither should not carry
tools, routes, prompt text or a navigation entry for any of it.

``selfdev.mode`` names which of the three it is. The operator sets it in the configuration
(``[self_change] mode = "off" | "local" | "server"``); ``auto``, the default, is resolved here from
the prerequisites that are really present. The resolution is pure — the same settings and
configuration always give the same answer — so the doctor can recompute it without a running
session manager.

The mode is fixed for the life of the process: tools are registered, routes are mounted and the
prompt is assembled from it at startup. Changing it in the configuration takes effect on a restart.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from daedalus import supervisor_client
from daedalus.config import RuntimeConfig, Settings

PUBLISHED_FILE = "capabilities.json"
"""Where the resolved capabilities are written in the state directory, for the supervisor to read."""

SelfDevMode = Literal["off", "local", "server"]
SELFDEV_MODES: tuple[SelfDevMode, ...] = ("off", "local", "server")

SELFDEV_TOOLS: dict[SelfDevMode, frozenset[str]] = {
    "off": frozenset(),
    # Local mode has a checkout and no remote: a worktree to work in, and a tool that commits it into the
    # checkout the process runs from. There is nothing to propose to and no image to rebuild.
    "local": frozenset({"SelfWorkspace", "SelfApply"}),
    "server": frozenset({"SelfWorkspace", "SelfPropose", "SelfRebuild", "SelfRollback"}),
}
"""The self-development tools each mode registers. Every tool a mode leaves out is not registered at
all, so the model never sees a name it cannot call."""

ALL_SELFDEV_TOOLS: frozenset[str] = frozenset().union(*SELFDEV_TOOLS.values())


@dataclass(frozen=True, slots=True)
class SelfDev:
    """The resolved self-development mode, what was asked for, and why it came out this way."""

    mode: SelfDevMode
    configured: str
    """What the configuration asked for: ``auto`` or an explicit mode."""
    reasons: list[str] = field(default_factory=list)
    """Prose the operator reads in the doctor and the app: what was found, in the order it was probed."""
    missing: list[str] = field(default_factory=list)
    """Prerequisites of ``mode`` that are absent. Non-empty only when the operator named the mode
    explicitly; an ``auto`` resolution never lands on a mode whose prerequisites it did not find."""

    @property
    def tools(self) -> frozenset[str]:
        return SELFDEV_TOOLS[self.mode]

    @property
    def disabled_tools(self) -> frozenset[str]:
        """Tool names discovery must skip in this mode."""
        return ALL_SELFDEV_TOOLS - self.tools

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "configured": self.configured, "reasons": list(self.reasons), "missing": list(self.missing), "tools": sorted(self.tools)}


@dataclass(frozen=True, slots=True)
class Browser:
    """Whether this installation has a browser for its agents: a browser daemon's run directory it was
    told of, in either environment. Decided at startup like the rest, because the browser's tools are
    registered then; whether the daemon answers at this moment is the browser service's to say."""

    envs: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.envs)

    def as_dict(self) -> dict[str, Any]:
        return {"configured": self.configured, "envs": list(self.envs)}


BROWSER_TOOL_PREFIX = "Browser"
"""Every browser tool's name starts with it; an installation without a browser registers none."""


def resolve_browser(settings: Settings) -> Browser:
    return Browser(envs=tuple(env for env, run_dir in (("container", settings.browser_container_dir), ("host", settings.browser_host_dir)) if run_dir is not None))


@dataclass(frozen=True, slots=True)
class Computer:
    """Whether this installation may offer computer use: a native one the launcher told of a desktop
    daemon's run directory. Docker mode never does — the daemon drives the screen of the machine it
    runs on, and a container's agent has none of its own — and the operator chose the native
    application as the only home of the feature. Decided at startup, because the tools are registered
    then; whether the daemon is up, and whether a session's model can see, is judged per run."""

    native: bool = False
    run_dir: str = ""

    @property
    def configured(self) -> bool:
        return self.native and bool(self.run_dir)

    def as_dict(self) -> dict[str, Any]:
        return {"configured": self.configured, "native": self.native}


COMPUTER_TOOLS: frozenset[str] = frozenset({"Computer", "ComputerApps"})
"""The computer tools; an installation that cannot offer computer use registers none of them."""


def resolve_computer(settings: Settings) -> Computer:
    run_dir = settings.computer_host_dir
    return Computer(native=bool(settings.native), run_dir=str(run_dir) if run_dir is not None and str(run_dir).strip() else "")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Everything the app tells its surfaces about what this installation can do.

    The mode is decided once and never changes; the two change fields do, so they are passed in by whoever
    reports the capabilities rather than resolved here — ``dataclasses.replace`` on the resolved value, so
    there is one shape the app reads and no second route to poll for a banner.
    """

    selfdev: SelfDev
    browser: Browser = field(default_factory=Browser)
    computer: Computer = field(default_factory=Computer)
    restart_required: dict[str, Any] | None = None
    """The change committed to the checkout and waiting for a restart, or ``None``."""
    last_change: dict[str, Any] | None = None
    """What became of the change before it: applied, refused by the preflight, or reversed after it could
    not boot. What the app shows once the restart is over."""

    def as_dict(self) -> dict[str, Any]:
        return {"selfdev": self.selfdev.as_dict(), "browser": self.browser.as_dict(), "computer": self.computer.as_dict(), "restart_required": self.restart_required, "last_change": self.last_change}


# -- prerequisite probes ------------------------------------------------------------------


def _git_dir(repo: Path) -> Path | None:
    """The repository's git directory, for a normal checkout and for a linked worktree alike."""
    marker = repo / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.partition(":")[2].strip())
    if not target.is_absolute():
        target = (repo / target).resolve()
    # A linked worktree's git directory is <main>/.git/worktrees/<name>; the remotes are the main one's.
    if target.parent.name == "worktrees":
        target = target.parent.parent
    return target if target.is_dir() else None


def writable_checkout(repo: Path) -> bool:
    """A git checkout this process may write to — the minimum for changing one's own code at all."""
    git_dir = _git_dir(repo)
    return git_dir is not None and os.access(repo, os.W_OK)


def has_origin(repo: Path) -> bool:
    """Whether the checkout has an ``origin`` remote to push a branch to.

    Read from the git configuration rather than asked of git: this runs on the way up, and a probe
    that can block on a subprocess is a probe that can delay every start.
    """
    git_dir = _git_dir(repo)
    if git_dir is None:
        return False
    try:
        text = (git_dir / "config").read_text(encoding="utf-8")
    except OSError:
        return False
    return '[remote "origin"]' in text


REBUILDER_HEARTBEAT = "alive"
"""The file the rebuilder sidecar touches on every pass of its loop."""
REBUILDER_HEARTBEAT_SECONDS = 120.0
"""How stale that file may be and still mean a rebuilder is there. The sidecar's loop is five seconds;
this is wide enough for a loaded host and far short of the sidecar having been stopped."""


def rebuild_channel(settings: Settings) -> str:
    """How a new build reaches the running process, or an empty string when nothing can deliver one.

    The rebuilder is asked whether it is running, not whether the compose file mentions it. The shipped
    file always mentions it — the service is behind a profile that is off by default — so reading the
    file said yes on every installation that had a checkout, and the trigger this promises would be
    written into a directory nothing reads.
    """
    if supervisor_client.present(settings.supervisor_address):
        return "the supervisor" + (" on the loopback port" if settings.supervisor_tcp.strip() else " socket")
    heartbeat = settings.rebuild_trigger_dir / REBUILDER_HEARTBEAT
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except OSError:
        return ""
    return "the compose rebuilder" if age <= REBUILDER_HEARTBEAT_SECONDS else ""


def publish(capabilities: Capabilities, state_dir: Path) -> None:
    """Write what was resolved where the supervisor can read it.

    The supervisor decides whether a restart is preflighted, and it decides before the bot is up. Its
    own probe of the same prerequisites is a second rule set that can — and did — disagree with this
    one; what it reads instead is this file, from the start before. Best effort: a state directory that
    cannot be written is not a reason not to start.
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / PUBLISHED_FILE).write_text(
            json.dumps({"selfdev": capabilities.selfdev.as_dict(), "at": datetime.now(UTC).isoformat()}, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# -- resolution ---------------------------------------------------------------------------


def _probe(settings: Settings) -> tuple[dict[str, bool], list[str]]:
    """What is present, and one line per finding."""
    bot, core = settings.bot_repo_dir, settings.core_repo_dir
    found = {
        "checkouts": writable_checkout(bot) and writable_checkout(core),
        "token": bool(settings.github_token.strip()),
        "remotes": has_origin(bot) and has_origin(core),
        "rebuild": bool(rebuild_channel(settings)),
    }
    channel = rebuild_channel(settings)
    reasons = [
        "both repositories are writable git checkouts" if found["checkouts"] else "the host and core repositories are not both writable git checkouts",
        "a GitHub token is configured" if found["token"] else "no GitHub token is configured",
        "both checkouts have an origin remote" if found["remotes"] else "the checkouts have no origin remote to push to",
        f"a rebuild reaches the running process through {channel}" if channel else "nothing can deliver a new build to the running process",
    ]
    return found, reasons


_PREREQUISITES: dict[SelfDevMode, tuple[tuple[str, str], ...]] = {
    "off": (),
    "local": (("checkouts", "a writable git checkout of the host and the core"),),
    "server": (
        ("checkouts", "a writable git checkout of the host and the core"),
        ("token", "a GitHub token"),
        ("remotes", "an origin remote on both checkouts"),
        ("rebuild", "a rebuild channel (the supervisor socket or the compose rebuilder)"),
    ),
}


def resolve_selfdev(settings: Settings, config: RuntimeConfig) -> SelfDev:
    """The mode this installation runs in, from the configuration and what is really there."""
    configured = str(config.self_change.mode or "auto")
    found, reasons = _probe(settings)
    server_ready = all(found[key] for key, _ in _PREREQUISITES["server"])
    local_ready = all(found[key] for key, _ in _PREREQUISITES["local"])
    if configured == "auto":
        mode: SelfDevMode = "server" if server_ready else "local" if local_ready else "off"
        headline = {
            "server": "self-development runs against the repositories on GitHub",
            "local": "self-development is local: the checkout is edited and applied by a restart",
            "off": "self-development is off: this installation cannot change its own code",
        }[mode]
        return SelfDev(mode=mode, configured=configured, reasons=[headline, *reasons])
    mode = configured if configured in SELFDEV_MODES else "off"  # type: ignore[assignment]
    missing = [label for key, label in _PREREQUISITES[mode] if not found[key]]
    headline = f"the operator set selfdev.mode = {mode}" if configured in SELFDEV_MODES else f"selfdev.mode = {configured!r} is not a mode; treated as off"
    return SelfDev(mode=mode, configured=configured, reasons=[headline, *reasons], missing=missing)


def resolve(settings: Settings, config: RuntimeConfig) -> Capabilities:
    return Capabilities(selfdev=resolve_selfdev(settings, config), browser=resolve_browser(settings), computer=resolve_computer(settings))


__all__ = [
    "ALL_SELFDEV_TOOLS",
    "BROWSER_TOOL_PREFIX",
    "COMPUTER_TOOLS",
    "PUBLISHED_FILE",
    "SELFDEV_MODES",
    "SELFDEV_TOOLS",
    "Browser",
    "Capabilities",
    "Computer",
    "SelfDev",
    "SelfDevMode",
    "has_origin",
    "publish",
    "rebuild_channel",
    "resolve",
    "resolve_browser",
    "resolve_computer",
    "resolve_selfdev",
    "writable_checkout",
]
