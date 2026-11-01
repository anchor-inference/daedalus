"""The headless bench runner records a verdict, turns, tokens and a trajectory per task; the file tools
and Exec follow an exec backend when a session drives another machine."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from protocore.contracts.tools import ToolContext

from daedalus.bench.manifest import Manifest, Task
from daedalus.bench.runner import BenchRunner
from daedalus.config import Settings
from daedalus.host.filesystem import ExecOutcome, ShellFS
from daedalus.host.services import SessionServices, locator
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider


class LocalShellBackend:
    """An exec backend that runs the command here, in a fixed directory: the container stand-in."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[str] = []

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float) -> ExecOutcome:
        self.commands.append(command)
        proc = await asyncio.create_subprocess_exec("bash", "-lc", command, cwd=cwd or str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ExecOutcome(exit_code=proc.returncode or 0, output=out.decode("utf-8", "replace"))


async def test_shell_fs_round_trips_files_through_the_backend(tmp_path: Path) -> None:
    fs = ShellFS(LocalShellBackend(tmp_path))
    target = tmp_path / "pkg" / "mod.py"
    await fs.write_text(target, "x = 1\nif x:\n    print('it''s')\n")
    assert await fs.read_text(target) == "x = 1\nif x:\n    print('it''s')\n"
    await fs.write_text(tmp_path / "no-newline.txt", "abc")
    assert await fs.read_text(tmp_path / "no-newline.txt") == "abc"
    assert await fs.exists(target) and await fs.is_file(target) and not await fs.is_dir(target)
    assert await fs.is_dir(tmp_path / "pkg") and await fs.listdir(tmp_path / "pkg") == ["mod.py"]
    assert await fs.size(tmp_path / "no-newline.txt") == 3
    assert await fs.find(tmp_path, "**/*.py", 10) == ["pkg/mod.py"]
    code, out, _ = await fs.search(tmp_path, "print", glob=None, case_insensitive=False, limit=10)
    assert code == 0 and "mod.py" in out
    code, _, _ = await fs.search(tmp_path, "absent-token", glob=None, case_insensitive=False, limit=10)
    assert code == 1


async def test_file_tools_and_exec_follow_the_backend(tmp_path: Path) -> None:
    from daedalus.tools.files import edit_file, read_file, write_file
    from daedalus.tools.shell import exec_command

    remote = tmp_path / "remote"
    remote.mkdir()
    backend = LocalShellBackend(remote)
    locator.register(SessionServices(session_id="bench-fs", workspace_dir=remote, exec_backend=backend))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="bench-fs", metadata={"tool_call_id": "c1"})
    try:
        assert not (await write_file().invoke(ctx, {"path": "a.txt", "content": "hello\nworld\n"})).is_error
        assert (remote / "a.txt").read_text() == "hello\nworld\n"
        assert any("base64 -d" in c for c in backend.commands), "the write did not go through the backend"
        result = await edit_file().invoke(ctx, {"path": "a.txt", "old_string": "world", "new_string": "there"})
        assert not result.is_error and (remote / "a.txt").read_text() == "hello\nthere\n"
        backend.commands.clear()
        result = await read_file().invoke(ctx, {"path": "a.txt"})
        assert "there" in result.content and any("tail -n +1 " in c for c in backend.commands), "the read did not go through the backend"
        big = "x" * 70_000 + "\n" + "y" * 70_000 + "\nend"
        assert not (await write_file().invoke(ctx, {"path": "big.txt", "content": big})).is_error
        assert (remote / "big.txt").read_text() == big
        (remote / "blob.bin").write_bytes(bytes(range(256)) * 4)
        result = await read_file().invoke(ctx, {"path": "blob.bin"})
        assert result.is_error and "binary" in result.content
        result = await exec_command().invoke(ctx, {"command": f"cat {shlex.quote(str(remote / 'a.txt'))} && exit 3"})
        assert result.is_error and "exit_code=3" in result.content and "hello" in result.content
        assert any("a.txt" in c for c in backend.commands)
    finally:
        locator.unregister("bench-fs")


async def test_bench_runner_records_pass_turns_tokens_and_a_trajectory(settings: Settings, db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"tool": "Write", "args": {"path": "answer.txt", "content": "42\n"}}, {"text": "wrote the answer"}])
    manifest = Manifest(name="smoke", tasks=[Task(id="write-42", prompt="Write 42 into answer.txt", check="test \"$(cat answer.txt)\" = 42", files={"README.md": "task"})])
    out = tmp_path / "out"
    async with BenchRunner(settings, model_config(), out_dir=out) as runner:
        assert runner.manager is not None
        runner.manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
        records = await runner.run(manifest)
    record = records[0]
    assert record.passed is True and record.status == "completed"
    assert record.turns == 2 and record.tool_calls == 1
    assert record.cost_usd is None and record.input_tokens == 0  # the scripted provider bypasses the usage sink: unpriced, uncounted
    assert not (out / "trajectories" / "write-42.json").read_text().count('"name": "Write"') == 0
    lines = (out / "records.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["task"] == "write-42"
    steps = json.loads((out / "trajectories" / "write-42.json").read_text())["steps"]
    assert any(s["tool_calls"] and s["tool_calls"][0]["name"] == "Write" for s in steps)
    summary = json.loads((out / "summary.json").read_text())
    assert summary["passed"] == 1 and summary["judged"] == 1 and summary["pass_rate"] == 1.0
    assert not (settings.workspaces_dir / record.session_id).exists()  # a passed task's workspace is gone
