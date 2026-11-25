"""What changed in a git working tree, read and acted on with git in the tree's own environment: the
Terminals workspace's side panel (the files and one file's diff of a terminal's folder) and a chat's
Changes tab (the same, plus discarding and committing them).

Everything runs through a caller-supplied ``run(argv, max_output)`` — the terminal daemon's
``exec.run`` in that environment — so a host folder is read and changed on the host, by the daemon's
program list and roots, and audited there like every other program it runs. Nothing here touches
this process's own filesystem: a file is restored or removed by git, never by a path this module
joined itself.

git runs with :data:`daedalus.host.gitrun.GIT_SAFE` in front of every subcommand: the folder is
whatever the terminal's shell or the agent is in, a repository whose configuration nobody here
chose, and a ``core.fsmonitor``, a hook or an external diff in it must not run because the operator
opened a panel or pressed Commit. ``GIT_OPTIONAL_LOCKS=0`` keeps a diff from rewriting the index
under a program working in the folder.

Every call also says ``--literal-pathspecs``. A path the operator names reaches git as a pathspec,
and without the flag ``*`` or ``:/`` is a pattern: a revert of the "file" ``*`` discarded every
change in the repository.

Every diff also says ``--no-ext-diff --no-textconv``. ``GIT_SAFE`` empties ``diff.external``, and an
empty external diff is not "none": git tries to run a program named "" and dies on the first file,
so a patch read without the flag came back empty for every file.

Renames are switched off: a rename is a deletion and an addition, two rows the panel can open and
revert one at a time, instead of one record whose path is neither file.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple, Protocol

from daedalus.host.gitrun import git_argv, nul_names, numstat_entries

FILES_MAX = 500
"""Rows a listing carries. A folder with more changes than this is a checkout or a build, and a
longer list is neither readable nor cheap to send every few seconds."""
DIFF_MAX_BYTES = 512 << 10
"""What one file's diff may be. Past that the panel says the rest was cut, rather than freezing on
a generated file."""
LIST_MAX_OUTPUT = 1 << 20
SMALL_OUTPUT = 64 << 10
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
"""git's empty tree: what a repository with no commit yet is compared against, so its files list as added."""
COUNTING_AT_ONCE = 8
"""How many new files' lines are counted at the same time: each count is one call to the daemon."""


class Ran(Protocol):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


Runner = Callable[[list[str], int], Awaitable[Ran]]


class ChangesRefused(ValueError):
    """A path refused: absolute, climbing out of the repository, not a path at all, or not one of the
    changed files a revert or a commit may name."""


class ChangesBlocked(RuntimeError):
    """A revert or a commit that cannot go ahead as asked: no repository, nothing to do, a list too
    long to act on unseen, no identity to commit as, or git's own refusal."""


class Where(NamedTuple):
    root: str
    """The repository's top folder, as git prints it."""
    prefix: str
    """Where the folder asked about is inside it: ``""`` for the top, else ``sub/dir/``."""
    branch: str
    """``HEAD`` for a detached head, ``""`` when git could not say."""
    against: str
    head: str
    """The commit checked out; ``""`` in a repository with no commit yet."""


def _git(*args: str) -> list[str]:
    return git_argv("--literal-pathspecs", *args)


def _said(result: Ran) -> str:
    return (result.stderr or result.stdout).strip().splitlines()[-1][:300] if (result.stderr or result.stdout).strip() else f"git exited with {result.exit_code}"


def _not_git(folder: str, detail: str) -> dict[str, Any]:
    return {"root": folder, "prefix": "", "branch": "", "base": "", "files": [], "truncated": False, "reason": "not_git", "detail": detail}


async def _where(run: Runner, folder: str, base: str | None) -> Where | dict[str, Any]:
    """Where ``folder`` is in its repository and the revision to compare against; or, when the folder
    is not in a repository, the answer that says so."""
    top = await run(_git("-C", folder, "rev-parse", "--show-toplevel", "--show-prefix"), SMALL_OUTPUT)
    lines = top.stdout.split("\n")
    if top.exit_code != 0 or not lines[0].strip():
        return _not_git(folder, _said(top))
    if len(lines) > 3:
        # A folder name with a newline in it: the prefix cannot be told from the next line, and a
        # wrong prefix is a revert that reaches outside the folder.
        return _not_git(folder, "the folder's name holds a line break")
    root, prefix = lines[0], lines[1] if len(lines) > 1 else ""
    branch_run, head_run = await asyncio.gather(
        run(_git("-C", root, "rev-parse", "--abbrev-ref", "HEAD"), SMALL_OUTPUT),
        run(_git("-C", root, "rev-parse", "--verify", "--quiet", "HEAD"), SMALL_OUTPUT),
    )
    branch = branch_run.stdout.strip() if branch_run.exit_code == 0 else ""
    head = head_run.stdout.strip() if head_run.exit_code == 0 else ""
    return Where(root, prefix, branch, base or ("HEAD" if head else EMPTY_TREE), head)


