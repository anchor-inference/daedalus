"""Staff worktrees: one git worktree per staff member per project folder, inside that folder.

A staff member who works in a git folder gets ``<folder>/.agents/worktrees/<staff-slug>`` and a branch
``agent/<staff-slug>/<task-id>-<title-slug>`` cut from the folder's current branch. The worktree lives
inside the project folder rather than in a volume of its own so that the session's walls, the terminal
daemon's allowed roots and a CLI running on the host all name it by the same path, and so that it works
the same in the host environment, which has no such volume. It is reused from task to task — the
dependencies a staff member installed there stay installed — and only switched to the next task's branch
when it is clean.

Git is driven in the operator's own repository, so the rules are conservative:

- nothing is forced: a dirty worktree is refused rather than reset, a branch is deleted with ``-d`` only
  once it is merged, a merge that conflicts is aborted and the folder left as it was;
- the operator's identity is never touched: the staff identity lives in the worktree's own config;
- ``.agents/`` goes into ``info/exclude``, never into the repository's ``.gitignore``;
- operations that change the repository's shared worktree and ref state run one at a time per
  repository, keyed by its common git directory, so two staff members hired into one repository at the
  same moment do not race ``git worktree add`` against each other.

A folder of this process's own environment is driven with local git. A folder of the other environment
(a host folder while Daedalus runs in a container) is driven through the host terminal bridge's
``exec_run``; without the bridge it is refused with the reason, and the staff member stays unassigned
rather than silently working in the shared folder.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from daedalus.host.gitrun import GitError, mask_credentials, run_git
from daedalus.stores.projects import ProjectFolder

logger = logging.getLogger(__name__)

WORKTREES_DIR = Path(".agents") / "worktrees"
BRANCH_PREFIX = "agent/"
TITLE_SLUG_CHARS = 32
STAFF_SLUG_CHARS = 32
IDENTITY = ("daedalus", "daedalus@localhost")
GIT_TIMEOUT = 300.0

# The directories a session writes into the folder it works in: the inbox files arrive in, and the
# per-tool scratch of Exec, its background jobs, the services it hosts, the snapshots, and the staff
# worktrees under ``.agents/``. In a workspace of the session's own they are the whole of the
# directory. In a project they land in the operator's repository, where they have no business showing
# up in `git status` or being swept into a commit by `git add -A`.
# The Board regenerates PLAN.md in the live session workspace; an untracked copy is
# host output, while a tracked edit is still caught by the tracked-file clean check.
SESSION_ARTEFACTS = ("inbox/", ".exec/", ".jobs/", ".services/", ".checkpoints/", ".agents/", "PLAN.md")
EXCLUDE_MARKER = "# daedalus: what an agent working in this folder writes into it"


class WorktreeError(RuntimeError):
    """A worktree operation that did not happen; the message names the folder and what to do."""


class WorktreeRefused(WorktreeError):
    """The folder or the worktree is not in a state this operation may touch: read-only, not a git
    repository, uncommitted changes, a merge that would conflict."""


class WorktreeUnavailable(WorktreeError):
    """The folder is out of this process's reach: a host folder without the host terminal bridge."""


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    """The worktree's root: ``<folder>/.agents/worktrees/<staff-slug>``."""
    branch: str
    base_ref: str
    """What the branch was cut from: the folder's branch at assignment (a commit id when detached)."""
    folder: Path
    env: str
    subdir: str = ""
    """Where the folder sits inside its repository (``git rev-parse --show-prefix``), empty when the
    folder is the repository's top. A worktree checks out the whole repository, so a staff member
    assigned to a sub-folder works in the same sub-folder of its worktree."""

    @property
    def cwd(self) -> Path:
        """Where a staff session in this worktree works."""
        return self.path / self.subdir if self.subdir else self.path


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    dirty: bool
    ahead: int
    """Commits on the branch that its base does not have."""
    behind: int
    """Commits on the base since the branch was cut (or last caught up)."""


@dataclass(frozen=True, slots=True)
class BranchCommit:
    sha: str
    author: str
    at: str
    subject: str


@dataclass(frozen=True, slots=True)
class BranchFile:
    path: str
    added: int | None
    """Lines added; ``None`` for a binary file, which git counts as ``-``."""
    removed: int | None


