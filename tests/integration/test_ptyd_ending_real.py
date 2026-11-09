"""Ending a terminal against the real daemon: what the host says runs in it, and that the process is
gone once it is ended — the shell, the command under it, and the row among the running ones.

The question "end it?" is asked from the view's ``at_prompt`` and ``running``; this proves both are
true to what is actually running, by the shell's own marks (bash with its integration), by the
terminal's foreground process group alone (``sh``, which prints no marks), and inside the sandbox
when the daemon can give one. The command's process is looked for in ``/proc`` before and after.

Two ways to name the daemon:

- ``DAEDALUS_PTYD_BIN=<path to ptyd>``: started here, as the host environment's daemon runs.
- ``DAEDALUS_PTYD_RUN_DIR=<run dir>``: one already serving there — the container environment's,
  started from the agent's image in a throwaway container whose run directory is mounted here.

Both need ``DAEDALUS_INTEGRATION=1``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import TerminalsConfig
from daedalus.stores.database import Database
from daedalus.terminals.model import Origin, Owner, TerminalSpec
from daedalus.terminals.service import Terminals

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")
RUN_DIR = os.environ.get("DAEDALUS_PTYD_RUN_DIR", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not (BINARY and Path(BINARY).is_file()) and not (RUN_DIR and Path(RUN_DIR).is_dir()), reason="set DAEDALUS_PTYD_BIN or DAEDALUS_PTYD_RUN_DIR"),
]


class FreeOnly:
    async def exists(self, owner: Owner) -> bool:
        return owner.kind == "free"

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        return {}

    async def project_of(self, owner: Owner) -> str | None:
        return None

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        return None

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        return [cwd]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-end-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def run_dir(base: Path) -> AsyncIterator[Path]:
    if RUN_DIR:
        yield Path(RUN_DIR)
        return
    process = subprocess.Popen(  # noqa: S603 — the binary the caller named, with fixed arguments
        [BINARY, "serve", "--env", "host", "--run-dir", str(base / "run"), "--state-dir", str(base / "state"), "--shell", "/bin/bash"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if (base / "run" / "endpoint").exists():
            break
        await asyncio.sleep(0.05)
    yield base / "run"
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 10)


@pytest.fixture
async def service(db: Database, run_dir: Path) -> AsyncIterator[Terminals]:
    cfg = TerminalsConfig()
    env = "container" if RUN_DIR else "host"
    service = Terminals(db, run_dirs={env: run_dir, "container" if env == "host" else "host": None}, config=lambda: cfg, owners=FreeOnly())
    await service.start()
    assert await service.wait_available(env, timeout=15)
    service.env_under_test = env  # type: ignore[attr-defined]
    try:
        yield service
    finally:
        await service.close()


def _pids(argv: list[str]) -> list[int]:
    """The processes on this machine whose command line is exactly ``argv``: a container's are seen
    here too, under the pids the host's namespace gives them."""
    wanted = ("\0".join(argv) + "\0").encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if (entry / "cmdline").read_bytes() == wanted:
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


async def _until(check: Any, timeout: float = 20.0) -> None:
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.1)


async def _view(service: Terminals, terminal_id: str) -> dict[str, Any]:
    return dict(await service.get(terminal_id))


async def _ends_a_running_command(service: Terminals, base: Path, *, argv: list[str] | None, seconds: str, sandbox: bool = False, marked: bool) -> None:
    env = service.env_under_test  # type: ignore[attr-defined]
    cwd = str(base) if env == "host" else "/tmp"
    view = await service.create(TerminalSpec(env=env, owner=Owner("free"), cwd=cwd, argv=argv, sandbox=sandbox))
    terminal_id = view["id"]

    # A fresh shell sits at its prompt: ending it would stop nothing.
    async def idle() -> bool:
        return bool((await _view(service, terminal_id))["at_prompt"])

    await _until(idle)
    await service.write(terminal_id, text=f"sleep {seconds}\r", origin=Origin("test"), wait_keyboard=False)

    # A command runs: the host says so, and names it when the shell marked it.
    async def busy() -> bool:
        current = await _view(service, terminal_id)
        return current["at_prompt"] is False and bool(_pids(["sleep", seconds]))

    await _until(busy)
    current = await _view(service, terminal_id)
    assert current["running"] == (f"sleep {seconds}" if marked else None)
    command = _pids(["sleep", seconds])
    assert command

    ended = await service.kill(terminal_id)
    assert ended["status"] == "exited"
    assert terminal_id not in {v["id"] for v in await service.list(status="running")}

    async def gone() -> bool:
        return not _pids(["sleep", seconds])

    await _until(gone, timeout=15)


async def test_a_marked_shell_names_its_command_and_ending_kills_it(service: Terminals, base: Path) -> None:
    # The daemon's own shell, started with its integration: an argv is run exactly as given, unmarked.
    await _ends_a_running_command(service, base, argv=None, seconds="4711", marked=True)


async def test_a_shell_without_marks_is_read_by_its_foreground_group_and_ending_kills_it(service: Terminals, base: Path) -> None:
    await _ends_a_running_command(service, base, argv=["/bin/sh"], seconds="4712", marked=False)


async def test_a_sandboxed_shell_is_read_through_the_sandbox_and_ending_kills_it(service: Terminals, base: Path) -> None:
    env = service.env_under_test  # type: ignore[attr-defined]
    if not any(e.env == env and e.sandbox == "ok" for e in service.environments()):
        pytest.skip("this daemon cannot sandbox here")
    await _ends_a_running_command(service, base, argv=None, seconds="4713", sandbox=True, marked=True)
