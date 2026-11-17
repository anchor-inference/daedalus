"""A background job's log, read a piece at a time for the app's Jobs tab, the way ``tail -f`` follows it.

The tab used to open a job's log as a file in Preview, which read the whole file once and never
again: a running build showed what it had written by the click and nothing after, and a log of a few
hundred megabytes was read in full to show its last screen. Here the app asks for the bytes after the
ones it already holds, so a poll of a quiet job moves nothing, and a first look at a large log reads
only its tail.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
from typing import Any

from daedalus.host.filesystem import open_regular
from daedalus.tools.shell import Job, RemoteJob
from daedalus.tools.shell import outcome as shell_outcome

LOG_PIECE_MAX = 1024 * 1024
"""The most one read returns. A job that writes faster than the app asks is shown from its tail again,
not caught up a megabyte at a time behind a log that keeps running away."""
REMOTE_PIECE_MAX = 384 * 1024
"""One read from another machine stays under the host daemon's cap on one reply, base64 included."""
TAIL_LINES = 400
"""What a machine that can only be asked for its last lines (the operator's terminal client) gives."""

TASK_STATES = {"succeeded": "done", "failed": "failed", "killed": "cancelled", "timed_out": "failed", "lost": "lost"}
"""A job's outcome word as the state of a row in the Jobs list."""


def task_state(job: Job | RemoteJob) -> tuple[str, str | None]:
    """The row state and the outcome word of a job; the word is None while it runs."""
    word = shell_outcome(job)
    return ("running" if word is None else TASK_STATES[word]), word


def window_start(offset: int, size: int, limit: int) -> tuple[int, bool]:
    """Where a read begins, and whether the reader must drop what it holds and start over.

    A negative offset asks for the tail. An offset past the end means the log is not the one the
    reader followed (truncated, or written again under the same name), so it gets the tail too.
    """
    if offset < 0 or offset > size:
        return max(0, size - limit), True
    return offset, False


def decode_piece(data: bytes, *, mid_char: bool, hold_tail: bool) -> tuple[str, int, int]:
    """The text of a piece of UTF-8, with how many bytes were skipped at its head and kept back at its tail.

    A piece cut at an arbitrary byte can begin or end inside a character. Its head is skipped when the
    read began in the middle of the file (``mid_char``): the bytes before them were never asked for. Its
    tail is held back while the job runs (``hold_tail``), so the rest of that character arrives with the
    next piece instead of each half becoming a replacement mark; once the job has ended nothing more
    will come and the tail is decoded as it is.
    """
    skip = 0
    if mid_char:
        while skip < min(3, len(data)) and data[skip] & 0xC0 == 0x80:
            skip += 1
    end = len(data)
    if hold_tail and end > skip:
        lead = end - 1
        while lead > skip and end - lead < 4 and data[lead] & 0xC0 == 0x80:
            lead -= 1
        first = data[lead]
        need = 2 if 0xC0 <= first < 0xE0 else 3 if 0xE0 <= first < 0xF0 else 4 if first >= 0xF0 else 1
        if end - lead < need:
            end = lead
    return data[skip:end].decode("utf-8", "replace"), skip, len(data) - end


def _read_local(path: os.PathLike[str], offset: int, limit: int) -> tuple[int, int, bool, bytes]:
    with open_regular(path) as handle:  # type: ignore[arg-type]
        size = os.fstat(handle.fileno()).st_size
        start, restart = window_start(offset, size, limit)
        handle.seek(start)
        return size, start, restart, handle.read(min(limit, max(0, size - start)))


async def _read_remote(job: RemoteJob, offset: int, limit: int) -> tuple[int, int, bool, bytes] | str:
    """The piece from the machine the job runs on, in one round trip; the text of its tail where only that is offered."""
    if callable(getattr(job.backend, "job_tail", None)):
        return await job.tail(TAIL_LINES)
    limit = min(limit, REMOTE_PIECE_MAX)
    log = shlex.quote(str(job.log))
    # The size and the start are worked out over there, in the same command as the read, so a log that
    # grows between two round trips cannot be read from a start computed against an older size.
    script = (
        f"s=$(wc -c < {log} 2>/dev/null | tr -d ' ') || s=-1; [ -n \"$s\" ] || s=-1; "
        f"o={offset}; restart=0; if [ \"$s\" -lt 0 ]; then echo \"-1 0 0\"; exit 0; fi; "
        f"if [ \"$o\" -lt 0 ] || [ \"$o\" -gt \"$s\" ]; then restart=1; o=$(( s > {limit} ? s - {limit} : 0 )); fi; "
        f"echo \"$s $o $restart\"; tail -c +$(( o + 1 )) {log} 2>/dev/null | head -c {limit} | base64"
    )
    outcome = await job.backend.run(script, cwd=None, env=None, timeout=30.0)
    lines = outcome.output.splitlines()
    words = lines[0].split() if outcome.exit_code == 0 and lines else []
    if len(words) != 3 or not all(word.lstrip("-").isdigit() for word in words):
        raise OSError(f"could not read the log of {job.id}: {outcome.output.strip()[-300:] or 'no answer'}")
    size, start, restart = int(words[0]), int(words[1]), words[2] == "1"
    if size < 0:
        raise FileNotFoundError(f"the log of {job.id} is not there")
    try:
        data = base64.b64decode("".join(lines[1:]), validate=True)
    except ValueError as exc:
        raise OSError(f"the log of {job.id} came back unreadable") from exc
    return size, start, restart, data


async def read_job_log(job: Job | RemoteJob, *, offset: int, limit: int) -> dict[str, Any]:
    """The log of ``job`` from byte ``offset`` (its tail when negative), with how the job stands.

    The state is read before the bytes. A reader told the job ended then holds every byte the job
    wrote, and stops asking once ``next`` reaches ``size``; read the other way round, the last lines
    written between the read and the look could be missed for good.
    """
    limit = max(1, min(int(limit), LOG_PIECE_MAX))
    state, word = task_state(job)
    running = state == "running"
    view: dict[str, Any] = {"id": job.id, "state": state, "outcome": word, "exit_code": job.exit_code, "running": running}
    read = await _read_remote(job, offset, limit) if isinstance(job, RemoteJob) else await asyncio.to_thread(_read_local, job.log, offset, limit)
    if isinstance(read, str):
        # Only the last lines can be had: the whole view is replaced on every read, and there is no offset to follow.
        return {**view, "text": read, "start": 0, "next": 0, "size": -1, "restart": True, "lines_only": True}
    size, start, restart, data = read
    text, skipped, held = decode_piece(data, mid_char=restart and start > 0, hold_tail=running)
    return {**view, "text": text, "start": start + skipped, "next": start + len(data) - held, "size": size, "restart": restart, "lines_only": False}
