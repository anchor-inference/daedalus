"""What the Terminals page's status line says about a terminal beyond its own listing: the git branch
of the folder it is in, and the command-line agent working in it.

Nothing here keeps state of its own about agents. A staff member's model, effort, status and spend
are already kept by the team (``staff``, ``staff_sessions.status`` and ``usage_json``); this only
turns them into the row the app reads. The one thing that is new is the branch, and it is read the
cheapest way that is still true: one ``git status`` per folder, at most once per ``BRANCH_TTL_S``,
in the background, with the last answer served meanwhile. A listing never waits on git.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from daedalus.host.gitrun import git_argv
from daedalus.terminals.model import ExecResult, TerminalError

logger = logging.getLogger(__name__)

BRANCH_TTL_S = 10.0
"""The longest a folder's branch is served without being read again. The app polls the listing every
few seconds; a branch changes by a person's hand, and ten seconds late is still the right branch."""
GIT_TIMEOUT_S = 2.0
"""A status that takes longer is a huge tree or a slow disk; the status line then says nothing rather
than hold a daemon's worker."""
BRANCH_FOLDERS_MAX = 256
"""How many folders are remembered; the oldest read is forgotten first."""
STATUS_OUTPUT_MAX = 16 << 10
"""Enough for the branch header and the first changed path, which is all "dirty" needs."""
GIT_QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

STATUS_ARGV = git_argv("--no-optional-locks", "status", "--porcelain=v2", "--branch", "--untracked-files=normal")
"""``--no-optional-locks`` because this runs beside the operator's own git in the same folder: a plain
``git status`` refreshes the index and takes its lock to do it, and a ``git commit`` typed in the
terminal at that moment fails on ``index.lock``."""

Runner = Callable[[str, list[str], str, float], Awaitable[ExecResult]]
"""Runs ``argv`` in an environment at a folder with a timeout: the terminals service's ``exec_run``."""


def parse_status(stdout: str) -> dict[str, Any] | None:
    """``{branch, detached, dirty}`` from ``git status --porcelain=v2 --branch``; None without a head.

    A detached head is named by its short commit, as a prompt names it. A repository with no commit
    yet still has the branch it will commit to."""
    head = oid = ""
    dirty = False
    for line in stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):].strip()
        elif line and not line.startswith("#"):
            dirty = True
    if not head:
        return None
    if head == "(detached)":
        if not oid or oid == "(initial)":
            return None
        return {"branch": oid[:7], "detached": True, "dirty": dirty}
    return {"branch": head, "detached": False, "dirty": dirty}


@dataclass(slots=True)
class _Folder:
    checked_at: float
    value: dict[str, Any] | None


