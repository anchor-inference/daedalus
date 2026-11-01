"""What changed in a terminal's folder, for the Terminals workspace's side panel: the files and one
file's diff, read with git in the terminal's own environment.

Everything runs through a caller-supplied ``run(argv, max_output)`` — the terminal daemon's
``exec.run`` in that environment — so a host terminal's folder is read on the host, by the daemon's
program list and roots, and audited there like every other program it runs. Nothing here touches
this process's own filesystem.

git runs with :data:`daedalus.host.gitrun.GIT_SAFE` in front of every subcommand: the folder is
whatever the terminal's shell is in, a repository whose configuration nobody here chose, and a
``core.fsmonitor`` or an external diff in it must not run because the operator opened a panel.
``GIT_OPTIONAL_LOCKS=0`` keeps a diff from rewriting the index under a program working in the folder.

Every diff also says ``--no-ext-diff --no-textconv``. ``GIT_SAFE`` empties ``diff.external``, and an
empty external diff is not "none": git tries to run a program named "" and dies on the first file,
so a patch read without the flag came back empty for every file.

Renames are switched off: a rename is a deletion and an addition, two rows the panel can open,
instead of one record whose path is neither file.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from daedalus.host.gitrun import git_argv, nul_names, numstat_entries

FILES_MAX = 500
"""Rows the Files tab lists. A folder with more changes than this is a checkout or a build, and a
longer list is neither readable nor cheap to send every few seconds."""
DIFF_MAX_BYTES = 512 << 10
"""What one file's diff may be. Past that the panel says the rest was cut, rather than freezing on
a generated file."""
LIST_MAX_OUTPUT = 1 << 20
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
"""git's empty tree: what a repository with no commit yet is compared against, so its files list as added."""


class Ran(Protocol):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


Runner = Callable[[list[str], int], Awaitable[Ran]]


class ChangesRefused(ValueError):
    """A path the diff will not read: absolute, climbing out of the repository, or not a path at all."""


def _said(result: Ran) -> str:
    return (result.stderr or result.stdout).strip().splitlines()[-1][:300] if (result.stderr or result.stdout).strip() else f"git exited with {result.exit_code}"


async def _where(run: Runner, folder: str, base: str | None) -> tuple[str, str, str] | dict[str, Any]:
    """The repository's top folder, its branch and the revision to compare against; or, when the folder
    is not in a repository, the answer that says so."""
    top = await run(git_argv("-C", folder, "rev-parse", "--show-toplevel"), 64 << 10)
    if top.exit_code != 0 or not top.stdout.strip():
        return {"root": folder, "branch": "", "base": "", "files": [], "truncated": False, "reason": "not_git", "detail": _said(top)}
    root = top.stdout.strip().splitlines()[0]
    branch_run, head_run = await asyncio.gather(
        run(git_argv("-C", root, "rev-parse", "--abbrev-ref", "HEAD"), 64 << 10),
        run(git_argv("-C", root, "rev-parse", "--verify", "--quiet", "HEAD"), 64 << 10),
    )
    branch = branch_run.stdout.strip() if branch_run.exit_code == 0 else ""
    against = base or ("HEAD" if head_run.exit_code == 0 else EMPTY_TREE)
    return root, branch, against


async def changed_files(run: Runner, folder: str, *, base: str | None = None) -> dict[str, Any]:
    """The files that differ from ``base`` (the last commit when not given) in the repository that
    holds ``folder``, plus the files git does not track yet, sorted by path and bounded by
    :data:`FILES_MAX`. Each row is ``{path, status, added, removed}``: ``status`` is git's letter
    (``A``, ``M``, ``D``, ``T``) or ``?`` for an untracked file; the counts are None for a binary or
    an untracked file."""
    where = await _where(run, folder, base)
    if isinstance(where, dict):
        return where
    root, branch, against = where
    statuses, counts, others = await asyncio.gather(
        run(git_argv("-C", root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", against, "--"), LIST_MAX_OUTPUT),
        run(git_argv("-C", root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", against, "--"), LIST_MAX_OUTPUT),
        run(git_argv("-C", root, "ls-files", "--others", "--exclude-standard", "-z"), LIST_MAX_OUTPUT),
    )
    if statuses.exit_code != 0:
        return {"root": root, "branch": branch, "base": against, "files": [], "truncated": False, "reason": "git_failed", "detail": _said(statuses)}
    fields = statuses.stdout.split("\0")
    rows: dict[str, dict[str, Any]] = {}
    for i in range(0, len(fields) - 1, 2):
        letter, path = fields[i][:1], fields[i + 1]
        if letter and path:
            rows[path] = {"path": path, "status": letter, "added": None, "removed": None}
    for added, removed, path in numstat_entries(counts.stdout) if counts.exit_code == 0 else []:
        if path in rows:
            rows[path].update(added=added, removed=removed)
    for path in nul_names(others.stdout) if others.exit_code == 0 else []:
        rows.setdefault(path, {"path": path, "status": "?", "added": None, "removed": None})
    ordered = sorted(rows.values(), key=lambda r: r["path"])
    truncated = len(ordered) > FILES_MAX or statuses.truncated or others.truncated
    return {"root": root, "branch": branch, "base": against, "files": ordered[:FILES_MAX], "truncated": truncated, "reason": "", "detail": ""}


def check_path(path: str) -> str:
    """A path inside the repository, as git prints it. Refused: an absolute one, one that climbs out
    with ``..``, an empty one, or one with a NUL (which would end the argument early)."""
    text = (path or "").strip()
    if not text or "\0" in text or text.startswith("/") or posixpath.isabs(text):
        raise ChangesRefused("a file of the repository, as a path relative to its top folder")
    if ".." in text.split("/"):
        raise ChangesRefused("the path climbs out of the repository")
    return text


async def file_diff(run: Runner, folder: str, path: str, *, base: str | None = None) -> dict[str, Any]:
    """One file's unified diff against ``base``; an untracked file is diffed against nothing, so all of
    it reads as added. Cut at :data:`DIFF_MAX_BYTES` with ``truncated`` set."""
    name = check_path(path)
    where = await _where(run, folder, base)
    if isinstance(where, dict):
        return {"path": name, "diff": "", "truncated": False, "untracked": False, "reason": where["reason"], "detail": where["detail"]}
    root, _branch, against = where
    tracked = await run(git_argv("-C", root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", against, "--", name), DIFF_MAX_BYTES + 4096)
    text, untracked, cut = tracked.stdout, False, tracked.truncated
    if not text.strip():
        listed = await run(git_argv("-C", root, "ls-files", "--others", "--exclude-standard", "-z", "--", name), 64 << 10)
        if name in nul_names(listed.stdout):
            # --no-index exits 1 when the two differ, which for a new file is always: that is the answer, not a failure.
            fresh = await run(git_argv("-C", root, "diff", "--no-ext-diff", "--no-textconv", "--no-index", "--no-color", "--", "/dev/null", name), DIFF_MAX_BYTES + 4096)
            text, untracked, cut = fresh.stdout, True, fresh.truncated
    if len(text.encode()) > DIFF_MAX_BYTES:
        text = text.encode()[:DIFF_MAX_BYTES].decode("utf-8", "ignore")
        cut = True
    return {"path": name, "diff": text, "truncated": cut, "untracked": untracked, "reason": "", "detail": ""}


__all__ = ["DIFF_MAX_BYTES", "FILES_MAX", "GIT_ENV", "ChangesRefused", "Runner", "changed_files", "check_path", "file_diff"]
