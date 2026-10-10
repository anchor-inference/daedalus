"""The background-job contract: every job is heard of exactly once, and no command can hide one.

Three defects this guards. A waiter written as ``while pgrep -f "out.mp4"; do sleep 3; done`` matched
its own shell and never ended. A render started with ``nohup`` inside a plain Exec was watched by
nobody, so its end went unreported. And jobs of the bot's own process were forgotten across a restart,
so the agent waited for news that could not come.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from daedalus.config import Settings
from daedalus.extensions import jobs as job_watch
from daedalus.extensions.jobs import KEY_PREFIX
from daedalus.stores.database import Database, _all_jobs_kept
from daedalus.tools.job_guard import self_matching_pattern
from daedalus.tools.shell import RemoteJob, exec_command, job_kill, job_wait
from tests.support.waiting import until_await
from tests.unit.test_host_exec import context
from tests.unit.test_job_wake import capture, ended, host_session, start, watch_for
from tests.unit.test_session_runner import ScriptedProvider, _manager

INCIDENT = 'cd promo/video; while pgrep -f "promo-v4.mp4" >/dev/null; do sleep 3; done; tail -c 200 ../scratch/render-v4.log; ls -la ../out/promo-v4.mp4'
INCIDENT_AFTER = 'cd promo/video; sleep 2; while pgrep -f "remotion render Promo ../out/promo-v3" >/dev/null; do sleep 2; done; tail -c 120 ../scratch/render-v3.log'


@pytest.fixture(autouse=True)
def quick_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher's clocks shrunk to test size; what they decide is the same."""
    monkeypatch.setattr(job_watch, "TICK_SECONDS", 0.05)
    monkeypatch.setattr(job_watch, "HOST_REFRESH_SECONDS", 0.0)
    monkeypatch.setattr(job_watch, "QUIET_SECONDS", 0.0)
    monkeypatch.setattr(job_watch, "MAX_HOLD_SECONDS", 30.0)


async def session(settings: Settings, db: Database) -> tuple[Any, str]:
    manager = await _manager(settings, db, ScriptedProvider([]))
    state = await manager.create_session("jobs")
    return manager, state.session.id


async def told(watch: Any, submitted: list[dict[str, Any]], count: int = 1) -> None:
    async def check() -> bool:
        await watch.check()
        return len(submitted) >= count

    await until_await(check, f"{count} report(s) were submitted")


# -- commands that hide a job ---------------------------------------------------------------------


def test_a_pgrep_loop_that_finds_its_own_shell_is_recognised_and_the_safe_forms_are_not() -> None:
    assert self_matching_pattern(INCIDENT) == "promo-v4.mp4"
    assert self_matching_pattern(INCIDENT_AFTER) is not None
    assert self_matching_pattern("while pgrep -f '[r]ender'; do sleep 3; done") is None, "the bracket idiom cannot match its own spelling"
    assert self_matching_pattern("pgrep -f x") is None, "a lone pgrep leaves itself out"
    assert self_matching_pattern("while pgrep -x node; do sleep 3; done") is None, "-x matches names, not command lines"


@pytest.mark.parametrize("background", [False, True])
async def test_exec_refuses_a_waiter_that_would_find_itself_and_names_the_tool_to_use(background: bool) -> None:
    for command in (INCIDENT, INCIDENT_AFTER):
        refused = await exec_command().invoke(context("none"), {"command": command, "background": background})
        assert refused.is_error and "JobWait" in refused.content and refused.content.startswith("refused:")


# -- a process left behind a plain Exec -----------------------------------------------------------


