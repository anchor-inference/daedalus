"""Moving a project folder — a chat's or a project's — between the container and the host.

In a Docker installation a folder lives either in the agent's container (under the workspaces root,
made by the installation) or on the operator's machine, which the container reaches only through the
host terminal daemon. Moving one is a copy, a check and a switch, in that order, so the original is
never touched until its copy has been verified and the database points at the copy:

1. Refuse while anything works in the folder: a run in flight or being prepared, a background job, a
   terminal or a service whose directory is in it, a live staff session, another project that has
   the same folder. Then hold new runs off it until the move is over.
2. Copy the whole tree as one tar stream — directories, files with their permission bits and times,
   links that stay inside the folder, ``.git`` like anything else. Container to host, this process
   reads the folder and the daemon unpacks it (``fs.unpack_*``) into a new or empty folder inside the
   host's home; host to container, the daemon packs it (``fs.pack_*``) and this process unpacks it
   under the workspaces root, where a new container folder would be made.
3. Verify: the files, folders, links and bytes counted on disk at the destination are the ones sent,
   and the SHA-256 of the stream is the same at both ends.
4. Switch the folder's row (path and environment) and what names the old path, in one transaction
   (:meth:`ProjectStore.relocate_folder`), and repair git's links to worktrees that moved with it.
5. Only then remove the original: always from the container, since a copy left there would be a
   second, silently diverging folder; from the host only when the operator asked for it, because a
   folder on their machine is often a repository they work in by hand, and keeping it costs nothing
   but disk. The daemon removes it only if it is still exactly what it sent.

A move that fails or is cancelled before the switch leaves the original untouched and removes the
partial copy (the daemon removes its half; a host that stops answering cleans up after its idle
time). Once the switch has begun, the move is finished rather than cancelled.

What a move does not carry, it says: links that point out of the folder, sockets and devices (and,
from the host, files the daemon never reads, such as keys). Snapshots taken in the old place belong to
that place's path and are not carried; the session's history from then on is snapshotted in the new
one. Natively there is no container, and nothing here is offered.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import posixpath
import queue
import secrets
import shutil
import stat
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, BinaryIO

from daedalus.host.gitrun import run_git
from daedalus.slugs import page_slug_from
from daedalus.stores.projects import Project, ProjectError, ProjectFolder, folder_name, normalise_root
from daedalus.terminals.bridge import HostDaemonOutdated

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager
    from daedalus.terminals.bridge import HostBridge

logger = logging.getLogger(__name__)

SEND_CHUNK = 448 << 10
"""What one ``fs.unpack_write`` carries: under the daemon's 512 KiB, base64 and all in one frame."""
READ_CHUNK = 640 << 10
"""What one ``fs.pack_read`` asks for: one reply's worth."""
MAX_ENTRIES = 2_000_000
MAX_BYTES = 64 << 30
"""A folder moved whole is a project, not a disk; the daemon holds the same bounds."""
SKIPPED_SHOWN = 50
QUEUE_CHUNKS = 8
"""Chunks buffered between the thread that reads or writes the tree and the calls to the daemon."""
HOST_PROJECTS = ("Daedalus", "projects")
"""Where on the operator's machine a folder moved out of the container goes unless they choose another
place: ``~/Daedalus/projects/<name>``. The native app keeps the folders it makes in its data folder,
where the operator never has to look; a folder moved to the machine is moved there to be worked on
by hand, so it goes where it can be found, under one folder of its own that holds nothing else."""
KEPT_JOBS = 20
GIT_TIMEOUT = 60.0


class MoveRefused(ValueError):
    """A move cannot start (or cannot switch): the words say why and what to do."""


class MoveFailed(RuntimeError):
    """The copy did not arrive whole; the original is untouched and the partial copy removed."""


class _Stopped(Exception):
    """The other end of a stream gave up; the thread on this end stops."""


# -- the tree -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    rel: str
    kind: str  # "dir", "file", "symlink"
    size: int = 0
    mode: int = 0
    mtime: int = 0
    link: str = ""


@dataclass(frozen=True, slots=True)
class Counts:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes: int = 0

    @classmethod
    def of(cls, raw: dict[str, Any]) -> Counts:
        return cls(int(raw.get("files") or 0), int(raw.get("dirs") or 0), int(raw.get("symlinks") or 0), int(raw.get("bytes") or 0))

    def view(self) -> dict[str, int]:
        return {"files": self.files, "dirs": self.dirs, "symlinks": self.symlinks, "bytes": self.bytes}


@dataclass(slots=True)
class Scan:
    entries: list[Entry] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    skipped_count: int = 0
    counts: Counts = field(default_factory=Counts)


