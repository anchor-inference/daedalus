"""A background job that ends wakes the session that started it, once, the way a finished subagent does.

The defect these guard: a job started with ``Exec(background=true)`` ended in silence — nothing looked
at it again unless the agent called JobOutput — and a host job stayed "running" in the Tasks section
with a stop button long after it had finished, because its exit code was only ever asked for by the
agent's own tools.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.extensions import jobs as job_watch
from daedalus.extensions.jobs import KEY_PREFIX, JobWatch
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.shell import RemoteJob, exec_command, job_kill, job_list, job_output
from tests.support.waiting import until, until_await
from tests.unit.test_host_exec import ShellHost, container_only, host_disk, host_project
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture(autouse=True)
def quick_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher's clocks shrunk to test size; what they decide is the same."""
    monkeypatch.setattr(job_watch, "TICK_SECONDS", 0.05)
    monkeypatch.setattr(job_watch, "HOST_REFRESH_SECONDS", 0.0)
    monkeypatch.setattr(job_watch, "QUIET_SECONDS", 0.0)
    monkeypatch.setattr(job_watch, "MAX_HOLD_SECONDS", 30.0)


def context(session_id: str, call: str = "c1") -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": call})


def watch_for(manager: SessionManager) -> JobWatch:
    watch = JobWatch(SimpleNamespace(manager=manager, extensions={}))  # type: ignore[arg-type]
    manager.service_hooks["jobs"] = watch.service
    manager.delete_hooks.append(watch.forget)
    return watch


def capture(manager: SessionManager) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []

    async def fake_submit(session_id: str, text: str, attachments: Any = (), **kwargs: Any) -> str:
        submitted.append({"session_id": session_id, "text": text, **kwargs})
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


async def start(session_id: str, command: str, call: str = "c1") -> str:
    started = await exec_command().invoke(context(session_id, call), {"command": command, "background": True})
    assert not started.is_error, started.content
    return str(started.metadata["job_id"])


async def ended(manager: SessionManager, session_id: str, job_id: str) -> None:
    job = manager.jobs[session_id][job_id]
    if isinstance(job, RemoteJob):
        async def done() -> bool:
            await job.refresh()
            return not job.running

        await until_await(done, f"host job {job_id} ended")
    else:
        await until(lambda: not job.running, f"job {job_id} ended")


def texts_of(request: Any) -> list[str]:
    return [b.text for m in request.messages if m.role is MessageRole.user for b in m.content_blocks if isinstance(b, TextBlock)]


# -- the session's own jobs -----------------------------------------------------------------------


async def test_a_job_that_ends_while_the_session_is_idle_starts_a_turn_with_how_it_ended(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "noted"}])
    manager = await _manager(settings, db, provider)
    try:
        watch = watch_for(manager)
        state = await manager.create_session("idle")
        job_id = await start(state.session.id, f"sleep 0.5; for i in $(seq 1 20); do echo line-$i; done; echo API_KEY={SECRET}; exit 3")
        await ended(manager, state.session.id, job_id)
        waiter = asyncio.create_task(_wait_finished(manager))
        await watch.check()
        finished = await waiter
        assert finished[0][0] == state.session.id and finished[0][2] == "completed", "the end of the job started a turn"
        [note] = [t for t in texts_of(provider.requests[0]) if "background job" in t]
        assert f"[background job {job_id} failed with exit code 3 after " in note
        assert "sleep 0.5; for i in" in note, "the command is named"
        assert "line-20" in note and "line-5\n" not in note, "only the last lines of the log"
        assert SECRET not in note, "the log is masked before it reaches the model"
        history = await manager.sessions.list_transcript(state.session.id)
        assert any(m.metadata.get("daedalus.origin") == "job" for m in history)
        # The core's own work pool is off: it is not ours, and looking for it reported every run detached.
        events = await db.fetchall("SELECT payload FROM session_events WHERE session_id = ?", (state.session.id,))
        assert events and not any("background_tasks_detached" in row["payload"] for row in events)
        # One end, one wake: later rounds find the job told.
        submitted = capture(manager)
        for _ in range(3):
            await watch.check()
        assert submitted == []
    finally:
        await manager.close()