class BranchCache:
    """The branch of each folder a terminal is in, read in the background and served from memory.

    ``peek`` answers at once with what is known (None before the first read finishes, and for a
    folder that is not a repository) and starts a read when the last one is ``ttl`` old. The time is
    taken when a read starts, so a folder is read at most once per ``ttl`` however slow git is, and
    only one read of a folder runs at a time.
    """

    def __init__(self, run: Runner, *, ttl: float = BRANCH_TTL_S, timeout: float = GIT_TIMEOUT_S, grace: float = 3.0, limit: int = BRANCH_FOLDERS_MAX, clock: Callable[[], float] = time.monotonic) -> None:
        self._run = run
        self.ttl = ttl
        self.timeout = timeout
        self.grace = grace
        """How long past ``timeout`` a read may take before it is given up here: the daemon kills git
        at ``timeout``, and this bounds a daemon that stopped answering at all."""
        self.limit = limit
        self._clock = clock
        self._folders: OrderedDict[tuple[str, str], _Folder] = OrderedDict()
        self._reading: dict[tuple[str, str], asyncio.Task[None]] = {}

    def peek(self, env: str, folder: str) -> dict[str, Any] | None:
        if not folder or not PurePosixPath(folder).is_absolute() and not _windows_absolute(folder):
            return None
        key = (env, folder)
        known = self._folders.get(key)
        now = self._clock()
        if key not in self._reading and (known is None or now - known.checked_at >= self.ttl):
            if known is None:
                known = self._folders[key] = _Folder(now, None)
            else:
                known.checked_at = now
            self._folders.move_to_end(key)
            while len(self._folders) > self.limit:
                old, _ = self._folders.popitem(last=False)
                if (task := self._reading.pop(old, None)) is not None:
                    task.cancel()
            self._reading[key] = asyncio.get_running_loop().create_task(self._read(key), name=f"branch {env}:{folder}")
        return dict(known.value) if known.value is not None else None

    async def _read(self, key: tuple[str, str]) -> None:
        env, folder = key
        value: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self.timeout + self.grace):
                result = await self._run(env, list(STATUS_ARGV), folder, self.timeout)
            if result.exit_code == 0 and not result.timed_out:
                value = parse_status(result.stdout)
        except (TerminalError, TimeoutError) as exc:
            logger.debug("the branch of %s in %s is not known: %s", folder, env, exc)
        except Exception:  # noqa: BLE001 — a status line is never worth a failure anywhere else
            logger.info("the branch of %s in %s could not be read", folder, env, exc_info=True)
        finally:
            self._reading.pop(key, None)
        known = self._folders.get(key)
        if known is not None:
            known.value = value

    async def settle(self) -> None:
        """Wait for the reads under way: for a test, and for a close that wants them done."""
        while self._reading:
            await asyncio.gather(*list(self._reading.values()), return_exceptions=True)

    async def close(self) -> None:
        tasks = list(self._reading.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._reading.clear()


def _windows_absolute(path: str) -> bool:
    """``C:\\work`` or ``C:/work``: the host environment of a Windows machine reports its folders so."""
    return len(path) > 2 and path[1] == ":" and path[2] in "\\/"


# -- agents ------------------------------------------------------------------------------------

AGENT_STATES = {
    "starting": "working", "working": "working",
    "question": "waiting", "permission": "waiting",
    "turn_done_unseen": "done",
    "idle": "idle", "no_signal": "idle", "suspended": "idle", "exited": "idle",
    "error": "error",
}
"""A staff session's status in the four words of the status line, and ``error`` beside them. Silence
(``no_signal``) is rest, not a failure: a CLI that says nothing for a while is usually thinking."""


def usage_view(snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The spend of a staff session as the status line reads it, from its ``usage_json``.

    Each count is None where the CLI did not say it, never a guess: the context meter needs the
    window the CLI itself reported, and a cache share needs the cached count beside the whole prompt
    (``input_tokens`` counts the cached part too)."""
    if not snapshot:
        return None

    def count(name: str) -> int | None:
        value = snapshot.get(name)
        try:
            return int(value) if value is not None and int(value) > 0 else None
        except (TypeError, ValueError):
            return None

    cost = snapshot.get("cost_usd")
    try:
        cost_usd = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    view = {
        "input_tokens": count("input_tokens"),
        "output_tokens": count("output_tokens"),
        "cache_read_tokens": count("cache_read_tokens"),
        "context_tokens": count("context_tokens"),
        "context_window": count("context_window"),
        "cost_usd": cost_usd,
        "source": "metered" if snapshot.get("source") == "metered" else "subscription",
        "windows": [w for w in snapshot.get("windows") or [] if isinstance(w, Mapping)],
        "at": str(snapshot.get("at") or "") or None,
    }
    if all(view[k] is None for k in ("input_tokens", "output_tokens", "context_tokens", "cost_usd")):
        return None
    return view


def staff_agent(*, harness: str, model: str, effort: str, status: str, waiting_for: str, usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """The agent of a staff member's terminal: what the team knows of it, in the status line's shape."""
    return {
        "kind": harness,
        "source": "staff",
        "model": model or None,
        "effort": effort or None,
        "state": AGENT_STATES.get(status, "idle"),
        "status": status,
        "waiting_for": waiting_for or None,
        "usage": usage_view(usage),
    }


AGENT_PROGRAMS = {
    "claude": "claude", "codex": "codex", "grok": "grok", "opencode": "opencode", "pi": "pi",
    "cursor-agent": "cursor", "gemini": "gemini", "aider": "aider",
}
"""The command-line agents a shell's running command is recognised as, by the program's name."""

_MODEL_FLAGS = ("--model", "-m")
_EFFORT_FLAGS = ("--effort", "--reasoning-effort")


def detect_agent(command: str) -> dict[str, Any] | None:
    """The agent a shell's running command starts, read from the command line the shell marked.

    Only what the line says is known: the program, and a model or an effort given as a flag. Its
    state and spend are not (nothing reads its transcript), so they are absent rather than guessed:
    an interactive CLI keeps the shell busy the whole time, and "busy" is not "working"."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    while words and ("=" in words[0] and not words[0].startswith("=") or words[0] in ("env", "exec", "command", "nohup", "time")):
        words.pop(0)
    if words and words[0] in ("npx", "bunx", "pnpx") and len(words) > 1:
        words.pop(0)
    if not words:
        return None
    program = PurePosixPath(words[0]).name.removesuffix(".exe")
    kind = AGENT_PROGRAMS.get(program)
    if kind is None:
        return None
    model = effort = None
    for i, word in enumerate(words[1:], start=1):
        following = words[i + 1] if i + 1 < len(words) else None
        name, _, inline = word.partition("=")
        if name in _MODEL_FLAGS:
            model = inline or following or model
        elif name in _EFFORT_FLAGS:
            effort = inline or following or effort
        elif word == "-c" and following and following.startswith("model_reasoning_effort="):
            effort = following.partition("=")[2].strip("\"'") or effort
    return {"kind": kind, "source": "detected", "model": model, "effort": effort, "state": None, "status": None, "waiting_for": None, "usage": None}


__all__ = ["AGENT_PROGRAMS", "AGENT_STATES", "BRANCH_TTL_S", "GIT_TIMEOUT_S", "BranchCache", "detect_agent", "parse_status", "staff_agent", "usage_view"]