def link_escapes(rel: str, target: str) -> bool:
    """Whether a link at ``rel`` (inside the folder) pointing at ``target`` leads out of it: the
    daemon's rule, so a link this side sends is one the other side takes."""
    if not target or "\0" in target or target.startswith("/") or PureWindowsPath(target).drive or target.startswith("\\"):
        return True
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    return joined == ".." or joined.startswith("../")


def entry_name(name: str) -> str:
    """One name of a stream, checked as the daemon checks it: relative, no ``..`` anywhere; "" is the folder itself."""
    if len(name) > 4096 or "\0" in name:
        raise MoveFailed("an entry name in the stream is too long or holds a NUL")
    trimmed = name.removeprefix("./").rstrip("/")
    if trimmed in ("", "."):
        return ""
    if trimmed.startswith("/"):
        raise MoveFailed(f"the stream holds an absolute name {name!r}")
    if ".." in trimmed.split("/"):
        raise MoveFailed(f"the stream holds a name that climbs out of the folder: {name!r}")
    return posixpath.normpath(trimmed)


def scan_tree(root: Path) -> Scan:
    """Every entry under ``root`` in a stable order, links not followed; what cannot be moved is
    counted and named instead."""
    scan = Scan()
    files = dirs = links = total = 0

    def skip(rel: str, reason: str) -> None:
        scan.skipped_count += 1
        if len(scan.skipped) < SKIPPED_SHOWN:
            scan.skipped.append({"path": rel, "reason": reason})

    pending = [""]
    while pending:
        here = pending.pop()
        directory = root / here if here else root
        with os.scandir(directory) as found:
            names = sorted(found, key=lambda item: item.name)
        below: list[str] = []
        for item in names:
            rel = f"{here}/{item.name}" if here else item.name
            info = item.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                scan.entries.append(Entry(rel, "dir", mode=mode, mtime=int(info.st_mtime)))
                dirs += 1
                below.append(rel)
            elif stat.S_ISREG(info.st_mode):
                scan.entries.append(Entry(rel, "file", size=info.st_size, mode=mode, mtime=int(info.st_mtime)))
                files += 1
                total += info.st_size
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(item.path)
                if link_escapes(rel, target):
                    skip(rel, "link outside the folder")
                    continue
                scan.entries.append(Entry(rel, "symlink", mode=mode, mtime=int(info.st_mtime), link=target))
                links += 1
            else:
                skip(rel, "special file")
                continue
            if len(scan.entries) > MAX_ENTRIES or total > MAX_BYTES:
                raise MoveRefused(f"the folder holds more than {MAX_ENTRIES} entries or {MAX_BYTES >> 30} GiB; a folder that large is moved by hand")
        # Depth first, in name order: the stream lists a folder right after its parent's entries.
        pending.extend(reversed(below))
    scan.counts = Counts(files, dirs, links, total)
    return scan


def count_tree(root: Path) -> Counts:
    """What a folder holds on disk, links not followed: the check of a copy."""
    files = dirs = links = total = 0
    for here, subdirs, names in os.walk(root, followlinks=False):
        for name in subdirs:
            if os.path.islink(os.path.join(here, name)):
                links += 1
            else:
                dirs += 1
        for name in names:
            info = os.lstat(os.path.join(here, name))
            if stat.S_ISLNK(info.st_mode):
                links += 1
            elif stat.S_ISREG(info.st_mode):
                files += 1
                total += info.st_size
    return Counts(files, dirs, links, total)


class _Progress:
    """The bytes of files carried so far, written by the thread on the tree's side."""

    def __init__(self) -> None:
        self.bytes = 0


def _put(chunks: queue.Queue[Any], item: Any, stop: threading.Event) -> None:
    while True:
        if stop.is_set():
            raise _Stopped
        try:
            chunks.put(item, timeout=0.25)
            return
        except queue.Full:
            continue


def _get(chunks: queue.Queue[Any], stop: threading.Event) -> Any:
    while True:
        if stop.is_set():
            raise _Stopped
        try:
            return chunks.get(timeout=0.25)
        except queue.Empty:
            continue


class _ChunkSink:
    """A file object for ``tarfile`` that hands what is written to a queue in chunks."""

    def __init__(self, chunks: queue.Queue[Any], stop: threading.Event) -> None:
        self.chunks = chunks
        self.stop = stop
        self.buffer = bytearray()

    def write(self, data: bytes) -> int:
        self.buffer += data
        while len(self.buffer) >= SEND_CHUNK:
            _put(self.chunks, bytes(self.buffer[:SEND_CHUNK]), self.stop)
            del self.buffer[:SEND_CHUNK]
        return len(data)

    def flush(self) -> None:
        if self.buffer:
            _put(self.chunks, bytes(self.buffer), self.stop)
            self.buffer.clear()


