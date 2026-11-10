"""An MCP server set to run on the host machine: started there through the host terminal daemon's
``exec.stream``, spoken to over its socket, ended with the connection, and named honestly when the
daemon is too old to do it.

The daemon is the fake one; the program it "runs" is the real echo server, started here as a
subprocess and bridged frame by frame, so the protocol travels the whole way as it would.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.runtime.tool_registry import ToolRegistry

from daedalus.config import McpServerConfig, RuntimeConfig, TerminalsConfig
from daedalus.extensions import mcp_admin
from daedalus.host.policy import Policy
from daedalus.mcp import manager as mcp_manager
from daedalus.mcp.host_stdio import LAUNCH, host_argv
from daedalus.mcp.manager import McpManager, mcp_tool_name
from daedalus.stores.database import Database
from daedalus.terminals.bridge import OUTDATED_STREAM, HostBridge
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd, FakeStream
from tests.unit.test_terminals_sidechannels import FreeOwners

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_echo_server.py"
SECRET = "sk-host-secret-value-1234"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))  # a unix socket path is at most 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def _where(place: str) -> Iterator[None]:
    """``WHERE`` is one list shared by name across modules, so it is changed in place and put back."""
    saved = mcp_manager.WHERE[0]
    mcp_manager.WHERE[0] = place
    try:
        yield
    finally:
        mcp_manager.WHERE[0] = saved


@pytest.fixture
def in_docker() -> Iterator[None]:
    with _where("container"):
        yield


@pytest.fixture
def native() -> Iterator[None]:
    with _where("machine"):
        yield


async def _bridged(stream: FakeStream) -> None:
    """The program the fake daemon runs: what follows the launch line, started for real here, its
    stdin, stdout and stderr carried over the stream as the daemon carries them."""
    assert stream.argv[:4] == ["bash", "-c", LAUNCH, "mcp-server"], stream.argv
    process = await asyncio.create_subprocess_exec(*stream.argv[4:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   env={**os.environ, **stream.env})
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None

    async def pump(source: asyncio.StreamReader, send: Any) -> None:
        while chunk := await source.read(4096):
            await send(chunk)

    async def feed() -> None:
        while piece := await stream.stdin.get():
            process.stdin.write(piece)
            await process.stdin.drain()
        process.stdin.close()

    await stream.err(b"host side says hello\n")
    tasks = [asyncio.create_task(pump(process.stdout, stream.out)), asyncio.create_task(pump(process.stderr, stream.err)), asyncio.create_task(feed())]
    try:
        await process.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
    await stream.exit(process.returncode or 0)


@pytest.fixture
async def daemon(short_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(short_dir / "run", env="host").start()
    fake.home = str(short_dir / "home")
    fake.stream_program = _bridged
    yield fake
    await fake.stop()


@pytest.fixture
async def service(db: Database, short_dir: Path, daemon: FakePtyd) -> AsyncIterator[Terminals]:
    made = Terminals(db, run_dirs={"container": None, "host": short_dir / "run"}, config=lambda: TerminalsConfig(), owners=FreeOwners())  # type: ignore[arg-type]
    await made.start()
    assert await made.wait_available("host")
    yield made
    await made.close()


def _host_echo(**extra: Any) -> McpServerConfig:
    return McpServerConfig(transport="stdio", command=sys.executable, args=[str(SERVER)], location="host", env={"ECHO_KEY": SECRET}, **extra)


async def test_a_host_server_is_started_on_the_host_and_ended_with_its_stream(in_docker: None, service: Terminals, daemon: FakePtyd, db: Database) -> None:
    bridge = HostBridge(lambda: service)
    manager = McpManager({"echo": _host_echo()}, ToolRegistry())
    manager.host_bridge = lambda: bridge
    try:
        await manager.ensure("echo", deadline=30)
        stream = daemon.streamed.get_nowait()
        # The server's own env and nothing of this container's: its paths mean nothing on the machine.
        assert stream.env == {"ECHO_KEY": SECRET}
        assert manager.tool_names("echo") == {mcp_tool_name("echo", "echo"), mcp_tool_name("echo", "add")}
        result = await manager._connections["echo"].call("echo", {"text": "from the host"})
        assert "from the host" in str(result.content)
        row = next(item for item in manager.status() if item["name"] == "echo")
        assert row["state"] == "ready" and row["runs_in"] == "host"
        assert "host side says hello" in row["stderr"]
        # The audit names the program and its environment's names, never a value.
        rows = await db.fetchall("SELECT action, detail_json FROM terminal_audit WHERE action = 'exec_stream'")
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail_json"])
        assert detail["env"] == ["ECHO_KEY"] and SECRET not in rows[0]["detail_json"]
    finally:
        await manager.close()
    # Closing the connection closes the stream, which is the daemon's signal to end the server's group.
    await asyncio.wait_for(stream.closed_by_host.wait(), 10)


async def test_a_host_server_that_ends_by_itself_says_so_and_comes_back_on_the_next_call(in_docker: None, service: Terminals, daemon: FakePtyd) -> None:
    bridge = HostBridge(lambda: service)
    manager = McpManager({"echo": _host_echo()}, ToolRegistry())
    manager.host_bridge = lambda: bridge
    try:
        await manager.ensure("echo", deadline=30)
        first = daemon.streamed.get_nowait()
        connection = manager._connections["echo"]
        # The daemon restarted under it, or the server exited: the card must not go on saying "ready".
        await first.exit(0)
        async with asyncio.timeout(10):
            while connection.state != "unavailable":
                await asyncio.sleep(0.02)
        assert "exited (code 0)" in (connection.error or "")
        result = await connection.call("echo", {"text": "again"})
        assert "again" in str(result.content)
        assert daemon.streamed.get_nowait() is not first
    finally:
        await manager.close()


async def test_a_daemon_from_before_streaming_is_named_as_old(in_docker: None, db: Database, short_dir: Path) -> None:
    old = await FakePtyd(short_dir / "old", env="host").start()
    old.exec_stream_version = 0
    service = Terminals(db, run_dirs={"container": None, "host": short_dir / "old"}, config=lambda: TerminalsConfig(), owners=FreeOwners())  # type: ignore[arg-type]
    await service.start()
    manager = McpManager({"echo": _host_echo()}, ToolRegistry())
    try:
        assert await service.wait_available("host")
        bridge = HostBridge(lambda: service)
        assert bridge.stream_problem() == OUTDATED_STREAM
        manager.host_bridge = lambda: bridge
        with pytest.raises(RuntimeError, match="older than this version"):
            await manager.ensure("echo", deadline=10)
        assert "host-terminal.sh install" in (manager._connections["echo"].error or "")
        assert not any(method == "exec.stream" for method, _ in old.calls)
    finally:
        await manager.close()
        await service.close()
        await old.stop()


async def test_without_a_bridge_a_host_server_says_what_it_needs(in_docker: None) -> None:
    manager = McpManager({"echo": _host_echo()}, ToolRegistry())
    try:
        with pytest.raises(RuntimeError, match="no host terminal bridge"):
            await manager.ensure("echo", deadline=10)
    finally:
        await manager.close()


async def test_a_host_server_that_will_not_start_shows_its_last_words(in_docker: None, service: Terminals, daemon: FakePtyd) -> None:
    async def missing(stream: FakeStream) -> None:
        await stream.err(b"the command 'mobile-mcp' is not installed on the host machine\n")
        await stream.exit(127)

    daemon.stream_program = missing
    bridge = HostBridge(lambda: service)
    manager = McpManager({"phone": McpServerConfig(command="mobile-mcp", location="host")}, ToolRegistry())
    manager.host_bridge = lambda: bridge
    try:
        with pytest.raises(RuntimeError) as failed:
            await manager.ensure("phone", deadline=10)
        assert "not installed on the host machine" in str(failed.value)
    finally:
        await manager.close()


async def test_a_program_off_the_daemons_list_is_refused_with_its_words(in_docker: None, service: Terminals, daemon: FakePtyd) -> None:
    daemon.exec_allow.discard("bash")
    bridge = HostBridge(lambda: service)
    manager = McpManager({"echo": _host_echo()}, ToolRegistry())
    manager.host_bridge = lambda: bridge
    try:
        with pytest.raises(RuntimeError, match="not among the programs exec.stream may run"):
            await manager.ensure("echo", deadline=10)
    finally:
        await manager.close()


async def test_natively_a_host_server_starts_here(native: None) -> None:
    used: list[str] = []
    manager = McpManager({"echo": McpServerConfig(command=sys.executable, args=[str(SERVER)], location="host")}, ToolRegistry())
    manager.host_bridge = lambda: used.append("bridge")  # type: ignore[func-returns-value, return-value]
    try:
        await manager.ensure("echo", deadline=30)
        assert used == [] and next(item for item in manager.status())["runs_in"] == "machine"
    finally:
        await manager.close()


# -- the launch line, run by a real bash ----------------------------------------------------------


def _launch(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(host_argv(argv[0], argv[1:]), capture_output=True, text=True, timeout=30, check=False,  # noqa: S603
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)})


def test_the_launch_line_runs_the_server_and_says_what_is_missing(tmp_path: Path) -> None:
    done = _launch(tmp_path, "printf", "%s|%s", "a b", "c")
    assert done.returncode == 0 and done.stdout == "a b|c"
    missing = _launch(tmp_path, "no-such-mcp-server", "--flag")
    assert missing.returncode == 127 and missing.stdout == ""
    assert "the command 'no-such-mcp-server' is not installed on the host machine" in missing.stderr
    # uvx is uv tool run where only uv is installed, as in the container.
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\necho \"uv $*\"\n")
    uv.chmod(0o755)
    assert _launch(tmp_path, "uvx", "some-mcp", "--x").stdout.strip() == "uv tool run some-mcp --x"


# -- who may place a server on the host -----------------------------------------------------------


def test_a_session_outside_the_host_is_asked_before_it_adds_a_host_server() -> None:
    arguments = {"name": "phone", "command": "npx", "args": ["-y", "@mobilenext/mobile-mcp"], "location": "host"}
    asked = Policy().evaluate("McpAdd", arguments)
    assert asked.action == "ask" and asked.rule == "mcp.host"
    # A session whose commands already run on the machine gains nothing by it, and natively there is no other place.
    assert Policy(drives_host=True).evaluate("McpAdd", arguments).action == "allow"
    assert Policy(native=True).evaluate("McpAdd", arguments).action == "allow"
    assert Policy().evaluate("McpAdd", {**arguments, "location": "server"}).action == "allow"
    # An operator's allow rule does not lower it: it is a built-in safety question.
    from daedalus.host.policy import Rule
    assert Policy(rules=[Rule(id="mine", tool="McpAdd", action="allow", note="", pattern=".")]).evaluate("McpAdd", arguments).action == "ask"


# -- the admin: where a server goes ----------------------------------------------------------------


class _App:
    def __init__(self, *, problem: str = "", host_folder: bool = True) -> None:
        self.config = RuntimeConfig()
        self.extensions = {"host_bridge": SimpleNamespace(stream_problem=lambda: problem)}
        mcp = McpManager({}, ToolRegistry())
        self.enabled: list[str] = []

        async def mcp_service(op: str, *, session_id: str, server: str) -> str:
            self.enabled.append(server)
            return "on"

        async def get_state(session_id: str) -> Any:
            return SimpleNamespace(session=SimpleNamespace(id=session_id, metadata={}), project=None)

        self.manager = SimpleNamespace(mcp=mcp, mcp_service=mcp_service, get_state=get_state,
                                       host_folder_of=lambda *_: object() if host_folder else None)

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.manager.mcp.reload(config.mcp.servers)


async def _quiet_probe(app: Any, name: str) -> dict[str, Any]:
    return {"name": name, "state": "unavailable", "error": "not started in this test", "tools": [], "stderr": ""}


async def test_the_agent_places_a_server_where_its_session_works(in_docker: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_admin, "probe", _quiet_probe)
    spec = {"transport": "stdio", "command": "npx", "args": ["-y", "@mobilenext/mobile-mcp"], "location": ""}
    app = _App()
    text = await mcp_admin.agent_service(app, "add", session_id="s1", name="phone", spec=spec)  # type: ignore[arg-type]
    assert app.config.mcp.servers["phone"].location == "host" and "the host machine" in text
    # A session in the container keeps its servers there unless it asks.
    other = _App(host_folder=False)
    await mcp_admin.agent_service(other, "add", session_id="s2", name="phone", spec=spec)  # type: ignore[arg-type]
    assert other.config.mcp.servers["phone"].location == "server"
    # A change that does not say keeps where the server was.
    await mcp_admin.agent_service(app, "add", session_id="s1", name="phone", spec={**spec, "description": "phone tools"})  # type: ignore[arg-type]
    assert app.config.mcp.servers["phone"].location == "host"
    # An old daemon refuses the host before anything is saved, with what to do about it.
    old = _App(problem=OUTDATED_STREAM)
    with pytest.raises(mcp_admin.McpAdminError, match="host-terminal.sh install"):
        await mcp_admin.agent_service(old, "add", session_id="s3", name="phone", spec={**spec, "location": "host"})  # type: ignore[arg-type]
    assert "phone" not in old.config.mcp.servers
    assert await _default_is_server_for_old(old)
    with pytest.raises(mcp_admin.McpAdminError, match="location"):
        await mcp_admin.agent_service(app, "add", session_id="s1", name="x", spec={**spec, "location": "laptop"})  # type: ignore[arg-type]
    for each in (app, other, old):
        await each.manager.mcp.close()


async def _default_is_server_for_old(app: Any) -> bool:
    return await mcp_admin._default_location(app, "s3") == "server"


def test_the_place_is_kept_on_an_edit_and_dropped_for_an_http_server(in_docker: None) -> None:
    stored = _host_echo()
    edited = mcp_admin.build("echo", {"transport": "stdio", "command": sys.executable, "args": [str(SERVER)], "description": "now described"}, stored)
    assert edited.location == "host"
    moved = mcp_admin.build("echo", {"transport": "http", "url": "https://mcp.example.com/mcp"}, stored)
    assert moved.location == "server"
    app = _App()
    row = {"offered": True, "problem": ""}
    assert mcp_admin.host_status(app) == row  # type: ignore[arg-type]


def test_natively_the_host_is_not_offered(native: None) -> None:
    assert mcp_admin.host_status(_App()) == {"offered": False, "problem": ""}  # type: ignore[arg-type]
