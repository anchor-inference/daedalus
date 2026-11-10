"""Whether the sandbox Exec runs in can be made right now, what is wrong when it cannot, and who is told.

The sandbox is bubblewrap, and bubblewrap depends on the machine around it: namespaces, a seccomp profile,
and a ``/proc`` that belongs to this process's own PID namespace. The last one is the failure this module
was written for. Something outside the container entered its mount namespace alone (``nsenter -t <pid> -m``)
and mounted a fresh proc over ``/proc``; a proc filesystem shows the PID namespace of whoever mounted it, so
the container's ``/proc`` showed the host's processes from then on. bubblewrap reads the namespaces of the
child it has just made from ``/proc/<child pid>/ns``, by the child's number in the container, which in the
host's view is nobody, and it died with ``bwrap: open /proc/<pid>/ns/ns failed`` before running anything.
Its child stayed behind, blocked for ever on the signal its parent never sent, holding the command's
output, which is what the "a background process still holds its output" note mistook for the agent's.

Nothing here runs a command for the agent. It answers three questions — is ``/proc`` this process's own,
can it be put right, what does the machine look like — and it keeps the one fact several callers share: the
sandbox is down since when and for what reason, so the operator is told once per outage and the agent is
told how long it has lasted.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MNT_DETACH = 2
ASK_OPERATOR_AFTER_SECONDS = 120.0
"""How long Exec may be unavailable before the agent is told to stop and ask rather than wait or do other work."""


def proc_view_problem() -> str:
    """Why the ``/proc`` this process sees is not its own, or an empty string when it is.

    ``/proc/self`` names the reader's process as the mounted proc's PID namespace numbers it, so it equals
    ``getpid()`` exactly when the proc belongs to this process's PID namespace.
    """
    if not sys.platform.startswith("linux"):
        return ""
    try:
        seen = os.readlink("/proc/self")
    except OSError as exc:
        return f"/proc/self cannot be read here ({exc.strerror})"
    if seen != str(os.getpid()):
        return (
            f"the /proc this process sees belongs to another PID namespace (/proc/self is {seen}, this process is {os.getpid()}): "
            "a proc filesystem was mounted over this one from outside, and bwrap cannot find the processes it starts in it"
        )
    return ""


def proc_mounts(mountinfo: str = "/proc/self/mountinfo") -> int:
    """How many proc filesystems are mounted on ``/proc``, one over the other."""
    try:
        with open(mountinfo, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/proc" and "-" in fields:
            after = fields[fields.index("-") + 1:]
            count += bool(after) and after[0] == "proc"
    return count


def repair_proc_view() -> str:
    """Take a proc mounted over the container's own off ``/proc``; returns what was done, or empty when nothing.

    Only where it is unambiguous: root in the agent's container (never natively, where the machine is the
    operator's), the view really foreign, and a second proc under the top one to fall back to. Detached
    rather than unmounted, so a process holding a file in it does not keep the wrong view in place.
    """
    from daedalus.tools.shell import _native  # Lazy: the shell module imports this one.

    if _native() or not sys.platform.startswith("linux") or os.geteuid() != 0:
        return ""
    if not proc_view_problem() or proc_mounts() < 2:
        return ""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.umount2(b"/proc", MNT_DETACH) != 0:
        code = ctypes.get_errno()
        return f"could not take the foreign proc off /proc: {os.strerror(code)}"
    if proc_view_problem():
        return "took a foreign proc off /proc, and the one under it is foreign too"
    logger.warning("a proc of another PID namespace was mounted over /proc; it was taken off, and /proc is this container's own again")
    return "took a proc of another PID namespace off /proc"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        return f"unreadable ({exc.strerror})"


def diagnostics(bwrap: str | None, status: str) -> list[tuple[str, str]]:
    """What the doctor shows and the log records whenever bwrap fails: each a label and a value."""
    rows: list[tuple[str, str]] = [("probe", status)]
    if bwrap is None:
        rows.append(("bwrap", "not found"))
    else:
        try:
            version = subprocess.run([bwrap, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            version = f"does not answer ({type(exc).__name__})"
        rows.append(("bwrap", f"{bwrap}, {version or 'no version'}"))
    if not sys.platform.startswith("linux"):
        return rows
    rows.append(("/proc", proc_view_problem() or f"this process's own ({proc_mounts()} proc mounted on it)"))
    try:
        names = sorted(os.listdir("/proc/self/ns"))
        rows.append(("/proc/self/ns", ", ".join(os.readlink(f"/proc/self/ns/{name}") for name in names)))
    except OSError as exc:
        rows.append(("/proc/self/ns", f"unreadable ({exc.strerror})"))
    pids = f"pid_max {_read('/proc/sys/kernel/pid_max')}"
    if os.path.exists("/sys/fs/cgroup/pids.max"):
        pids += f", cgroup {_read('/sys/fs/cgroup/pids.current')} of {_read('/sys/fs/cgroup/pids.max')}"
    rows.append(("pids", pids))
    rows.append(("user namespaces", _read("/proc/sys/user/max_user_namespaces")))
    return rows


@dataclass(slots=True)
class Outage:
    """The sandbox has been unavailable since ``since`` (monotonic) for ``reason``; ``number`` tells outages apart."""

    reason: str
    since: float
    number: int


Listener = Callable[[str, "Outage"], Awaitable[None]]
"""Called with ``"down"`` when an outage starts and ``"up"`` when it ends."""

_outage: Outage | None = None
_count = 0
_listeners: dict[str, Listener] = {}
suspended = False
"""The operator switched the sandbox off until the agent's next start, from the notice about an outage. Never
consulted for a guest's turn, which runs no command outside its sandbox."""


