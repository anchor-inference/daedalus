"""The host commands' memory cap: the setting, its trip to the host's daemon, what Settings reads
back, and what the agent is told when a command was killed for it."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from daedalus.config import TerminalsConfig, memory_cap_request
from daedalus.host.host_exec import HostExecBackend
from daedalus.stores.database import Database
from daedalus.terminals.model import ExecResult
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd
from tests.unit.test_terminals_sidechannels import FreeOwners, eventually, frequent_housekeeping  # noqa: F401


def test_the_setting_says_a_share_a_size_or_none() -> None:
    assert TerminalsConfig().host_memory_cap == "60%"
    assert memory_cap_request("60%") == {"percent": 60.0}
    assert memory_cap_request("24G") == {"bytes": 24 << 30}
    assert memory_cap_request("0.5G") == {"bytes": 512 << 20}
    assert memory_cap_request("none") == {"unlimited": True}
    for bad in ("0%", "101%", "0.1G", "60", "lots", "-5G", ""):
        with pytest.raises(ValidationError):
            TerminalsConfig(host_memory_cap=bad)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))  # a unix socket path is at most 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def host(short_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = FakePtyd(short_dir / "run", env="host")
    fake.memory_cap = {"supported": True, "mode": "jobs", "machine_bytes": 16 << 30, "max_bytes": 9 << 30, "high_bytes": None,
                       "swap_max_bytes": 2 << 30, "current_bytes": 3 << 30, "oom_kills": 0}
    await fake.start()
    yield fake
    await fake.stop()


@pytest.mark.usefixtures("frequent_housekeeping")
async def test_the_cap_reaches_the_daemon_at_connect_and_when_it_changes(db: Database, host: FakePtyd) -> None:
    config = TerminalsConfig()
    service = Terminals(db, run_dirs={"container": None, "host": host.run_dir}, config=lambda: config, owners=FreeOwners())  # type: ignore[arg-type]
    await service.start()
    try:
        assert await service.wait_available("host")

        def sent() -> list[dict[str, object]]:
            return [params for method, params in host.calls if method == "memory.set_cap"]

        async def first() -> bool:
            return sent() == [{"percent": 60.0}]

        await eventually(first)
        view = await service.memory_cap("host")
        assert view["available"] and view["setting"] == "60%" and view["status"]["max_bytes"] == int((16 << 30) * 0.6)
        assert view["status"]["current_bytes"] == 3 << 30 and view["status"]["machine_bytes"] == 16 << 30

        config.host_memory_cap = "none"

        async def changed() -> bool:
            return sent()[-1:] == [{"unlimited": True}]

        await eventually(changed)
        assert len(sent()) == 2, "an unchanged setting is not sent again every tick"
        assert (await service.memory_cap("host"))["status"]["max_bytes"] is None

        # A restarted daemon has only what it saved: the setting is sent again at the new connection.
        await host.restart()

        async def again() -> bool:
            return len(sent()) == 3

        await eventually(again)
        assert sent()[-1] == {"unlimited": True}
    finally:
        await service.close()


async def test_a_daemon_without_the_cap_is_not_asked_and_says_so(db: Database, short_dir: Path) -> None:
    fake = await FakePtyd(short_dir / "run", env="host").start()
    service = Terminals(db, run_dirs={"container": None, "host": fake.run_dir}, config=lambda: TerminalsConfig(), owners=FreeOwners())  # type: ignore[arg-type]
    await service.start()
    try:
        assert await service.wait_available("host")
        view = await service.memory_cap("host")
        assert not view["available"] and view["reason"] == "outdated" and view["setting"] == "60%"
        assert not [m for m, _ in fake.calls if m.startswith("memory.")]
    finally:
        await service.close()
        await fake.stop()


class _Bridge:
    def __init__(self, result: ExecResult) -> None:
        self.result = result

    async def exec_run(self, env: str, argv: list[str], **_: object) -> ExecResult:
        return self.result


async def test_a_command_killed_for_the_cap_says_so() -> None:
    killed = ExecResult(exit_code=101, signal="", stdout="error: test failed, process didn't exit successfully (signal: 9, SIGKILL: kill)\n",
                        stderr="", truncated=False, timed_out=False, duration_ms=5000, oom_kills=1, memory_cap_bytes=24 << 30)
    outcome = await HostExecBackend(lambda: _Bridge(killed), "/home/someone/project").run("cargo test", cwd=None, env=None, timeout=60)  # type: ignore[arg-type,return-value]
    assert "killed for the memory cap of host commands" in outcome.output and "24.0 GB" in outcome.output
    assert "Settings → Limits" in outcome.output and outcome.exit_code == 101
    plain = ExecResult(exit_code=-1, signal="KILL", stdout="", stderr="", truncated=False, timed_out=False, duration_ms=1)
    outcome = await HostExecBackend(lambda: _Bridge(plain), "/home/someone/project").run("sleep 9", cwd=None, env=None, timeout=60)  # type: ignore[arg-type,return-value]
    assert "memory cap" not in outcome.output and "[ended by KILL]" in outcome.output
