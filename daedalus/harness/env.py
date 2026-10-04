"""The environment a command-line agent is launched with.

A CLI launched from inside another tool inherits that tool's traces. ``CLAUDECODE`` makes Claude
Code believe it is nested inside itself; ``TMUX`` makes a TUI talk to a multiplexer that is not
there. So the inherited environment is filtered, the terminal's own settings are fixed, the CLI's
updater is switched off, and the launch's identity is added.

``CLAUDE_CONFIG_DIR`` is the exception to "every ``CLAUDE*`` goes": it says where a host's Claude
keeps its sign-in and settings, and without it a host whose Claude lives in a non-default directory
would launch signed out.

The inherited environment is the terminal daemon's, not this process's: the daemon builds the
final environment. ``launch_environment`` is the rule as a pure function (and what a local spawn
uses); ``terminal_environment`` is the same rule as the daemon takes it — patterns to strip and
variables to set.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from daedalus.harness.capabilities import Capabilities

STRIP = ("CLAUDE*", "TMUX*")
KEEP = frozenset({"CLAUDE_CONFIG_DIR"})
DAEMON_STRIP = ("TMUX*",)
"""What a terminal specification asks the daemon to strip beyond its own list, which already holds
``CLAUDE*`` with ``CLAUDE_CONFIG_DIR`` kept."""
FIXED = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
UTF8_FALLBACK = "C.UTF-8"
STAFF_INHERITED_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "PATHEXT", "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def staff_inherited_env(harness: str) -> list[str]:
    """Keep process basics and only the selected CLI's configuration locator.

    The shared HOME still contains other CLI accounts; this only prevents accidental environment
    inheritance until credentials can be mounted behind a separate read boundary.
    """
    names = list(STAFF_INHERITED_ENV)
    if harness == "claude":
        names.append("CLAUDE_CONFIG_DIR")
    return names


def _stripped(name: str) -> bool:
    if name in KEEP:
        return False
    return any(name.startswith(p[:-1]) if p.endswith("*") else name == p for p in STRIP)


def launch_environment(base: Mapping[str, str], extra: Mapping[str, str], *, capabilities: Capabilities | None = None) -> dict[str, str]:
    """``base`` filtered, then the fixed terminal settings, the updater switches, and ``extra`` on top.

    ``extra`` wins over everything, including the fixed settings, so one launch can still choose
    differently; but it cannot bring back a stripped name by accident — it has to name it.
    """
    out = {k: v for k, v in base.items() if not _stripped(k)}
    out.update(FIXED)
    _utf8(out)
    if capabilities is not None:
        out.update(dict(capabilities.autoupdate_off))
    out.update(extra)
    return out


def _utf8(env: dict[str, str]) -> None:
    """A UTF-8 character type without overriding a locale that already is one (a host terminal in
    ``ru_RU.UTF-8`` keeps it). The daemon applies the same rule to what it spawns."""
    effective = next((env[k] for k in ("LC_ALL", "LC_CTYPE", "LANG") if env.get(k)), "")
    if "utf-8" in effective.lower() or "utf8" in effective.lower():
        return
    for name in ("LC_ALL", "LC_CTYPE"):
        env.pop(name, None)
    env["LANG"] = UTF8_FALLBACK


@dataclass(frozen=True, slots=True)
class TerminalEnvironment:
    strip: tuple[str, ...]
    """Patterns the daemon removes from what it inherited (a name, or a prefix ending in ``*``)."""
    set: dict[str, str]


def terminal_environment(extra: Mapping[str, str], *, capabilities: Capabilities | None = None) -> TerminalEnvironment:
    """The rule in the form a terminal specification carries it.

    The daemon strips every ``CLAUDE*`` and ``TMUX*`` on its own, keeping ``CLAUDE_CONFIG_DIR``,
    so ``CLAUDE*`` is not passed on: a caller's strip pattern knows no exception, and passing it
    removed ``CLAUDE_CONFIG_DIR`` again — a host Claude with its configuration elsewhere then
    started as if never signed in (found against the real Claude Code). The terminal settings and
    the locale are the daemon's as well.
    """
    values: dict[str, str] = {}
    if capabilities is not None:
        values.update(dict(capabilities.autoupdate_off))
    values.update(extra)
    return TerminalEnvironment(strip=DAEMON_STRIP, set=values)


__all__ = ["DAEMON_STRIP", "FIXED", "KEEP", "STRIP", "UTF8_FALLBACK", "TerminalEnvironment", "launch_environment", "terminal_environment"]
