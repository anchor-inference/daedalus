"""Running git (and its companion ``gh``) as a subprocess, with the error a caller can act on.

Self-development and the staff worktrees both drive git in repositories they do not own the shape of,
so both need the same two things from a failed command: the exit code, because some git commands
answer a yes-or-no question with it (``merge-base --is-ancestor``), and the tail of what git said,
with any credential in a remote URL masked before it reaches a log or a chat.

Every repository the host runs git in is one the agent can write: a session workspace, a staff
worktree, a project folder. A repository's own configuration can name programs git runs on its
behalf, so every git command here starts with :data:`GIT_SAFE` and runs with
:func:`child_environment` rather than this process's environment.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Sequence
from pathlib import Path

_TOKEN_RE = re.compile(r"(https?://)[^/@\s]+@")


class GitError(RuntimeError):
    """A git or gh command that failed or timed out; ``returncode`` is None for a timeout."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


GIT_SAFE = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "-c", "core.quotepath=off",
    "--no-pager",
)
"""What goes between ``git`` and its subcommand, everywhere the host runs one.

The first four close the ways a read or a snapshot runs a program the repository names: a
``core.fsmonitor`` script runs on ``git status`` and ``git add``, a hook on ``git commit``, an external
diff on ``git diff``, a pager on anything that prints. A planted ``.git/config`` or ``hooks/pre-commit``
used to execute as the host, with the host's provider and Telegram keys in its environment.
``core.quotepath=off`` is for the readers: with it on, git prints ``отчёт.md`` as
``"\\320\\276..."`` and a list built from that output names a file that does not exist.

What this cannot close is a driver a repository names for one path pattern (a ``filter`` or
``textconv`` of ``.gitattributes``): its name is the repository's choice, so it cannot be switched
off by name. :func:`child_environment` is what limits what such a program could take with it."""

CHILD_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TERM", "TMPDIR", "TEMP", "TMP",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "GH_CONFIG_DIR", "GH_HOST",
    # What a Windows program cannot start without, or finds its profile by.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "HOMEDRIVE", "HOMEPATH",
})
CHILD_ENV_PREFIXES = ("LC_", "GIT_")


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment git (and ``gh``, ``rg``) runs with: what finds the program, the operator's own git
    configuration and the network, and nothing else.

    An allowlist rather than a list of secrets to remove: the host's environment holds provider keys,
    the Telegram token and the keyproxy secret under names nobody enumerates in advance, and a program
    a repository smuggled in through :data:`GIT_SAFE`'s gap would read every one of them. A credential a
    command does need (the GitHub token of a push) is passed in ``extra`` by the caller that needs it."""
    kept = {k: v for k, v in os.environ.items() if k in CHILD_ENV_NAMES or k.startswith(CHILD_ENV_PREFIXES)}
    return {**kept, **(extra or {})}


def git_argv(*args: str) -> list[str]:
    """``git`` with :data:`GIT_SAFE` in front of ``args``: the argv for a git run anywhere, a bridge included."""
    return ["git", *GIT_SAFE, *args]


def nul_names(text: str) -> list[str]:
    """The names of a ``-z`` listing (``ls-files -z``, ``diff --name-only -z``), in order.

    Listings are always read with ``-z``: one name per line breaks on a name that holds a newline, and
    git quotes a name with a quote or a backslash in it even with ``core.quotepath`` off."""
    return [name for name in text.split("\0") if name]


def numstat_entries(text: str) -> list[tuple[int | None, int | None, str]]:
    """``git diff --numstat -z`` as ``(added, removed, path)``; a binary file has None for both counts.

    A rename is a record of its own shape: the counts, an empty path, then the old and the new name
    as two more fields. The new name is the one kept: it is the file that is there to open. Read
    line by line, the same rename came out as ``dir/{old => new}``, a path no file has."""
    fields = text.split("\0")
    out: list[tuple[int | None, int | None, str]] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path:
            path = fields[i + 1] if i + 1 < len(fields) else ""
            i += 2
        if path:
            out.append((int(added) if added.isdigit() else None, int(removed) if removed.isdigit() else None, path))
    return out


_SECRET_ENV = re.compile(r"^(TELEGRAM_.*|KEYPROXY_.*|.*_API_KEY|.*_SECRET|.*_PASSWORD|.*_TOKEN)$")


def setup_environment(extra: dict[str, str]) -> dict[str, str]:
    """The environment a project's setup command runs with: the host's, minus its credentials.

    A setup command is the project's own script (``npm ci``, ``uv sync``); it needs PATH and the
    toolchains, not the bot's Telegram or provider keys in its logs."""
    return {**{k: v for k, v in os.environ.items() if not _SECRET_ENV.match(k)}, **extra}


def mask_credentials(text: str) -> str:
    """Hide the user-and-token part of an ``https://user:token@host`` URL."""
    return _TOKEN_RE.sub(r"\1***@", text)


async def run_command(cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    """Run a git/gh command; returns stdout only, stderr goes into the error message."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=child_environment(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise GitError(f"timed out: {' '.join(cmd)}") from exc
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = mask_credentials((err.decode("utf-8", "replace") + text)[-1500:])
        raise GitError(f"{' '.join(cmd[:3])} failed ({proc.returncode}):\n{detail}", returncode=proc.returncode)
    return text


async def run_git(args: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    """``git <args>`` in ``cwd``, behind :data:`GIT_SAFE`; see :func:`run_command`."""
    return await run_command(git_argv(*args), cwd=cwd, env=env, timeout=timeout)
