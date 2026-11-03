"""A foreground command still running at its foreground time becomes a background job, not a kill.

The defect these guard: a foreground Exec ran until the tool timeout and its process group was then
killed, so a long build, install or test run started without ``background=true`` was lost and had to
run again from the start. Now the same process is kept as a job of the session at the moment its
foreground time is up: its output so far is in the result and in the job's log, everything after
goes on into that log, and its end wakes the agent like any job's. Cancelling the run still kills it.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from types import SimpleNamespace

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.jobs import KEY_PREFIX
from daedalus.host.services import SessionServices, locator
from daedalus.processes import pid_alive
from daedalus.remote.hands import DeviceExecBackend
from daedalus.stores.database import Database
from daedalus.tools.shell import Job, RemoteJob, _foreground_seconds, _run, exec_command, job_kill, job_output
from tests.support.waiting import until, until_await
from tests.unit.test_job_wake import capture, context, ended, host_session, watch_for
from tests.unit.test_remote_relay import DEVICE, _tool_results, rig
from tests.unit.test_session_runner import ScriptedProvider, _manager


async def told(watch: object, submitted: list[dict[str, object]]) -> dict[str, object]:
    async def check() -> bool:
        await watch.check()  # type: ignore[attr-defined]
        return bool(submitted)

    await until_await(check, "the job's end was told")
    return submitted[0]


# -- the local machine ----------------------------------------------------------------------------


async def test_a_command_running_past_its_time_is_kept_as_a_job_whose_end_wakes_the_agent(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        state = await manager.create_session("build")
        sid = state.session.id
        result = await exec_command().invoke(context(sid), {"command": "echo early-line; sleep 1.5; echo late-line; exit 7", "timeout_seconds": 1})
        assert not result.is_error, "a command that goes on is not a failure"
        assert result.metadata["handed_over"] is True and result.metadata["running"] is True
        job_id = str(result.metadata["job_id"])
        assert f"moved to the background as {job_id}" in result.content and "early-line" in result.content
        assert "do not poll" in result.content and "late-line" not in result.content
        job = manager.jobs[sid][job_id]
        assert isinstance(job, Job) and job.running and pid_alive(job.process.pid if job.process else None), "the same process carries on"
        assert (await db.kv_get(KEY_PREFIX + sid))[0]["id"] == job_id, "it is recorded the moment it is kept"
        assert [view["state"] for view in await manager.task_views(sid)] == ["running"], "the app's Jobs list shows it"

        wake = await told(watch, submitted)
        assert f"[background job {job_id} failed with exit code 7 after " in str(wake["text"]) and "late-line" in str(wake["text"])
        log = job.log.read_text()
        assert "early-line" in log and "late-line" in log, "the log holds what came before the hand-over and after it"
        assert job.log.parent.name == ".jobs"
    finally:
        await manager.close()


async def test_jobkill_ends_a_kept_command_and_its_group(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("kill")).session.id
        result = await exec_command().invoke(context(sid), {"command": "echo started; sleep 30 & sleep 30; wait", "timeout_seconds": 1})
        job_id = str(result.metadata["job_id"])
        job = manager.jobs[sid][job_id]
        assert isinstance(job, Job) and job.process is not None
        pid = job.process.pid
        stopped = await job_kill().invoke(context(sid), {"job_id": job_id})
        assert not stopped.is_error and "was killed by you" in stopped.content
        assert not job.running and not pid_alive(pid)
        await until(lambda: not any(pid_alive(int(p)) for p in _group(pid)), "every process of its group ended")
    finally:
        await manager.close()


def _group(pgid: int) -> list[str]:
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if os.getpgid(int(entry)) == pgid:
                    found.append(entry)
            except (ProcessLookupError, PermissionError):
                pass
    return found


async def test_a_cancelled_run_still_kills_its_command_before_the_hand_over(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="cancel", workspace_dir=tmp_path))
    try:
        marker = tmp_path / "pid"
        call = asyncio.create_task(exec_command().invoke(context("cancel"), {"command": f"echo $$ > {marker}; exec sleep 30"}))
        await until(lambda: marker.exists() and marker.read_text().strip().isdigit(), "the command started")
        pid = int(marker.read_text())
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await until(lambda: not pid_alive(pid), "the stopped run took its command with it")
        assert not (tmp_path / ".jobs").exists(), "nothing was kept"
    finally:
        locator.unregister("cancel")


def test_the_foreground_time_is_the_setting_or_the_calls_own_and_always_under_the_tool_timeout(tmp_path: Path) -> None:
    config = RuntimeConfig()
    config.tools.exec.background_after_seconds = 45
    services = SessionServices(session_id="s", workspace_dir=tmp_path, tool_timeout_seconds=900.0, extra={"manager": SimpleNamespace(config=config)})
    ctx = context("s")
    locator.register(services)
    try:
        assert _foreground_seconds(ctx, services, None) == 45
        assert _foreground_seconds(ctx, services, 300) == 300, "an explicit timeout is how long the call blocks"
        assert _foreground_seconds(ctx, services, 5000) == 885, "never past the tool timeout, which cancels the call"
        services.tool_timeout_seconds = 30.0
        assert _foreground_seconds(ctx, services, None) == 15
    finally:
        locator.unregister("s")


async def test_an_explicit_timeout_blocks_that_long_and_then_keeps_the_command(settings: Settings, db: Database) -> None:
    """``timeout_seconds`` is how long the model will wait, not a deadline to kill at: a model that asked
    for two seconds and got a still-running build gets the build, as a job."""
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("deadline")).session.id
        quick = await exec_command().invoke(context(sid, "c-quick"), {"command": "sleep 0.2; echo quick", "timeout_seconds": 5})
        assert not quick.is_error and "exit_code=0" in quick.content and "handed_over" not in quick.metadata
        slow = await exec_command().invoke(context(sid, "c-slow"), {"command": "sleep 3; echo slow-done", "timeout_seconds": 2})
        assert slow.metadata.get("handed_over") is True and "still running after 2." in slow.content
        job = manager.jobs[sid][str(slow.metadata["job_id"])]
        await ended(manager, sid, job.id)
        assert job.exit_code == 0 and "slow-done" in job.log.read_text(), "the command was never killed"
    finally:
        await manager.close()


async def test_a_wait_kept_as_a_job_stays_a_wait(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("wait")).session.id
        result = await _run(context(sid), "sleep 30", None, 1, None, kind="wait", label="file /x to appear")
        job = manager.jobs[sid][str(result.metadata["job_id"])]
        assert job.kind == "wait" and job.label == "file /x to appear", "a wait kept as a job is reported as a wait"
        await job_kill().invoke(context(sid), {"job_id": job.id})
    finally:
        await manager.close()


async def test_a_kept_local_command_a_restart_ended_is_told_as_lost(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        sid = (await manager.create_session("restart")).session.id
        result = await exec_command().invoke(context(sid), {"command": "echo before; sleep 30", "timeout_seconds": 1})
        job_id = str(result.metadata["job_id"])
        await watch.check()
        job = manager.jobs[sid][job_id]
        assert isinstance(job, Job) and job.process is not None
        os.killpg(job.process.pid, signal.SIGKILL)  # the process the bot dies with
        manager.jobs.clear()

        after = watch_for(manager)
        submitted = capture(manager)
        await after.adopt()
        wake = await told(after, submitted)
        assert f"[background job {job_id} was lost" in str(wake["text"])
    finally:
        await manager.close()


# -- the host, through its terminal daemon --------------------------------------------------------


async def test_a_host_command_running_past_its_time_is_kept_on_the_host_and_wakes_the_agent(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        sid, disk = await host_session(manager, tmp_path)
        quick = await exec_command().invoke(context(sid, "c-quick"), {"command": "echo fast; exit 3", "timeout_seconds": 5})
        assert quick.is_error and "exit_code=3" in quick.content and "fast" in quick.content and "handed_over" not in quick.metadata
        assert not any((disk / ".jobs").glob("*")), "a command that ended in time leaves nothing on the host"

        result = await exec_command().invoke(context(sid, "c-slow"), {"command": "echo early-host; sleep 2; echo late-host; exit 5", "timeout_seconds": 1})
        assert not result.is_error and result.metadata.get("handed_over") is True, result.content
        job_id = str(result.metadata["job_id"])
        assert f"moved to the background as {job_id}" in result.content and "on the host" in result.content and "early-host" in result.content
        job = manager.jobs[sid][job_id]
        assert isinstance(job, RemoteJob) and job.pid > 0
        assert (disk / ".jobs" / f"{job_id}.log").is_file(), "its log is the job's, on the host"

        wake = await told(watch, submitted)
        assert f"[background job {job_id} failed with exit code 5 after " in str(wake["text"]) and "late-host" in str(wake["text"])
        log = (disk / ".jobs" / f"{job_id}.log").read_text()
        assert "early-host" in log and "late-host" in log
    finally:
        await manager.close()


async def test_a_cancelled_host_command_is_ended_on_the_host(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid, disk = await host_session(manager, tmp_path)
        call = asyncio.create_task(exec_command().invoke(context(sid, "c-stop"), {"command": "sleep 30"}))
        pidfile: list[Path] = []

        def started() -> bool:
            pidfile[:] = [p for p in (disk / ".jobs").glob("*.pid") if p.read_text().strip().isdigit()] if (disk / ".jobs").is_dir() else []
            return bool(pidfile)

        await until(started, "the command started on the host")
        pid = int(pidfile[0].read_text())
        assert pid_alive(pid)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await until(lambda: not pid_alive(pid), "the stopped run ended its command on the host")
        assert not manager.jobs.get(sid), "nothing was kept"
    finally:
        await manager.close()


async def test_a_kept_host_command_survives_a_restart_and_is_told_when_it_ends(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        sid, disk = await host_session(manager, tmp_path)
        result = await exec_command().invoke(context(sid), {"command": "echo before; sleep 2; echo after-restart", "timeout_seconds": 1})
        job_id = str(result.metadata["job_id"])
        await watch.check()
        manager.jobs.clear()  # the process goes away; the command runs on, on the host

        after = watch_for(manager)
        submitted = capture(manager)
        await after.adopt()
        assert isinstance(manager.jobs[sid][job_id], RemoteJob), "it is known again"
        wake = await told(after, submitted)
        assert f"[background job {job_id} succeeded after " in str(wake["text"]) and "after-restart" in str(wake["text"])
        assert "after-restart" in (await job_output().invoke(context(sid), {"job_id": job_id})).content
    finally:
        await manager.close()


# -- the operator's terminal client, over the relay ---------------------------------------------------


async def test_a_command_on_the_terminal_running_past_its_time_is_kept_there_and_wakes_the_agent(settings: Settings, db: Database, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    root = root.resolve()
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo early-dev; sleep 1.5; echo late-dev; exit 3", "timeout_seconds": 1}}, {"text": "it goes on"}])
    async with rig(settings, db, provider, root) as built:
        sid = await built.session()
        client = built.terminal()
        await client.connect()
        try:
            watch = watch_for(built.manager)
            await built.manager.submit(sid, "build it")
            assert await built.settled(1) == ["completed"]
            [result] = _tool_results(provider)
            assert "moved to the background as job-" in result and "on the operator's terminal" in result and "early-dev" in result
            [job] = built.manager.jobs[sid].values()
            assert isinstance(job, RemoteJob) and isinstance(job.backend, DeviceExecBackend) and job.where == f"device:{DEVICE}"
            assert job.id in client.jobs, "the terminal keeps it under the id the server chose"
            [request] = [r for r in client.requests if r["method"] == "exec.run"]
            assert request["params"]["job"]["log"] == str(root / ".jobs" / f"{job.id}.log") and "timeout_ms" not in request["params"]

            submitted = capture(built.manager)
            wake = await told(watch, submitted)
            assert f"[background job {job.id} failed with exit code 3 after " in str(wake["text"]) and "late-dev" in str(wake["text"])
            log = (root / ".jobs" / f"{job.id}.log").read_text()
            assert "early-dev" in log and "late-dev" in log
            polls = [row for row in await built.journal.list(session_id=sid) if row["method"] == "job.status"]
            assert polls == [], "the watcher's questions about a job are not journaled one by one"
        finally:
            await client.stop_jobs()
            await client.disconnect()


async def test_a_terminal_job_is_killed_survives_a_restart_and_is_lost_with_its_client(settings: Settings, db: Database, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    root = root.resolve()
    provider = ScriptedProvider([
        {"tool": "Exec", "args": {"command": "echo serving; sleep 30", "background": True}},
        {"tool": "Exec", "args": {"command": "echo second; sleep 30", "background": True}},
        {"text": "started"},
    ])
    async with rig(settings, db, provider, root) as built:
        sid = await built.session()
        client = built.terminal()
        await client.connect()
        try:
            watch = watch_for(built.manager)
            await built.manager.submit(sid, "serve")
            assert await built.settled(1) == ["completed"]
            first, second = built.manager.jobs[sid].values()
            assert isinstance(first, RemoteJob) and first.running and set(client.jobs) == {first.id, second.id}
            assert "serving" in (await job_output().invoke(context(sid), {"job_id": first.id})).content, "read between turns"

            stopped = await job_kill().invoke(context(sid), {"job_id": first.id})
            assert not stopped.is_error and "was killed by you" in stopped.content
            assert client.jobs[first.id]["exit_code"] is not None
            await watch.check()

            # The bot restarts: the jobs are read back from their records and asked after again.
            built.manager.jobs.clear()
            after = watch_for(built.manager)
            await after.adopt()
            again = built.manager.jobs[sid][second.id]
            assert isinstance(again, RemoteJob) and isinstance(again.backend, DeviceExecBackend)
            await again.refresh()
            assert again.running, "the terminal still runs it"

            # The client restarts too: it holds none of its old jobs, which went with it.
            await client.stop_jobs()
            await client.disconnect()
            client = built.terminal()
            await client.connect()
            submitted = capture(built.manager)
            wake = await told(after, submitted)
            assert f"[background job {second.id} was lost: the operator's terminal client exited or restarted" in str(wake["text"])
        finally:
            await client.stop_jobs()
            await client.disconnect()


async def test_a_terminal_too_old_for_jobs_is_asked_for_none(settings: Settings, db: Database, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    root = root.resolve()
    provider = ScriptedProvider([
        {"tool": "Exec", "args": {"command": "echo plain", "timeout_seconds": 1}},
        {"tool": "Exec", "args": {"command": "sleep 30", "background": True}},
        {"text": "done"},
    ])
    async with rig(settings, db, provider, root) as built:
        sid = await built.session()
        client = built.terminal(jobs=False)
        await client.connect()
        try:
            await built.manager.submit(sid, "go")
            assert await built.settled(1) == ["completed"]
            plain, background = _tool_results(provider)
            assert "plain" in plain and "exit_code=0" in plain
            assert "too old to run background jobs" in background
            assert all("job" not in r["params"] for r in client.requests if r["method"] == "exec.run")
        finally:
            await client.disconnect()