async def _count_new(run: Runner, root: str, rows: list[dict[str, Any]]) -> None:
    """Fill in the lines of files git does not track yet, which ``--numstat`` of a diff leaves out:
    each is diffed against nothing. ``--no-index`` exits 1 whenever the two differ, which for a new
    file is always, so the exit code says nothing here."""
    gate = asyncio.Semaphore(COUNTING_AT_ONCE)

    async def count(row: dict[str, Any]) -> None:
        async with gate:
            result = await run(_git("-C", root, "diff", "--no-ext-diff", "--no-textconv", "--no-index", "--numstat", "-z", "--", "/dev/null", row["path"]), SMALL_OUTPUT)
        for added, removed, _path in numstat_entries(result.stdout):
            row.update(added=added, removed=removed, binary=added is None)

    await asyncio.gather(*(count(row) for row in rows))


async def changed_files(run: Runner, folder: str, *, base: str | None = None, scoped: bool = False, count_new: int = 0) -> dict[str, Any]:
    """The files that differ from ``base`` (the last commit when not given) in the repository that
    holds ``folder``, plus the files git does not track yet, sorted by path and bounded by
    :data:`FILES_MAX`. ``scoped`` keeps to ``folder`` itself rather than its whole repository.

    Each row is ``{path, status, added, removed, binary}``, the path from the repository's top:
    ``status`` is git's letter (``A``, ``M``, ``D``, ``T``) or ``?`` for an untracked file; the counts
    are None for a binary file, and for an untracked one beyond the first ``count_new``. A row whose
    path ends in ``/`` is a repository of its own nested in this one (a worktree under
    ``.agents/worktrees``): git does not look inside it, and nothing here acts on it."""
    where = await _where(run, folder, base)
    if isinstance(where, dict):
        return where
    return await _changed(run, where, scoped=scoped, count_new=count_new)


