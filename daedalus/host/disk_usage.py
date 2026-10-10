"""Measuring workspaces and removing what is in them, inside the managed workspaces root only.

Everything here is synchronous filesystem work meant to run in a thread: the guard
(:mod:`daedalus.extensions.disk_guard`) and the routes call it through ``asyncio.to_thread``.

Three rules hold for every walk and every removal, because the trees being walked are the ones an
agent filled without thinking about it:

- A walk never follows a symbolic link and never crosses into another filesystem. A link inside a
  workspace may point at the operator's home or at ``/``; a bind mount may be the operator's real
  folder. Neither is the workspace's to be measured for, and certainly not to be deleted.
- A walk is bounded by a number of directory entries. A runaway workspace is exactly the one with
  millions of files, and a measurement that took as long as the workspace is big would hold a
  thread for minutes; past the bound the size is a lower bound and says so.
- A removal is refused when any component of the path is a link, when the real path is not inside
  the workspace, when the path is the workspace itself, and for the directories the installation
  keeps there (the checkpoint store, the inbox, a ``.git``).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

PROTECTED_NAMES = frozenset({".checkpoints", "inbox"})
"""Top-level directories of a workspace no clean-up removes: the snapshot store revert reads, and
the folder the operator's attachments arrive in."""

SKIP_WHEN_SEARCHING = frozenset({".git", ".checkpoints", "node_modules", ".venv"})
"""Directories the throwaway search does not look inside: nothing there is the agent's scratch, and
a ``node_modules`` holds packages named ``tmp`` that are anything but throwaway."""

SEARCH_DEPTH = 3
"""How deep under a workspace a throwaway directory is looked for. The copies that filled the disk
sat one or two levels down; deeper than this, a match is more likely a library's own ``tmp``."""

GIT_TIMEOUT_SECONDS = 20


@dataclass(slots=True)
class DirStat:
    """One directory's share of a measurement."""

    bytes: int = 0
    files: int = 0
    newest: float = 0.0


@dataclass(slots=True)
class Measurement:
    """A workspace's size, and the sizes of its directories down to ``depth`` levels."""

    bytes: int = 0
    files: int = 0
    newest: float = 0.0
    truncated: bool = False
    entries: int = 0
    dirs: dict[str, DirStat] = field(default_factory=dict)
    """By relative POSIX path (``a`` and ``a/b``); only directories, and only down to the depth asked."""
    measured_at: float = 0.0

    def children(self, parent: str = "") -> list[tuple[str, DirStat]]:
        """The directories directly under ``parent`` (``""`` is the workspace), largest first."""
        depth = 0 if not parent else parent.count("/") + 1
        prefix = parent + "/" if parent else ""
        found = [(path, stat_) for path, stat_ in self.dirs.items() if path.startswith(prefix) and path.count("/") == depth]
        return sorted(found, key=lambda item: item[1].bytes, reverse=True)


def disk_bytes(info: os.stat_result) -> int:
    """What an entry occupies on disk, as ``du`` counts it: blocks, not the apparent length, so a
    sparse file is not counted as full and a directory's own blocks are counted at all."""
    blocks = getattr(info, "st_blocks", None)
    return int(blocks) * 512 if blocks is not None else int(info.st_size)


def changed(info: os.stat_result) -> float:
    """When an entry last changed, for the age rules: the later of its mtime and its ctime.

    The mtime alone lies about copies: ``cp -a``, ``rsync -a`` and an unpacked wheel all carry the
    source's old mtimes, so a repository copied this morning would look weeks old and be swept while
    in use. The ctime is set when the inode is made and cannot be set back by ``touch``."""
    return max(info.st_mtime, info.st_ctime)


