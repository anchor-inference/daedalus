"""Running git (and its companion ``gh``) as a subprocess, with the error a caller can act on.

Self-development and the staff worktrees both drive git in repositories they do not own the shape of,
so both need the same two things from a failed command: the exit code, because some git commands
answer a yes-or-no question with it (``merge-base --is-ancestor``), and the tail of what git said,
with any credential in a remote URL masked before it reaches a log or a chat.
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
        env={**os.environ, **(env or {})},
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
    """``git <args>`` in ``cwd``; see :func:`run_command`."""
    return await run_command(["git", *args], cwd=cwd, env=env, timeout=timeout)