def current() -> Outage | None:
    return _outage


def listen(name: str, listener: Listener) -> None:
    """Call ``listener`` on every outage's start and end; a second one under the same name replaces the first,
    so an application started again in the same process does not leave the old one's listening."""
    _listeners[name] = listener


async def _tell(event: str, outage: Outage) -> None:
    for listener in list(_listeners.values()):
        try:
            await listener(event, outage)
        except Exception:  # noqa: BLE001 — a notice that fails must not fail the command it is about
            logger.exception("could not announce that the sandbox is %s", event)


async def report_down(reason: str) -> Outage:
    """The sandbox cannot be made: start an outage (and tell the listeners) or keep the one that runs."""
    global _outage, _count
    if _outage is not None:
        _outage.reason = reason
        return _outage
    _count += 1
    _outage = Outage(reason=reason, since=time.monotonic(), number=_count)
    from daedalus.tools.shell import bwrap_path  # Lazy: the shell module imports this one.

    rows = await asyncio.to_thread(diagnostics, bwrap_path(), reason)
    logger.warning("the Exec sandbox is unavailable: %s", "; ".join(f"{label}: {value}" for label, value in rows))
    await _tell("down", _outage)
    return _outage


async def report_up() -> None:
    """The sandbox was made again: end the outage, if there was one."""
    global _outage
    if _outage is None:
        return
    ended, _outage = _outage, None
    logger.warning("the Exec sandbox works again after %.0f s", time.monotonic() - ended.since)
    await _tell("up", ended)


def suspend() -> None:
    """The operator's answer to an outage: commands run without the sandbox until this process restarts.

    The outage is closed without an "up": nothing came back, the wall was taken away on purpose.
    """
    global _outage, suspended
    suspended = True
    _outage = None
    logger.warning("the operator switched the Exec sandbox off until the next start")


def agent_text(outage: Outage) -> str:
    """What the agent reads instead of a failed command: the machine is at fault, and what to do and not do."""
    lasted = time.monotonic() - outage.since
    if lasted >= ASK_OPERATOR_AFTER_SECONDS:
        what_now = (
            f"Exec has been unavailable for {lasted / 60:.0f} min. Stop here: end the turn and tell the operator what is "
            "blocked; do not keep going around it."
        )
    else:
        what_now = (
            "Do work that needs no shell meanwhile, or end the turn and tell the operator what is blocked. Exec is checked "
            "again on the next call."
        )
    return (
        f"sandbox unavailable: {outage.reason}\n"
        "The command did not run, and nothing in it was wrong: the sandbox every command runs in cannot be made on this "
        "machine right now. The operator has been told.\n"
        f"{what_now} Do not retry it in another form and do not invent a workaround for what one command would do "
        "(no in-browser converters or codecs, no reimplementing a command-line tool, no files written another way): "
        "dozens of steps around a missing shell cost more than waiting for it."
    )


__all__ = [
    "ASK_OPERATOR_AFTER_SECONDS", "Outage", "agent_text", "current", "diagnostics", "listen", "proc_mounts",
    "proc_view_problem", "repair_proc_view", "report_down", "report_up", "suspend",
]