@dataclass(frozen=True, slots=True)
class BranchComparison:
    """What merging a staff branch into a folder's current branch would bring, read without changing anything."""

    branch: str
    exists: bool
    current: str
    """The folder's branch now (a commit id when detached)."""
    merged: bool
    """Every commit of the branch is already in the folder's branch."""
    clean: bool
    """No uncommitted change to a tracked file in the folder: a merge would not refuse."""
    commits: list[BranchCommit]
    more_commits: bool
    files: list[BranchFile]
    patch: str
    patch_complete: bool
    """False when the patch was cut: some files left out, or the text clipped at its limit."""
    conflicts: list[str] | None
    """The files a merge would conflict in (``[]`` for none); ``None`` when git could not tell."""

    @property
    def added(self) -> int:
        return sum(f.added or 0 for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed or 0 for f in self.files)


_CYRILLIC = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"],
    strict=True,
))


def slug(text: str, *, limit: int) -> str:
    """Lowercase ``[a-z0-9._-]``, at most ``limit`` characters, safe as one component of a git ref.

    Cyrillic is transliterated and accents are dropped, because the operator names staff in Russian as
    often as in English and a name that slugged to nothing would leave every such staff member sharing
    one worktree. Runs of anything else become one ``-``; ``..``, a leading or trailing ``.``/``-`` and a
    trailing ``.lock`` are removed because git refuses them in a ref name.
    """
    text = "".join(_CYRILLIC.get(ch, ch) for ch in text.lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"-{2,}", "-", text)[:limit]
    while True:
        trimmed = text.strip(".-").removesuffix(".lock")
        if trimmed == text:
            return text
        text = trimmed


def staff_slug(name: str) -> str:
    """The directory and branch component for a staff member's name.

    Two names can share a slug ("Anna" and "Анна"), and the slug is what names the worktree, so the
    staff registry refuses to hire a second active member whose slug is taken in the project.
    """
    return slug(name, limit=STAFF_SLUG_CHARS) or "staff"


def branch_name(staff: str, task_id: str, task_title: str) -> str:
    """``agent/<staff-slug>/<task-id>-<title-slug>``; the ``agent/`` ref space is the one a sandboxed
    session in a worktree may write (see ``worktree_writable_paths``)."""
    task = slug(str(task_id), limit=TITLE_SLUG_CHARS) or "task"
    title = slug(task_title, limit=TITLE_SLUG_CHARS)
    return f"{BRANCH_PREFIX}{staff_slug(staff)}/{task}-{title}" if title else f"{BRANCH_PREFIX}{staff_slug(staff)}/{task}"


def exclude_lines(prefix: str = "") -> list[str]:
    """The ``info/exclude`` patterns for a folder at ``prefix`` inside its repository."""
    return [f"/{prefix}{name}" for name in SESSION_ARTEFACTS]


def append_exclude(path: Path, lines: Sequence[str]) -> None:
    """Append to an ``info/exclude`` file the lines it does not have yet, under the marker.

    ``info/exclude`` rather than ``.gitignore``: the ignore file is the repository's and is committed,
    and a project is somebody else's repository — this is a note to their checkout, not a change to
    their project. Lines already there, the operator's or an earlier note's, are left alone, so a note
    written before a new artefact directory existed gains just that line.
    """
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {line.strip() for line in current.splitlines()}
    missing = [line for line in lines if line not in present]
    if not missing:
        return
    head = "" if not current or current.endswith("\n") else "\n"
    marker = "" if EXCLUDE_MARKER in present else EXCLUDE_MARKER + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(head + marker + "".join(line + "\n" for line in missing))


class HostExec(Protocol):
    """What the worktrees need from the host terminal bridge: a git argv run in a folder of ``env``."""

    async def exec_run(self, env: str, argv: list[str], *, cwd: str, env_vars: dict[str, str] | None = None, timeout: float, stdin: bytes | None = None) -> Any: ...


class _Git(Protocol):
    env: str

    async def run(self, args: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None) -> str: ...

    async def ensure_excluded(self, folder: Path) -> None: ...

    def same_path(self, path: str) -> str: ...


# Git must never stop to ask: there is no terminal behind these commands, and a prompt for a password
# or an editor would hang the operation until its timeout.
_QUIET = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}