async def test_a_job_that_ends_during_a_turn_is_steered_into_that_turn(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([
        {"tool": "Exec", "args": {"command": "echo built-it; sleep 0.2", "background": True}},
        {"tool": "Exec", "args": {"command": "sleep 3"}},
        {"text": "both done"},
    ])
    manager = await _manager(settings, db, provider)
    watch = watch_for(manager)
    watcher = asyncio.create_task(watch.run())
    try:
        state = await manager.create_session("busy")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "build it")
        finished = await waiter
        assert [f[2] for f in finished] == ["completed"]
        assert len(provider.requests) == 3, "the note went into the running turn instead of starting another"
        notes = [t for t in texts_of(provider.requests[2]) if "[background job " in t]
        assert len(notes) == 1 and "succeeded" in notes[0] and "built-it" in notes[0]
        assert not manager.jobs[state.session.id] or all(j.reported for j in manager.jobs[state.session.id].values())
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await manager.close()


async def test_a_job_the_agent_read_to_its_end_wakes_nothing(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        state = await manager.create_session("awaited")
        sid = state.session.id
        read = await start(sid, "sleep 0.3; echo read", "c1")
        listed = await start(sid, "sleep 0.3; echo listed", "c2")
        killed = await start(sid, "sleep 30", "c3")
        await ended(manager, sid, read)
        await ended(manager, sid, listed)
        # The watcher may already have seen them end; the agent's read still comes first.
        await watch.check()
        submitted.clear()
        manager.jobs[sid][read].reported = False
        manager.jobs[sid][listed].reported = False
        assert "succeeded" in (await job_output().invoke(context(sid), {"job_id": read})).content
        assert "succeeded" in (await job_list().invoke(context(sid), {})).content
        assert not (await job_kill().invoke(context(sid), {"job_id": killed})).is_error
        for _ in range(3):
            await watch.check()
        assert submitted == [], "every end here was read by the agent itself"
        # An instant failure is in Exec's own answer and is not told again either.
        instant = await start(sid, "exit 7", "c4")
        assert manager.jobs[sid][instant].reported
        await watch.check()
        assert submitted == []
    finally:
        await manager.close()


async def test_a_burst_of_ends_is_told_in_one_message(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_watch, "QUIET_SECONDS", 3600.0)
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        state = await manager.create_session("burst")
        sid = state.session.id
        ids = [await start(sid, f"echo part-{n}; exit {n}", f"c{n}") for n in (1, 2)]
        for job in manager.jobs[sid].values():
            job.reported = False  # they ended inside Exec's 0.3 s; here they stand for jobs that ended later
        ids.append(await start(sid, "sleep 0.3; echo part-0", "c0"))
        await ended(manager, sid, ids[-1])
        await watch.check()
        assert submitted == [], "the burst is held while ends keep arriving"
        monkeypatch.setattr(job_watch, "QUIET_SECONDS", 0.0)
        await watch.check()
        [wake] = submitted
        assert wake["text"].startswith("[3 background jobs ended]")
        assert wake["steer"] is True and wake["origin"] == "job" and wake["as_answer"] is False
        for job_id in ids:
            assert f"[background job {job_id} " in wake["text"]
        assert "part-0" in wake["text"] and "part-2" in wake["text"]
        await watch.check()
        assert len(submitted) == 1
    finally:
        await manager.close()


async def test_a_job_the_operator_stopped_is_told_as_killed(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        state = await manager.create_session("stopped")
        job_id = await start(state.session.id, "echo waiting; sleep 30")
        assert await manager.stop_task(state.session.id, job_id)
        await watch.check()
        [wake] = submitted
        assert f"[background job {job_id} was killed by the operator" in wake["text"] and "waiting" in wake["text"]
    finally:
        await manager.close()


# -- jobs on the host -----------------------------------------------------------------------------


async def host_session(manager: SessionManager, tmp_path: Path) -> tuple[str, Path]:
    container_only(manager)
    disk = host_disk(tmp_path)
    manager.host_bridge = ShellHost(disk)  # type: ignore[assignment]
    project = await host_project(manager)
    state = await manager.create_session("Labs chat", project_id=project.id)
    assert state.services is not None
    state.services.extra["manager"] = manager
    return state.session.id, disk


async def test_a_host_job_that_ends_wakes_the_session_and_leaves_the_tasks_section(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        submitted = capture(manager)
        sid, _disk = await host_session(manager, tmp_path)
        job_id = await start(sid, "sleep 0.5; echo RENDER DONE frames=4776; exit 4")
        assert (await db.kv_get(KEY_PREFIX + sid))[0]["id"] == job_id, "a host job is recorded as soon as it starts"
        assert [v["state"] for v in await manager.task_views(sid)] == ["running"]

        async def told() -> bool:
            await watch.check()
            return bool(submitted)

        await until_await(told, "the host job's end was told")
        [wake] = submitted
        assert f"[background job {job_id} failed with exit code 4 after " in wake["text"] and "RENDER DONE frames=4776" in wake["text"]
        # Nobody called JobOutput, and the Tasks section still knows the job is over.
        [view] = await manager.task_views(sid)
        assert view["state"] == "failed" and view["stop_supported"] is False
        record = (await db.kv_get(KEY_PREFIX + sid))[0]
        assert record["exit_code"] == 4 and record["reported"] is True
    finally:
        await manager.close()


async def test_a_host_job_that_ended_while_the_bot_was_down_is_told_after_the_restart(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch = watch_for(manager)
        sid, disk = await host_session(manager, tmp_path)
        job_id = await start(sid, "sleep 0.5; echo finished-alone")
        record = (await db.kv_get(KEY_PREFIX + sid))[0]
        # The process goes away with the job still running: only its record is left.
        manager.jobs.clear()
        exit_file = disk / ".jobs" / f"{job_id}.log.exit"
        await until(exit_file.exists, "the host job finished on its own")
        mtime = exit_file.stat().st_mtime
        # The host has since handed the job's process id to something that is very much alive.
        record["pid"] = os.getpid()
        await db.kv_set(KEY_PREFIX + sid, [record])

        after = watch_for(manager)
        submitted = capture(manager)
        await after.adopt()
        assert isinstance(manager.jobs[sid][job_id], RemoteJob), "the job is known again, JobOutput included"
        assert "finished-alone" in (await job_output().invoke(context(sid), {"job_id": job_id})).content
        manager.jobs[sid][job_id].reported = False  # JobOutput read it; here it stands for a job nobody read
        await after.check()
        [wake] = submitted
        assert f"[background job {job_id} succeeded after " in wake["text"] and "finished-alone" in wake["text"]
        assert abs((manager.jobs[sid][job_id].ended or 0) - int(mtime)) <= 1, "the duration ends when the job did, not at the restart"
        await after.check()
        assert len(submitted) == 1
        del watch
    finally:
        await manager.close()


async def test_a_deleted_session_takes_its_host_job_records_along(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        watch_for(manager)
        sid, _disk = await host_session(manager, tmp_path)
        await start(sid, "sleep 30")
        assert await db.kv_get(KEY_PREFIX + sid)
        assert await manager.delete_session(sid)
        assert await db.kv_get(KEY_PREFIX + sid) is None
    finally:
        await manager.close()


def test_the_note_reads_durations_and_outcomes_plainly() -> None:
    assert job_watch._duration(9) == "9s" and job_watch._duration(556) == "9m 16s" and job_watch._duration(3 * 3600 + 120) == "3h 2m"
    long = "rm -rf a; " + "x" * 400 + "\nnext line"
    short = job_watch._short(long)
    assert len(short) == job_watch.COMMAND_CHARS and short.endswith("…") and "\n" not in short
    assert time.time() - job_watch.started_at(SimpleNamespace(started=time.monotonic() - 60, started_at=0.0)) == pytest.approx(60, abs=2)  # type: ignore[arg-type]
