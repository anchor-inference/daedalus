"""The rules files of the folders below the workspace root, shown to the agent as its file tools reach them.

A project keeps rules for one part of it next to that part: ``miniapp/AGENTS.md`` for the app,
``desktop/AGENTS.md`` for the launcher. The root's file is in every run's opening message
(:meth:`SessionManager.workspace_notes`); these were never seen unless the agent happened to open
them. Now the first time in a run that Read, Write, Edit, Find or Search reaches a file or
folder below one, its ``AGENTS.md`` (and ``CLAUDE.md``, when it says something else) is put at the
head of that tool's result.

Why the result and not the core's own discovery (``rules_discovery_enabled``): the core adds the
bodies to the system prompt when a rule activates, so each new folder rewrites the prompt's first
bytes and the provider serves the next request without its cache, the whole conversation billed again
as new input. A tool result is the end of the conversation, which is new input anyway; nothing before
it moves. At the head of the result rather than the tail, because an older result is later cut down
to its head (the stale-result trim) and the rules are the part worth keeping.

Only files inside the session's working folder are read: never a folder above it, never another of the
project's folders, and nothing below a hidden or vendored folder. What a run was shown is forgotten
when the next run starts, so a rule cut out of an old result or folded into a summary is shown again
on the next touch rather than lost for the rest of the session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

RULE_FILES = ("AGENTS.md", "CLAUDE.md")
RULE_MAX_CHARS = 8_000
"""The longest rule body shown; past it the agent is told to Read the file for the rest."""
RULE_READ_BYTES = 1 << 20
SKIPPED_FOLDERS = frozenset({"node_modules", "vendor", "__pycache__", "site-packages"})
"""Folders whose rules are a dependency's, not the project's; hidden folders are skipped as well."""
STATE_KEY = "nested_rules_seen"
"""Where in :attr:`SessionServices.extra` the folders already looked at in this run are kept."""


def folders_below(root: Path, target: Path, *, folder: bool) -> list[Path]:
    """The folders strictly below ``root`` on the way to ``target``, outermost first.

    ``target`` itself is one of them when it is a ``folder``. Empty when ``target`` is not under
    ``root``. The walk stops at the first hidden or vendored folder: nothing below it is the project's.
    """
    relative = _relative(root, target)
    if relative is None:
        return []
    parts = relative.parts if folder else relative.parts[:-1]
    found: list[Path] = []
    current = root
    for part in parts:
        if part.startswith(".") or part in SKIPPED_FOLDERS:
            break
        current = current / part
        found.append(current)
    return found


def _relative(root: Path, target: Path) -> Path | None:
    for base in (root, Path(os.path.realpath(root))):
        try:
            return target.relative_to(base)
        except ValueError:
            continue
    return None


def forget(services: Any) -> None:
    """Start a new run: every folder's rules are shown again on its first touch."""
    services.extra.pop(STATE_KEY, None)


async def rules_note(services: Any, target: Path, *, folder: bool = False, redact: Any = None) -> str:
    """The rules of the folders on the way to ``target`` not yet shown in this run, ready to put at the
    head of a tool result, or an empty string.

    ``redact`` is the session's secret filter, applied to each body as to the root's notes.
    """
    root = Path(services.workspace_dir)
    if target.is_absolute():
        target = Path(os.path.normpath(target))
    else:
        target = root / target
    seen: set[str] = services.extra.setdefault(STATE_KEY, set())
    if folder and target == root:
        return ""
    candidates = [path for path in folders_below(root, target, folder=folder) if str(path) not in seen]
    if not candidates:
        return ""
    seen.update(str(path) for path in candidates)
    fs = services.fs
    try:
        present = await fs.existing_files([path / name for path in candidates for name in RULE_FILES])
    except OSError:
        return ""
    blocks: list[str] = []
    for path in candidates:
        bodies: list[str] = []
        for name in RULE_FILES:
            rule = path / name
            if rule not in present:
                continue
            try:
                text = (await fs.read_text(rule, limit=RULE_READ_BYTES)).strip()
            except (OSError, ValueError):
                continue
            if not text or text in bodies:
                continue
            bodies.append(text)
            shown = (_relative(root, rule) or rule).as_posix()
            if len(text) > RULE_MAX_CHARS:
                text = text[:RULE_MAX_CHARS] + f"\n[… {shown} continues; Read it for the rest]"
            if redact is not None:
                text = redact(text)
            under = (_relative(root, path) or path).as_posix()
            blocks.append(
                f"[rules from {shown}, for the work under {under}/, on top of the workspace's own; shown once a run]\n"
                f"{text}\n[end of {shown}]"
            )
    return "\n\n".join(blocks) + "\n\n" if blocks else ""


__all__ = ["RULE_FILES", "folders_below", "forget", "rules_note"]
