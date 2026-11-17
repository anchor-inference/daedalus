"""A job's log as the Jobs tab follows it, and the one count of running jobs every Jobs counter shows.

The tab opened a job's log as a file in Preview, read once: a running job showed what it had written by
the click and nothing after. It now asks for the bytes after the ones it holds. And the counter on the
tab added the session's working helpers to its own jobs, so it said three over a list of one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.filesystem import ExecOutcome
from daedalus.host.job_log import decode_piece, read_job_log, window_start
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.shell import Job, RemoteJob, job_kill
from tests.support.waiting import until_await
from tests.unit.test_host_exec import context
from tests.unit.test_job_wake import ended, host_session, start
from tests.unit.test_session_runner import ScriptedProvider, _manager


def local_job(log: Path, *, returncode: int | None) -> Job:
    """A job of this process whose child is a stand-in: running while ``returncode`` is None."""
    process: Any = SimpleNamespace(returncode=returncode, pid=0)
    return Job(id=log.stem, command="make", cwd=log.parent, log=log, process=process, started=0.0)


def test_a_read_starts_where_asked_and_at_the_tail_when_asked_for_it_or_the_log_shrank() -> None:
    assert window_start(10, 100, 50) == (10, False)
    assert window_start(100, 100, 50) == (100, False), "caught up: nothing new, nothing to drop"
    assert window_start(-1, 100, 30) == (70, True)
    assert window_start(-1, 10, 30) == (0, True)
    assert window_start(150, 100, 30) == (70, True), "a log shorter than what was read is another log"


def test_a_character_cut_by_the_piece_is_held_back_while_the_job_runs_and_skipped_at_a_tail_read() -> None:
    word = "сборка".encode()
    text, skipped, held = decode_piece(word[:5], mid_char=False, hold_tail=True)
    assert (text, skipped, held) == ("сб", 0, 1), "the half of 'о' waits for its other half"
    text, _, held = decode_piece(word[:5], mid_char=False, hold_tail=False)
    assert held == 0 and text.endswith("�"), "an ended job has nothing more to send: decoded as it is"
    text, skipped, _ = decode_piece(word[1:], mid_char=True, hold_tail=True)
    assert skipped == 1 and text == "борка"
    assert decode_piece(b"plain\n", mid_char=True, hold_tail=True) == ("plain\n", 0, 0)
    assert decode_piece("€".encode()[:2], mid_char=False, hold_tail=True) == ("", 0, 2)


async def test_a_running_local_job_is_followed_from_its_tail_and_then_byte_by_byte(tmp_path: Path) -> None:
    log = tmp_path / "job-1-t.log"
    log.write_bytes(b"x" * 5000 + b"\nlast line\n")
    job = local_job(log, returncode=None)
    first = await read_job_log(job, offset=-1, limit=100)
    assert first["running"] and first["state"] == "running" and first["restart"] and first["size"] == 5011
    assert first["text"].endswith("last line\n") and first["next"] == 5011 and first["start"] == 4911
    quiet = await read_job_log(job, offset=first["next"], limit=100)
    assert quiet["text"] == "" and not quiet["restart"] and quiet["next"] == 5011, "a quiet job moves nothing"
    with log.open("ab") as handle:
        handle.write("готово".encode()[:7])  # the log cut inside a character, as a writer can leave it
    grown = await read_job_log(job, offset=quiet["next"], limit=100)
    assert grown["text"] == "гот" and grown["next"] == 5017
    with log.open("ab") as handle:
        handle.write("готово".encode()[7:] + b"\n")
    job.process.returncode = 2  # type: ignore[union-attr]
    final = await read_job_log(job, offset=grown["next"], limit=100)
    assert final["text"] == "ово\n" and final["next"] == final["size"]
    assert final["state"] == "failed" and final["outcome"] == "failed" and final["exit_code"] == 2 and not final["running"]


async def test_a_log_rewritten_shorter_is_read_again_from_its_tail(tmp_path: Path) -> None:
    log = tmp_path / "job-1-t.log"
    log.write_text("short\n")
    piece = await read_job_log(local_job(log, returncode=0), offset=900, limit=100)
    assert piece["restart"] and piece["text"] == "short\n" and piece["state"] == "done"


async def test_a_machine_that_only_gives_its_last_lines_is_read_that_way(tmp_path: Path) -> None:
    class Terminal:
        async def job_tail(self, job_id: str, lines: int) -> str:
            return "a\nb\n"

        async def run(self, *args: Any, **kwargs: Any) -> ExecOutcome:
            raise AssertionError("a terminal client is not driven through a shell")

    job = RemoteJob(id="job-1-t", command="make", cwd=tmp_path, log=tmp_path / "x.log", pid=1, started=0.0, backend=Terminal())
    piece = await read_job_log(job, offset=40, limit=100)
    assert piece["lines_only"] and piece["restart"] and piece["text"] == "a\nb" and piece["size"] == -1


async def test_a_host_job_is_followed_through_the_shell_over_there(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid, _ = await host_session(manager, tmp_path)
        job_id = await start(sid, "printf 'one\\n'; sleep 0.6; printf 'two\\n'")
        job = manager.jobs[sid][job_id]
        assert isinstance(job, RemoteJob)

        async def wrote_one() -> bool:
            return "one" in (await read_job_log(job, offset=-1, limit=4096))["text"]

        await until_await(wrote_one, "the first line reached the log")
        first = await read_job_log(job, offset=-1, limit=4096)
        assert first["restart"] and not first["lines_only"] and first["text"] == "one\n" and first["next"] == 4
        await ended(manager, sid, job_id)
        rest = await read_job_log(job, offset=first["next"], limit=4096)
        assert rest["text"] == "two\n" and not rest["restart"] and rest["next"] == rest["size"] == 8
        assert rest["state"] == "done" and rest["exit_code"] == 0
    finally:
        await manager.close()


async def test_the_jobs_count_is_the_sessions_own_jobs_and_its_working_helpers_are_counted_apart(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    helper_run: asyncio.Task[None] | None = None
    try:
        leader = (await manager.create_session("leader")).session.id
        helper = (await manager.create_session("helper", metadata={"subagent_of": leader})).session.id
        own = await start(leader, "sleep 30", "c1")
        await start(helper, "sleep 30", "c2")  # a job the helper started: the helper's, not the leader's
        helper_run = asyncio.ensure_future(asyncio.sleep(30))
        manager._states[helper].task = helper_run  # the helper at work
        rows = {row["id"]: row for row in await manager.session_catalog()}
        assert rows[leader]["background_count"] == 1, "the helper and its job are not the leader's jobs"
        assert rows[leader]["subagents_running"] == 1
        assert rows[helper]["background_count"] == 1 and rows[helper]["subagents_running"] == 0
        assert manager.running_jobs(leader) == 1
        assert [view["id"] for view in await manager.task_views(leader)] == [own]
        await job_kill().invoke(context(leader), {"job_id": own})
        assert manager.running_jobs(leader) == 0
    finally:
        if helper_run is not None:
            helper_run.cancel()
        for sid in list(manager.jobs):
            for job_id in list(manager.jobs[sid]):
                await job_kill().invoke(context(sid), {"job_id": job_id})
        await manager.close()


async def test_the_route_follows_a_job_and_the_detail_counts_jobs_and_helpers_apart(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    leader = helper = ""

    async def helpers(session_id: str, **_: Any) -> list[dict[str, Any]]:
        # The detail reads a leader's helpers from the subagents extension; one of them is at work.
        return [{"session_id": helper, "status": "running", "running": True}] if session_id == leader else []

    extensions = {"subagents": SimpleNamespace(views=helpers)}
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions=extensions, guard=None, create_session=manager.create_session)
    headers = {"X-Daedalus-Token": "tok"}
    helper_run: asyncio.Task[None] | None = None
    try:
        leader = (await manager.create_session("leader")).session.id
        helper = (await manager.create_session("helper", metadata={"subagent_of": leader})).session.id
        log = tmp_path / "job-9-t.log"
        log.write_text("compiling\n")
        manager.jobs.setdefault(leader, {})["job-9-t"] = local_job(log, returncode=None)
        helper_run = asyncio.ensure_future(asyncio.sleep(30))
        manager._states[helper].task = helper_run
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            first = (await client.get(f"/api/sessions/{leader}/tasks/job-9-t/log", headers=headers)).json()
            assert first["text"] == "compiling\n" and first["running"] and first["restart"]
            with log.open("a") as handle:
                handle.write("linked\n")
            more = (await client.get(f"/api/sessions/{leader}/tasks/job-9-t/log", params={"offset": first["next"]}, headers=headers)).json()
            assert more["text"] == "linked\n" and not more["restart"]
            missing = await client.get(f"/api/sessions/{helper}/tasks/job-9-t/log", headers=headers)
            assert missing.status_code == 404, "a job is read only through the session that owns it"
            detail = (await client.get(f"/api/sessions/{leader}", headers=headers)).json()
            assert detail["background_count"] == 1 and detail["subagents_running"] == 1
            tasks = (await client.get(f"/api/sessions/{leader}/tasks", headers=headers)).json()
            assert tasks["background_count"] == 1 and [task["id"] for task in tasks["tasks"]] == ["job-9-t"]
            log.unlink()
            assert (await client.get(f"/api/sessions/{leader}/tasks/job-9-t/log", headers=headers)).status_code == 404
    finally:
        if helper_run is not None:
            helper_run.cancel()
        manager.jobs.get(leader, {}).clear()
        await manager.close()
