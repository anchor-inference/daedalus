"""A slow MCP server is given time to start, a warm-up is bounded as a whole, and errors carry no secrets."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from protocore.runtime.tool_registry import ToolRegistry

from daedalus.config import McpServerConfig
from daedalus.mcp.manager import McpConnection, McpManager, McpStillStarting, mcp_tool_name

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_echo_server.py"


def _slow(seconds: float) -> McpServerConfig:
    """The echo server behind a start-up delay, as a server that installs or indexes before it answers."""
    code = f"import runpy, sys, time; time.sleep({seconds}); sys.argv = [{str(SERVER)!r}]; runpy.run_path({str(SERVER)!r}, run_name='__main__')"
    return McpServerConfig(transport="stdio", command=sys.executable, args=["-c", code])


def _fails_once(marker: Path) -> McpServerConfig:
    """Exits at once the first time, serves the second: a server whose first start loses a race."""
    code = (
        "import pathlib, runpy, sys; m = pathlib.Path(sys.argv[1])\n"
        "if not m.exists():\n    m.write_text('x'); sys.exit(1)\n"
        f"sys.argv = [{str(SERVER)!r}]; runpy.run_path({str(SERVER)!r}, run_name='__main__')"
    )
    return McpServerConfig(transport="stdio", command=sys.executable, args=["-c", code, str(marker)])


async def test_a_server_still_starting_is_waited_for_not_restarted() -> None:
    manager = McpManager({"slow": _slow(1.5)}, ToolRegistry())
    try:
        try:
            await manager.ensure("slow", deadline=0.3)
        except McpStillStarting:
            pass
        else:
            raise AssertionError("a server that needs 1.5 s answered within 0.3 s")
        connection = manager._connections["slow"]
        starting = connection._task
        assert starting is not None and not starting.done()
        # Asked again while it starts, as the next run's warm-up does: the same start goes on.
        await manager.ensure("slow", deadline=20)
        assert connection._task is starting
        assert manager.tool_names("slow") == {mcp_tool_name("slow", "echo"), mcp_tool_name("slow", "add")}
    finally:
        await manager.close()


async def test_warm_up_is_parallel_bounded_and_tries_a_failed_server_again(tmp_path: Path) -> None:
    servers = {"slow": _slow(1.0), "flaky": _fails_once(tmp_path / "started-once")}
    manager = McpManager(servers, ToolRegistry())
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = await manager.warm(["slow", "flaky"], budget=0.4)
        assert loop.time() - started < 3.0
        assert outcome["slow"] == "starting"
        # The warm-up gave up waiting, not the server: it is ready a moment later without being asked.
        for _ in range(200):
            if manager._connections["slow"].session is not None:
                break
            await asyncio.sleep(0.05)
        assert manager._connections["slow"].state == "ready"
        outcome = await manager.warm(["flaky"], budget=20)
        assert outcome["flaky"] == "ready", outcome
    finally:
        await manager.close()


def test_a_connection_error_names_no_secret_of_the_server() -> None:
    config = McpServerConfig(
        transport="http",
        url="https://mcp.example.test/v1?api_key=QUERYSECRET42&team=core",
        headers={"Authorization": "Bearer HEADERSECRET77", "X-Short": "shrt"},
        env={"SERVICE_TOKEN": "ENVSECRET99"},
    )
    connection = McpConnection("remote", config)
    raw = RuntimeError(
        "GET https://mcp.example.test/v1?api_key=QUERYSECRET42&team=core failed; sent Authorization: Bearer HEADERSECRET77, "
        "X-Short shrt, env ENVSECRET99, and https://other.test/x?access_token=ELSEWHERE123&page=2"
    )
    text = connection.describe(raw)
    for secret in ("QUERYSECRET42", "HEADERSECRET77", "ENVSECRET99", "shrt", "ELSEWHERE123"):
        assert secret not in text, text
    assert "mcp.example.test/v1" in text and "page=2" in text
