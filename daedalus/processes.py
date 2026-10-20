"""Ending a process tree and asking whether a process is alive, on every platform the agent runs on.

The tools start their commands in a session of their own (``start_new_session=True``) so that a
timeout or a kill reaches everything the command started, and on Linux and macOS that is
``os.killpg``. Windows has neither: ``start_new_session`` is accepted and ignored there, and
``os.killpg`` does not exist, so every timeout and every JobKill of a native Windows installation
failed with ``module 'os' has no attribute 'killpg'`` and left the command running. The nearest
thing Windows has to a process group is the tree, and ``taskkill /T`` is what walks it.

``os.kill(pid, 0)`` is no answer there either: ``signal.CTRL_C_EVENT`` is 0 on Windows, so the
"is it alive" probe every POSIX program knows sends a console control event to a process group
instead, and fails for any process that is not one — which read as "dead" for a launcher that was
running.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess

WINDOWS = os.name == "nt"

TASKKILL_SECONDS = 15.0
"""How long ``taskkill`` may take. It returns in well under a second; the bound is for a machine so
loaded that it does not, so a kill can never be the thing that hangs a tool."""


def end_tree(pid: int, *, hard: bool) -> None:
    """End the process ``pid`` was started as the leader of, and everything it started.

    On POSIX that is the process group — the caller started it in a session of its own — and
    ``hard`` chooses SIGKILL over SIGTERM; ``ProcessLookupError`` and ``PermissionError`` reach the
    caller as they always have. On Windows the tree is ended with ``taskkill /T /F`` whichever was
    asked: a console program started without a console of its own has no window to receive the
    polite ``taskkill`` (WM_CLOSE), so there is nothing softer that would arrive. A process that is
    already gone is not an error there: ``taskkill`` says so and this returns.
    """
    if not WINDOWS:
        os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
        return
    try:
        subprocess.run(  # noqa: S603 — a fixed program and a number
            ["taskkill", "/T", "/F", "/PID", str(int(pid))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=TASKKILL_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def pid_alive(pid: int | None) -> bool:
    """Whether a process with this id exists now (and, on Linux, is not a zombie awaiting a reaper)."""
    if not pid or pid <= 0:
        return False
    if WINDOWS:
        return _windows_alive(int(pid))
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
        return bool(fields) and fields[0] != "Z"
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return False
    except OSError:
        return False
    # A POSIX system without /proc (macOS): signal 0 asks without sending anything.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is only not ours to signal
    except OSError:
        return False
    return True


def _windows_alive(pid: int) -> bool:
    import ctypes  # Lazy: only Windows reaches here, and the module is not free to import
    from ctypes import wintypes  # Lazy: as above

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means a process is there and belongs to someone else; anything else, that
        # there is none.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        # An exited process whose handle someone still holds keeps its id and its exit code;
        # STILL_ACTIVE is the one value that means it is running.
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


DRAIN_AFTER_EXIT_SECONDS = 1.5
"""How long a command's output is still read after its shell has exited: long enough for the last
lines a just-finished child flushes, short enough that a server started with ``&`` does not hold the
call. Python's ``Process.wait`` and ``communicate`` wait for every pipe to close, so a backgrounded
child that inherited stdout kept a one-line command waiting for its whole timeout and was then
killed as "timed out"."""

EXIT_POLL_SECONDS = 0.05


async def shell_exited(proc: asyncio.subprocess.Process) -> int:
    """Return the exit code once the process itself has exited, whatever still holds its pipes.

    ``proc.wait()`` resolves only when the pipes are closed as well; ``returncode`` is set the moment
    the child watcher reaps the process, so it is polled.
    """
    while proc.returncode is None:
        await asyncio.sleep(EXIT_POLL_SECONDS)
    return proc.returncode


async def read_until_exit(proc: asyncio.subprocess.Process, reader: asyncio.Task[object], *,
                          grace: float = DRAIN_AFTER_EXIT_SECONDS) -> bool:
    """Wait for ``reader`` (which reads ``proc``'s output to the end) and for ``proc`` to exit.

    True when the output ended. False when the process exited and something it left behind still
    held the output after ``grace`` seconds: the reader is then cancelled and the pipes are closed,
    so the leftover child is not killed but its next write to the closed pipe may end it. An
    exception from the reader is raised here. The caller bounds the whole wait.
    """
    exited = asyncio.ensure_future(shell_exited(proc))
    try:
        await asyncio.wait({reader, exited}, return_when=asyncio.FIRST_COMPLETED)
        if reader.done():
            reader.result()
            await exited
            return True
        done, _ = await asyncio.wait({reader}, timeout=grace)
        if done:
            reader.result()
            return True
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        # The transport is the only handle on the pipes; with the process already reaped, closing it
        # closes them and kills nothing.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        return False
    finally:
        exited.cancel()


__all__ = ["DRAIN_AFTER_EXIT_SECONDS", "end_tree", "pid_alive", "read_until_exit", "shell_exited"]
