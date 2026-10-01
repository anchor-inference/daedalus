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


__all__ = ["end_tree", "pid_alive"]
