"""A native session working in a host folder from the container: its commands and file tools go
through the host terminal daemon, it refuses to start while the daemon is down, and a Daedalus staff
member does its task there the same way."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import Settings
from daedalus.extensions.board import Board
from daedalus.host.host_exec import BRIDGE_DOWN, DAEMON_MAX_TIMEOUT, OLD_DAEMON, PRELUDE, HostExecBackend
from daedalus.host.session_runner import HostUnreachable, SessionManager, host_home_name
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, Project
from daedalus.terminals.model import ExecResult
from tests.support.waiting import until_await
from tests.unit.test_launch_controls import OPERATOR
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import BRIEF, close_team, team_for

HOST_ROOT = "/home/someone/labs"


@dataclass
class ShellHost:
    """The host terminal daemon's ``exec.run`` over a directory of this machine standing in for the
    operator's: host paths under ``HOST_ROOT`` are rewritten to ``disk`` on the way in and back on the
    way out, and the program really runs here. Only what the daemon allows is run."""

    disk: Path
    up: bool = True
    allow: tuple[str, ...] = ("bash", "git")
    calls: list[tuple[list[str], str, bytes | None]] = field(default_factory=list)
    ignore_sigpipe: bool = False
    """Run the programs with SIGPIPE ignored, as the daemon's systemd unit does by default: this
    process ignores it, and ``restore_signals=False`` hands that on instead of resetting it."""

    def available(self) -> bool:
        return self.up

    def here(self, text: str) -> str:
        return text.replace(HOST_ROOT, str(self.disk))

    def there(self, text: str) -> str:
        return text.replace(str(self.disk), HOST_ROOT)

    async def exec_run(self, env: str, argv: list[str], *, cwd: str, env_vars: dict[str, str] | None = None,
                       timeout: float, stdin: bytes | None = None) -> ExecResult:
        if not self.up:
            raise ConnectionError("the host terminal bridge is not available: the host terminal service is not running")
        assert env == "host" and cwd.startswith("/"), "the daemon refuses a relative working directory"
        self.calls.append((argv, cwd, stdin))
        if argv[0] not in self.allow:
            raise OSError(f"forbidden: {argv[0]} is not among the programs exec.run may run")
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.disk), **(env_vars or {})}
        try:
            done = await asyncio.to_thread(subprocess.run, [self.here(a) for a in argv], cwd=self.here(cwd), input=stdin,
                                           env=environment, capture_output=True, timeout=timeout, check=False,
                                           restore_signals=not self.ignore_sigpipe)
        except subprocess.TimeoutExpired:
            return ExecResult(exit_code=-1, signal="SIGKILL", stdout="", stderr="", truncated=False, timed_out=True, duration_ms=int(timeout * 1000))
        return ExecResult(exit_code=done.returncode, signal="", stdout=self.there(done.stdout.decode("utf-8", "replace")),
                          stderr=self.there(done.stderr.decode("utf-8", "replace")), truncated=False, timed_out=False, duration_ms=1)


def host_disk(tmp_path: Path) -> Path:
    disk = tmp_path / "operator-machine"
    (disk / "src").mkdir(parents=True)
    (disk / "README.md").write_text("Labs: experiments\n")
    return disk


async def host_project(manager: SessionManager, *, readonly: bool = False) -> Project:
    project = await manager.projects.create("Labs", [FolderSpec(HOST_ROOT, env="host", readonly=readonly)])
    found = await manager.projects.get(project.id)
    assert found is not None and found.primary.env == "host"
    return found


def context(session_id: str, call: str = "c1") -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": call})


def container_only(manager: SessionManager) -> None:
    if manager.projects.local_env != "container":
        pytest.skip("a host folder is driven through the daemon only from a container installation")


# -- the backend ----------------------------------------------------------------------------------


class RecordingBridge:
    def __init__(self, result: ExecResult | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def exec_run(self, env: str, argv: list[str], **kwargs: Any) -> ExecResult:
        self.calls.append({"env": env, "argv": argv, **kwargs})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def result(**overrides: Any) -> ExecResult:
    values: dict[str, Any] = {"exit_code": 0, "signal": "", "stdout": "", "stderr": "", "truncated": False, "timed_out": False, "duration_ms": 1}
    values.update(overrides)
    return ExecResult(**values)


async def test_a_command_runs_in_bash_on_the_host_in_the_folder_with_one_output_stream() -> None:
    bridge = RecordingBridge(result(exit_code=2, stdout="made\n", stderr="warned\n"))
    backend = HostExecBackend(lambda: bridge, HOST_ROOT)  # type: ignore[arg-type, return-value]
    outcome = await backend.run("make test", cwd=None, env={"CI": "1"}, timeout=4 * 3600)
    [call] = bridge.calls
    assert call["env"] == "host" and call["argv"][:2] == ["bash", "-c"] and call["argv"][2] == PRELUDE + "make test"
    assert call["cwd"] == HOST_ROOT, "a command without a directory of its own runs in the folder"
    assert call["env_vars"] == {"CI": "1"}
    assert call["timeout"] == DAEMON_MAX_TIMEOUT, "the daemon refuses a call longer than its own cap outright"
    assert outcome.exit_code == 2 and "made" in outcome.output and "warned" in outcome.output and not outcome.timed_out

    await backend.run("ls", cwd=f"{HOST_ROOT}/src", env=None, timeout=10)
    assert bridge.calls[-1]["cwd"] == f"{HOST_ROOT}/src"


async def test_the_answers_of_the_daemon_become_outcomes_the_tools_can_say() -> None:
    timed = await HostExecBackend(lambda: RecordingBridge(result(exit_code=-1, signal="SIGKILL", timed_out=True)), HOST_ROOT).run("sleep 99", cwd=None, env=None, timeout=1)  # type: ignore[arg-type, return-value]
    assert timed.timed_out and "SIGKILL" in timed.output
    cut = await HostExecBackend(lambda: RecordingBridge(result(stdout="x" * 10, truncated=True)), HOST_ROOT).run("yes", cwd=None, env=None, timeout=1)  # type: ignore[arg-type, return-value]
    assert "cut the output" in cut.output
    down = await HostExecBackend(lambda: RecordingBridge(ConnectionError("gone")), HOST_ROOT).run("ls", cwd=None, env=None, timeout=1)  # type: ignore[arg-type, return-value]
    assert down.exit_code == 255 and down.output == BRIDGE_DOWN
    old = await HostExecBackend(lambda: RecordingBridge(OSError("forbidden: bash is not among the programs exec.run may run")), HOST_ROOT).run("ls", cwd=None, env=None, timeout=1)  # type: ignore[arg-type, return-value]
    assert old.exit_code == 126 and old.output == OLD_DAEMON, "an older daemon is named with the way to update it"
    none = await HostExecBackend(lambda: None, HOST_ROOT).run("ls", cwd=None, env=None, timeout=1)
    assert none.output == BRIDGE_DOWN


async def test_a_greeting_profile_does_not_end_up_in_the_output(tmp_path: Path) -> None:
    disk = host_disk(tmp_path)
    (disk / ".profile").write_text("echo 'Welcome to the lab'\nexport LAB_TOOL=ready\n")
    backend = HostExecBackend(lambda: ShellHost(disk), HOST_ROOT)  # type: ignore[arg-type, return-value]
    outcome = await backend.run("cat README.md; echo $LAB_TOOL", cwd=None, env=None, timeout=30)
    assert outcome.output == "Labs: experiments\nready\n", "the profile is read for its environment, and its words are not a file's first line"


async def test_bytes_are_put_on_the_host_through_stdin_in_pieces(tmp_path: Path) -> None:
    disk = host_disk(tmp_path)
    host = ShellHost(disk)
    data = bytes(range(256)) * 2100  # past one piece, and nothing a shell argument could carry
    await HostExecBackend(lambda: host, HOST_ROOT).put_bytes(f"{HOST_ROOT}/inbox/blob.bin", data)  # type: ignore[arg-type, return-value]
    assert (disk / "inbox" / "blob.bin").read_bytes() == data
    assert len(host.calls) == 3 and all(len(stdin or b"") <= 256 * 1024 for _, _, stdin in host.calls)


# -- a session in a host folder -----------------------------------------------------------------------


async def test_a_session_in_a_host_folder_runs_from_its_own_directory_and_works_on_the_host(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.files import edit_file, read_file, write_file
    from daedalus.tools.shell import exec_command

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        host = ShellHost(disk)
        manager.host_bridge = host  # type: ignore[assignment]
        project = await host_project(manager)
        state = await manager.create_session("Labs chat", project_id=project.id)
        sid = state.session.id
        assert state.workspace == settings.workspaces_dir / host_home_name(sid) and state.workspace.is_dir()
        services = state.services
        assert services is not None and isinstance(services.exec_backend, HostExecBackend)
        assert services.workspace_dir == Path(HOST_ROOT) and services.walls is None
        assert manager.work_dir(state) == Path(HOST_ROOT)

        ctx = context(sid)
        assert not (await write_file().invoke(ctx, {"path": "src/lab.py", "content": "x = 1\n"})).is_error
        assert (disk / "src" / "lab.py").read_text() == "x = 1\n", "a relative path lands in the host folder"
        assert not (await edit_file().invoke(ctx, {"path": "src/lab.py", "old_string": "x = 1", "new_string": "x = 2"})).is_error
        assert (disk / "src" / "lab.py").read_text() == "x = 2\n"
        read = await read_file().invoke(ctx, {"path": "README.md"})
        assert "Labs: experiments" in read.content
        ran = await exec_command().invoke(ctx, {"command": "pwd && ls src"})
        assert not ran.is_error and HOST_ROOT in ran.content and "lab.py" in ran.content
        assert all(argv[0] == "bash" for argv, _, _ in host.calls), "everything went through the daemon's bash"
        assert not (state.workspace / "src").exists(), "nothing of the work is in the directory the session runs from"

        notes = await manager.workspace_notes(state)
        assert notes == ""
        (disk / "AGENTS.md").write_text("Labs: keep experiments small\n")
        assert "keep experiments small" in await manager.workspace_notes(state), "its notes are read on the host"
        assert "on the operator's own machine" in manager.notes_for(state)

        await manager.delete_session(sid)
        assert not state.workspace.exists(), "the directory it ran from goes with it"
    finally:
        await manager.close()


async def test_a_session_is_refused_a_host_folder_while_the_daemon_is_down(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        project = await host_project(manager)
        with pytest.raises(HostUnreachable, match=r"/home/someone/labs is on the host.*systemctl --user start daedalus-ptyd"):
            await manager.create_session("Labs chat", project_id=project.id)
        host = ShellHost(host_disk(tmp_path), up=False)
        manager.host_bridge = host  # type: ignore[assignment]
        with pytest.raises(HostUnreachable, match="not answering"):
            await manager.create_session("Labs chat", project_id=project.id, folder_id=project.primary.id)
        assert await manager.db.fetchone("SELECT 1 FROM sessions WHERE project_id = ?", (project.id,)) is None, "nothing is left behind"
    finally:
        await manager.close()


async def test_a_session_of_its_own_directory_and_a_read_only_folder(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.files import write_file

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        manager.host_bridge = ShellHost(disk)  # type: ignore[assignment]
        project = await host_project(manager)
        own = await manager.create_session("Errand", project_id=project.id, own_directory=True)
        assert own.services is not None and own.services.workspace_dir == Path(HOST_ROOT) / ".agents" / own.session.id
        assert (disk / ".agents" / own.session.id / "inbox").is_dir(), "its directory is made on the host before a command needs it"

        closed = await host_project(manager, readonly=True)
        reader = await manager.create_session("Reader", project_id=closed.id)
        refused = await write_file().invoke(context(reader.session.id), {"path": "x.txt", "content": "no"})
        assert refused.is_error and "read-only" in refused.content and not (disk / "x.txt").exists()
    finally:
        await manager.close()


async def test_a_background_job_runs_on_the_host_and_is_read_and_ended_through_the_daemon(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.shell import exec_command, job_kill, job_list, job_output

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        manager.host_bridge = ShellHost(disk)  # type: ignore[assignment]
        project = await host_project(manager)
        state = await manager.create_session("Labs chat", project_id=project.id)
        ctx = context(state.session.id)
        started = await exec_command().invoke(ctx, {"command": "echo serving; sleep 60", "background": True})
        assert not started.is_error and "running" in started.content and "on the host" in started.content
        job_id = started.content.split(":", 1)[0]
        assert (disk / ".jobs" / f"{job_id}.log").is_file(), "the log is in the folder's .jobs, on the host"

        async def said() -> bool:
            return "serving" in (await job_output().invoke(ctx, {"job_id": job_id})).content

        await until_await(said, "the job's output was read on the host")
        assert "running" in (await job_list().invoke(ctx, {})).content
        assert [v["state"] for v in await manager.task_views(state.session.id)] == ["running"]
        ended = await job_kill().invoke(ctx, {"job_id": job_id})
        assert not ended.is_error and "was killed by you" in ended.content and "None" not in ended.content

        quick = await exec_command().invoke(ctx, {"command": "exit 4", "background": True})
        quick_id = quick.content.split(":", 1)[0]

        async def ended_by_itself() -> bool:
            return "failed with exit code 4" in (await job_output().invoke(ctx, {"job_id": quick_id})).content

        await until_await(ended_by_itself, "a job that ended was seen with its exit code")
    finally:
        await manager.close()


async def test_a_service_is_refused_on_the_host_with_the_way_round(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.services import service_start

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        manager.host_bridge = ShellHost(host_disk(tmp_path))  # type: ignore[assignment]
        state = await manager.create_session("Labs chat", project_id=(await host_project(manager)).id)
        assert state.services is not None
        state.services.extra["manager"] = manager
        manager.service_hooks["services"] = lambda *a, **k: None
        refused = await service_start().invoke(context(state.session.id), {"name": "web", "command": "python -m http.server"})
        assert refused.is_error and "Exec(background=true)" in refused.content
    finally:
        await manager.close()


# -- a Daedalus staff member in a host folder -----------------------------------------------------------


async def test_a_daedalus_member_starts_works_and_reports_in_a_host_folder(settings: Settings, db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"tool": "Exec", "args": {"command": "echo measured > result.txt"}},
        {"tool": "Report", "args": {"kind": "done", "note": "result.txt holds the measurement"}},
        {"text": "Measured and reported."},
    ])
    manager = await _manager(settings, db, provider)
    team = await team_for(settings, manager)
    dispatcher = team.app.extensions["effects"]
    dispatcher.delivery_ready.clear()
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        host = ShellHost(disk, up=False)
        manager.host_bridge = host  # type: ignore[assignment]
        project = await host_project(manager)
        member = await manager.staff.hire(project.id, name="Ada", role="Measurements", isolation="shared")
        assert not (await team.runtimes["daedalus"].available("host")).ok, "a member cannot start while the daemon is down"

        host.up = True
        assert (await team.runtimes["daedalus"].available("host")).ok
        task = await Board(team.app).add(title="Measure", project_id=project.id, brief=BRIEF, operator=True)
        receipt = await team.assign(member, task, principal=OPERATOR, client_operation_id="host-native-launch",
                                    expected_entity_revision=task["entity_revision"])
        assert await dispatcher.step()
        assert (await dispatcher.store.view(receipt["effect_id"]))["state"] == "completed"

        async def reported() -> bool:
            row = await db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (task["id"],))
            return row is not None and row["status"] == "review"

        await until_await(reported, "the member worked on the host and handed in its report")
        assert (disk / "result.txt").read_text() == "measured\n", "its command ran in the host folder"
        session = await db.fetchone("SELECT session_id FROM staff_sessions WHERE task_id = ?", (task["id"],))
        state = await manager.get_state(session["session_id"])
        assert state is not None and state.workspace == settings.workspaces_dir / host_home_name(state.session.id)
        live = await team.live_of(member)
        if live is not None:
            _, cwd = await team.cwd_of(live)
            assert cwd == HOST_ROOT, "where the member works is the host folder, not the directory it runs from"
        assert any(argv[0] == "bash" and "result.txt" in argv[-1] for argv, _, _ in host.calls)
    finally:
        await close_team(manager)
        await manager.close()