class _ChunkSource:
    """A file object for ``tarfile`` that reads what the daemon sent from a queue."""

    def __init__(self, chunks: queue.Queue[Any], stop: threading.Event) -> None:
        self.chunks = chunks
        self.stop = stop
        self.buffer = bytearray()
        self.ended = False

    def read(self, size: int = -1) -> bytes:
        while not self.ended and (size < 0 or len(self.buffer) < size):
            item = _get(self.chunks, self.stop)
            if item is None:
                self.ended = True
            elif isinstance(item, BaseException):
                raise item
            else:
                self.buffer += item
        if size < 0:
            size = len(self.buffer)
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out


def write_tar(root: Path, scan: Scan, sink: _ChunkSink, progress: _Progress) -> None:
    """The stream of a scanned folder. Each file is opened without following a link and must still
    be a regular file of at least its scanned size: one that shrank or was swapped changed under the
    move, and the stream fails rather than carry something else."""
    with tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT) as tar:  # type: ignore[call-overload]
        for entry in scan.entries:
            info = tarfile.TarInfo(entry.rel)
            info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = entry.mode, entry.mtime, 0, 0, "", ""
            if entry.kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif entry.kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, entry.link
                tar.addfile(info)
            else:
                info.type, info.size = tarfile.REGTYPE, entry.size
                try:
                    fd = os.open(root / entry.rel, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
                except OSError as exc:
                    raise MoveFailed(f"{entry.rel} changed during the move: {exc.strerror}") from None
                with os.fdopen(fd, "rb") as handle:
                    now = os.fstat(handle.fileno())
                    if not stat.S_ISREG(now.st_mode) or now.st_size < entry.size:
                        raise MoveFailed(f"{entry.rel} changed during the move")
                    try:
                        tar.addfile(info, handle)
                    except OSError as exc:
                        raise MoveFailed(f"{entry.rel} could not be read: {exc}") from None
                progress.bytes += entry.size
    sink.flush()


def extract_tar(source: BinaryIO | _ChunkSource, dest: Path, *, total_bytes: int, entries: int, progress: _Progress) -> Counts:
    """Unpack a stream into ``dest``, an empty folder this move made, by the daemon's rules: names
    relative and without ``..``, links only inside the folder, nothing written through a link the
    stream made, directories, files and links only, and never more than was declared."""
    links: set[str] = set()
    modes: list[tuple[Path, int]] = []
    written = 0
    seen = 0
    with tarfile.open(fileobj=source, mode="r|") as tar:  # type: ignore[call-overload]
        for member in tar:
            seen += 1
            if seen > entries:
                raise MoveFailed(f"the stream holds more than the {entries} entries it announced")
            rel = entry_name(member.name)
            if not rel:
                if not member.isdir():
                    raise MoveFailed("the folder itself can only be a directory entry")
                continue
            parent = posixpath.dirname(rel)
            while parent:
                if parent in links:
                    raise MoveFailed(f"{rel} would be written through the link {parent}")
                parent = posixpath.dirname(parent)
            if rel in links:
                raise MoveFailed(f"{rel} is in the stream twice")
            full = dest / rel
            mode = stat.S_IMODE(member.mode)
            if member.isdir():
                full.mkdir(parents=True, exist_ok=True)
                if not stat.S_ISDIR(os.lstat(full).st_mode):
                    raise MoveFailed(f"{rel} is not a directory")
                modes.append((full, mode | 0o700))
            elif member.isreg():
                if member.size < 0 or written + member.size > total_bytes:
                    raise MoveFailed(f"the stream carries more than the {total_bytes} bytes it announced")
                full.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as out:
                    data = tar.extractfile(member)
                    copied = 0
                    while data is not None and (block := data.read(1 << 20)):
                        out.write(block)
                        copied += len(block)
                        progress.bytes += len(block)
                    if copied != member.size:
                        raise MoveFailed(f"{rel} is shorter than the stream said")
                    os.fchmod(out.fileno(), mode | 0o600)
                written += member.size
                os.utime(full, (member.mtime, member.mtime), follow_symlinks=False)
            elif member.issym():
                if link_escapes(rel, member.linkname):
                    raise MoveFailed(f"the link {rel} points out of the folder ({member.linkname})")
                full.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, full)
                links.add(rel)
            else:
                raise MoveFailed(f"{rel} is an entry of a kind a folder is not moved with")
    for path, mode in reversed(modes):
        with contextlib.suppress(OSError):
            os.chmod(path, mode)
    return count_tree(dest)


def worktree_links(root: Path) -> list[str]:
    """Where the linked worktrees of a repository at ``root`` are, as git recorded them: the paths
    ``git worktree repair`` is given after the repository and they have moved together."""
    out: list[str] = []
    base = root / ".git" / "worktrees"
    with contextlib.suppress(OSError):
        for item in sorted(base.iterdir()):
            with contextlib.suppress(OSError):
                gitdir = (item / "gitdir").read_text(encoding="utf-8").strip()
                if gitdir:
                    out.append(str(Path(gitdir).parent))
    return out


