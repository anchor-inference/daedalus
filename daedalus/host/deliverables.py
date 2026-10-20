"""What a project's orchestrator may show the operator of the project's files, and from where.

The orchestrator runs the team and writes nothing, yet the operator judges the team's work by its
files: a member's document lives in that member's worktree (``<folder>/.agents/worktrees/<member>``)
until the operator merges it, and a project folder is often on the host, out of the container's
reach. The file pane and the two delivering tools (SendFile, AttachMedia) therefore read for the
orchestrator inside the project's folders — worktrees included, since they lie inside them — and
nowhere else, and they never open the repository's own metadata or a file that holds credentials,
which no operator asked an orchestrator to pass around.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from daedalus.host.worktrees import WORKTREES_DIR
from daedalus.stores.files import FileRefused
from daedalus.stores.projects import Project, ProjectFolder

SEALED_NAMES = frozenset({".git", ".checkpoints", ".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc", ".git-credentials"})
"""Path components never shown or delivered: git's metadata (a worktree's ``.git`` is a file naming
the repository's), the snapshot store, and the usual homes of credentials."""

_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

WORKTREE_PANE = "wt~"
"""The prefix of a worktree's id among the folder ids of an orchestrator's file pane. A folder id is
``[A-Za-z0-9_-]`` and a worktree's directory is a staff slug, so ``~`` separates them unambiguously
and survives a URL path unencoded."""

_PANE_RE = re.compile(r"wt~(?P<folder>[A-Za-z0-9_-]{1,160})~(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})\Z")


def secret_part(path: str) -> str | None:
    """The component of ``path`` that seals it, or ``None`` when it may be shown.

    ``.env`` and its variants hold the keys a project runs with; a template of one
    (``.env.example``) holds none and stays visible, because it is the file a reader wants to see.
    """
    for part in PurePosixPath(path.replace("\\", "/")).parts:
        if part in SEALED_NAMES:
            return part
        if part == ".env" or (part.startswith(".env.") and not part.endswith(_TEMPLATE_SUFFIXES)):
            return part
    return None


def worktree_pane(folder_id: str, name: str) -> str:
    """The pane id of the staff worktree ``name`` of the folder ``folder_id``."""
    return f"{WORKTREE_PANE}{folder_id}~{name}"


def parse_worktree_pane(pane: str) -> tuple[str, str] | None:
    """``(folder_id, worktree directory name)`` for a worktree pane id, ``None`` for anything else.

    The name is held to one plain path component, so a pane id can only ever name a directory
    directly under the folder's worktree directory — never ``..``, never a path with a slash.
    """
    match = _PANE_RE.match(pane)
    if match is None or match["name"] in (".", ".."):
        return None
    return match["folder"], match["name"]


def worktree_root(folder: ProjectFolder, name: str) -> str:
    """Where the staff worktree ``name`` of ``folder`` lives, as a path of the folder's environment."""
    return posixpath.join(str(folder.path), str(WORKTREES_DIR), name)


def locate(project: Project, raw: str) -> tuple[ProjectFolder, str]:
    """The project folder a path lies in and the path made absolute and normal.

    A relative path is taken from the primary folder, as an orchestrator's Peek takes it. The path is
    judged by its text here; the reader that opens it then judges the real path where it lives (the
    local filesystem's realpath, or the host bridge's own containment), so a link out of a folder is
    refused there rather than trusted here.
    """
    text = raw.strip()
    if not text:
        raise FileRefused("name a file: a path inside one of the project's folders")
    if text.startswith("~"):
        raise FileRefused(f"{text} names a home directory; give a path inside one of the project's folders")
    if not text.startswith("/"):
        if project.primary is None:
            raise FileRefused(f"{project.name} has no folder for {text!r} to be in")
        text = posixpath.join(str(project.primary.path), text)
    clean = posixpath.normpath(text)
    for folder in sorted(project.folders, key=lambda f: -len(str(f.path))):
        root = posixpath.normpath(str(folder.path))
        if clean == root or clean.startswith(root.rstrip("/") + "/"):
            sealed = secret_part(posixpath.relpath(clean, root))
            if sealed is not None:
                raise FileRefused(f"{clean} is inside {sealed}, which holds the repository's metadata or credentials and is never delivered")
            return folder, clean
    raise FileRefused(
        f"{clean} is not in any of {project.name}'s folders; a member's file is in its worktree "
        f"(<folder>/{WORKTREES_DIR}/<member>/…), which ReviewResult(op='inspect') and Peek show"
    )


__all__ = ["SEALED_NAMES", "WORKTREE_PANE", "locate", "parse_worktree_pane", "secret_part", "worktree_pane", "worktree_root"]
