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

Host jobs outlive a restart of this process (they run detached on the operator's machine), so their
records are kept in the ``kv`` table and read back at start: a job that ended while the bot was down is
told after it is up again. Jobs of this process die with it and are not kept.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.host.host_exec import HostExecBackend
from daedalus.tools.shell import Job, RemoteJob

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
"""Host job records kept per session; the oldest finished ones go first."""
KEY_PREFIX = "host_jobs:"


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


def _outcome(code: int | None) -> str:
    if code == 0:
        return "succeeded"
    if code is None or code < 0:
        # A negative code is a signal (a local job) or a host job that ended without its wrapper
        # writing a code, which is what a kill of the whole group leaves.
        return f"was killed (exit code {code})" if code is not None and code != -1 else "was killed"
    return f"failed with exit code {code}"


def started_at(job: Job | RemoteJob) -> float:
    """The wall-clock start of a job, from this process's monotonic clock where nothing better is kept."""
    if isinstance(job, RemoteJob) and job.started_at:
        return job.started_at
    return time.time() - max(0.0, time.monotonic() - job.started)


def _record(job: RemoteJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "command": job.command,
        "cwd": str(job.cwd),
        "log": str(job.log),
        "pid": job.pid,
        "started_at": started_at(job),
        "exit_code": job.exit_code,
        "ended": job.ended,
        "reported": job.reported,
    }


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
        backend = state.services.exec_backend if state is not None and state.services is not None else None
        if not isinstance(backend, HostExecBackend):
            # The session is gone, or no longer works on the host: nobody could ask after these jobs.
            await manager.db.execute("DELETE FROM kv WHERE key = ?", (KEY_PREFIX + session_id,))
            return
        jobs = manager.jobs.setdefault(session_id, {})
        now_wall, now_mono = time.time(), time.monotonic()
        for record in records:
            if record["id"] in jobs:
                continue
            start = float(record.get("started_at") or now_wall)
            jobs[record["id"]] = RemoteJob(
                id=record["id"], command=record["command"], cwd=Path(record["cwd"]), log=Path(record["log"]),
                pid=int(record["pid"]), started=now_mono - max(0.0, now_wall - start), backend=backend,
                exit_code=record.get("exit_code"), ended=record.get("ended"), reported=bool(record.get("reported")),
                started_at=start,
            )
        self._written[session_id] = list(records)

    async def _persist(self, session_id: str) -> None:
        jobs = self.manager.jobs.get(session_id, {})
        records = [_record(job) for job in jobs.values() if isinstance(job, RemoteJob)]
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

    async def check(self) -> None:
        """One round: see which jobs ended, keep the host records, and tell the bursts that are over."""
        manager = self.manager
        now = time.monotonic()
        for session_id, jobs in list(manager.jobs.items()):
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
                if job.running:
                    continue
                self._asked.pop(key, None)
                if job.ended is None:
                    job.ended = time.time()
                self._detected.setdefault(key, now)
            try:
                await self._persist(session_id)
            except Exception:  # noqa: BLE001 — the records are written again next round
                logger.warning("could not record the host jobs of session %s", session_id, exc_info=True)
            await self._tell_if_due(session_id, now)

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
        head = f"[background job {job.id} {_outcome(job.exit_code)} after {_duration(took)}: `{_short(job.command)}`]"
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
            # A host job just started: its record is written now, not on the next round.
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
