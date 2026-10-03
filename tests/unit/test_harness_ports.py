"""The production environment port (the staff runtime's, which the manager shares) and the visible
runner, over a stand-in for the terminals service: which refusals mean "not installed" and which
mean "the environment is down", reads that continue by offset, the home the daemon reports, and a
run in a terminal that ends, fails, or outlasts its time."""

from __future__ import annotations

from typing import Any

import pytest

from daedalus.harness import ports
from daedalus.harness.contract import EnvironmentUnavailable, ProgramNotFound
from daedalus.harness.ports import TerminalRunner
from daedalus.harness.runtime import RuntimeEnvironment
from daedalus.terminals.model import (
    EnvStatus,
    EnvUnavailable,
    ExecResult,
    FileChunk,
    NotFound,
    OutputChunk,
    TerminalSpec,
)


class Service:
    def __init__(self) -> None:
        self.runs: list[tuple[str, list[str], dict[str, Any]]] = []
        self.data = b"x" * 1500
        self.created: list[tuple[TerminalSpec, bool]] = []
        self.statuses: list[str] = ["running", "running", "exited"]
        self.exit_code: int | None = 0
        self.output = "downloading\ninstalled 2.1.281\n"

    def environments(self) -> list[EnvStatus]:
        return [EnvStatus(env="container", available=True, home="/root"), EnvStatus(env="host", available=False)]

    async def exec_run(self, env: str, argv: list[str], **kw: Any) -> ExecResult:
        self.runs.append((env, argv, kw))
        if argv[0] == "missing":
            raise NotFound("running missing: missing was not found")
        if argv[0] == "down":
            raise EnvUnavailable("running down: the container terminal service is not available")
        return ExecResult(exit_code=0, signal="", stdout="2.1.281 (Claude Code)\n", stderr="", truncated=False, timed_out=False, duration_ms=3, path="/home/operator/.local/bin/claude")

    async def fs_read(self, env: str, path: str, *, offset: int = 0, max_bytes: int = 0) -> FileChunk:
        chunk = self.data[offset : offset + min(max_bytes, 400)]
        return FileChunk(data=chunk, offset=offset, next_offset=offset + len(chunk), size=len(self.data), eof=offset + len(chunk) >= len(self.data))

    async def fs_stat(self, env: str, path: str) -> dict[str, Any]:
        return {"exists": path.endswith("there")}

    async def fs_list(self, env: str, path: str) -> dict[str, Any]:
        return {"entries": [{"name": "a.md", "type": "file"}, {"name": "b.md", "type": "file"}], "truncated": False}

    async def create(self, spec: TerminalSpec, *, confirm_over_cap: bool = False) -> dict[str, Any]:
        self.created.append((spec, confirm_over_cap))
        return {"id": "t-1"}

    async def get(self, terminal_id: str) -> dict[str, Any]:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"id": terminal_id, "status": status, "exit_code": self.exit_code if status == "exited" else None}

    async def read_output(self, terminal_id: str, *, since_seq: int, max_bytes: int = 0, strip: bool = True) -> OutputChunk:
        data = self.output.encode()
        return OutputChunk(from_seq=since_seq, to_seq=len(data), head_seq=len(data), gap=False, data=data[since_seq : since_seq + max_bytes].decode())


async def test_the_port_tells_a_missing_program_from_an_environment_that_is_down() -> None:
    service = Service()
    port = RuntimeEnvironment(service, "container", home="/home/operator", actor="harness")  # type: ignore[arg-type]
    result = await port.run(["claude", "--version"], cwd="/srv", env={"A": "1"}, timeout=5)
    assert (result.stdout, result.path) == ("2.1.281 (Claude Code)\n", "/home/operator/.local/bin/claude")
    assert service.runs[0] == ("container", ["claude", "--version"], {"cwd": "/srv", "env_vars": {"A": "1"}, "timeout": 5, "actor": "harness", "resources": None})
    with pytest.raises(ProgramNotFound):
        await port.run(["missing"])
    with pytest.raises(EnvironmentUnavailable):
        await port.run(["down"])
    assert (port.name, port.home) == ("container", "/home/operator")
    # Not given, the home is the one the environment's daemon reports.
    assert RuntimeEnvironment(service, "container").home == "/root"  # type: ignore[arg-type]
    assert RuntimeEnvironment(service, "host").home == ""  # type: ignore[arg-type]


async def test_a_read_continues_by_offset_up_to_its_limit() -> None:
    service = Service()
    port = RuntimeEnvironment(service, "host")  # type: ignore[arg-type]
    assert await port.read("/f") == service.data
    assert await port.read("/f", offset=100, limit=500) == service.data[100:600]
    assert await port.stat("/is/there") == {"exists": True} and await port.stat("/is/not") is None
    assert await port.list("/d") == ["a.md", "b.md"]


async def test_a_visible_run_is_the_operators_terminal_and_reports_how_it_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "EXIT_POLL_S", 0.001)
    service = Service()
    runner = TerminalRunner(service)  # type: ignore[arg-type]
    assert await runner.start("container", ["sh", "-c", "true"], title="Installing Claude Code") == "t-1"
    spec, over_cap = service.created[0]
    assert (spec.owner.kind, spec.argv, spec.title, spec.created_by, over_cap) == ("free", ["sh", "-c", "true"], "Installing Claude Code", "operator", True)
    assert await runner.wait("t-1", timeout=5) == (0, service.output)

    service.statuses, service.exit_code = ["exited"], 3
    assert (await runner.wait("t-1", timeout=5))[0] == 3
    # Still running at the deadline: no exit code, and the terminal is left for the operator.
    service.statuses = ["running"]
    assert (await runner.wait("t-1", timeout=0.01))[0] is None
