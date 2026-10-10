"""Background jobs that end wake the session that started them.

A job started with ``Exec(background=true)`` used to end in silence: nothing in the host looked at it
again unless the agent called JobOutput, so a render that finished after the agent's turn was over was
never heard of, and the Tasks section kept a host job "running" for as long as the process lived. The
watcher here looks at every session's jobs every couple of seconds and, for each one that has ended and
whose end the agent has not read itself, submits one note to the session — the same way a finished
subagent reports to its leader: a steer while a turn runs, a new turn when the session is idle.

One note per burst: jobs of one session that end close together are told in one message, so ten
parallel builds ending within a second wake the agent once and not ten times. A job is told once: the
mark that it was is set before the note is submitted, and the agent's own JobOutput, JobKill or JobList
that read its end sets the same mark, so a job awaited within the turn wakes nothing.

Every job's record is kept in the ``kv`` table and read back at start, so the contract holds across a
restart. A host job outlives this process (it runs detached on the operator's machine): one that ended
while the bot was down is told after it is up again, and one whose process vanished without its exit
file is told as lost. A job of this process does not outlive it, and is told as lost — before, it was
simply never heard of again, and the agent waited for news that could not come.

While a job runs the watcher says something at most twice, once each: when its log has not grown for
``tools.jobs.quiet_minutes`` (possibly stuck — a waiter that finds itself forever looks exactly like
this), and when it has run past ``tools.jobs.max_hours``. A service is expected to run and quietly, and
is spared both; a wait has its own timeout, and is spared both too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.host.host_exec import HostExecBackend
from daedalus.tools.shell import Job, RemoteJob, grew, outcome, outcome_words, title

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
"""How often a job of this process is looked at: one attribute read, so it can be often."""
HOST_REFRESH_SECONDS = 5.0
"""How often a running host job is asked about: each ask is one command through the terminal daemon."""
QUIET_SECONDS = 3.0
"""A burst is over once no further job of the session has ended for this long."""
MAX_HOLD_SECONDS = 15.0
"""The longest a finished job waits for its burst to end, so a steady trickle still gets told."""
TAIL_LINES = 8
TAIL_LINE_CHARS = 240
TAIL_CHARS = 1500
COMMAND_CHARS = 160
KEPT_RECORDS = 30
"""Job records kept per session; the oldest finished ones go first."""
QUIET_MINUTES = 15.0
MAX_HOURS = 6.0
"""The defaults of ``tools.jobs``, for a watcher whose manager has no configuration (the tests)."""
KEY_PREFIX = "jobs:"


def _short(command: str, limit: int = COMMAND_CHARS) -> str:
    line = " ".join(command.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def started_at(job: Job | RemoteJob) -> float:
    """The wall-clock start of a job, from this process's monotonic clock where nothing better is kept."""
    if job.started_at:
        return job.started_at
    return time.time() - max(0.0, time.monotonic() - job.started)


def _record(job: Job | RemoteJob) -> dict[str, Any]:
    pid = job.pid if isinstance(job, RemoteJob) else (job.process.pid if job.process is not None else 0)
    return {
        "id": job.id,
        "where": job.where,
        "kind": job.kind,
        "label": job.label,
        "command": job.command,
        "cwd": str(job.cwd),
        "log": str(job.log),
        "pid": pid,
        "started_at": started_at(job),
        "exit_code": job.exit_code,
        "ended": job.ended,
        "reported": job.reported,
        "stopped_by": job.stopped_by,
        "lost": job.lost,
        "flags": list(job.flags),
    }