async def test_a_foreground_exec_that_leaves_a_process_behind_says_nothing_will_report_it(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        run = exec_command()
        left = await run.invoke(context(sid), {"command": "sleep 5 > /dev/null 2>&1 & echo started"})
        assert not left.is_error and "started" in left.content and "nothing will tell you when it ends" in left.content
        for command in ("echo a && echo b", "echo 'x & y'", "cat <<'EOF'\nfish & chips\nEOF"):
            plain = await run.invoke(context(sid), {"command": command})
            assert not plain.is_error and "[note:" not in plain.content, command
    finally:
        await manager.close()


# -- JobWait, in the foreground -------------------------------------------------------------------


async def wait(sid: str, **args: Any) -> Any:
    return await job_wait().invoke(context(sid), args)


async def test_jobwait_returns_when_a_process_exits_a_file_appears_or_a_port_opens(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, sid = await session(settings, db)
    try:
        child = subprocess.Popen(["sleep", "0.5"])
        # Reaped in a thread: a zombie keeps its /proc entry and would look alive for ever.
        threading.Thread(target=child.wait, daemon=True).start()
        gone = await wait(sid, pid=child.pid, timeout_seconds=20)
        assert not gone.is_error and gone.metadata["satisfied"] is True

        target = tmp_path / "out.bin"
        threading.Timer(0.5, target.write_text, ["x"]).start()
        appeared = await wait(sid, path=str(target), timeout_seconds=20)
        assert not appeared.is_error and appeared.metadata["satisfied"] is True

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            open_port = await wait(sid, port=port, timeout_seconds=20)
        assert not open_port.is_error and "accepts connections" in open_port.content
    finally:
        await manager.close()


async def test_jobwait_returns_the_line_that_matched_in_a_log(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, sid = await session(settings, db)
    try:
        log = tmp_path / "render.log"
        log.write_text("frame 1\n")
        threading.Timer(0.5, lambda: log.open("a").write("frame 2\nRENDER DONE frames=2\n")).start()
        found = await wait(sid, log=str(log), pattern="RENDER DONE", timeout_seconds=20)
        assert not found.is_error and "RENDER DONE frames=2" in found.content
    finally:
        await manager.close()


async def test_jobwait_that_runs_out_of_time_is_an_error_marked_timed_out(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, sid = await session(settings, db)
    try:
        late = await wait(sid, path=str(tmp_path / "never"), timeout_seconds=1)
        assert late.is_error and "timed out" in late.content
        assert late.metadata["timed_out"] is True and late.metadata["satisfied"] is False
    finally:
        await manager.close()


async def test_jobwait_for_a_job_returns_its_outcome_and_refuses_a_background_wait_for_it(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        job_id = await start(sid, "sleep 0.6; echo built; exit 2")
        done = await wait(sid, job_id=job_id, timeout_seconds=20)
        assert not done.is_error and "failed with exit code 2" in done.content and "built" in done.content
        assert done.metadata["running"] is False and done.metadata["exit_code"] == 2

        running = await start(sid, "sleep 30", "c2")
        refused = await wait(sid, job_id=running, background=True)
        assert refused.is_error and "already wakes you" in refused.content
        assert (await wait(sid, job_id="job-nope")).is_error
        await job_kill().invoke(context(sid), {"job_id": running})
    finally:
        await manager.close()


async def test_jobwait_for_a_pattern_in_a_jobs_own_log(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        job_id = await start(sid, "sleep 0.6; echo READY now; sleep 30")
        found = await wait(sid, job_id=job_id, pattern="READY", timeout_seconds=20)
        assert not found.is_error and "READY now" in found.content
        await job_kill().invoke(context(sid), {"job_id": job_id})
    finally:
        await manager.close()


# -- JobWait, in the background -------------------------------------------------------------------


async def test_a_background_wait_is_a_job_of_kind_wait_and_is_reported_once_when_it_holds(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, sid = await session(settings, db)
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        target = tmp_path / "ready.flag"
        started = await wait(sid, path=str(target), timeout_seconds=60, background=True)
        assert not started.is_error
        job = manager.jobs[sid][started.metadata["job_id"]]
        assert job.kind == "wait" and job.label.startswith("wait for ")
        target.write_text("x")
        await told(watch, submitted)
        for _ in range(3):
            await watch.check()
        [report] = submitted
        assert f"[wait {job.id} succeeded" in report["text"] and "wait for" in report["text"]
    finally:
        await manager.close()


async def test_a_background_wait_that_runs_out_of_time_is_reported_as_timed_out(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, sid = await session(settings, db)
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        started = await wait(sid, path=str(tmp_path / "never"), timeout_seconds=1, background=True)
        job_id = started.metadata["job_id"]
        await told(watch, submitted)
        assert "timed out" in submitted[0]["text"]
        [view] = [v for v in await manager.task_views(sid) if v["id"] == job_id]
        assert view["outcome"] == "timed_out" and view["kind"] == "wait"
    finally:
        await manager.close()


# -- services and the nudges ----------------------------------------------------------------------


async def test_a_service_is_never_called_quiet_but_its_end_is_reported(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sid = await session(settings, db)
    try:
        monkeypatch.setattr(manager.config.tools.jobs, "quiet_minutes", 0.0002)
        watch = watch_for(manager)
        submitted = capture(manager)
        started = await exec_command().invoke(context(sid), {"command": "sleep 30", "service": True})
        job = manager.jobs[sid][started.metadata["job_id"]]
        assert job.kind == "service"
        for _ in range(8):
            await watch.check()
            time.sleep(0.01)
        assert submitted == [] and job.flags == []
        assert (await job_kill().invoke(context(sid), {"job_id": job.id})).content
        job.reported = False
        await told(watch, submitted)
        assert f"[service {job.id} " in submitted[0]["text"]
    finally:
        await manager.close()


async def test_a_quiet_job_is_nudged_once_and_its_end_is_still_reported_once(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sid = await session(settings, db)
    try:
        monkeypatch.setattr(manager.config.tools.jobs, "quiet_minutes", 0.0002)
        watch = watch_for(manager)
        submitted = capture(manager)
        job_id = await start(sid, "sleep 30")
        await told(watch, submitted)
        assert "possibly stuck" in submitted[0]["text"]
        for _ in range(6):
            await watch.check()
            time.sleep(0.01)
        assert len(submitted) == 1, "the reminder is said once"
        manager.jobs[sid][job_id].reported = False
        await job_kill().invoke(context(sid), {"job_id": job_id})
        manager.jobs[sid][job_id].reported = False
        await told(watch, submitted, 2)
        await watch.check()
        assert len(submitted) == 2 and "killed" in submitted[1]["text"]
    finally:
        await manager.close()


async def test_a_job_past_its_hours_is_nudged_as_overdue_once(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sid = await session(settings, db)
    try:
        monkeypatch.setattr(manager.config.tools.jobs, "quiet_minutes", 0.0)
        monkeypatch.setattr(manager.config.tools.jobs, "max_hours", 0.00001)
        watch = watch_for(manager)
        submitted = capture(manager)
        job_id = await start(sid, "sleep 30")
        await told(watch, submitted)
        assert "past the" in submitted[0]["text"]
        for _ in range(5):
            await watch.check()
            time.sleep(0.01)
        assert len(submitted) == 1
        await job_kill().invoke(context(sid), {"job_id": job_id})
    finally:
        await manager.close()


# -- restarts -------------------------------------------------------------------------------------


async def test_a_local_job_that_a_restart_ended_is_told_once_as_lost(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        watch = watch_for(manager)
        job_id = await start(sid, "sleep 30")
        await watch.check()
        assert (await db.kv_get(KEY_PREFIX + sid))[0]["where"] == "local"
        pid = manager.jobs[sid][job_id].process.pid
        os.killpg(pid, signal.SIGKILL)  # the process the bot dies with
        manager.jobs.clear()

        after = watch_for(manager)
        submitted = capture(manager)
        await after.adopt()
        await after.check()
        [report] = submitted
        assert f"[background job {job_id} was lost" in report["text"]
        [view] = await manager.task_views(sid)
        assert view["state"] == "lost" and view["outcome"] == "lost" and view["stop_supported"] is False
        # Another start finds the record told, and says nothing.
        manager.jobs.clear()
        again = watch_for(manager)
        await again.adopt()
        await again.check()
        assert len(submitted) == 1
    finally:
        await manager.close()


async def test_a_host_job_that_vanished_is_lost_and_one_the_agent_killed_is_killed(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        sid, _disk = await host_session(manager, tmp_path)
        vanishing = await start(sid, "sleep 30", "c1")
        stopped = await start(sid, "sleep 30", "c2")
        os.killpg(manager.jobs[sid][vanishing].pid, signal.SIGKILL)  # from outside: no exit file is written
        await job_kill().invoke(context(sid, "c3"), {"job_id": stopped})
        manager.jobs[sid][stopped].reported = False
        await told(watch, submitted)
        text = "\n".join(s["text"] for s in submitted)
        assert "vanished without an exit code" in text and "was killed by you" in text
        outcomes = {v["id"]: v["outcome"] for v in await manager.task_views(sid)}
        assert outcomes == {vanishing: "lost", stopped: "killed"}
        assert isinstance(manager.jobs[sid][vanishing], RemoteJob)
    finally:
        await manager.close()


# -- one report, coalesced ------------------------------------------------------------------------


async def test_three_jobs_that_end_together_are_told_in_one_message_and_not_again(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        ids = [await start(sid, f"sleep 0.4; echo n{n}", f"c{n}") for n in range(3)]
        for job_id in ids:
            await ended(manager, sid, job_id)
        await watch.check()
        [report] = submitted
        assert report["text"].startswith("[3 background jobs ended]") and all(f"[background job {i} " in report["text"] for i in ids)
        for _ in range(3):
            await watch.check()
        assert len(submitted) == 1
    finally:
        await manager.close()


# -- what the app and the model are shown ---------------------------------------------------------


async def test_the_task_views_carry_the_contract_for_running_done_and_lost_jobs(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        watch = watch_for(manager)
        capture(manager)
        running = await start(sid, "sleep 30", "c1")
        done = await start(sid, "sleep 0.4; exit 0", "c2")
        await ended(manager, sid, done)
        lost = await start(sid, "sleep 30", "c3")
        os.killpg(manager.jobs[sid][lost].process.pid, signal.SIGKILL)
        manager.jobs[sid][lost].process = None  # what a restart leaves of a job of the process: a record, no child
        manager.jobs[sid][lost].lost = True
        manager.jobs[sid][lost].ended = time.time()
        await watch.check()  # the watcher is what stamps the moment a job was first seen finished
        views = {v["id"]: v for v in await manager.task_views(sid)}
        assert views[running]["state"] == "running" and views[running]["outcome"] is None and views[running]["ended_at"] is None
        assert views[running]["kind"] == "job" and views[running]["where"] == "local" and views[running]["command"] == "sleep 30"
        assert views[done]["state"] == "done" and views[done]["outcome"] == "succeeded" and views[done]["exit_code"] == 0 and views[done]["ended_at"]
        assert views[lost]["state"] == "lost" and views[lost]["outcome"] == "lost"
        for key in ("title", "flag", "reported"):
            assert key in views[running]
        counted = {r["id"]: r for r in await manager.session_catalog()}
        assert counted[sid]["background_count"] == 1, "only the running job counts"
        await job_kill().invoke(context(sid), {"job_id": running})
    finally:
        await manager.close()


async def test_the_turn_context_names_running_jobs_and_promises_a_wake_up(settings: Settings, db: Database) -> None:
    manager, sid = await session(settings, db)
    try:
        assert manager._running_jobs_note(sid) == ""
        job_id = await start(sid, "sleep 30")
        note = manager._running_jobs_note(sid)
        assert job_id in note and "sleep 30" in note and "wakes you" in note
        await job_kill().invoke(context(sid), {"job_id": job_id})
        assert manager._running_jobs_note(sid) == ""
    finally:
        await manager.close()


# -- the migration --------------------------------------------------------------------------------


async def test_the_migration_moves_host_job_records_and_survives_a_second_run(tmp_path: Path) -> None:
    async with aiosqlite.connect(tmp_path / "state.db", isolation_level=None) as conn:
        await conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        record = {"id": "job-1", "command": "make", "pid": 7}
        await conn.execute("INSERT INTO kv VALUES (?, ?)", ("host_jobs:abc", json.dumps([record])))
        await conn.execute("INSERT INTO kv VALUES (?, ?)", ("host_jobs:bad", "x"))
        for _ in range(2):
            await _all_jobs_kept(conn)
            rows = {k: v for k, v in await (await conn.execute("SELECT key, value FROM kv")).fetchall()}
            assert list(rows) == ["jobs:abc"]
            assert json.loads(rows["jobs:abc"]) == [{**record, "where": "host"}]