class LocalGit:
    """Git in a folder of this process's own environment."""

    def __init__(self, env: str) -> None:
        self.env = env

    async def run(self, args: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
        return await run_git(args, cwd=cwd, env={**_QUIET, **(env or {})}, timeout=GIT_TIMEOUT)

    def same_path(self, path: str) -> str:
        return os.path.realpath(path)

    async def ensure_excluded(self, folder: Path) -> None:
        """Add the session artefacts, ``.agents/`` among them, to the repository's ``info/exclude``.

        Resolved through ``--git-path`` rather than ``<folder>/.git``, so it is the shared file when the
        folder is itself a linked worktree, and anchored at the folder's place in the repository when
        the folder is a sub-directory of it. Only the missing lines are appended; the operator's own
        lines are left as they are.
        """
        path = Path((await self.run(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], cwd=folder)).strip())
        prefix = (await self.run(["rev-parse", "--show-prefix"], cwd=folder)).strip()
        try:
            append_exclude(path, exclude_lines(prefix))
        except OSError as exc:
            raise WorktreeError(f"could not write {path}, so the staff worktrees under {folder} would show in its git status: {exc}") from exc


class RemoteGit:
    """Git in a folder of the other environment, through the host terminal bridge's ``exec_run``."""

    def __init__(self, host: HostExec, env: str) -> None:
        self.host = host
        self.env = env

    async def run(self, args: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
        argv = ["git", *args]
        try:
            result = await self.host.exec_run(self.env, argv, cwd=str(cwd), env_vars={**_QUIET, **(env or {})}, timeout=GIT_TIMEOUT)
        except OSError as exc:
            raise WorktreeUnavailable(f"the host terminal bridge did not answer: {exc}") from exc
        stdout, stderr = _text(getattr(result, "stdout", "")), _text(getattr(result, "stderr", ""))
        if getattr(result, "timed_out", False):
            raise GitError(f"timed out: {' '.join(argv)}")
        code = int(getattr(result, "exit_code", 1))
        if code != 0:
            detail = mask_credentials((stderr + stdout)[-1500:])
            raise GitError(f"{' '.join(argv[:3])} failed ({code}):\n{detail}", returncode=code)
        return stdout

    def same_path(self, path: str) -> str:
        return os.path.normpath(path)

    async def ensure_excluded(self, folder: Path) -> None:
        """Check, and only check, that ``.agents/`` is excluded in the host repository.

        The bridge runs git and reads files; it does not write them, and appending to ``info/exclude``
        through a git alias or a crafted patch would be a way around its allowlist rather than a use of
        it. So a host repository whose exclude file lacks the line is refused with the line to add.
        """
        try:
            await self.run(["check-ignore", "-q", f"{WORKTREES_DIR.as_posix()}/"], cwd=folder)
        except GitError as exc:
            if exc.returncode != 1:
                raise
            prefix = (await self.run(["rev-parse", "--show-prefix"], cwd=folder)).strip()
            raise WorktreeUnavailable(
                f"{folder} does not exclude /{prefix}.agents/ from git, and the host bridge cannot write it; "
                f"add the line /{prefix}.agents/ to the repository's .git/info/exclude on the host, then assign again"
            ) from None


def _literal(paths: Sequence[str]) -> list[str]:
    """Paths as literal pathspecs: a file a staff member named ``:(glob)*`` is still one file."""
    return [f":(literal){p}" for p in paths]


_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def _conflicted_names(message: str) -> list[str]:
    """The conflicted files from a failed ``merge-tree --name-only``: the lines of its output after the
    tree id. The error keeps only the tail of the output, so a very long list may lose its start."""
    _, _, detail = message.partition("\n")
    names = [line.strip() for line in detail.splitlines() if line.strip() and not _OID.match(line.strip())]
    return list(dict.fromkeys(names))


def _text(value: Any) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    return str(value or "")


class StaffWorktrees:
    """Create, reuse, commit, inspect, merge and remove staff worktrees."""

    def __init__(self, local_env: str, *, host: HostExec | None = None) -> None:
        self.local_env = local_env
        self.host = host
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # -- plumbing ------------------------------------------------------------------------------

    def _git(self, env: str) -> _Git:
        if env == self.local_env:
            return LocalGit(env)
        if self.host is None:
            raise WorktreeUnavailable("host folders need the host terminal bridge")
        return RemoteGit(self.host, env)

    async def _lock(self, git: _Git, folder: Path) -> asyncio.Lock:
        """One lock per repository, whichever of its folders or worktrees the call came through."""
        common = (await git.run(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=folder)).strip()
        key = (git.env, git.same_path(common))
        return self._locks.setdefault(key, asyncio.Lock())

    async def _registered(self, git: _Git, folder: Path) -> dict[str, dict[str, str]]:
        """The repository's worktrees by path, from ``git worktree list --porcelain``."""
        out = await git.run(["worktree", "list", "--porcelain"], cwd=folder)
        found: dict[str, dict[str, str]] = {}
        for block in out.strip().split("\n\n"):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                fields[key] = value
            if "worktree" in fields:
                found[git.same_path(fields["worktree"])] = fields
        return found

    async def _branch_exists(self, git: _Git, folder: Path, branch: str) -> bool:
        try:
            await git.run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=folder)
        except GitError as exc:
            if exc.returncode == 1:
                return False
            raise
        return True

    async def _is_dirty(self, git: _Git, path: Path, *, untracked: bool = True) -> bool:
        args = ["status", "--porcelain"] + ([] if untracked else ["--untracked-files=no"])
        return bool((await git.run(args, cwd=path)).strip())

    async def _identity(self, git: _Git, path: Path) -> None:
        """Commits in the worktree carry the agent's name, and the operator's own config is not touched.

        ``git config user.name`` in a linked worktree writes the repository's shared config, which is
        the operator's: every commit they make afterwards would be signed ``daedalus``. The per-worktree
        config needs ``extensions.worktreeConfig``, a flag git itself reads and nothing else changes.
        """
        enabled = ""
        try:
            enabled = (await git.run(["config", "--get", "extensions.worktreeConfig"], cwd=path)).strip()
        except GitError as exc:
            if exc.returncode != 1:
                raise
        if enabled.lower() != "true":
            await git.run(["config", "extensions.worktreeConfig", "true"], cwd=path)
        await git.run(["config", "--worktree", "user.name", IDENTITY[0]], cwd=path)
        await git.run(["config", "--worktree", "user.email", IDENTITY[1]], cwd=path)

    # -- the operations ------------------------------------------------------------------------

    async def check(self, folder: ProjectFolder) -> str:
        """Raise :class:`WorktreeRefused` when no worktree can be made in ``folder``, as `prepare` would.

        Asked of git itself, in the folder's own environment: the stored ``is_git`` is known only for
        folders of this process's environment and only for a repository's top, and a host folder or a
        subfolder of a repository reads as "not git" there although a worktree is made in it fine.
        ``WorktreeUnavailable`` when the folder cannot be asked at all."""
        git = self._git(folder.env)
        await self._branchable(git, folder)
        return await self._source_head(git, folder.path)

    async def _source_head(self, git: _Git, folder: Path, *, expected: str | None = None) -> str:
        """Return a clean source commit, refusing a checkout that changed since assignment.

        The source may move while a launch waits in the queue; its branch name alone is not a safe
        base for a later worktree write.
        """
        head = (await git.run(["rev-parse", "HEAD"], cwd=folder)).strip()
        if expected is not None and head != expected:
            raise WorktreeRefused(f"{folder} moved since assignment; assign again from its current commit")
        if await self._is_dirty(git, folder, untracked=False):
            raise WorktreeRefused(f"{folder} has uncommitted changes; commit or stash them before assigning a worktree")
        # The host may already have its own untracked inbox or worktree directory here before it
        # first updates info/exclude. Those are not changes to the operator's source.
        prefix = (await git.run(["rev-parse", "--show-prefix"], cwd=folder)).strip()
        artefacts = [f":(exclude,top){prefix}{name.rstrip('/')}" for name in SESSION_ARTEFACTS]
        others = await git.run(["ls-files", "--others", "--exclude-standard", "--", ":(top)", *artefacts], cwd=folder)
        if others.strip():
            raise WorktreeRefused(f"{folder} has uncommitted changes; commit or stash them before assigning a worktree")
        return head

    async def _branchable(self, git: _Git, folder: ProjectFolder) -> str:
        """The folder's path inside its repository, once it is known that a branch can be cut there."""
        where = folder.path
        if folder.readonly:
            raise WorktreeRefused(f"{where} is read-only, so no worktree can be made in it")
        try:
            top = (await git.run(["rev-parse", "--show-toplevel"], cwd=where)).strip()
            prefix = (await git.run(["rev-parse", "--show-prefix"], cwd=where)).strip()
        except GitError:
            raise WorktreeRefused(f"{where} is not a git repository, so no worktree can be made in it") from None
        if not top:
            raise WorktreeRefused(f"{where} is not a git working tree, so no worktree can be made in it")
        try:
            await git.run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=where)
        except GitError:
            raise WorktreeRefused(f"{where} has no commit yet, so there is nothing to branch a worktree from") from None
        return prefix

    async def prepare(self, folder: ProjectFolder, staff: str, task_id: str, task_title: str, *, expected_head: str | None = None) -> Worktree:
        """The staff member's worktree in ``folder``, on the task's branch, ready to work in.

        Reuses ``<folder>/.agents/worktrees/<staff-slug>`` when it is registered and clean and switches it
        to the task's branch (made from the folder's current branch unless it exists already); creates it
        otherwise. A failed creation is retried once after ``git worktree prune``, which clears a worktree
        whose directory was deleted behind git's back. ``staff`` is the member's name or slug.
        """
        git = self._git(folder.env)
        where = folder.path
        prefix = await self._branchable(git, folder)
        slug_ = staff_slug(staff)
        branch = branch_name(slug_, task_id, task_title)
        path = where / WORKTREES_DIR / slug_
        async with await self._lock(git, where):
            # Nothing in the repository is written until the source is still the one assigned and
            # clean. Pin the commit for branch creation so a later branch move cannot change its base.
            head = await self._source_head(git, where, expected=expected_head)
            await git.ensure_excluded(where)
            await self._source_head(git, where, expected=head)
            try:
                base = (await git.run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=where)).strip()
            except GitError:
                base = (await git.run(["rev-parse", "HEAD"], cwd=where)).strip()
            try:
                await self._place(git, where, path, branch, head)
            except GitError as first:
                logger.info("worktree for %s in %s failed once, pruning and retrying: %s", slug_, where, first)
                try:
                    await git.run(["worktree", "prune"], cwd=where)
                    await self._source_head(git, where, expected=head)
                    await self._place(git, where, path, branch, head)
                except GitError as exc:
                    raise WorktreeError(f"could not prepare the worktree of {slug_} in {where}: {exc}") from exc
            await self._identity(git, path)
        return Worktree(path=path, branch=branch, base_ref=base, folder=where, env=folder.env, subdir=prefix.rstrip("/"))

    async def _place(self, git: _Git, folder: Path, path: Path, branch: str, base: str) -> None:
        entry = (await self._registered(git, folder)).get(git.same_path(str(path)))
        if entry is not None and "prunable" not in entry:
            if await self._is_dirty(git, path):
                raise WorktreeRefused(f"the worktree {path} has uncommitted changes; commit or pause the previous task first")
            if entry.get("branch") == f"refs/heads/{branch}":
                return
            if await self._branch_exists(git, folder, branch):
                await git.run(["switch", branch], cwd=path)
            else:
                await git.run(["switch", "-c", branch, base], cwd=path)
            return
        if await self._branch_exists(git, folder, branch):
            await git.run(["worktree", "add", str(path), branch], cwd=folder)
        else:
            await git.run(["worktree", "add", "-b", branch, str(path), base], cwd=folder)

    async def commit_wip(self, worktree: Worktree, message: str) -> str | None:
        """Commit everything in the worktree on its branch; the new commit's id, or None when clean.

        Hooks are skipped (``--no-verify``): this is the commit a pause makes so that nothing is lost, and
        a lint hook refusing half-done work would leave the pause undone. The work is reviewed before it
        is merged, and hooks run then.
        """
        git = self._git(worktree.env)
        await git.run(["add", "-A"], cwd=worktree.path)
        try:
            await git.run(["diff", "--cached", "--quiet"], cwd=worktree.path)
            return None
        except GitError as exc:
            if exc.returncode != 1:
                raise
        await git.run(["commit", "--no-verify", "--quiet", "-m", message], cwd=worktree.path)
        return (await git.run(["rev-parse", "HEAD"], cwd=worktree.path)).strip()

    async def status(self, worktree: Worktree) -> WorktreeStatus:
        git = self._git(worktree.env)
        dirty = await self._is_dirty(git, worktree.path)
        counts = (await git.run(["rev-list", "--left-right", "--count", f"{worktree.base_ref}...HEAD"], cwd=worktree.path)).split()
        return WorktreeStatus(dirty=dirty, behind=int(counts[0]), ahead=int(counts[1]))

    async def merged(self, folder: ProjectFolder, branch: str, into: str = "HEAD") -> bool:
        """Whether every commit of ``branch`` is already in ``into`` (the folder's current branch by default)."""
        git = self._git(folder.env)
        try:
            await git.run(["merge-base", "--is-ancestor", f"refs/heads/{branch}", into], cwd=folder.path)
        except GitError as exc:
            if exc.returncode == 1:
                return False
            raise
        return True

    async def unmerged(self, folder: ProjectFolder) -> list[str]:
        """The ``agent/`` branches whose work is not in the folder's current branch yet."""
        git = self._git(folder.env)
        out = await git.run(["for-each-ref", "--format=%(refname:short)", "--no-merged", "HEAD", f"refs/heads/{BRANCH_PREFIX}"], cwd=folder.path)
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def current_branch(self, folder: ProjectFolder) -> str:
        """The folder's branch, or its commit id when HEAD is detached."""
        git = self._git(folder.env)
        try:
            return (await git.run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=folder.path)).strip()
        except GitError:
            return (await git.run(["rev-parse", "HEAD"], cwd=folder.path)).strip()

    async def commit_identity(self, folder: ProjectFolder, branch: str | None = None) -> str:
        """Resolve a commit identity without accepting a mutable branch label as review evidence."""
        git = self._git(folder.env)
        ref = f"refs/heads/{branch}" if branch else "HEAD"
        return (await git.run(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=folder.path)).strip()

    async def compare(self, folder: ProjectFolder, branch: str, *, max_commits: int = 50, max_files: int = 100, max_lines: int = 8000, max_chars: int = 200_000) -> BranchComparison:
        """What merging ``branch`` into the folder's current branch would bring, and whether it would conflict.

        Everything is measured against the folder's ``HEAD``, not against the base the branch was cut
        from: the merge brings what the branch has and the folder lacks, and that is what the operator
        is asked to accept. The patch is bounded twice — a lock file or a generated bundle committed by a
        staff member must not become a response of tens of megabytes — first by leaving out the files
        past ``max_files`` or ``max_lines`` changed lines (asking git only for the ones kept), then by
        clipping the text. The conflict check is ``git merge-tree --write-tree``, which computes the
        merge in the object store and never touches the folder or its index.
        """
        git = self._git(folder.env)
        where = folder.path
        ref = f"refs/heads/{branch}"
        current = await self.current_branch(folder)
        clean = not await self._is_dirty(git, where, untracked=False)
        if not await self._branch_exists(git, where, branch):
            return BranchComparison(branch, False, current, False, clean, [], False, [], "", True, None)
        merged = await self.merged(folder, branch)
        log = await git.run(["log", "--no-color", f"--max-count={max_commits + 1}", "--format=%H%x1f%an%x1f%aI%x1f%s", f"HEAD..{ref}"], cwd=where)
        commits = [BranchCommit(*(line.split("\x1f") + ["", "", "", ""])[:4]) for line in log.splitlines() if line.strip()]
        numstat = await git.run(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--numstat", f"HEAD...{ref}"], cwd=where)
        files: list[BranchFile] = []
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                files.append(BranchFile(parts[2], None if parts[0] == "-" else int(parts[0]), None if parts[1] == "-" else int(parts[1])))
        kept: list[str] = []
        lines = 0
        for f in files:
            size = (f.added or 0) + (f.removed or 0)
            if len(kept) >= max_files or (kept and lines + size > max_lines):
                break
            kept.append(f.path)
            lines += size
        patch = ""
        if kept:
            patch = await git.run(["diff", "--no-color", "--no-ext-diff", "--no-textconv", f"HEAD...{ref}", "--", *_literal(kept)], cwd=where)
        complete = len(kept) == len(files) and len(patch) <= max_chars
        conflicts: list[str] | None = []
        if not merged and commits:
            try:
                await git.run(["merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", ref], cwd=where)
            except GitError as exc:
                # Exit 1 is git's "the merge has conflicts"; its output is the tree and then the names.
                # Anything else is a git that could not answer, and the merge is not called safe.
                conflicts = _conflicted_names(str(exc)) if exc.returncode == 1 else None
        return BranchComparison(branch, True, current, merged, clean, commits[:max_commits], len(commits) > max_commits, files, patch[:max_chars], complete, conflicts)

    async def merge(self, folder: ProjectFolder, branch: str, *, message: str = "",
                    expected_head: str | None = None, expected_branch_tip: str | None = None) -> str:
        """Merge ``branch`` into the folder's current branch as a merge commit; the merge commit's id.

        Always ``--no-ff``, even when a fast-forward would do: the merge commit is the record that a
        staff task landed, and reverting it takes the whole task back in one step. A folder with
        uncommitted changes to tracked files is refused before anything runs; a merge that stops on a
        conflict (or on the operator's own ``pre-merge-commit`` hook) is aborted, so the folder is left
        exactly as it was, and refused with git's account.

        The merge commit is the operator's commit in the operator's repository, so it carries their
        identity; only a repository with none at all (a fresh container) gets the agent's, because git
        would otherwise refuse the merge with "tell me who you are".
        """
        if folder.readonly:
            raise WorktreeRefused(f"{folder.path} is read-only; nothing can be merged into it")
        git = self._git(folder.env)
        async with await self._lock(git, folder.path):
            if expected_head is not None:
                actual_head = (await git.run(["rev-parse", "--verify", "HEAD^{commit}"], cwd=folder.path)).strip()
                if actual_head != expected_head:
                    raise WorktreeRefused("the folder HEAD changed after review")
            if expected_branch_tip is not None:
                actual_tip = (await git.run(["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], cwd=folder.path)).strip()
                if actual_tip != expected_branch_tip:
                    raise WorktreeRefused("the staff branch changed after review")
            if await self._is_dirty(git, folder.path, untracked=False):
                raise WorktreeRefused(f"{folder.path} has uncommitted changes; commit or stash them before merging {branch}")
            identity: dict[str, str] = {}
            try:
                await git.run(["var", "GIT_COMMITTER_IDENT"], cwd=folder.path)
            except GitError:
                identity = {"GIT_AUTHOR_NAME": IDENTITY[0], "GIT_AUTHOR_EMAIL": IDENTITY[1], "GIT_COMMITTER_NAME": IDENTITY[0], "GIT_COMMITTER_EMAIL": IDENTITY[1]}
            args = ["merge", "--no-ff", "--no-edit"] + (["-m", message] if message else []) + [branch]
            try:
                await git.run(args, cwd=folder.path, env=identity)
            except GitError as exc:
                try:
                    await git.run(["merge", "--abort"], cwd=folder.path)
                except GitError:
                    pass  # nothing to abort: the merge refused before it started
                raise WorktreeRefused(f"merging {branch} into {folder.path} did not go through and was undone: {exc}") from exc
            return (await git.run(["rev-parse", "HEAD"], cwd=folder.path)).strip()

    async def delete_branch(self, folder: ProjectFolder, branch: str) -> bool:
        """Delete a merged branch with ``-d``; False when git keeps it (unmerged, or checked out in a worktree)."""
        git = self._git(folder.env)
        async with await self._lock(git, folder.path):
            try:
                await git.run(["branch", "-d", branch], cwd=folder.path)
            except GitError:
                return False
            return True

    async def remove(self, worktree: Worktree, *, delete_branch_if_merged: bool) -> bool:
        """Remove the worktree; with ``delete_branch_if_merged``, also its branch once the folder's current
        branch has all of it. Returns whether the branch was deleted.

        An unmerged branch is always kept — it is the only copy of the work — and a dirty worktree is
        refused rather than forced. A worktree whose directory is already gone is pruned.
        """
        git = self._git(worktree.env)
        folder = worktree.folder
        async with await self._lock(git, folder):
            entry = (await self._registered(git, folder)).get(git.same_path(str(worktree.path)))
            if entry is not None and "prunable" not in entry:
                if await self._is_dirty(git, worktree.path):
                    raise WorktreeRefused(f"the worktree {worktree.path} has uncommitted changes; commit them before it is removed")
                await git.run(["worktree", "remove", str(worktree.path)], cwd=folder)
            else:
                await git.run(["worktree", "prune"], cwd=folder)
            if not delete_branch_if_merged or not await self._branch_exists(git, folder, worktree.branch):
                return False
            try:
                await git.run(["merge-base", "--is-ancestor", f"refs/heads/{worktree.branch}", "HEAD"], cwd=folder)
            except GitError as exc:
                if exc.returncode == 1:
                    return False
                raise
            await git.run(["branch", "-d", worktree.branch], cwd=folder)
            return True
