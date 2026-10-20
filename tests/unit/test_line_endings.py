"""An edit keeps the file's line endings, and Read numbers lines the way an editor does."""

from __future__ import annotations

from pathlib import Path

from protocore.contracts.tools import ToolContext

from daedalus.host.filesystem import LocalFS
from daedalus.host.services import SessionServices, locator
from daedalus.tools.files import edit_file, multi_edit, read_file, write_file


def _ctx(session_id: str) -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": "c"})


def _session(tmp_path: Path, session_id: str) -> ToolContext:
    locator.register(SessionServices(session_id=session_id, workspace_dir=tmp_path))
    return _ctx(session_id)


async def test_the_local_filesystem_reads_and_writes_line_endings_untranslated(tmp_path: Path) -> None:
    target = tmp_path / "dos.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    fs = LocalFS()
    assert await fs.read_text(target) == "one\r\ntwo\r\n"
    await fs.write_text(target, "a\nb\r\n")
    assert target.read_bytes() == b"a\nb\r\n"


async def test_a_crlf_file_stays_crlf_after_an_edit(tmp_path: Path) -> None:
    ctx = _session(tmp_path, "crlf")
    target = tmp_path / "dos.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    try:
        result = await edit_file().invoke(ctx, {"path": "dos.txt", "old_string": "alpha\nbeta", "new_string": "alpha\nBETA\nextra"})
        assert not result.is_error, result.content
        assert target.read_bytes() == b"alpha\r\nBETA\r\nextra\r\ngamma\r\n"
        result = await multi_edit().invoke(ctx, {"path": "dos.txt", "edits": [{"old_string": "gamma", "new_string": "g1\ng2"}]})
        assert not result.is_error, result.content
        assert target.read_bytes() == b"alpha\r\nBETA\r\nextra\r\ng1\r\ng2\r\n"
    finally:
        locator.unregister("crlf")


async def test_an_lf_file_stays_lf_after_an_edit(tmp_path: Path) -> None:
    ctx = _session(tmp_path, "lf")
    target = tmp_path / "unix.txt"
    target.write_bytes(b"alpha\nbeta\n")
    try:
        result = await edit_file().invoke(ctx, {"path": "unix.txt", "old_string": "beta", "new_string": "b1\nb2"})
        assert not result.is_error, result.content
        assert target.read_bytes() == b"alpha\nb1\nb2\n"
    finally:
        locator.unregister("lf")


async def test_a_mixed_file_keeps_its_untouched_lines_and_takes_the_dominant_ending(tmp_path: Path) -> None:
    ctx = _session(tmp_path, "mixed")
    target = tmp_path / "mixed.txt"
    # Three CRLF endings and one LF: the edit writes CRLF, and the lone LF line is left as it was.
    target.write_bytes(b"a\r\nb\r\nc\r\nlf-line\nd")
    try:
        result = await edit_file().invoke(ctx, {"path": "mixed.txt", "old_string": "b\nc", "new_string": "b\nnew\nc"})
        assert not result.is_error, result.content
        assert target.read_bytes() == b"a\r\nb\r\nnew\r\nc\r\nlf-line\nd"
    finally:
        locator.unregister("mixed")


async def test_writing_over_a_crlf_file_keeps_crlf_and_a_new_file_is_lf(tmp_path: Path) -> None:
    ctx = _session(tmp_path, "write")
    old = tmp_path / "old.txt"
    old.write_bytes(b"x\r\ny\r\n")
    try:
        assert not (await write_file().invoke(ctx, {"path": "old.txt", "content": "p\nq\n"})).is_error
        assert old.read_bytes() == b"p\r\nq\r\n"
        assert not (await write_file().invoke(ctx, {"path": "new.txt", "content": "p\nq\n"})).is_error
        assert (tmp_path / "new.txt").read_bytes() == b"p\nq\n"
    finally:
        locator.unregister("write")


async def test_read_numbers_lines_only_at_carriage_returns_and_line_feeds(tmp_path: Path) -> None:
    ctx = _session(tmp_path, "numbers")
    # U+2028, form feed and U+0085 are characters inside a line to every editor and to grep;
    # ``str.splitlines`` treats each as a break and shifted every later line number.
    (tmp_path / "odd.txt").write_bytes("first still first\x0cand\x85more\r\nsecond\rthird\nfourth\n".encode())
    try:
        result = await read_file().invoke(ctx, {"path": "odd.txt"})
        assert not result.is_error, result.content
        rows = result.content.split("\n")
        assert rows[0].split("\t", 1) == ["     1", "first still first\x0cand\x85more"]
        assert [row.split("\t", 1)[0].strip() for row in rows] == ["1", "2", "3", "4"]
        assert rows[3].endswith("fourth")
        assert result.metadata["total_lines"] == 4
    finally:
        locator.unregister("numbers")