def _from_record(record: dict[str, Any], backend: Any, now_wall: float, now_mono: float) -> Job | RemoteJob:
    start = float(record.get("started_at") or now_wall)
    common: dict[str, Any] = {
        "id": record["id"], "command": record["command"], "cwd": Path(record["cwd"]), "log": Path(record["log"]),
        "started": now_mono - max(0.0, now_wall - start), "ended": record.get("ended"), "reported": bool(record.get("reported")),
        "started_at": start, "kind": record.get("kind") or "job", "label": record.get("label") or "",
        "stopped_by": record.get("stopped_by") or "", "lost": bool(record.get("lost")), "flags": list(record.get("flags") or []),
        "grown_at": now_mono,
    }
    if record["where"] == "host":
        return RemoteJob(pid=int(record["pid"]), backend=backend, exit_code=record.get("exit_code"), **common)
    job = Job(process=None, **common)
    if record.get("exit_code") is None and not job.lost:
        # It was running when the last process ended, and it was that process's child: it is gone, and
        # nobody saw how it ended. Told as lost, once, like any other end.
        job.lost = True
        job.ended = now_wall
        job.reported = False
    return job


def _local_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


class JobWatch:
    """Watches every session's background jobs and tells each session of the ones that ended."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self._detected: dict[tuple[str, str], float] = {}
        """When the watcher first saw each finished, untold job end, by (session, job): the burst clock."""
        self._asked: dict[tuple[str, str], float] = {}
        """When each running host job was last asked about."""
        self._written: dict[str, list[dict[str, Any]]] = {}
        """The host job records last written per session, so an unchanged list is not written again."""

    @property
    def manager(self) -> SessionManager:
        manager = self.app.manager
        assert manager is not None
        return manager

    # -- keeping host jobs across a restart -----------------------------------------------------

    async def adopt(self) -> None:
        """Read back the host jobs the last process recorded and watch them again."""
        manager = self.manager
        rows = await manager.db.fetchall("SELECT key FROM kv WHERE key LIKE ?", (KEY_PREFIX + "%",))
        for row in rows:
            session_id = str(row["key"]).removeprefix(KEY_PREFIX)
            try:
                await self._adopt_session(session_id)
            except Exception:  # noqa: BLE001 — one session's records must not cost the others theirs
                logger.exception("could not read back the host jobs of session %s", session_id)

    async def _adopt_session(self, session_id: str) -> None:
        manager = self.manager
        records = await manager.db.kv_get(KEY_PREFIX + session_id, []) or []
        state = await manager.get_state(session_id)
        if state is None:
            await manager.db.execute("DELETE FROM kv WHERE key = ?", (KEY_PREFIX + session_id,))
            return
        backend = state.services.exec_backend if state.services is not None else None
        jobs = manager.jobs.setdefault(session_id, {})
        now_wall, now_mono = time.time(), time.monotonic()
        for record in records:
            if record["id"] in jobs:
                continue
            if record["where"] == "host" and not isinstance(backend, HostExecBackend):
                continue  # the session no longer works on the host: nobody could ask after this job
            jobs[record["id"]] = _from_record(record, backend, now_wall, now_mono)
        self._written[session_id] = list(records)
        # Written back at once: a local job just found lost is a change the next start must not find again.
        await self._persist(session_id)

    async def _persist(self, session_id: str) -> None:
        jobs = self.manager.jobs.get(session_id, {})
        records = [_record(job) for job in jobs.values() if isinstance(job, Job | RemoteJob)]
        # The newest records stay; a finished, told job is the first to go, a running one never does.
        while len(records) > KEPT_RECORDS:
            spare = next((r for r in records if r["exit_code"] is not None and r["reported"]), None)
            if spare is None:
                break
            records.remove(spare)
        if records == self._written.get(session_id, []):
            return
        if records:
            await self.manager.db.kv_set(KEY_PREFIX + session_id, records)
        else:
            await self.manager.db.execute("DELETE FROM kv WHERE key = ?", (KEY_PREFIX + session_id,))
        self._written[session_id] = records

    async def forget(self, session_id: str) -> None:
        """A deleted session takes its records along; its jobs were already ended by the delete."""
        self._written.pop(session_id, None)
        for key in [k for k in self._detected if k[0] == session_id]:
            self._detected.pop(key, None)
        await self.manager.db.execute("DELETE FROM kv WHERE key = ?", (KEY_PREFIX + session_id,))

    # -- watching ------------------------------------------------------------------------------

    def _limits(self) -> tuple[float, float]:
        """The quiet and the overdue thresholds, in seconds; zero switches one off."""
        config = getattr(getattr(getattr(self.manager, "config", None), "tools", None), "jobs", None)
        quiet = float(getattr(config, "quiet_minutes", QUIET_MINUTES)) * 60.0
        overdue = float(getattr(config, "max_hours", MAX_HOURS)) * 3600.0
        return quiet, overdue

    async def check(self) -> None:
        """One round: see which jobs ended, keep the records, nudge about the stuck, tell the bursts that are over."""
        manager = self.manager
        now = time.monotonic()
        for session_id, jobs in list(manager.jobs.items()):
            nudges: list[tuple[Job | RemoteJob, str]] = []
            for job in list(jobs.values()):
                if job.reported:
                    continue
                key = (session_id, job.id)
                if isinstance(job, RemoteJob) and job.running:
                    if now - self._asked.get(key, 0.0) < HOST_REFRESH_SECONDS:
                        continue
                    self._asked[key] = now
                    try:
                        await job.refresh()
                    except Exception:  # noqa: BLE001 — the host is asked again next round
                        logger.debug("could not ask after host job %s", job.id, exc_info=True)
                elif job.running:
                    grew(job, _local_size(job.log))
                if job.running:
                    if (flag := self._due_flag(job, now)) is not None:
                        nudges.append((job, flag))
                    continue
                self._asked.pop(key, None)
                if job.ended is None:
                    job.ended = time.time()
                self._detected.setdefault(key, now)
            try:
                await self._persist(session_id)
            except Exception:  # noqa: BLE001 — the records are written again next round
                logger.warning("could not record the jobs of session %s", session_id, exc_info=True)
            await self._tell_if_due(session_id, now)
            if nudges:
                await self._nudge(session_id, nudges)

    def _due_flag(self, job: Job | RemoteJob, now: float) -> str | None:
        """The nudge a running job has earned and not yet had: "quiet", "overdue", or None."""
        if job.kind != "job":
            return None
        quiet, overdue = self._limits()
        if quiet > 0 and "quiet" not in job.flags and job.grown_at and now - job.grown_at >= quiet:
            return "quiet"
        if overdue > 0 and "overdue" not in job.flags and now - job.started >= overdue:
            return "overdue"
        return None

    async def _nudge(self, session_id: str, nudges: list[tuple[Job | RemoteJob, str]]) -> None:
        manager = self.manager
        if manager.shutting_down or manager.recovering:
            return
        for job, flag in nudges:
            job.flags.append(flag)  # before the submit, which yields: a nudge is said once
        try:
            await self._persist(session_id)
            blocks = [await self._nudge_block(job, flag) for job, flag in nudges]
            await manager.submit(session_id, "\n\n".join(blocks), steer=True, as_answer=False, origin="job")
        except Exception:  # noqa: BLE001 — a nudge is a courtesy; the end is still reported
            logger.exception("could not nudge session %s about its running jobs", session_id)

    async def _nudge_block(self, job: Job | RemoteJob, flag: str) -> str:
        took = _duration(time.time() - started_at(job))
        if flag == "quiet":
            silent = _duration(time.monotonic() - job.grown_at)
            head = f"[background job {job.id} is still running after {took}, and its log has not grown for {silent}: possibly stuck — `{_short(title(job))}`]"
        else:
            head = f"[background job {job.id} has been running for {took}, past the {_duration(self._limits()[1])} expected of a job — `{_short(title(job))}`]"
        tail = await self._tail(job)
        advice = "JobOutput reads it, JobKill stops it; it still wakes you when it ends, and this is the only reminder."
        return self.manager.redactor.redact(f"{head}\n{advice}" + (f"\nthe last lines of {job.log}:\n{tail}" if tail else ""))

    async def _tell_if_due(self, session_id: str, now: float) -> None:
        manager = self.manager
        jobs = manager.jobs.get(session_id, {})
        due = [job for job in jobs.values() if not job.running and not job.reported]
        # A job the agent read the end of after it was detected leaves its burst clock behind.
        for key in [k for k in self._detected if k[0] == session_id and (k[1] not in jobs or jobs[k[1]].reported)]:
            self._detected.pop(key, None)
        if not due:
            return
        seen = [self._detected.get((session_id, job.id), now) for job in due]
        if now - max(seen) < QUIET_SECONDS and now - min(seen) < MAX_HOLD_SECONDS:
            return
        if manager.shutting_down or manager.recovering:
            return  # told once the process is up again: a host job's record still says it was not
        # Marked before the submit, which yields: a second round, or the agent's own JobOutput, must
        # not find the same jobs untold and tell them again.
        for job in due:
            job.reported = True
            self._detected.pop((session_id, job.id), None)
        try:
            await self._persist(session_id)
            text = await self.note(due)
            await manager.submit(session_id, text, steer=True, as_answer=False, origin="job")
        except Exception:  # noqa: BLE001 — as a subagent's report: logged, and the agent reads JobList
            logger.exception("could not tell session %s that its background jobs ended", session_id)

    async def note(self, jobs: list[Job | RemoteJob]) -> str:
        """The message for one burst: a header, then each job's outcome, command and last lines."""
        blocks = [await self._block(job) for job in sorted(jobs, key=lambda j: j.ended or 0.0)]
        if len(blocks) == 1:
            return blocks[0]
        return f"[{len(blocks)} background jobs ended]\n\n" + "\n\n".join(blocks)

    async def _block(self, job: Job | RemoteJob) -> str:
        took = (job.ended or time.time()) - started_at(job)
        noun = {"wait": "wait", "service": "service"}.get(job.kind, "background job")
        head = f"[{noun} {job.id} {outcome_words(job)} after {_duration(took)}: `{_short(title(job))}`]"
        if job.label:
            head += f"\ncommand: `{_short(job.command)}`" if outcome(job) not in ("succeeded", "timed_out") else ""
        tail = await self._tail(job)
        where = f"the last lines of {job.log}" if tail else f"it printed nothing; the log is {job.log}"
        # The command and its log alike: a command line carries a key as readily as its output prints one.
        return self.manager.redactor.redact(f"{head}\n{where}" + (f":\n{tail}" if tail else ""))

    async def _tail(self, job: Job | RemoteJob) -> str:
        try:
            if isinstance(job, RemoteJob):
                text = await job.tail(TAIL_LINES)
            else:
                text = await asyncio.to_thread(_local_tail, job.log)
        except Exception:  # noqa: BLE001 — the note goes without the lines; the log stays where it is
            logger.debug("could not read the tail of job %s", job.id, exc_info=True)
            return ""
        rows = [row if len(row) <= TAIL_LINE_CHARS else row[: TAIL_LINE_CHARS - 1] + "…" for row in text.splitlines()[-TAIL_LINES:]]
        return "\n".join(rows)[-TAIL_CHARS:].strip("\n")

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "track":
            # A job just started: its record is written now, not on the next round.
            await self._persist(str(kwargs["session_id"]))
            return None
        raise ValueError(op)

    async def run(self) -> None:
        try:
            await self.adopt()
        except Exception:  # noqa: BLE001 — new jobs are still watched
            logger.exception("could not read back the host jobs of the last process")
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.check()
            except Exception:  # noqa: BLE001 — one bad round must not end the watching
                logger.exception("the background job watcher failed a round")


def _local_tail(path: Path) -> str:
    """The end of a local log, read from its last 64 KiB: a render log can run to megabytes."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


async def install(app: Application) -> list[asyncio.Task[None]]:
    watch = JobWatch(app)
    app.extensions["jobs"] = watch
    assert app.manager is not None
    app.manager.service_hooks["jobs"] = watch.service
    app.manager.delete_hooks.append(watch.forget)
    return [asyncio.create_task(watch.run(), name="job-watch")]