async def _changed(run: Runner, where: Where, *, scoped: bool, count_new: int) -> dict[str, Any]:
    scope = [where.prefix] if scoped and where.prefix else []
    statuses, counts, others = await asyncio.gather(
        run(_git("-C", where.root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", where.against, "--", *scope), LIST_MAX_OUTPUT),
        run(_git("-C", where.root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", where.against, "--", *scope), LIST_MAX_OUTPUT),
        run(_git("-C", where.root, "ls-files", "--others", "--exclude-standard", "-z", "--", *scope), LIST_MAX_OUTPUT),
    )
    found = {"root": where.root, "prefix": where.prefix if scoped else "", "branch": where.branch, "base": where.against}
    if statuses.exit_code != 0:
        return {**found, "files": [], "truncated": False, "reason": "git_failed", "detail": _said(statuses)}
    fields = statuses.stdout.split("\0")
    rows: dict[str, dict[str, Any]] = {}
    for i in range(0, len(fields) - 1, 2):
        letter, path = fields[i][:1], fields[i + 1]
        if letter and path:
            rows[path] = {"path": path, "status": letter, "added": None, "removed": None, "binary": False}
    for added, removed, path in numstat_entries(counts.stdout) if counts.exit_code == 0 else []:
        if path in rows:
            rows[path].update(added=added, removed=removed, binary=added is None)
    for path in nul_names(others.stdout) if others.exit_code == 0 else []:
        rows.setdefault(path, {"path": path, "status": "?", "added": None, "removed": None, "binary": False})
    ordered = sorted(rows.values(), key=lambda r: r["path"])
    truncated = len(ordered) > FILES_MAX or statuses.truncated or others.truncated
    shown = ordered[:FILES_MAX]
    if count_new:
        await _count_new(run, where.root, [r for r in shown if r["status"] == "?" and not r["path"].endswith("/")][:count_new])
    return {**found, "files": shown, "truncated": truncated, "reason": "", "detail": ""}


def check_path(path: str) -> str:
    """A path inside the repository, as git prints it. Refused: an absolute one, one that climbs out
    with ``..``, an empty one, or one with a NUL (which would end the argument early)."""
    text = (path or "").strip()
    if not text or "\0" in text or text.startswith("/") or posixpath.isabs(text):
        raise ChangesRefused("a file of the repository, as a path relative to its top folder")
    if ".." in text.split("/"):
        raise ChangesRefused("the path climbs out of the repository")
    return text


def _binary(diff: str) -> bool:
    return any(line.startswith(("Binary files ", "GIT binary patch")) for line in diff.splitlines()[:8])


async def file_diff(run: Runner, folder: str, path: str, *, base: str | None = None, scoped: bool = False) -> dict[str, Any]:
    """One file's unified diff against ``base``; an untracked file is diffed against nothing, so all of
    it reads as added. Cut at :data:`DIFF_MAX_BYTES` with ``truncated`` set; ``binary`` when git
    would only say that the two differ. ``scoped`` refuses a file outside ``folder``.

    The untracked branch reads the file itself (``--no-index``), so it is taken only for a name
    ``ls-files`` lists exactly: git never lists a name behind a symbolic link, which is what keeps
    ``link-to-elsewhere/secret`` from being read through it."""
    name = check_path(path)
    where = await _where(run, folder, base)
    if isinstance(where, dict):
        return {"path": name, "diff": "", "truncated": False, "untracked": False, "binary": False, "reason": where["reason"], "detail": where["detail"]}
    if scoped and not name.startswith(where.prefix):
        raise ChangesRefused("the file is outside this folder")
    tracked = await run(_git("-C", where.root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", where.against, "--", name), DIFF_MAX_BYTES + 4096)
    text, untracked, cut = tracked.stdout, False, tracked.truncated
    if not text.strip():
        listed = await run(_git("-C", where.root, "ls-files", "--others", "--exclude-standard", "-z", "--", name), SMALL_OUTPUT)
        if name in nul_names(listed.stdout):
            # --no-index exits 1 when the two differ, which for a new file is always: that is the answer, not a failure.
            fresh = await run(_git("-C", where.root, "diff", "--no-ext-diff", "--no-textconv", "--no-index", "--no-color", "--", "/dev/null", name), DIFF_MAX_BYTES + 4096)
            text, untracked, cut = fresh.stdout, True, fresh.truncated
    if len(text.encode()) > DIFF_MAX_BYTES:
        text = text.encode()[:DIFF_MAX_BYTES].decode("utf-8", "ignore")
        cut = True
    return {"path": name, "diff": text, "truncated": cut, "untracked": untracked, "binary": _binary(text), "reason": "", "detail": ""}


async def tracking(run: Runner, where: Where) -> dict[str, Any]:
    """The branch's upstream and how far the branch is ahead of it and behind it; None for each when
    the branch follows nothing (or there is no commit yet). Read from the refs already fetched: this
    never asks a remote."""
    if not where.head:
        return {"upstream": None, "ahead": None, "behind": None}
    upstream, counts = await asyncio.gather(
        run(_git("-C", where.root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"), SMALL_OUTPUT),
        run(_git("-C", where.root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"), SMALL_OUTPUT),
    )
    parts = counts.stdout.split()
    if upstream.exit_code != 0 or counts.exit_code != 0 or len(parts) != 2 or not all(p.isdigit() for p in parts):
        return {"upstream": None, "ahead": None, "behind": None}
    return {"upstream": upstream.stdout.strip(), "ahead": int(parts[1]), "behind": int(parts[0])}


async def listing(run: Runner, folder: str, *, count_new: int = 0) -> dict[str, Any]:
    """A chat's Changes tab: :func:`changed_files` kept to ``folder``, against the last commit, with
    the commit checked out, the upstream and the totals. ``repository`` is False, not an error, for a
    folder outside any repository."""
    where = await _where(run, folder, None)
    if isinstance(where, dict):
        return {"repository": False, "root": "", "prefix": "", "branch": "", "head": "", "upstream": None, "ahead": None, "behind": None,
                "files": [], "added": 0, "removed": 0, "truncated": False, "reason": where["reason"], "detail": where["detail"]}
    found, ahead = await asyncio.gather(_changed(run, where, scoped=True, count_new=count_new), tracking(run, where))
    files = found["files"]
    return {
        "repository": True, "root": where.root, "prefix": where.prefix, "branch": where.branch, "head": where.head, **ahead,
        "files": files, "added": sum(f["added"] or 0 for f in files), "removed": sum(f["removed"] or 0 for f in files),
        "truncated": found["truncated"], "reason": found["reason"], "detail": found["detail"],
    }


async def _chosen(run: Runner, folder: str, paths: list[str] | None, *, doing: str) -> tuple[Where, list[dict[str, Any]]]:
    """The rows of the folder's own listing a revert or a commit acts on: every one for ``None``, else
    exactly the ones named.

    A name is taken only when it is, character for character, a row git listed for this folder now.
    That one rule is the boundary: git never lists a file outside the folder's scope, beyond a
    symbolic link or inside a nested repository, so nothing the operator types can reach one."""
    where = await _where(run, folder, None)
    if isinstance(where, dict):
        raise ChangesBlocked(f"the folder is not in a git repository ({where['detail']})")
    found = await _changed(run, where, scoped=True, count_new=0)
    if found["reason"]:
        raise ChangesBlocked(f"git could not list the changes: {found['detail']}")
    rows = {row["path"]: row for row in found["files"]}
    if paths is None:
        if found["truncated"]:
            raise ChangesBlocked(f"more than {FILES_MAX} files changed, more than the list shows; name the files to {doing}, or do it in a terminal")
        chosen = [row for row in found["files"] if not row["path"].endswith("/")]
    else:
        chosen, seen = [], set()
        for path in paths:
            name = check_path(path)
            if name != path:
                # Stripped, " a.txt" would name a.txt: a different file, and this one is destructive.
                raise ChangesRefused(f"{path!r} has spaces around it; name the file exactly as the list shows it")
            row = rows.get(name)
            if row is None:
                raise ChangesRefused(f"{name} is not a changed file of this folder")
            if name.endswith("/"):
                raise ChangesRefused(f"{name} is a repository of its own inside this one; {doing} it from its own folder")
            if name not in seen:
                seen.add(name)
                chosen.append(row)
    if not chosen:
        raise ChangesBlocked(f"nothing to {doing}: the folder has no changes")
    return where, chosen


async def revert(run: Runner, folder: str, paths: list[str] | None) -> dict[str, Any]:
    """Discard the working-tree changes of ``paths`` (every changed file of ``folder`` for ``None``):
    a tracked file goes back to the last commit, in the index and on disk; a file git does not track
    is deleted. ``deleted`` names every file whose content is gone for good — the untracked ones and
    the ones added since the last commit — and ``restored`` the ones put back as committed.

    git does both halves (``restore``, then ``clean`` without ``-d`` or ``-x``): it refuses a path
    beyond a symbolic link, and never removes a directory or an ignored file."""
    where, chosen = await _chosen(run, folder, paths, doing="revert")
    if not where.head:
        raise ChangesBlocked("the repository has no commit yet, so there is nothing to go back to")
    back = [row["path"] for row in chosen if row["status"] != "?"]
    gone = [row["path"] for row in chosen if row["status"] == "?"]
    if back:
        restored = await run(_git("-C", where.root, "restore", "--source=HEAD", "--staged", "--worktree", "--", *back), SMALL_OUTPUT)
        if restored.exit_code != 0:
            raise ChangesBlocked(f"git did not restore the files, and nothing was deleted: {_said(restored)}")
    if gone:
        cleaned = await run(_git("-C", where.root, "clean", "-f", "-q", "--", *gone), SMALL_OUTPUT)
        if cleaned.exit_code != 0:
            raise ChangesBlocked(f"the tracked files were restored, but git did not delete the new ones: {_said(cleaned)}")
    added = [row["path"] for row in chosen if row["status"] == "A"]
    return {
        "root": where.root, "branch": where.branch, "reverted": [row["path"] for row in chosen],
        "restored": [path for path in back if path not in added], "deleted": sorted(gone + added),
    }


async def commit(run: Runner, folder: str, message: str, paths: list[str] | None) -> dict[str, Any]:
    """Commit ``paths`` (every changed file of ``folder`` for ``None``) with ``message``, as the
    identity the repository's configuration names, and answer the new commit. Only those files go
    into it: what else the index holds stays staged and out of the commit. Nothing is pushed.

    ``user.useConfigOnly`` refuses a guessed identity: without it a container with no git identity
    committed as ``root@<container id>``, an author nobody can tell from a stranger. ``--no-verify``
    beside ``GIT_SAFE``'s empty hooks path, and ``--no-gpg-sign`` because a repository's own
    ``gpg.program`` is a program it names, and a signing prompt would hold the request."""
    text = message.strip()
    if not text or "\0" in text:
        raise ChangesRefused("a commit needs a message")
    where, chosen = await _chosen(run, folder, paths, doing="commit")
    who = await run(_git("-C", where.root, "-c", "user.useConfigOnly=true", "var", "GIT_AUTHOR_IDENT"), SMALL_OUTPUT)
    if who.exit_code != 0:
        raise ChangesBlocked(f"git has no identity to commit as here; set user.name and user.email for the repository ({_said(who)})")
    names = [row["path"] for row in chosen]
    staged = await run(_git("-C", where.root, "add", "-A", "--", *names), SMALL_OUTPUT)
    if staged.exit_code != 0:
        raise ChangesBlocked(f"git did not stage the files: {_said(staged)}")
    made = await run(_git("-C", where.root, "-c", "user.useConfigOnly=true", "commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", text, "--", *names), SMALL_OUTPUT)
    if made.exit_code != 0:
        raise ChangesBlocked(f"git did not commit; the files are staged: {_said(made)}")
    head = await run(_git("-C", where.root, "rev-parse", "--verify", "HEAD"), SMALL_OUTPUT)
    return {"root": where.root, "branch": where.branch, "commit": head.stdout.strip(), "files": names}


__all__ = [
    "DIFF_MAX_BYTES", "FILES_MAX", "GIT_ENV", "ChangesBlocked", "ChangesRefused", "Runner", "Where",
    "changed_files", "check_path", "commit", "file_diff", "listing", "revert", "tracking",
]
