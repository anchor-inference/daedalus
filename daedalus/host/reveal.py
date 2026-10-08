"""Showing a folder or a file in the operator's own file manager: Explorer, Finder, or whatever the
Linux desktop opens a directory with.

It is offered only when the file manager is in front of the operator: the installation is native
and the request comes from this same machine. On the server deployment the "file manager" would be
a container's, and a phone reaching a native installation over the network is not sitting at it.

What may be revealed is confined to the folders a session or a project already owns — the caller
passes those roots, and a path that resolves outside them, through a symlink or ``..``, is refused.
Nothing here ever executes the file: a file is selected in its folder, and only a directory is
handed to the platform's opener.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Platform = Literal["windows", "macos", "linux"]
Runner = Callable[[Sequence[str] | str], None]
"""Starts a command and returns at once; the file manager outlives the request that opened it."""


class RevealRefused(Exception):
    """The path is not one this installation will show: outside the allowed folders, or gone."""


def platform_family(platform: str | None = None) -> Platform:
    """The family of the platform the host runs on, which on a native installation is the operator's."""
    name = sys.platform if platform is None else platform
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "macos"
    return "linux"


def is_local_client(host: str | None) -> bool:
    """Whether a request came from this machine: a loopback address, IPv4 or IPv6."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Target:
    path: Path
    directory: bool


def confine(raw: str, roots: Sequence[Path], *, base: Path | None = None) -> Target:
    """``raw`` resolved and checked to lie inside one of ``roots``.

    A relative path is taken from ``base`` (the first root when none is named), an absolute one as it
    is, a leading ``~`` as the home folder. Both sides are resolved before they are compared, so a
    symlink inside a root that points out of it is outside, and a root reached through a symlink
    still contains its own files. The empty path is the base folder itself.
    """
    resolved_roots = [Path(os.path.realpath(root)) for root in roots]
    if not resolved_roots:
        raise RevealRefused("there is no folder to reveal here")
    anchor = Path(os.path.realpath(base)) if base is not None else resolved_roots[0]
    text = raw.strip()
    if "\x00" in text:
        raise RevealRefused("that path is not valid")
    candidate = Path(text).expanduser() if text else anchor
    target = Path(os.path.realpath(candidate if candidate.is_absolute() else anchor / candidate))
    if not any(target == root or root in target.parents for root in resolved_roots):
        raise RevealRefused("that path is outside the folders this can show")
    if not target.exists():
        raise RevealRefused("that path does not exist any more")
    return Target(target, target.is_dir())


def reveal_command(target: Target, platform: Platform) -> Sequence[str] | str:
    """The command that shows ``target``: a folder opened, a file selected in its folder.

    Windows gets a single command line rather than a list: Explorer parses ``/select,`` itself and
    wants the path quoted after the comma, which the list form would escape into ``\\"``. A Windows
    path cannot contain a double quote, and one that does is refused rather than let through.
    """
    path = str(target.path)
    if platform == "windows":
        if '"' in path:
            raise RevealRefused("that path is not valid")
        return f'explorer.exe "{path}"' if target.directory else f'explorer.exe /select,"{path}"'
    if platform == "macos":
        return ["open", path] if target.directory else ["open", "-R", path]
    # xdg-open has no "select this file": it is given the folder, never the file, which it would
    # otherwise open in its default application.
    return ["xdg-open", path if target.directory else str(target.path.parent)]


def start_detached(command: Sequence[str] | str) -> None:
    """Start the file manager and forget it: no shell, no inherited input or output, its own session.

    The child is reaped on a daemon thread, so a Linux host does not collect zombies one per click.
    Explorer exits with 1 even when it worked, so the exit status says nothing and is not read.
    """
    options: dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name != "nt":
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)  # type: ignore[call-overload]
    threading.Thread(target=process.wait, name="reveal-reap", daemon=True).start()


def reveal(target: Target, *, platform: Platform | None = None, runner: Runner = start_detached) -> None:
    """Show ``target`` in the file manager of ``platform`` (the host's own by default)."""
    runner(reveal_command(target, platform or platform_family()))


__all__ = ["Platform", "RevealRefused", "Runner", "Target", "confine", "is_local_client", "platform_family", "reveal", "reveal_command", "start_detached"]