def measure(root: Path, *, max_entries: int, depth: int = 2) -> Measurement:
    """Walk ``root`` and add up what it holds, without following links or crossing mounts.

    A file with several hard links is counted once: hard links are what the agent is told to use
    instead of copies, and counting each name would report the saving as no saving at all.
    """
    out = Measurement(measured_at=time.time())
    try:
        top = os.lstat(root)
    except OSError:
        return out
    if not stat.S_ISDIR(top.st_mode):
        return out
    device = top.st_dev
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(str(root), ())]
    while stack:
        path, parts = stack.pop()
        try:
            iterator = os.scandir(path)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                if out.entries >= max_entries:
                    out.truncated = True
                    return out
                out.entries += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_dev != device:
                    continue  # a mount point: somebody else's filesystem
                is_dir = stat.S_ISDIR(info.st_mode)
                size = 0
                if is_dir or info.st_nlink <= 1 or (info.st_dev, info.st_ino) not in seen:
                    if not is_dir and info.st_nlink > 1:
                        seen.add((info.st_dev, info.st_ino))
                    size = disk_bytes(info)
                child = (*parts, entry.name)
                out.bytes += size
                out.newest = max(out.newest, changed(info))
                if not is_dir:
                    out.files += 1
                # The entry counts for every directory above it down to the depth kept; a directory
                # counts for itself too, so an empty one still appears with its own few blocks.
                own = child if is_dir else parts
                for level in range(1, min(len(own), depth) + 1):
                    key = "/".join(own[:level])
                    slot = out.dirs.get(key)
                    if slot is None:
                        slot = out.dirs[key] = DirStat()
                    slot.bytes += size
                    slot.newest = max(slot.newest, changed(info))
                    if not is_dir:
                        slot.files += 1
                if is_dir:
                    stack.append((entry.path, child))
    return out


def matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def find_throwaway(root: Path, patterns: Sequence[str], *, max_depth: int = SEARCH_DEPTH) -> list[Path]:
    """The directories under ``root`` whose name is a throwaway pattern, shallowest first.

    A match is not looked inside: ``_scratch/run1/tmp`` goes with ``_scratch``. Links and other
    filesystems are not entered, and neither are the directories in :data:`SKIP_WHEN_SEARCHING`.
    """
    try:
        device = os.lstat(root).st_dev
    except OSError:
        return []
    found: list[Path] = []
    level: list[Path] = [root]
    for depth in range(1, max_depth + 1):
        following: list[Path] = []
        for directory in level:
            try:
                iterator = os.scandir(directory)
            except OSError:
                continue
            with iterator:
                for entry in iterator:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
                        continue
                    if depth == 1 and entry.name in PROTECTED_NAMES:
                        continue
                    if matches(entry.name, patterns):
                        found.append(Path(entry.path))
                    elif entry.name not in SKIP_WHEN_SEARCHING:
                        following.append(Path(entry.path))
        level = following
    return found


def stale(path: Path, *, older_than: float, max_entries: int) -> tuple[bool, int]:
    """Whether nothing under ``path`` (itself included) changed after ``older_than``, and its size.

    The walk stops at the first recent entry, so a directory in use costs almost nothing to ask
    about; one too big to finish within ``max_entries`` is not called stale, since the part not
    walked may be the part in use.
    """
    try:
        top = os.lstat(path)
    except OSError:
        return False, 0
    if not stat.S_ISDIR(top.st_mode) or changed(top) > older_than:
        return False, 0
    device = top.st_dev
    total = disk_bytes(top)
    entries = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            return False, 0  # an unreadable corner may be the part in use
        with iterator:
            for entry in iterator:
                entries += 1
                if entries > max_entries:
                    return False, total
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if changed(info) > older_than:
                    return False, total
                if info.st_dev != device:
                    continue
                total += disk_bytes(info)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
    return True, total


def enclosing_repository(path: Path, stop_at: Path) -> Path | None:
    """The nearest directory above ``path``, up to and including ``stop_at``, that holds a ``.git``.

    Only the parents are asked. A repository *inside* a throwaway directory is one of the copies
    the guard exists to remove; what must not be touched is a directory a real checkout tracks.
    """
    current = path.parent
    while True:
        if (current / ".git").exists():
            return current
        if current == stop_at or stop_at not in current.parents:
            return None
        current = current.parent


