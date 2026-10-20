"""Looking into a project's folders without touching them: what an orchestrator's ``Peek`` reads through.

An orchestrator may check something itself — a file, a search, what a branch holds — but never
changes anything, and it reads only inside its project's walls. Everything here is read-only by
construction: files are opened for reading, and git runs as an argument vector with every setting
that could make a read run something (a filesystem monitor, a hook, an external diff or text
conversion) switched off, and with the optional index lock disabled so a status does not write.

A folder of this process's own environment is read directly (:class:`LocalFolderAccess`). A host
folder seen from the agent's container is read through the host terminal bridge
(:class:`BridgedFolderAccess`): the daemon's ``fs.*`` side channels for files and listings, and its
``exec.run`` for git — searching included, as ``git grep``, because git is on the daemon's program list
and ripgrep is not. The bounds are the same on both sides, and the daemon adds its own walls: only
the host project folders are its roots, and its deny list keeps credentials out of every read. A
folder that cannot be read either way answers with the reason (:class:`UnreachableFolder`); all three
are a :class:`FolderAccess`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import io
import os
import posixpath
import re
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Protocol, TypeVar

from daedalus.host.gitrun import GIT_SAFE, GitError, run_command

_T = TypeVar("_T")

OUTPUT_MAX_CHARS = 12_000
"""What one Peek may return. The orchestrator pays for every character on every later turn of its run."""
READ_MAX_LINES = 400
LINE_MAX_CHARS = 2000
LIST_MAX_ENTRIES = 300
FIND_MAX = 300
SEARCH_MAX = 200
GIT_LOG_MAX = 100
GIT_TIMEOUT_SECONDS = 20.0
SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".agents"})
BRIDGE_READ_CHUNK = 256 << 10
BRIDGE_READ_MAX_BYTES = 8 << 20
"""How far into a host file a bridged read goes looking for the lines asked for. Locally the file is
streamed; through the bridge every chunk is a round trip, so a window deep in a huge log is refused
with a sentence instead of costing hundreds of them."""
FIND_DIRS_MAX = 400
"""Directories a bridged ``find`` lists before it stops: each is a round trip to the host."""
SEARCH_COLUMNS = 300

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^:+-]*$")
"""A revision or a range. It may not begin with ``-``: a ref is an argument, and one that begins
with a dash is an option (``--output=<file>`` would make a read write)."""

# Every git call starts with the host's safe prefix: each part of it closes a way a read-only command
# can run a program in a repository whose configuration the orchestrator does not control.
_GIT_SAFE = ("git", *GIT_SAFE)
_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


class PeekRefused(ValueError):
    """What Peek will not do, in words the orchestrator can act on."""


def clip(text: str, limit: int = OUTPUT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[… {len(text) - limit} more characters; narrow the path, the pattern or the range]"


def numbered(window: Sequence[str], start: int, count: int) -> str:
    """Lines ``start+1 …`` as Peek shows them, and where to continue when there are more."""
    more = len(window) > count
    lines = [f"{start + i + 1:>6}\t{line.rstrip(chr(10))[:LINE_MAX_CHARS]}" for i, line in enumerate(window[:count])]
    tail = f"\n[more lines follow; continue with offset={start + count + 1}]" if more else ""
    return clip("\n".join(lines) or "(empty)") + tail


def text_window(data: bytes, name: str, *, offset: int = 1, limit: int = 200) -> str:
    """Lines of a kept file (an attachment, a staff member's artifact) as Peek shows a folder's: the
    same numbering and bounds. A binary file is described, not printed: it can still be handed on."""
    if b"\x00" in data[:8192]:
        raise PeekRefused(f"{name} is a binary file ({len(data)} bytes); it cannot be shown here, but it can be handed on with files=[…]")
    start = max(1, int(offset)) - 1
    count = max(1, min(int(limit), READ_MAX_LINES))
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    return numbered(lines[start : start + count + 1], start, count)


def check_ref(ref: str) -> str:
    ref = ref.strip()
    if ref and not _REF_RE.match(ref):
        raise PeekRefused(f"{ref!r} is not a revision Peek accepts")
    return ref


class FolderAccess(Protocol):
    """Read-only access to one folder of a project."""

    async def read(self, path: str, *, offset: int, limit: int) -> str: ...
    async def ls(self, path: str) -> str: ...
    async def find(self, pattern: str, path: str) -> str: ...
    async def search(self, pattern: str, path: str) -> str: ...
    async def git_log(self, ref: str, path: str, *, limit: int) -> str: ...
    async def git_diff(self, ref: str, path: str) -> str: ...
    async def git_status(self) -> str: ...


@dataclass(frozen=True, slots=True)
class UnreachableFolder:
    """A folder this process cannot read; every operation says why."""

    reason: str

    async def _refuse(self) -> str:
        raise PeekRefused(self.reason)

    async def read(self, path: str, *, offset: int, limit: int) -> str:
        return await self._refuse()

    async def ls(self, path: str) -> str:
        return await self._refuse()

    async def find(self, pattern: str, path: str) -> str:
        return await self._refuse()

    async def search(self, pattern: str, path: str) -> str:
        return await self._refuse()

    async def git_log(self, ref: str, path: str, *, limit: int) -> str:
        return await self._refuse()

    async def git_diff(self, ref: str, path: str) -> str:
        return await self._refuse()

    async def git_status(self) -> str:
        return await self._refuse()


class Inside(Protocol):
    """What a session's services answer about a path: may it be read, and is it the installation's own."""

    def contains(self, path: Path, *, write: bool = False) -> bool: ...
    def is_protected(self, path: Path) -> bool: ...


class LocalFolderAccess:
    """A folder of this process's environment, read inside the reader's walls."""

    def __init__(self, root: Path, walls: Inside) -> None:
        self.root = root
        self.walls = walls

    def target(self, path: str) -> Path:
        """``path`` relative to the folder (or absolute), refused outside the walls or inside the installation."""
        candidate = Path(path or ".").expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = Path(os.path.realpath(candidate))
        if not self.walls.contains(resolved):
            raise PeekRefused(f"{candidate} is outside the project's folders")
        if self.walls.is_protected(resolved):
            raise PeekRefused(f"{candidate} is part of the installation, not of the project")
        return resolved

    async def read(self, path: str, *, offset: int = 1, limit: int = 200) -> str:
        target = self.target(path)
        if target.is_dir():
            return await self.ls(path)
        if not target.is_file():
            raise PeekRefused(f"no such file: {target}")
        start = max(1, int(offset)) - 1
        count = max(1, min(int(limit), READ_MAX_LINES))
        return await asyncio.to_thread(self._read_lines, target, start, count)

    @staticmethod
    def _read_lines(target: Path, start: int, count: int) -> str:
        # Streamed: a log of gigabytes is read up to the lines asked for, never whole.
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
            if "\x00" in head:
                raise PeekRefused(f"{target} is a binary file")
            handle.seek(0)
            window = list(islice(handle, start, start + count + 1))
        return numbered(window, start, count)

    async def ls(self, path: str) -> str:
        target = self.target(path)
        if not target.is_dir():
            raise PeekRefused(f"not a directory: {target}")

        def listing() -> str:
            entries = sorted(os.scandir(target), key=lambda e: e.name)
            shown = [e.name + ("/" if e.is_dir() else "") for e in entries[:LIST_MAX_ENTRIES]]
            extra = f"\n[… {len(entries) - LIST_MAX_ENTRIES} more entries]" if len(entries) > LIST_MAX_ENTRIES else ""
            return (f"{target}:\n" + "\n".join(shown) + extra) if shown else f"{target} is empty"

        return clip(await asyncio.to_thread(listing))

    async def find(self, pattern: str, path: str) -> str:
        root = self.target(path)
        if not root.is_dir():
            raise PeekRefused(f"not a directory: {root}")
        wanted = pattern or "*"

        def walk() -> list[str]:
            found: list[str] = []
            for directory, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
                for name in sorted(files):
                    rel = os.path.relpath(os.path.join(directory, name), root)
                    if fnmatch.fnmatch(rel, wanted) or fnmatch.fnmatch(name, wanted):
                        found.append(rel)
                        if len(found) >= FIND_MAX:
                            return found
            return found

        matches = await asyncio.to_thread(walk)
        return clip("\n".join(matches) or "(no matches)")

    async def search(self, pattern: str, path: str) -> str:
        if not pattern:
            raise PeekRefused("search needs a pattern")
        root = self.target(path)
        argv = ["rg", "-n", "--no-heading", "--color", "never", "-m", "20", "--max-columns", "300", "--glob", "!.agents", "-e", pattern, "--", str(root)]
        try:
            out = await run_command(argv, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as exc:
            if exc.returncode == 1:
                return "(no matches)"
            raise PeekRefused(f"search failed: {str(exc)[-300:]}") from exc
        except FileNotFoundError as exc:
            raise PeekRefused("search needs ripgrep, which is not installed here") from exc
        lines = [line.replace(str(root) + "/", "", 1) for line in out.splitlines()[:SEARCH_MAX]]
        return clip("\n".join(lines) or "(no matches)")

    async def _git(self, args: Sequence[str]) -> str:
        try:
            return await run_command([*_GIT_SAFE, *args], cwd=self.root, env=_GIT_ENV, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as exc:
            raise PeekRefused(f"git: {str(exc)[-400:]}") from exc

    def _pathspec(self, path: str) -> list[str]:
        if not path:
            return []
        return ["--", str(self.target(path))]

    async def git_log(self, ref: str, path: str, *, limit: int = 20) -> str:
        count = max(1, min(int(limit), GIT_LOG_MAX))
        revision = check_ref(ref)
        out = await self._git(["log", "--no-decorate", "--no-textconv", "--format=%h %ad %an: %s", "--date=short", f"-n{count}", *([revision] if revision else []), *self._pathspec(path)])
        return clip(out.strip() or "(no commits)")

    async def git_diff(self, ref: str, path: str) -> str:
        revision = check_ref(ref)
        common = ["diff", "--no-ext-diff", "--no-textconv", *([revision] if revision else [])]
        spec = self._pathspec(path)
        stat = await self._git([*common, "--stat", *spec])
        patch = await self._git([*common, *spec])
        if not stat.strip():
            return "(no changes)"
        return clip(stat.strip() + "\n\n" + patch)

    async def git_status(self) -> str:
        out = await self._git(["status", "--short", "--branch"])
        return clip(out.strip() or "(clean)")


class HostFiles(Protocol):
    """What a bridged read needs of the host: :class:`daedalus.terminals.bridge.HostBridge`.

    The bridge being down is a ``ConnectionError``, a missing path a ``FileNotFoundError``, and any
    other refusal of the daemon an ``OSError`` carrying its words."""

    async def stat(self, path: str) -> dict[str, Any]: ...
    async def list_dir(self, path: str, *, limit: int) -> dict[str, Any]: ...
    async def read(self, path: str, *, offset: int, max_bytes: int) -> Any: ...
    async def exec_run(self, env: str, argv: list[str], *, cwd: str, env_vars: dict[str, str] | None = None, timeout: float) -> Any: ...


class BridgedFolderAccess:
    """A host folder read from the agent's container, through the host terminal daemon.

    ``roots`` are the project's folders of that environment: a path outside all of them is refused
    here, before the daemon is asked. The daemon checks again on the path it actually opens — a
    symlink out of a root, a file on its deny list — and its refusal comes back in its own words.
    """

    def __init__(self, root: str, *, roots: Sequence[str], bridge: HostFiles, env: str = "host") -> None:
        self.root = posixpath.normpath(root)
        self.roots = tuple(posixpath.normpath(r) for r in roots) or (self.root,)
        self.bridge = bridge
        self.env = env

    def target(self, path: str) -> str:
        """``path`` relative to the folder (or absolute), refused outside the project's host folders."""
        raw = (path or ".").strip() or "."
        if raw.startswith("~"):
            raise PeekRefused(f"{raw} names a home directory; give a path inside the project's folders")
        candidate = posixpath.normpath(raw if raw.startswith("/") else posixpath.join(self.root, raw))
        if not any(candidate == r or candidate.startswith(r.rstrip("/") + "/") for r in self.roots):
            raise PeekRefused(f"{candidate} is outside the project's folders")
        return candidate

    async def _host(self, pending: Awaitable[_T], path: str) -> _T:
        try:
            return await pending
        except ConnectionError as exc:
            raise PeekRefused(f"{self.root} is on the {self.env}, and {exc}; it can be read again once the host terminal answers") from None
        except FileNotFoundError:
            raise PeekRefused(f"no such file: {path}") from None
        except OSError as exc:
            raise PeekRefused(str(exc) or f"the {self.env} refused to read {path}") from None

    async def _stat(self, path: str) -> dict[str, Any]:
        return await self._host(self.bridge.stat(path), path)

    async def read(self, path: str, *, offset: int = 1, limit: int = 200) -> str:
        target = self.target(path)
        stat = await self._stat(target)
        if not stat.get("exists"):
            raise PeekRefused(f"no such file: {target}")
        if stat.get("type") == "dir":
            return await self.ls(path)
        if stat.get("type") != "file":
            raise PeekRefused(f"{target} is not a file")
        start = max(1, int(offset)) - 1
        count = max(1, min(int(limit), READ_MAX_LINES))
        wanted = start + count + 1
        data = b""
        eof = False
        while not eof and data.count(b"\n") < wanted and len(data) < BRIDGE_READ_MAX_BYTES:
            chunk = await self._host(self.bridge.read(target, offset=len(data), max_bytes=BRIDGE_READ_CHUNK), target)
            if not data and b"\x00" in chunk.data[:8192]:
                raise PeekRefused(f"{target} is a binary file")
            data += chunk.data
            eof = bool(chunk.eof) or not chunk.data
        if not eof and data.count(b"\n") < start:
            raise PeekRefused(f"line {start + 1} of {target} lies beyond the first {BRIDGE_READ_MAX_BYTES >> 20} MB, further than Peek reads on the {self.env}; search for what you need instead")
        window = list(islice(io.StringIO(data.decode("utf-8", errors="replace")), start, start + count + 1))
        return numbered(window, start, count)

    async def ls(self, path: str) -> str:
        target = self.target(path)
        listing = await self._host(self.bridge.list_dir(target, limit=LIST_MAX_ENTRIES), target)
        entries = sorted(listing.get("entries") or [], key=lambda e: str(e.get("name")))
        shown = [str(e.get("name")) + ("/" if e.get("type") == "dir" else "") for e in entries[:LIST_MAX_ENTRIES]]
        extra = "\n[… more entries]" if listing.get("truncated") else ""
        return clip((f"{target}:\n" + "\n".join(shown) + extra) if shown else f"{target} is empty")

    async def find(self, pattern: str, path: str) -> str:
        root = self.target(path)
        wanted = pattern or "*"
        found: list[str] = []
        # Depth first with sorted names, the order os.walk gives the local side.
        stack = [root]
        visited = 0
        while stack and len(found) < FIND_MAX:
            if visited >= FIND_DIRS_MAX:
                found.append(f"[… stopped after {FIND_DIRS_MAX} folders; narrow the path]")
                break
            directory = stack.pop()
            visited += 1
            listing = await self._host(self.bridge.list_dir(directory, limit=5000), directory)
            entries = sorted(listing.get("entries") or [], key=lambda e: str(e.get("name")))
            for entry in entries:
                name = str(entry.get("name"))
                if entry.get("type") == "file":
                    rel = posixpath.relpath(posixpath.join(directory, name), root)
                    if fnmatch.fnmatch(rel, wanted) or fnmatch.fnmatch(name, wanted):
                        found.append(rel)
                        if len(found) >= FIND_MAX:
                            break
            stack.extend(posixpath.join(directory, str(e.get("name"))) for e in reversed(entries) if e.get("type") == "dir" and str(e.get("name")) not in SKIPPED_DIRS)
        return clip("\n".join(found) or "(no matches)")

    async def _run(self, argv: list[str], what: str) -> Any:
        result = await self._host(self.bridge.exec_run(self.env, argv, cwd=self.root, env_vars=dict(_GIT_ENV), timeout=GIT_TIMEOUT_SECONDS), self.root)
        if getattr(result, "timed_out", False):
            raise PeekRefused(f"{what} took longer than {int(GIT_TIMEOUT_SECONDS)} s on the {self.env}; narrow the path or the range")
        return result

    async def search(self, pattern: str, path: str) -> str:
        if not pattern:
            raise PeekRefused("search needs a pattern")
        target = self.target(path)
        spec = posixpath.relpath(target, self.root)
        argv = [*_GIT_SAFE, "grep", "--no-index", "--exclude-standard", "-n", "-I", "-E", "--no-color", "-e", pattern, "--", spec, ":(exclude,glob)**/.agents/**"]
        result = await self._run(argv, "search")
        if result.exit_code == 1 and not result.stderr.strip():
            return "(no matches)"
        if result.exit_code != 0:
            raise PeekRefused(f"search failed: {(result.stderr or result.stdout)[-300:]}")
        lines = [line[:SEARCH_COLUMNS] for line in result.stdout.splitlines()[:SEARCH_MAX]]
        return clip("\n".join(lines) or "(no matches)")

    async def _git(self, args: Sequence[str]) -> str:
        result = await self._run([*_GIT_SAFE, *args], "git")
        if result.exit_code != 0:
            raise PeekRefused(f"git: {(result.stderr or result.stdout)[-400:]}")
        return str(result.stdout)

    def _pathspec(self, path: str) -> list[str]:
        if not path:
            return []
        return ["--", self.target(path)]

    async def git_log(self, ref: str, path: str, *, limit: int = 20) -> str:
        count = max(1, min(int(limit), GIT_LOG_MAX))
        revision = check_ref(ref)
        out = await self._git(["log", "--no-decorate", "--no-textconv", "--format=%h %ad %an: %s", "--date=short", f"-n{count}", *([revision] if revision else []), *self._pathspec(path)])
        return clip(out.strip() or "(no commits)")

    async def git_diff(self, ref: str, path: str) -> str:
        revision = check_ref(ref)
        common = ["diff", "--no-ext-diff", "--no-textconv", *([revision] if revision else [])]
        spec = self._pathspec(path)
        stat = await self._git([*common, "--stat", *spec])
        patch = await self._git([*common, *spec])
        if not stat.strip():
            return "(no changes)"
        return clip(stat.strip() + "\n\n" + patch)

    async def git_status(self) -> str:
        out = await self._git(["status", "--short", "--branch"])
        return clip(out.strip() or "(clean)")


__all__ = ["BridgedFolderAccess", "FolderAccess", "HostFiles", "LocalFolderAccess", "PeekRefused", "UnreachableFolder", "check_ref", "clip", "numbered", "text_window"]
