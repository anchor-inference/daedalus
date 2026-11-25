"""The branch a working directory is on, read from the repository's ``HEAD`` file.

The session detail carries this and the app polls it, so it must cost a few ``stat`` calls and one
small read: no ``git`` subprocess, which would spawn a process for every poll of every open chat and
stall the event loop if run inline. Nothing is cached, because a read of a thirty-byte file is
cheaper than the bookkeeping that would keep a cache honest across a checkout made in a terminal.

The directory belongs to the agent, which can write anything into it, so what is read is bounded
(one short read of one file) and trusted for nothing but the name it shows.
"""

from __future__ import annotations

import re
from pathlib import Path

SEARCH_DEPTH = 12
"""How many parents are tried for a ``.git``: a session works in a subfolder of its repository as often as in its root."""
HEAD_READ_BYTES = 512
"""A ``HEAD`` is a ref name or a hash; anything longer is not one."""
_BRANCH = re.compile(r"^ref: refs/heads/([^\x00-\x1f\x7f]{1,200})$")


def _git_dir(directory: Path, stop: Path | None) -> Path | None:
    """The directory that holds ``HEAD`` for the repository ``directory`` is in, or ``None``.

    ``.git`` is a directory in a checkout and a file (``gitdir: <path>``) in a linked worktree or a
    submodule. ``stop`` is the folder the search never leaves: a session's own directory under the
    workspaces root must not take the branch of whatever repository that root happens to sit in.
    """
    here = directory
    for _ in range(SEARCH_DEPTH):
        marker = here / ".git"
        try:
            if marker.is_dir():
                return marker
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace")[:HEAD_READ_BYTES].strip()
                if text.startswith("gitdir:"):
                    target = Path(text.removeprefix("gitdir:").strip())
                    return target if target.is_absolute() else here / target
                return None
        except OSError:
            return None
        if here == stop or here.parent == here:
            return None
        here = here.parent
    return None


def branch_of(directory: Path, *, stop: Path | None = None) -> str | None:
    """The checked-out branch of the repository ``directory`` is in; ``None`` outside a repository and
    on a detached ``HEAD``, which is on no branch."""
    git_dir = _git_dir(directory, stop)
    if git_dir is None:
        return None
    try:
        with (git_dir / "HEAD").open("rb") as head:
            text = head.read(HEAD_READ_BYTES).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    found = _BRANCH.match(text)
    return found.group(1) if found else None


__all__ = ["branch_of"]