def git_tracked(path: Path, stop_at: Path) -> bool:
    """Whether a repository around ``path`` tracks any file under it.

    When git cannot answer (no git, a broken repository, a timeout) the answer is yes: the caller
    deletes only what is known not to be tracked.
    """
    repo = enclosing_repository(path, stop_at)
    if repo is None:
        return False
    try:
        relative = path.relative_to(repo).as_posix()
        done = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(repo), "ls-files", "--error-unmatch", "--", relative],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return True
    if done.returncode == 0:
        return True
    # 1 is git's "matched nothing"; anything else is a failure, taken as tracked.
    return done.returncode != 1


class Refused(Exception):
    """A path a clean-up will not remove; ``reason`` is the word the app shows a sentence for."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def inside_root(workspace: Path, root: Path) -> bool:
    """Whether ``workspace`` is a real, top-level directory of the managed root (not a link to one)."""
    try:
        if workspace.is_symlink() or not workspace.is_dir():
            return False
        real_root = Path(os.path.realpath(root))
        return Path(os.path.realpath(workspace)).parent == real_root
    except OSError:
        return False


def resolve_target(workspace: Path, relative: str) -> Path:
    """The directory ``relative`` names inside ``workspace``, or :class:`Refused` with the reason.

    Every component is checked with ``lstat`` on the way down, not only the end: a link in the middle
    (``scratch -> /home/operator``) would otherwise make ``scratch/project`` the operator's project.
    """
    pure = PurePosixPath(relative.strip())
    if not relative.strip() or pure.is_absolute() or str(pure) in (".", ""):
        raise Refused("root")
    if any(part in ("..", ".", "") for part in pure.parts):
        raise Refused("outside")
    if pure.parts[0] in PROTECTED_NAMES or pure.name == ".git":
        raise Refused("protected")
    current = workspace
    for part in pure.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise Refused("missing") from None
        except OSError:
            raise Refused("missing") from None
        if stat.S_ISLNK(info.st_mode):
            raise Refused("symlink")
    real = Path(os.path.realpath(current))
    base = Path(os.path.realpath(workspace))
    if base not in real.parents:
        raise Refused("outside")
    return current


def remove(path: Path, *, max_entries: int) -> int:
    """Delete ``path`` (a directory or a file) and return the bytes it freed.

    The size is measured before and after rather than assumed: a file another process holds open,
    or a directory it cannot remove, stays, and the freed figure then says so.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    if stat.S_ISDIR(info.st_mode):
        before = measure(path, max_entries=max_entries, depth=0).bytes + disk_bytes(info)
        shutil.rmtree(path, ignore_errors=True)
        after = measure(path, max_entries=max_entries, depth=0).bytes if path.exists() else 0
        return max(0, before - after)
    try:
        path.unlink()
    except OSError:
        return 0
    return disk_bytes(info)


def free_space(path: Path) -> tuple[int, int]:
    """Free and total bytes on the filesystem holding ``path``; free as an unprivileged writer sees it."""
    try:
        info = os.statvfs(path)
    except OSError:
        return 0, 0
    return info.f_bavail * info.f_frsize, info.f_blocks * info.f_frsize


def human(size: float) -> str:
    """A size the way a person reads it: ``209 GB``, ``5.8 GB``, ``340 MB``."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000:
            return f"{value:.0f} {unit}" if unit == "B" or value >= 100 else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.0f} TB" if value >= 100 else f"{value:.1f} TB"


__all__ = [
    "PROTECTED_NAMES",
    "changed",
    "DirStat",
    "Measurement",
    "Refused",
    "disk_bytes",
    "enclosing_repository",
    "find_throwaway",
    "free_space",
    "git_tracked",
    "human",
    "inside_root",
    "matches",
    "measure",
    "remove",
    "resolve_target",
    "stale",
]
