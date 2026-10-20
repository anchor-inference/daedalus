"""Git-backed workspace checkpoints.

Every session workspace has a repository whose work tree is the workspace itself. A snapshot is
taken before every operator turn and after every run, so "undo the last three turns" can put the
files back as well as the history. The repository is derived data: deleting it loses nothing but
the undo.

The repository lives in the host's state directory, one per workspace (:func:`store_dir`), and not
in the workspace. Inside the workspace it was the agent's to write: a ``hooks/pre-commit`` or a
``core.fsmonitor`` planted in it ran as the host at the next snapshot, and a rewritten history could
make the operator's undo put back files the agent chose. The state directory is sealed from the
agent, so the store is out of its reach whatever git does with its configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.host.gitrun import child_environment, git_argv

logger = logging.getLogger(__name__)

EXCLUDES = (".agents/", "node_modules/", ".venv/", "__pycache__/", "*.pyc")
SKIP_DIRS = {".agents", "node_modules", ".venv", "__pycache__"}
NESTED_SCAN_DEPTH = 6
WORKSPACE_FILE = "daedalus-workspace"
"""The file in a store that names its workspace: what retention reads to tell a store whose workspace
was deleted from one whose workspace is only unreachable for now."""


class CheckpointError(RuntimeError):
    pass


def store_dir(root: Path, workspace: Path) -> Path:
    """The store of ``workspace`` under ``root``: the folder's name, for a person reading the
    directory, and a digest of its resolved path, so two folders of one name never share a chain."""
    resolved = workspace.resolve()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-.")[:40] or "workspace"
    return root / f"{name}-{hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()[:16]}"


SESSION_WORKSPACES_SQL = (
    # The folder the session works in: the one its metadata names, or its project's first.
    "SELECT s.id AS id, s.metadata AS metadata, COALESCE("
    "(SELECT f.path FROM project_folders f WHERE f.project_id = s.project_id AND f.id = json_extract(s.metadata, '$.folder_id')), "
    "(SELECT f.path FROM project_folders f WHERE f.project_id = s.project_id ORDER BY f.position LIMIT 1)) AS root "
    "FROM sessions s"
)


def session_workspace(row: Any, workspaces_dir: Path) -> Path:
    """Where a session works, the same three answers ``create_session`` gives, out of a row of
    :data:`SESSION_WORKSPACES_SQL`."""
    if row["root"]:
        return Path(str(row["root"]))
    try:
        named = json.loads(row["metadata"] or "{}").get("workspace")
    except (TypeError, ValueError, AttributeError):
        named = None
    return Path(str(named)) if named else workspaces_dir / str(row["id"])


LEGACY_DIR_NAME = ".checkpoints"


def move_workspace_stores(workspaces: Iterable[Path], root: Path) -> list[Path]:
    """Move every ``<workspace>/.checkpoints`` into its sealed store under ``root``; returns the
    workspaces moved. The one migration of the stores out of the workspaces: a second run finds
    nothing left to move, and a workspace whose store is already in place keeps the copy in the
    workspace for a person to look at rather than lose either."""
    moved: list[Path] = []
    for workspace in dict.fromkeys(workspaces):
        legacy = workspace / LEGACY_DIR_NAME
        try:
            if legacy.is_symlink() or not (legacy / "HEAD").is_file():
                continue
        except OSError:
            continue
        target = store_dir(root, workspace)
        if target.exists():
            logger.warning("checkpoint store of %s is already at %s; %s left where it is", workspace, target, legacy)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        subprocess.run(
            git_argv("config", "--file", str(target / "config"), "--unset", "core.worktree"),
            env=child_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30,
        )
        (target / WORKSPACE_FILE).write_text(str(workspace.resolve()) + "\n", encoding="utf-8")
        moved.append(workspace)
    return moved


class Checkpoints:
    def __init__(self, workspace: Path, root: Path) -> None:
        self.workspace = workspace
        self.git_dir = store_dir(root, workspace)

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
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-q")
            await self.relocate()
        await self._write_excludes(scan)
        await self._forget_private_files()

    async def _forget_private_files(self, *, index: Path | None = None) -> None:
        # Older snapshots may already track children. Ignore rules alone do not untrack them.
        await self._git("rm", "-rf", "--cached", "--ignore-unmatch", "--", ".agents", ":(glob)**/.agents/**", index=index)

    async def relocate(self) -> None:
        """Forget the work-tree path ``git init`` recorded, so a copied store never points back at its
        origin, and name the workspace this store now belongs to."""
        try:
            await self._git("config", "--unset", "core.worktree")
        except CheckpointError:
            pass
        (self.git_dir / WORKSPACE_FILE).write_text(str(self.workspace.resolve()) + "\n", encoding="utf-8")

    async def copy_to(self, workspace: Path, root: Path) -> Checkpoints:
        """This store copied for ``workspace`` (a fork's copy of this workspace), with the copy's
        history the same as this one's; an existing store of ``workspace`` is replaced."""
        target = Checkpoints(workspace, root)
        if (self.git_dir / "HEAD").exists():
            await asyncio.to_thread(_replace_tree, self.git_dir, target.git_dir)
            await target.relocate()
        return target

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


def _replace_tree(source: Path, target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)


__all__ = ["SESSION_WORKSPACES_SQL", "WORKSPACE_FILE", "move_workspace_stores", "session_workspace", "CheckpointError", "Checkpoints", "WorkspaceScan", "scan_workspace", "store_dir"]
