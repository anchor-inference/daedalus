"""Git-backed workspace checkpoints.

Every session workspace keeps a hidden repository (``.checkpoints``) whose work tree is
the workspace itself. A snapshot is taken before every operator turn and after every run,
so "undo the last three turns" can put the files back as well as the history. The
repository is derived data: deleting ``.checkpoints`` loses nothing but the undo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from daedalus.host.gitrun import child_environment, git_argv

logger = logging.getLogger(__name__)

DIR_NAME = ".checkpoints"
EXCLUDES = (DIR_NAME + "/", ".agents/", "node_modules/", ".venv/", "__pycache__/", "*.pyc")
SKIP_DIRS = {DIR_NAME, ".agents", "node_modules", ".venv", "__pycache__"}
NESTED_SCAN_DEPTH = 6


class CheckpointError(RuntimeError):
    pass


class Checkpoints:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.git_dir = workspace / DIR_NAME

    async def _git(self, *args: str, index: Path | None = None) -> str:
        env = child_environment({
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.workspace),
            "GIT_AUTHOR_NAME": "daedalus",
            "GIT_AUTHOR_EMAIL": "daedalus@localhost",
            "GIT_COMMITTER_NAME": "daedalus",
            "GIT_COMMITTER_EMAIL": "daedalus@localhost",
        })
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        proc = await asyncio.create_subprocess_exec(
            *git_argv(*args), cwd=str(self.workspace), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise CheckpointError(f"git {' '.join(args[:2])} failed: {err.decode('utf-8', 'replace').strip()[:300]}")
        return out.decode("utf-8", "replace")

    async def ensure(self, scan: WorkspaceScan | None = None) -> None:
        if not (self.git_dir / "HEAD").exists():
            self.workspace.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-q")
            await self.relocate()
        await self._write_excludes(scan)
        await self._forget_private_files()

    async def _forget_private_files(self, *, index: Path | None = None) -> None:
        # Older snapshots may already track children. Ignore rules alone do not untrack them.
        await self._git("rm", "-rf", "--cached", "--ignore-unmatch", "--", ".agents", ":(glob)**/.agents/**", index=index)

    async def relocate(self) -> None:
        """Forget the work-tree path ``git init`` recorded, so a copied ``.checkpoints`` never points back at its origin."""
        try:
            await self._git("config", "--unset", "core.worktree")
        except CheckpointError:
            pass

    async def _write_excludes(self, scan: WorkspaceScan | None = None) -> list[str]:
        """Exclude derived directories and every nested git repository (a snapshot cannot hold those).

        ``scan`` is the walk the caller has already done; without one this walks again.
        """
        nested = list((scan or await asyncio.to_thread(scan_workspace, self.workspace)).nested)
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = [*EXCLUDES, *(f"/{path}/" for path in nested)]
        text = "\n".join(lines) + "\n"
        if not exclude.exists() or exclude.read_text(encoding="utf-8") != text:
            exclude.write_text(text, encoding="utf-8")
        return nested

    async def snapshot(self, label: str, *, scan: WorkspaceScan | None = None) -> str:
        """Commit the current state of the workspace; returns the commit id.

        ``scan`` is the walk the caller has already done for the size cap: the excludes come
        out of the same pass rather than out of a second one.
        """
        await self.ensure(scan)
        await self._git("add", "-A", "--", ".")
        await self._git("commit", "-q", "--allow-empty", "-m", label[:200])
        return (await self._git("rev-parse", "HEAD")).strip()

    async def restore(self, sha: str, *, record: bool = True) -> list[str]:
        """Put the work tree back to ``sha``: tracked files reset, untracked files removed.

        The branch only ever moves forward: the state before the restore is committed first
        and the restored tree is committed after, so every earlier snapshot stays reachable
        and the restore itself can be undone. Destructive history edits use ``record=False``:
        neither the discarded tree nor the restore is snapshotted. Returns the nested repositories the restore
        could not touch (their contents stay as they are).
        """
        await self.ensure()
        if record:
            await self._git("add", "-A", "--", ".")
            await self._git("commit", "-q", "--allow-empty", "-m", f"before restore to {sha[:12]}")
        # Filter historical trees too, so restoring an older snapshot cannot resurrect or
        # overwrite a child's files. The temporary index never changes the working directory.
        with tempfile.TemporaryDirectory(dir=self.git_dir) as temporary:
            index = Path(temporary) / "index"
            await self._git("read-tree", sha, index=index)
            await self._forget_private_files(index=index)
            tree = (await self._git("write-tree", index=index)).strip()
        await self._git("read-tree", "-u", "--reset", tree)
        await self._git("clean", "-qfd")
        nested = await self._write_excludes()
        if record:
            await self._git("add", "-A", "--", ".")
            await self._git("commit", "-q", "--allow-empty", "-m", f"restored to {sha[:12]}")
        return nested

    async def truncate(self, sha: str) -> bool:
        """Make ``sha`` the oldest snapshot this store keeps, and give the rest back to the filesystem.

        The snapshots of a workspace are one chain of commits, so an older one cannot be dropped by
        deleting a ref — it is an ancestor of every newer one. The boundary is written into the
        store's ``shallow`` file instead, which is the same mechanism a ``git clone --depth`` uses:
        git then reads ``sha`` as a commit without parents, everything before it is unreachable, and
        ``git gc --prune=now`` removes those objects. The shas of the snapshots that stay do not
        change, which matters because the checkpoint rows and the undo point at them.

        Returns False when there is no such commit here and nothing was cut. The packing is
        best-effort: once the boundary is written the older snapshots are gone whether or not the
        objects were collected, and the next pass collects them.
        """
        if not (self.git_dir / "HEAD").exists():
            return False
        try:
            await self._git("cat-file", "-e", f"{sha}^{{commit}}")
        except CheckpointError:
            return False
        # Written beside the file and moved onto it: git takes its own ``shallow.lock`` for this, and
        # a crash halfway through a plain write leaves a truncated boundary that git reads as garbage.
        temporary = self.git_dir / "shallow.new"
        temporary.write_text(sha + "\n", encoding="utf-8")
        os.replace(temporary, self.git_dir / "shallow")
        try:
            await self._git("reflog", "expire", "--expire=now", "--all")
            await self._git("gc", "--prune=now", "--quiet")
        except CheckpointError as exc:
            logger.warning("checkpoints cut at %s but not packed: %s", sha[:12], exc)
        return True

    async def head(self) -> str | None:
        if not (self.git_dir / "HEAD").exists():
            return None
        try:
            return (await self._git("rev-parse", "HEAD")).strip()
        except CheckpointError:
            return None


@dataclass(frozen=True)
class WorkspaceScan:
    """What one walk of a workspace establishes: what a snapshot must exclude, and how big it would be."""

    nested: tuple[str, ...]
    size: int


def scan_workspace(path: Path) -> WorkspaceScan:
    """Walk the workspace once for both answers.

    The snapshot path needed three of these walks before every turn — one to find the nested
    repositories, one to size the tree (which found them again), one to write the excludes
    (which found them a third time) — and on a workspace of a hundred thousand files that was
    most of a second of every turn. The walk is not cached between turns on purpose: the
    excludes are consumed by ``git add -A`` immediately afterwards, and a repository the agent
    cloned during the turn must not be added to the snapshot because an older answer said it
    was not there.
    """
    found: list[str] = []
    total = 0
    base_depth = len(path.parts)
    for root, dirs, files in os.walk(path):
        depth = len(Path(root).parts) - base_depth
        if depth > 0 and depth <= NESTED_SCAN_DEPTH and (".git" in dirs or ".git" in files):
            # A nested repository is neither snapshotted nor counted: git will not take it.
            found.append(Path(root).relative_to(path).as_posix())
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return WorkspaceScan(nested=tuple(sorted(found)), size=total)


__all__ = ["DIR_NAME", "CheckpointError", "Checkpoints", "WorkspaceScan", "scan_workspace"]