def rebase(path: str, old: str, new: str) -> str | None:
    """``path`` moved from under ``old`` to under ``new``, or ``None`` when it was not under ``old``."""
    normal = posixpath.normpath(path)
    if normal == old:
        return new
    if normal.startswith(old.rstrip("/") + "/"):
        return new.rstrip("/") + normal[len(old.rstrip("/")):]
    return None


# -- the jobs -------------------------------------------------------------------------------------

TO_HOST = "to_host"
TO_CONTAINER = "to_container"
CANCELLABLE = ("preparing", "copying", "verifying")


@dataclass(slots=True)
class MoveJob:
    id: str
    project_id: str
    folder_id: str
    direction: str
    source: str
    destination: str
    remove_original: bool
    state: str = "preparing"
    """preparing → copying → verifying → switching → cleaning → done; or failed, cancelled."""
    total_bytes: int = 0
    done_bytes: int = 0
    total_files: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    skipped_count: int = 0
    original_removed: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task: asyncio.Task[None] | None = None
    progress: _Progress = field(default_factory=_Progress)

    @property
    def active(self) -> bool:
        return self.state not in ("done", "failed", "cancelled")

    def view(self) -> dict[str, Any]:
        done = self.total_bytes if self.state == "done" else min(self.progress.bytes, self.total_bytes)
        return {
            "id": self.id, "project_id": self.project_id, "folder_id": self.folder_id, "direction": self.direction,
            "source": self.source, "destination": self.destination, "remove_original": self.remove_original,
            "state": self.state, "active": self.active, "total_bytes": self.total_bytes, "done_bytes": done,
            "total_files": self.total_files, "error": self.error, "notes": list(self.notes), "skipped": list(self.skipped),
            "skipped_count": self.skipped_count, "original_removed": self.original_removed,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class FolderMover:
    """The moves of project folders between the container and the host, one at a time per folder.

    The jobs live in this process: a restart in the middle of one leaves the database pointing at the
    original, which was never touched, and the partial copy is removed by the daemon (host side) after
    its idle time, or is an unreferenced directory under the workspaces root (container side).
    """

    def __init__(self, manager: SessionManager, bridge: Callable[[], HostBridge | None], *, workspaces_dir: Path, publish: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self.manager = manager
        self._bridge = bridge
        self.workspaces_dir = workspaces_dir
        self._publish = publish
        self.jobs: dict[str, MoveJob] = {}

    @property
    def projects(self) -> Any:
        return self.manager.projects

    @property
    def db(self) -> Any:
        return self.manager.db

    def bridge(self) -> HostBridge | None:
        return self._bridge()

    # -- what may move ---------------------------------------------------------------------------

    async def _folder(self, project_id: str, folder_id: str) -> tuple[Project, ProjectFolder]:
        project = await self.projects.get(project_id)
        folder = project.folder(folder_id) if project is not None else None
        if project is None or folder is None:
            raise KeyError(folder_id)
        return project, folder

    def _direction(self, folder: ProjectFolder) -> tuple[str, str]:
        """``(direction, refusal)``: where this folder would go, or why it cannot move at all."""
        if self.projects.local_env != "container":
            return "", "this installation runs on the host without a container; its folders are on the host already"
        if folder.env == "container":
            if not folder.managed:
                return "", "this folder is mounted into the container from the host; it is on the host already, so add that folder as a host folder instead"
            return TO_HOST, ""
        return TO_CONTAINER, ""

    def running_move(self, folder_id: str) -> MoveJob | None:
        return next((job for job in self.jobs.values() if job.folder_id == folder_id and job.active), None)

    async def blockers(self, project: Project, folder: ProjectFolder) -> list[str]:
        """What works in the folder now, in words the operator can act on; empty when nothing does."""
        out: list[str] = []
        at_work = self.manager.sessions_at_work()
        for session in await self.projects.sessions_in_folder(project.id, folder.id):
            if session["id"] in at_work:
                out.append(f"the chat «{session['title'] or session['id']}» is working; stop it or wait for it to finish")
        old = str(folder.path)
        for row in await self.db.fetchall("SELECT id, title, cwd FROM terminals WHERE status = 'running' AND env = ?", (folder.env,)):
            if rebase(str(row["cwd"] or ""), old, old) is not None:
                out.append(f"the terminal «{row['title'] or row['id']}» is open in it; close it first")
        if folder.env == self.projects.local_env:
            for row in await self.db.fetchall("SELECT session_id, name, cwd FROM services WHERE status = 'running'"):
                if rebase(str(row["cwd"] or ""), old, old) is not None:
                    out.append(f"the service {row['name']} runs in it; stop it first")
        rows = await self.db.fetchall(
            "SELECT s.name, ss.folder_id, ss.worktree_path FROM staff_sessions ss JOIN staff s ON s.id = ss.staff_id WHERE ss.ended_at IS NULL")
        for row in rows:
            worktree = str(row["worktree_path"] or "")
            if row["folder_id"] == folder.id or (worktree and rebase(worktree, old, old) is not None):
                out.append(f"the staff member {row['name']} is at work in it; let it finish or stop it")
        holders = await self.projects.holders(folder.path, besides=project.id)
        if holders:
            out.append(f"the folder also belongs to {', '.join(holders)}; moving it would take it from under them, so remove it there first")
        return list(dict.fromkeys(out))

    def _host_problem(self) -> str:
        bridge = self.bridge()
        if bridge is None:
            return "this installation has no host terminal bridge; bash deploy/setup.sh offers to install it"
        return bridge.transfer_problem()

    def _host_paths(self) -> tuple[type[PurePosixPath] | type[PureWindowsPath], str]:
        bridge = self.bridge()
        info = bridge.daemon_info() if bridge is not None else {}
        flavour: type[PurePosixPath] | type[PureWindowsPath] = PureWindowsPath if info.get("os") == "windows" else PurePosixPath
        return flavour, str(info.get("home") or "")

    def _name_of(self, project: Project, folder: ProjectFolder) -> str:
        """The name a moved folder gets: the project's (or chat's) name as a folder name, else the one it has."""
        for raw in (page_slug_from(project.name), folder.label and page_slug_from(folder.label), folder.path.name):
            if not raw:
                continue
            with contextlib.suppress(ProjectError):
                return folder_name(raw)
        return "project"

    async def _free_host_path(self, base: str, flavour: type[PurePosixPath] | type[PureWindowsPath]) -> str:
        bridge = self.bridge()
        assert bridge is not None
        for index in range(1, 100):
            candidate = base if index == 1 else f"{base}-{index}"
            try:
                found = await bridge.stat_folder(candidate)
            except FileNotFoundError:
                return str(flavour(candidate))
            if not found.get("exists"):
                return str(flavour(candidate))
        return base

    def _free_container_path(self, name: str) -> Path:
        for index in range(1, 1000):
            candidate = self.workspaces_dir / (name if index == 1 else f"{name}-{index}")
            if not candidate.exists() and candidate not in self.manager.moving_folders.values():
                return candidate
        raise MoveRefused("no free folder name under the workspaces root")

    async def plan(self, project_id: str, folder_id: str) -> dict[str, Any]:
        """What the move dialog shows before anything happens: where the folder would go and why it
        cannot go now, if it cannot. ``problem`` is a code the app translates; ``detail`` the words."""
        project, folder = await self._folder(project_id, folder_id)
        direction, refusal = self._direction(folder)
        out: dict[str, Any] = {"project_id": project_id, "folder_id": folder_id, "direction": direction, "source": str(folder.path),
                               "env": folder.env, "destination": "", "destination_editable": direction == TO_HOST,
                               "host_home": "", "problem": "", "detail": "", "blockers": [], "job": None}
        running = self.running_move(folder_id)
        if running is not None:
            out["job"] = running.view()
        if refusal:
            return {**out, "problem": "not_movable", "detail": refusal}
        host_problem = self._host_problem()
        if host_problem:
            bridge = self.bridge()
            outdated = bridge is not None and bridge.available()
            return {**out, "problem": "host_outdated" if outdated else "host_down", "detail": host_problem}
        name = self._name_of(project, folder)
        if direction == TO_HOST:
            flavour, home = self._host_paths()
            out["host_home"] = home
            if home:
                out["destination"] = await self._free_host_path(str(flavour(home, *HOST_PROJECTS, name)), flavour)
        else:
            out["destination"] = str(self._free_container_path(name))
        out["blockers"] = await self.blockers(project, folder)
        if out["blockers"]:
            out["problem"], out["detail"] = "busy", "; ".join(out["blockers"])
        return out

    # -- starting and stopping ---------------------------------------------------------------------

    async def start(self, project_id: str, folder_id: str, *, destination: str = "", remove_original: bool = False) -> MoveJob:
        """Check everything that can be checked without copying, hold runs off the folder, and start
        the copy in the background. The checks are made again just before the switch."""
        project, folder = await self._folder(project_id, folder_id)
        direction, refusal = self._direction(folder)
        if refusal:
            raise MoveRefused(refusal)
        if self.running_move(folder_id) is not None or folder_id in self.manager.moving_folders:
            raise MoveRefused("this folder is being moved already")
        problem = self._host_problem()
        if problem:
            raise MoveRefused(problem)
        if direction == TO_HOST:
            target = (destination or "").strip()
            if not target:
                plan = await self.plan(project_id, folder_id)
                target = str(plan.get("destination") or "")
            if not target:
                raise MoveRefused("say where on the host the folder should go")
            try:
                dest_path = normalise_root(target)
            except ProjectError as exc:
                raise MoveRefused(str(exc)) from None
            _, home = self._host_paths()
            if home and (dest_path == Path(home) or Path(home) not in dest_path.parents):
                raise MoveRefused(f"a folder is moved to the host only into a new folder inside the home folder ({home})")
        else:
            dest_path = self._free_container_path(self._name_of(project, folder))
        try:
            await self.projects.check_folder(project_id, str(dest_path))
        except ProjectError as exc:
            raise MoveRefused(str(exc)) from None
        # Held first and checked second, with nothing awaited between the hold and the check of the
        # sessions: a run that starts after this sees the hold, and one that started before is seen
        # at work here.
        self.manager.moving_folders[folder_id] = dest_path
        try:
            found = await self.blockers(project, folder)
        except BaseException:
            self.manager.moving_folders.pop(folder_id, None)
            raise
        if found:
            self.manager.moving_folders.pop(folder_id, None)
            raise MoveRefused("; ".join(found))
        job = MoveJob(id=secrets.token_hex(8), project_id=project_id, folder_id=folder_id, direction=direction,
                      source=str(folder.path), destination=str(dest_path), remove_original=remove_original if direction == TO_CONTAINER else True)
        self.jobs[job.id] = job
        self._forget_old()
        job.task = asyncio.create_task(self._run(job), name=f"folder-move:{job.id}")
        return job

    def cancel(self, job_id: str) -> MoveJob:
        """Stop a move that has not begun its switch; one that has is finished instead."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state not in CANCELLABLE:
            raise MoveRefused("the move is past its copy and is being finished; it cannot be cancelled now")
        if job.task is not None:
            job.task.cancel()
        return job

    def get(self, job_id: str) -> MoveJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _forget_old(self) -> None:
        finished = [job for job in self.jobs.values() if not job.active]
        for job in sorted(finished, key=lambda item: item.finished_at or 0)[:-KEPT_JOBS] if len(finished) > KEPT_JOBS else []:
            self.jobs.pop(job.id, None)

    async def _announce(self, job: MoveJob) -> None:
        if self._publish is None:
            return
        with contextlib.suppress(Exception):
            await self._publish("project.changed", {"change": "folder_moved" if job.state == "done" else f"folder_move_{job.state}", "actor": "operator", "project_id": job.project_id})

    async def _run(self, job: MoveJob) -> None:
        try:
            if job.direction == TO_HOST:
                await self._to_host(job)
            else:
                await self._to_container(job)
            job.state = "done"
        except asyncio.CancelledError:
            job.state, job.error = "cancelled", "the move was cancelled; the folder is where it was"
        except (MoveRefused, MoveFailed, HostDaemonOutdated, ProjectError, OSError) as exc:
            job.state, job.error = "failed", str(exc) or exc.__class__.__name__
            logger.warning("folder move %s of %s failed: %s", job.id, job.source, job.error)
        except Exception as exc:  # noqa: BLE001 — a move that breaks says so on its card rather than vanishing
            job.state, job.error = "failed", f"{exc.__class__.__name__}: {exc}"
            logger.exception("folder move %s of %s failed", job.id, job.source)
        finally:
            self.manager.moving_folders.pop(job.folder_id, None)
            job.finished_at = time.time()
            await self._announce(job)

    async def _switch(self, job: MoveJob, env: str, *, is_git: bool | None) -> None:
        """Check once more that nothing works in the folder, then point everything at the copy."""
        job.state = "switching"
        project, folder = await self._folder(job.project_id, job.folder_id)
        found = await self.blockers(project, folder)
        if found:
            raise MoveRefused("something started working in the folder during the copy: " + "; ".join(found))
        await self.projects.relocate_folder(job.project_id, job.folder_id, path=Path(job.destination), env=env, is_git=is_git)
        project = await self.projects.get(job.project_id)
        for session in await self.projects.sessions_of(job.project_id):
            await self.manager.refresh_metadata(session["id"])
        await self.manager.reload_project(project, job.project_id)

    # -- container to host -----------------------------------------------------------------------

    async def _to_host(self, job: MoveJob) -> None:
        bridge = self.bridge()
        if bridge is None:
            raise MoveRefused(self._host_problem())
        source = Path(job.source)
        scan = await asyncio.to_thread(scan_tree, source)
        job.total_bytes, job.total_files = scan.counts.bytes, scan.counts.files
        job.skipped, job.skipped_count = scan.skipped, scan.skipped_count
        worktrees = await asyncio.to_thread(worktree_links, source)
        job.state = "copying"
        began = await bridge.unpack_begin(job.destination, total_bytes=scan.counts.bytes, entries=len(scan.entries))
        transfer = str(began.get("transfer") or "")
        chunks: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_CHUNKS)
        stop = threading.Event()

        def produce() -> None:
            try:
                write_tar(source, scan, _ChunkSink(chunks, stop), job.progress)
                _put(chunks, None, stop)
            except _Stopped:
                return
            except BaseException as exc:  # noqa: BLE001 — handed to the other end, which raises it
                with contextlib.suppress(_Stopped):
                    _put(chunks, exc, stop)

        producer = asyncio.get_running_loop().run_in_executor(None, produce)
        digest = hashlib.sha256()
        try:
            while True:
                item = await asyncio.to_thread(_get, chunks, stop)
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                digest.update(item)
                await bridge.unpack_write(transfer, item)
            job.state = "verifying"
            arrived = await bridge.unpack_finish(transfer)
            transfer = ""
            got = Counts.of(arrived)
            if got != scan.counts or str(arrived.get("sha256") or "") != digest.hexdigest():
                raise MoveFailed(f"the copy on the host does not match the folder: sent {scan.counts.view()}, found {got.view()}")
        except BaseException:
            stop.set()
            await asyncio.gather(producer, return_exceptions=True)
            await self._discard_host_copy(bridge, transfer, job)
            raise
        await producer
        try:
            check = await bridge.check_folder(job.destination)
            is_git: bool | None = check.is_git
        except Exception:  # noqa: BLE001 — git's answer is a convenience; the folder is there
            is_git = None
        try:
            await self._switch(job, "host", is_git=is_git)
        except BaseException:
            await self._discard_host_copy(bridge, "", job, whole=True)
            raise
        job.state = "cleaning"
        await self._repair_worktrees(job, worktrees, env="host")
        try:
            await asyncio.to_thread(shutil.rmtree, source)
            job.original_removed = True
        except OSError as exc:
            job.notes.append(f"the copy left in the container could not be removed ({exc}); delete {source} by hand")
        if scan.skipped_count:
            job.notes.append(f"{scan.skipped_count} entries were not carried (links that point out of the folder, sockets, devices)")

    async def _discard_host_copy(self, bridge: HostBridge, transfer: str, job: MoveJob, *, whole: bool = False) -> None:
        """Remove the partial copy on the host: through its transfer while it is open, which the daemon
        cleans up; a copy already finished is in no transfer any more and is left for the operator,
        named in the notes, since this side has no other way to delete a host folder."""
        if transfer:
            try:
                await asyncio.shield(bridge.unpack_abort(transfer))
            except Exception as exc:  # noqa: BLE001 — the daemon removes it after its idle time anyway
                job.notes.append(f"the partial copy on the host could not be removed now ({exc}); the host terminal removes it within ten minutes")
        elif whole:
            job.notes.append(f"a complete copy was left at {job.destination} on the host; the folder stays where it was, so delete the copy by hand")

    # -- host to container -----------------------------------------------------------------------

    async def _to_container(self, job: MoveJob) -> None:
        bridge = self.bridge()
        if bridge is None:
            raise MoveRefused(self._host_problem())
        dest = Path(job.destination)
        began = await bridge.pack_begin(job.source)
        transfer = str(began.get("transfer") or "")
        sent = Counts.of(began)
        job.total_bytes, job.total_files = sent.bytes, sent.files
        job.skipped, job.skipped_count = list(began.get("skipped") or []), int(began.get("skipped_count") or 0)
        job.state = "copying"
        chunks: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_CHUNKS)
        stop = threading.Event()
        entries = sent.files + sent.dirs + sent.symlinks

        def consume() -> Counts:
            return extract_tar(_ChunkSource(chunks, stop), dest, total_bytes=sent.bytes, entries=entries, progress=job.progress)

        try:
            await asyncio.to_thread(dest.mkdir, parents=True)
        except FileExistsError:
            with contextlib.suppress(Exception):
                await bridge.pack_close(transfer)
            raise MoveRefused(f"{dest} exists already; try again") from None
        consumer = asyncio.get_running_loop().run_in_executor(None, consume)
        digest = hashlib.sha256()
        try:
            while True:
                data, eof, sha = await bridge.pack_read(transfer, max_bytes=READ_CHUNK)
                if data:
                    digest.update(data)
                    await asyncio.to_thread(_feed, chunks, data, stop, consumer)
                if eof:
                    await asyncio.to_thread(_feed, chunks, None, stop, consumer)
                    break
            job.state = "verifying"
            got = await consumer
            on_disk = await asyncio.to_thread(count_tree, dest)
            if got != sent or on_disk != sent or sha != digest.hexdigest():
                raise MoveFailed(f"the copy in the container does not match the folder: sent {sent.view()}, found {on_disk.view()}")
        except BaseException:
            stop.set()
            await asyncio.gather(consumer, return_exceptions=True)
            await asyncio.to_thread(shutil.rmtree, dest, True)
            with contextlib.suppress(Exception):
                await asyncio.shield(bridge.pack_close(transfer))
            raise
        await asyncio.to_thread(_own_like_siblings, dest, self.workspaces_dir)
        worktrees = await asyncio.to_thread(worktree_links, dest)
        try:
            await self._switch(job, "container", is_git=None)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, dest, True)
            with contextlib.suppress(Exception):
                await asyncio.shield(bridge.pack_close(transfer))
            raise
        job.state = "cleaning"
        await self._repair_worktrees(job, worktrees, env="container")
        if job.remove_original and job.skipped_count:
            job.notes.append(f"the folder on the host was kept: {job.skipped_count} of its entries were not copied (files the host terminal never reads, links out of the folder, sockets)")
        if job.remove_original and not job.skipped_count:
            try:
                await bridge.pack_remove(transfer)
                job.original_removed = True
            except Exception as exc:  # noqa: BLE001 — the copy is in use already; the original only stays
                job.notes.append(f"the folder on the host was kept: {exc}")
        else:
            with contextlib.suppress(Exception):
                await bridge.pack_close(transfer)

    # -- after the switch --------------------------------------------------------------------------

    async def _repair_worktrees(self, job: MoveJob, recorded: list[str], *, env: str) -> None:
        """Point git's links between the repository and its worktrees at their new places. Only the
        worktrees that moved with the folder can be repaired; one elsewhere is named in the notes."""
        if not recorded:
            return
        moved = [new for path in recorded if (new := rebase(path, job.source, job.destination)) is not None]
        outside = [path for path in recorded if rebase(path, job.source, job.destination) is None]
        if outside:
            job.notes.append(f"{len(outside)} git worktrees outside the folder still point at its old place; run git worktree repair in them")
        if not moved:
            return
        argv = ["git", "worktree", "repair", *moved]
        try:
            if env == "host":
                bridge = self.bridge()
                assert bridge is not None
                result = await bridge.exec_run("host", argv, cwd=job.destination, env_vars={"GIT_TERMINAL_PROMPT": "0"}, timeout=GIT_TIMEOUT)
                failed = result.exit_code != 0
                detail = result.stderr.strip()
            else:
                # Through run_git, not with this process's environment: that holds the provider keys, and
                # a git hook in the moved folder would run with them.
                await run_git(argv[1:], cwd=Path(job.destination), env={"GIT_TERMINAL_PROMPT": "0"}, timeout=GIT_TIMEOUT)
                failed, detail = False, ""
        except Exception as exc:  # noqa: BLE001 — the files are moved; the links are a git chore left over
            failed, detail = True, str(exc)
        if failed:
            job.notes.append(f"git worktree repair did not finish ({detail[:200]}); run it in {job.destination}")


def _feed(chunks: queue.Queue[Any], item: Any, stop: threading.Event, consumer: asyncio.Future[Any]) -> None:
    """Hand a chunk to the unpacking thread, giving up when it has stopped (its error is raised when
    it is awaited).

    An unpacker that finished cleanly is not a failure: a streamed tar ends at its first empty block,
    so the unpacker returns before the padding after it and before the end marker arrive. Treating
    that as stopped failed a small folder's move about one time in three, whenever the unpacker won
    the race to the last chunk; what it unpacked is checked against what was sent when it is awaited."""
    while True:
        if consumer.done():
            if consumer.cancelled() or consumer.exception() is not None:
                raise _Stopped
            return
        if stop.is_set():
            raise _Stopped
        try:
            chunks.put(item, timeout=0.25)
            return
        except queue.Full:
            continue


def _own_like_siblings(path: Path, root: Path) -> None:
    """Give a folder copied into the workspaces root the owner and mode of the folders beside it, as
    a new container folder gets (:meth:`ProjectStore._make_root`); best effort."""
    try:
        siblings = sorted(child for child in root.iterdir() if child.is_dir() and child != path)
        template = (siblings[0] if siblings else root).stat()
    except OSError:
        return
    for here, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files, ""]:
            target = os.path.join(here, name) if name else here
            with contextlib.suppress(OSError, AttributeError):
                os.chown(target, template.st_uid, template.st_gid, follow_symlinks=False)


def iter_jobs(mover: FolderMover, *, folder_id: str = "", active_only: bool = False) -> Iterator[MoveJob]:
    for job in sorted(mover.jobs.values(), key=lambda item: item.started_at, reverse=True):
        if folder_id and job.folder_id != folder_id:
            continue
        if active_only and not job.active:
            continue
        yield job


__all__ = ["FolderMover", "MoveFailed", "MoveJob", "MoveRefused", "count_tree", "extract_tar", "scan_tree", "write_tar"]
