"""Read opens regular files only and streams them: a named pipe or a device is refused at once, a huge
file costs what its window costs, and the same holds through a shell on another machine."""

from __future__ import annotations

import asyncio
import io
import os
import random
import sys
import tracemalloc
from pathlib import Path

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.host import filesystem
from daedalus.host.filesystem import LocalFS, NotRegularFile, ShellFS, _byte_lines
from daedalus.host.services import SessionServices, locator
from daedalus.tools.files import edit_file, read_file, split_lines
from tests.unit.test_bench import LocalShellBackend

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="named pipes and /dev are POSIX")


def _ctx(session_id: str) -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": "c1"})


def _release(fifo: Path) -> None:
    """Hand a reader stuck on ``fifo`` its end of file, so a failing run does not leave a thread blocked
    for good (the interpreter waits for it at exit, and the whole suite would hang)."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


@posix_only
async def test_read_of_a_named_pipe_is_refused_at_once(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    locator.register(SessionServices(session_id="fifo", workspace_dir=tmp_path))
    try:
        result = await asyncio.wait_for(read_file().invoke(_ctx("fifo"), {"path": "pipe"}), timeout=5)
        assert result.is_error and "named pipe" in result.content and "regular file" in result.content
        edited = await asyncio.wait_for(edit_file().invoke(_ctx("fifo"), {"path": "pipe", "old_string": "a", "new_string": "b"}), timeout=5)
        assert edited.is_error
        with pytest.raises(NotRegularFile):
            await asyncio.wait_for(LocalFS().read_bytes(fifo, limit=1024), timeout=5)
        with pytest.raises(NotRegularFile):
            await asyncio.wait_for(LocalFS().read_text(fifo), timeout=5)
    finally:
        _release(fifo)
        locator.unregister("fifo")


@posix_only
async def test_a_pipe_swapped_in_after_the_check_does_not_hold_the_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The name passes the first look as a regular file and is a pipe by the time it is opened: the
    # non-blocking open returns at once and the check on the handle refuses it.
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    real_stat = os.stat
    regular = real_stat(Path(__file__))
    monkeypatch.setattr(filesystem.os, "stat", lambda path, *a, **k: regular if Path(path) == fifo else real_stat(path, *a, **k))
    try:
        with pytest.raises(NotRegularFile, match="named pipe"):
            await asyncio.wait_for(asyncio.to_thread(filesystem.open_regular, fifo), timeout=5)
    finally:
        _release(fifo)


@posix_only
async def test_read_of_a_device_is_refused_without_reading_it(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="dev", workspace_dir=tmp_path))
    try:
        result = await asyncio.wait_for(read_file().invoke(_ctx("dev"), {"path": "/dev/zero"}), timeout=5)
        assert result.is_error and "character device" in result.content
        with pytest.raises(NotRegularFile):
            await asyncio.wait_for(LocalFS().read_bytes(Path("/dev/zero"), limit=1 << 20), timeout=5)
    finally:
        locator.unregister("dev")


async def test_a_huge_single_line_is_streamed_not_loaded(tmp_path: Path) -> None:
    big = tmp_path / "bundle.min.js"
    with big.open("wb") as handle:
        for _ in range(24):
            handle.write(b"var a=1;" * (1 << 17))  # 1 MiB a piece, 24 MiB, and not one line break
    locator.register(SessionServices(session_id="huge", workspace_dir=tmp_path, max_tool_output_chars=4_000))
    try:
        tracemalloc.start()
        result = await read_file().invoke(_ctx("huge"), {"path": "bundle.min.js"})
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert not result.is_error and "on a single line" in result.content.splitlines()[0]
        assert len(result.content) <= 4_000
        assert peak < 8 << 20, f"the read held {peak} bytes for a 24 MiB file"
    finally:
        locator.unregister("huge")


async def test_a_file_too_large_to_count_still_reads_its_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filesystem, "LINE_COUNT_MAX_BYTES", 10_000)
    (tmp_path / "app.log").write_text("".join(f"event {i}\n" for i in range(20_000)), encoding="utf-8")
    locator.register(SessionServices(session_id="log", workspace_dir=tmp_path, max_tool_output_chars=4_000))
    try:
        result = await read_file().invoke(_ctx("log"), {"path": "app.log", "limit": 5})
        assert "     1\tevent 0" in result.content and "     5\tevent 4" in result.content
        assert "too large to count its lines" in result.content.splitlines()[0]
        assert "[more lines follow; continue with offset=6]" in result.content
        later = await read_file().invoke(_ctx("log"), {"path": "app.log", "offset": 19_999, "limit": 5})
        assert "event 19998" in later.content and "event 19999" in later.content and "continue with offset" not in later.content
    finally:
        locator.unregister("log")


async def test_an_offset_past_the_end_says_so(tmp_path: Path) -> None:
    (tmp_path / "s.txt").write_text("one\ntwo\n", encoding="utf-8")
    locator.register(SessionServices(session_id="past", workspace_dir=tmp_path))
    try:
        result = await read_file().invoke(_ctx("past"), {"path": "s.txt", "offset": 40})
        assert not result.is_error and "has 2 lines; offset=40 is past its end" in result.content
    finally:
        locator.unregister("past")


async def test_a_long_line_among_short_ones_is_named_as_cut(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("head\n" + "x" * 5_000 + "\ntail\n", encoding="utf-8")
    locator.register(SessionServices(session_id="wide", workspace_dir=tmp_path))
    try:
        result = await read_file().invoke(_ctx("wide"), {"path": "data.csv"})
        assert "     3\ttail" in result.content
        assert "line 2 ran past 2000 characters and was cut" in result.content
    finally:
        locator.unregister("wide")


async def test_an_edit_refuses_a_file_larger_than_it_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filesystem, "TEXT_MAX_BYTES", 1_000)
    (tmp_path / "big.txt").write_text("needle\n" + "hay\n" * 1_000, encoding="utf-8")
    locator.register(SessionServices(session_id="cap", workspace_dir=tmp_path))
    try:
        result = await edit_file().invoke(_ctx("cap"), {"path": "big.txt", "old_string": "needle", "new_string": "pin"})
        assert result.is_error and "more than the 1000 read here" in result.content and "Exec" in result.content
        assert (tmp_path / "big.txt").read_text(encoding="utf-8").startswith("needle\n")
    finally:
        locator.unregister("cap")


def test_lines_are_broken_the_same_streamed_as_whole() -> None:
    # Chunks as small as one byte put a CRLF across a boundary, the case the stream must not count twice.
    rng = random.Random(7)
    alphabet = ["a", "b", "\r", "\n", "\r\n", "é"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        data = text.encode("utf-8")
        size = rng.randint(1, 5)
        chunks = [data[i : i + size] for i in range(0, len(data), size)]
        streamed = [kept.decode("utf-8") for kept, _ in _byte_lines(chunks, hold=1_000)]
        assert streamed == split_lines(text), repr(text)
    for text in ("a\r\nb", "a\r", "\r\n\r\n", "x", "", "a\nb\n"):
        assert filesystem._count_lines(io.BytesIO(text.encode())) == len(split_lines(text)), repr(text)


@posix_only
async def test_read_through_a_shell_refuses_a_pipe_and_caps_a_long_line(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    fs = ShellFS(LocalShellBackend(tmp_path), timeout=20)
    try:
        with pytest.raises(NotRegularFile, match="named pipe"):
            await asyncio.wait_for(fs.read_window(fifo, start=0, max_lines=10, max_chars=4_000, line_chars=2_000), timeout=10)
        with pytest.raises(NotRegularFile):
            await asyncio.wait_for(fs.read_text(fifo), timeout=10)
        (tmp_path / "wide.txt").write_bytes(b"first\n" + b"y" * 3_000_000 + b"\nlast\n")
        window = await fs.read_window(tmp_path / "wide.txt", start=0, max_lines=400, max_chars=4_000, line_chars=2_000)
        assert window.lines[0] == "first" and len(window.lines[1]) == 2_000 and window.cut == [1]
        assert window.total_lines == 3 and window.size == 3_000_012
        tail = await fs.read_window(tmp_path / "wide.txt", start=2, max_lines=400, max_chars=4_000, line_chars=2_000)
        assert tail.lines == ["last"] and not tail.more
        (tmp_path / "blob").write_bytes(b"\x00\x01\x02")
        with pytest.raises(UnicodeDecodeError):
            await fs.read_window(tmp_path / "blob", start=0, max_lines=10, max_chars=4_000, line_chars=2_000)
    finally:
        _release(fifo)
