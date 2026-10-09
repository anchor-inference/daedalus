"""The host bridge: the operator's machine as a Docker installation reaches it.

In Docker the agent's container sees nothing of the host but the one directory the host's terminal
daemon keeps its socket in. So everything that has to happen on the host — a folder of a project
checked or made, git run in a staff worktree there — goes through that daemon's side channels, and
only while it answers. This module is the one place the rest of the application asks: whether the
bridge is up, run git there, check a folder there. Natively the host is this process's own
environment and none of it is needed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from daedalus.terminals.model import (
    EnvUnavailable,
    ExecResult,
    FileChunk,
    Forbidden,
    InvalidRequest,
    NotFound,
    TerminalError,
    Unsupported,
)

if TYPE_CHECKING:
    from daedalus.terminals.service import Terminals

HOST = "host"
_T = TypeVar("_T")
GIT_CHECK_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class FolderCheck:
    """What the host says about a folder that is to be a project's: whether it is there, whether it
    can be one, and what was done to it. ``problem`` is empty when it can be used as it is now."""

    path: str
    exists: bool
    is_dir: bool = False
    writable: bool | None = None
    """Whether the operator may write in it; ``None`` when the daemon cannot tell."""
    is_git: bool | None = None
    """Whether it is inside a git work tree; ``None`` when it does not exist or git could not say."""
    created: bool = False
    problem: str = ""

    def view(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "is_dir": self.is_dir, "writable": self.writable, "is_git": self.is_git, "created": self.created, "problem": self.problem}


class _Outdated(OSError):
    """A call the daemon does not know, as ``_side`` raises it: an ``OSError`` with the daemon's
    words like any refusal, so the readers that catch ``OSError`` are unchanged, and a type of its
    own so a caller that can name the remedy does."""


class HostDaemonOutdated(OSError):
    """The host daemon answers but predates a call this version makes. It is its own kind so the
    picker can say "update the host daemon" rather than show the bridge as down or the folder as
    refused: neither is true, and both would send the operator looking in the wrong place."""


OUTDATED_BROWSE = ("the host terminal daemon is older than this version and cannot browse folders; update it "
                   "(bash deploy/host-terminal.sh install, which ends the open host terminals)")


class HostBridge:
    """The host environment through its terminal daemon. The service is looked up on each call,
    because the bridge is installed and removed while this process runs, and the terminals service
    may be installed after whoever holds this object."""

    def __init__(self, terminals: Callable[[], Terminals | None]) -> None:
        self._terminals = terminals

    def _service(self) -> Terminals:
        service = self._terminals()
        if service is None or not service.configured(HOST):
            raise EnvUnavailable("this installation has no host terminal bridge; bash deploy/setup.sh offers to install it", env=HOST, reason="not_configured")
        return service

    def available(self) -> bool:
        """Whether the host daemon answers now — not merely whether it was ever installed."""
        service = self._terminals()
        return service is not None and service.available(HOST)

    async def exec_run(self, env: str, argv: list[str], *, cwd: str, env_vars: dict[str, str] | None = None, timeout: float, stdin: bytes | None = None) -> ExecResult:
        """Run a program of the daemon's list in ``env``, for the staff worktrees' git.

        The worktrees tell "the bridge did not answer" from "git failed" by an ``OSError``, so every
        refusal of the call itself is one; git's own failure is a result with its exit code.
        """
        try:
            return await self._service().exec_run(env, argv, cwd=cwd, env_vars=env_vars, timeout=timeout, stdin=stdin)
        except EnvUnavailable as exc:
            raise ConnectionError(f"the host terminal bridge is not available: {exc.message}") from None
        except TerminalError as exc:
            raise OSError(exc.message) from None

    async def stat(self, path: str) -> dict[str, Any]:
        """``{exists, type, size, …}`` of a path under the host's roots (the host project folders)."""
        return await self._side(lambda service: service.fs_stat(HOST, path))

    async def list_dir(self, path: str, *, limit: int) -> dict[str, Any]:
        """``{entries: [{name, type, size, mtime}], truncated}`` of a directory under the host's roots;
        what is on the daemon's deny list is left out."""
        return await self._side(lambda service: service.fs_list(HOST, path, limit=limit))

    async def browse(self, path: str, *, hidden: bool = False, limit: int = 500) -> dict[str, Any]:
        """The folders of one host directory for the new-project folder picker (see ``fs_browse``):
        not held to the roots, directories only, nothing denied or the daemon's own. A daemon without
        the call is ``HostDaemonOutdated``; down, missing and refused are as ``_side`` says."""
        try:
            return await self._side(lambda service: service.fs_browse(HOST, path, hidden=hidden, limit=limit))
        except _Outdated:
            raise HostDaemonOutdated(OUTDATED_BROWSE) from None

    async def stat_folder(self, path: str) -> dict[str, Any]:
        """``{exists, type, writable, …}`` of a folder that is to be a project's, held to the rules a
        project folder is (``fs.stat`` with ``as_root``), for the picker's check of its choice."""
        return await self._side(lambda service: service.fs_stat(HOST, path, as_root=True))

    async def mkdir(self, path: str, *, actor: str = "operator") -> dict[str, Any]:
        """Make a folder on the host with its parents, under the project-folder rules; the answer is
        ``stat_folder``'s plus ``created``. The service audits it, as every side-channel write."""
        return await self._side(lambda service: service.fs_mkdir(HOST, path, actor=actor))

    async def read(self, path: str, *, offset: int, max_bytes: int) -> FileChunk:
        """Up to ``max_bytes`` of a file under the host's roots, from ``offset``."""
        return await self._side(lambda service: service.fs_read(HOST, path, offset=offset, max_bytes=max_bytes))

    async def write(self, path: str, data: bytes, *, offset: int = 0, actor: str = "system") -> dict[str, Any]:
        """Write into a file of a staff member's inbox on the host (``<folder>/.agents/inbox/…``); see
        ``fs_write``. An existing file at offset 0 is a ``FileExistsError``; a daemon older than the call
        is an ``OSError`` that says to update the host terminal."""
        try:
            return await self._side(lambda service: service.fs_write(HOST, path, data, offset=offset, actor=actor))
        except OSError as exc:
            text = str(exc)
            if "not available in this terminal service" in text:
                raise OSError("the host terminal daemon is older than this version and cannot receive files; update it (bash deploy/host-terminal.sh install, which ends the open host terminals)") from None
            if "the file exists" in text:
                raise FileExistsError(text) from None
            raise

    async def _side(self, call: Callable[[Terminals], Awaitable[_T]]) -> _T:
        """A file read on the host, with the errors the readers tell apart: the bridge being down is a
        ``ConnectionError``, a path that is not there a ``FileNotFoundError``, and any other refusal
        (outside the roots, on the deny list) an ``OSError`` with the daemon's words."""
        try:
            return await call(self._service())
        except EnvUnavailable as exc:
            raise ConnectionError(f"the host terminal bridge is not available: {exc.message}") from None
        except NotFound as exc:
            raise FileNotFoundError(exc.message) from None
        except Unsupported as exc:
            raise _Outdated(exc.message) from None
        except TerminalError as exc:
            raise OSError(exc.message) from None

    async def check_folder(self, path: str, *, create_missing: bool = False, actor: str = "system") -> FolderCheck:
        """Look at a folder on the host that is to be a project's, and make it when asked and missing.

        The daemon holds the path to the rules a root of its file reads is held to: absolute, not the
        filesystem's root, not a folder that holds the home directory, nothing on its deny list. A
        path that breaks one comes back with the reason as ``problem``, not as an error; an error is
        only the bridge itself being unavailable.
        """
        service = self._service()
        try:
            if create_missing:
                stat = await service.fs_mkdir(HOST, path, actor=actor)
            else:
                stat = await service.fs_stat(HOST, path, as_root=True)
        except (Forbidden, InvalidRequest, NotFound) as exc:
            return FolderCheck(path=path, exists=False, problem=exc.message)
        exists = bool(stat.get("exists"))
        is_dir = stat.get("type") == "dir"
        writable = stat.get("writable")
        writable = bool(writable) if writable is not None else None
        problem = ""
        if not exists:
            problem = "the folder does not exist on the host"
        elif not is_dir:
            problem = "the path on the host is not a folder"
        elif writable is False:
            problem = "the folder on the host is not writable by the operator"
        is_git = await self._is_git(service, path) if exists and is_dir else None
        return FolderCheck(path=path, exists=exists, is_dir=is_dir, writable=writable, is_git=is_git, created=bool(stat.get("created")), problem=problem)

    async def _is_git(self, service: Terminals, path: str) -> bool | None:
        try:
            result = await service.exec_run(HOST, ["git", "rev-parse", "--is-inside-work-tree"], cwd=path, env_vars={"GIT_TERMINAL_PROMPT": "0"}, timeout=GIT_CHECK_TIMEOUT)
        except TerminalError:
            return None  # git missing on the host, or not allowed: unknown, which is not "no"
        if result.timed_out:
            return None
        return result.exit_code == 0 and result.stdout.strip() == "true"


__all__ = ["FolderCheck", "HostBridge", "HostDaemonOutdated"]
