"""How each of a staff member's settings is actually kept: by the host, by its CLI's own mode, only by
asking in the prompt, or not at all.

A setting on the hire form reads as a promise whichever of these keeps it. A reviewer hired
read-only used to run with its CLI's edit mode while its brief said not to write, and nothing on the
screen told the operator that "read-only" was then a request rather than a wall. The lines are
derived from the functions a launch itself calls (``Team.launch_permission_mode``, each adapter's
start mode, the isolation a Daedalus session gets), so the answer cannot drift from what starts.

Each line is codes, not prose: the app words them in the operator's language.
"""

from __future__ import annotations

from typing import Any, Literal

from daedalus.extensions.staff import Team
from daedalus.harness import claude, codex, cursor, grok
from daedalus.harness.capabilities import RESTRICTIVE_MODES
from daedalus.stores.projects import ProjectFolder
from daedalus.stores.staff import Staff, StaffError

Kept = Literal["host", "cli", "prompt", "none"]


def _line(setting: str, kept: Kept, reason: str, mode: str = "") -> dict[str, Any]:
    return {"setting": setting, "kept": kept, "reason": reason, "mode": mode}


def start_mode(member: Staff, permission_level: str) -> tuple[str, str]:
    """The CLI mode the member would start in, and Codex's approval policy beside its sandbox.

    Empty for a CLI with no modes. Raises ``StaffError`` where a launch would be refused, which is
    what a read-only member of a CLI with no no-write mode gets."""
    mode = Team.launch_permission_mode(member)
    try:
        if member.harness == "claude":
            return claude.start_mode(mode, permission_level), ""
        if member.harness == "grok":
            return grok.start_mode(mode, permission_level), ""
        if member.harness == "codex":
            return codex.start_modes(mode, permission_level)
        if member.harness == "cursor":
            return cursor.start_mode(mode), ""
    except KeyError:
        # A stored mode the adapter does not know is refused at launch; said as it is stored.
        return mode, ""
    return "", ""


def how_kept(member: Staff, folder: ProjectFolder, *, local_env: str, permission_level: str) -> list[dict[str, Any]]:
    """One line per setting the operator chose: where the member may write, what it may do without
    asking, its network, and the scope of its work."""
    if member.harness == "daedalus":
        return _daedalus(member, folder, local_env)
    refused = False
    try:
        mode, approval = start_mode(member, permission_level)
    except StaffError:
        refused, mode, approval = True, member.permission_mode, ""
    restrictive = mode in RESTRICTIVE_MODES.get(member.harness, frozenset())
    lines: list[dict[str, Any]] = []
    if refused:
        lines.append(_line("folder", "none", "readonly_refused"))
    elif restrictive:
        lines.append(_line("folder", "cli", "readonly", mode))
    elif member.harness == "codex" and mode == "workspace-write":
        lines.append(_line("folder", "cli", "sandbox", mode))
    elif member.harness == "codex" and mode == "danger-full-access":
        lines.append(_line("folder", "none", "anywhere", mode))
    else:
        lines.append(_line("folder", "prompt", "brief_worktree" if member.isolation == "worktree" else "brief_shared"))

    if member.harness == "pi":
        lines.append(_line("asking", "none", "never"))
    elif member.harness == "opencode":
        lines.append(_line("asking", "cli", "rules", permission_level))
    elif mode in ("bypassPermissions", "danger-full-access") or approval == "never":
        lines.append(_line("asking", "none", "bypass", mode))
    else:
        lines.append(_line("asking", "cli", "mode", approval or mode))

    if member.harness == "codex" and mode in ("read-only", "workspace-write"):
        lines.append(_line("network", "cli", "off", mode))
    else:
        lines.append(_line("network", "none", "open"))
    lines.append(_line("scope", "prompt", "brief"))
    return lines


def _daedalus(member: Staff, folder: ProjectFolder, local_env: str) -> list[dict[str, Any]]:
    """A Daedalus member runs in this process, so its walls are the host's own (``walls_for``), except
    in a folder of the other environment: there its tools run through the host terminal with no
    walls, and only the read-only promise is kept (``SessionManager._drive_host``)."""
    if folder.readonly or member.isolation == "readonly":
        where = _line("folder", "host", "readonly")
    elif not folder.local(local_env):
        where = _line("folder", "prompt", "remote")
    else:
        where = _line("folder", "host", "worktree" if member.isolation == "worktree" else "shared")
    return [where, _line("asking", "host", "policy"), _line("network", "none", "open"), _line("scope", "prompt", "brief")]


__all__ = ["how_kept", "start_mode"]
