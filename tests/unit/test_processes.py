"""Ending process trees and probing for live processes, on POSIX and the way Windows has to.

Windows is not the platform these tests run on, so what can only be true there is checked by taking
away what Windows lacks — ``os.killpg`` — and standing in for ``taskkill`` and the process probe.
A tool path that still reached for ``os.killpg`` fails here with the same AttributeError a native
Windows installation reported to the operator.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from protocore.contracts.tools import ToolContext

from daedalus import processes
from daedalus.extensions import services as services_module
from daedalus.extensions.api import _AppServer
from daedalus.host import launcher_bridge
from daedalus.host.services import SessionServices, locator
from tests.support.waiting import until

posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX idea")


@posix_only
async def test_end_tree_ends_the_whole_group_on_posix() -> None:
    # A shell with a grandchild: the grandchild holds the group alive after the shell is gone, which
    # is exactly what ending only the direct child would leave behind.
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; wait"], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    processes.end_tree(proc.pid, hard=True)
    proc.wait(timeout=10)
    await until(lambda: not processes.pid_alive(grandchild), "the grandchild went with its group")


def test_on_windows_the_tree_is_ended_by_taskkill_whatever_was_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(processes, "WINDOWS", True)
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.setattr(processes.subprocess, "run", lambda argv, **_: calls.append(argv))
    processes.end_tree(4321, hard=False)
    processes.end_tree(4321, hard=True)
    # /F both times: a console program started without a console has no window for the polite form.
    assert calls == [["taskkill", "/T", "/F", "/PID", "4321"]] * 2

    def missing(argv: list[str], **_: Any) -> None:
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(processes.subprocess, "run", missing)
    processes.end_tree(4321, hard=True)  # nothing to run it with is not the caller's problem


def test_pid_alive_tells_a_running_process_from_a_finished_one() -> None:
    assert processes.pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert not processes.pid_alive(proc.pid)
    assert not processes.pid_alive(None) and not processes.pid_alive(0)


def test_pid_alive_asks_windows_rather_than_sending_signal_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []
    monkeypatch.setattr(processes, "WINDOWS", True)
    monkeypatch.setattr(processes, "_windows_alive", lambda pid: asked.append(pid) or pid == 77)

    def no_kill(*_: Any) -> None:
        raise AssertionError("os.kill(pid, 0) is CTRL_C_EVENT on Windows")

    monkeypatch.setattr(os, "kill", no_kill)
    assert processes.pid_alive(77) and not processes.pid_alive(78) and asked == [77, 78]


def test_a_running_launcher_is_found_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "launcher.json").write_text(json.dumps({"port": 8770, "token": "t", "pid": 4242}), encoding="utf-8")

    def no_kill(*_: Any) -> None:
        raise OSError(87, "The parameter is incorrect")  # what signal 0 gets on Windows

    monkeypatch.setattr(launcher_bridge.os, "kill", no_kill)
    monkeypatch.setattr(launcher_bridge.os, "name", "nt")
    monkeypatch.setattr(launcher_bridge, "pid_alive", lambda pid: pid == 4242)
    found = launcher_bridge.read(state)
    assert found is not None and found.port == 8770


@posix_only
async def test_exec_timeout_and_job_kill_do_not_reach_for_killpg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.tools.shell import exec_command, job_kill, job_output

    real_killpg = os.killpg
    killed: list[int] = []

    def taskkill(argv: list[str], **_: Any) -> None:
        # Stands in for taskkill /T /F: the real group is ended so the tool sees its process exit.
        pid = int(argv[-1])
        killed.append(pid)
        try:
            real_killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    monkeypatch.setattr(processes, "WINDOWS", True)
    monkeypatch.setattr(processes.subprocess, "run", taskkill)
    monkeypatch.delattr(os, "killpg")
    locator.register(SessionServices(session_id="win-kill", workspace_dir=tmp_path))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="win-kill", metadata={"tool_call_id": "c"})
    try:
        timed = await exec_command().invoke(ctx, {"command": "sleep 30", "timeout_seconds": 1})
        assert timed.is_error and "TIMED OUT" in timed.content and len(killed) == 1
        started = await exec_command().invoke(ctx, {"command": "sleep 30", "background": True})
        job_id = str(started.metadata["job_id"])
        stopped = await job_kill().invoke(ctx, {"job_id": job_id})
        assert not stopped.is_error and len(killed) == 2
        assert (await job_output().invoke(ctx, {"job_id": job_id})).metadata["running"] is False
    finally:
        locator.unregister("win-kill")


def test_port_probe_does_not_share_a_port_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # SO_REUSEADDR on Windows binds over a listening socket, so a probe with it reads every port free.
    options: list[tuple[int, int, int]] = []

    class Probe:
        def __init__(self, *_: Any) -> None:
            pass

        def __enter__(self) -> Probe:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def setsockopt(self, *option: int) -> None:
            options.append(option)

        def bind(self, _address: tuple[str, int]) -> None:
            pass

    monkeypatch.setattr(services_module.socket, "socket", Probe)
    monkeypatch.setattr(services_module.os, "name", "nt")
    assert services_module.port_free(8180) and options == []
    monkeypatch.setattr(services_module.os, "name", "posix")
    assert services_module.port_free(8180) and options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


async def test_the_app_server_hands_a_stop_signal_on_to_the_application() -> None:
    told: list[bool] = []
    server = _AppServer(uvicorn.Config(app=lambda *_: None), lambda: told.append(True))
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit and told == [True]
    await asyncio.sleep(0)
