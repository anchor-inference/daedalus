"""Command output as the model reads it: no colour codes, no progress redraws, the raw log untouched.

A test runner's colours and a downloader's carriage-return progress bar arrived verbatim: one
`npm install` spent most of a result on `\\x1b[32m` and a hundred redrawn copies of one line.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.config import Settings
from daedalus.stores.database import Database
from daedalus.tools._common import plain_output
from daedalus.tools.shell import exec_command, job_output
from tests.unit.test_host_exec import context
from tests.unit.test_job_wake import ended, start
from tests.unit.test_session_runner import ScriptedProvider, _manager

NOISY = "\x1b[1m\x1b[32mPASS\x1b[0m tests/a.py\r\n\x1b]0;title\x07downloading  10%\rdownloading  50%\rdownloading 100%\n\x1b[2K\rdone\n"


def test_colour_codes_and_redraws_go_and_the_text_stays() -> None:
    assert plain_output(NOISY) == "PASS tests/a.py\ndownloading 100%\ndone\n"


def test_a_line_that_ends_in_a_carriage_return_keeps_its_last_drawing() -> None:
    assert plain_output("step 1/3\rstep 3/3\r\nnext") == "step 3/3\nnext"
    assert plain_output("spinner |\r") == "spinner |"


def test_plain_text_is_left_exactly_as_it_was() -> None:
    text = "a\tb\n  indented [brackets] and 50% done\n"
    assert plain_output(text) == text


async def test_exec_and_job_output_read_plain_while_the_log_keeps_the_raw_stream(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("plain")).session.id
        command = r"printf '\033[31mred\033[0m\n10%%\r90%%\r100%%\n'"
        foreground = await exec_command().invoke(context(sid), {"command": command})
        assert not foreground.is_error, foreground.content
        assert "\x1b" not in foreground.content and "\r" not in foreground.content
        assert "red\n100%" in foreground.content and "10%" not in foreground.content
        job_id = await start(sid, command, "c2")
        await ended(manager, sid, job_id)
        read = await job_output().invoke(context(sid, "c3"), {"job_id": job_id})
        lines = read.content.split("\n", 1)[1]  # below the header, which quotes the command
        assert "\x1b" not in lines and lines.startswith("red\n100%") and "90%" not in lines
        log = Path(manager.jobs[sid][job_id].log).read_bytes()
        assert b"\x1b[31m" in log and b"10%\r90%" in log, "the app's view of the log is the raw stream"
    finally:
        await manager.close()
