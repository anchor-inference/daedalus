"""Write, Edit and MultiEdit replace a file atomically: a write that fails part way leaves the old content
whole, here and through a shell on another machine, and the file keeps its mode, its links and its owner."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
from pathlib import Path

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.host import filesystem
from daedalus.host.filesystem import ExecOutcome, LocalFS, NotRegularFile, ShellFS, write_untranslated
from daedalus.host.services import SessionServices, locator
from daedalus.tools.files import edit_file
from tests.unit.test_bench import LocalShellBackend

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="modes, links and pipes are POSIX here")
OLD = "the old content\n" * 1_000


def _leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


def test_a_write_that_fails_part_way_leaves_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "notes.txt"
    target.write_text(OLD, encoding="utf-8")

    def disk_full(descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(filesystem.os, "fsync", disk_full)
    with pytest.raises(OSError, match="No space left"):
        write_untranslated(target, "the new content\n")
    assert target.read_text(encoding="utf-8") == OLD
    assert _leftovers(tmp_path) == []


def test_text_that_cannot_be_encoded_never_reaches_the_file(tmp_path: Path) -> None:
    # Written in place, the file was truncated before the encoder refused the text, and left empty.
    target = tmp_path / "notes.txt"
    target.write_text(OLD, encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        write_untranslated(target, "half \ud800 a character")
    assert target.read_text(encoding="utf-8") == OLD


async def test_an_edit_whose_write_fails_says_so_and_keeps_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app.py").write_text("x = 1\n" + OLD, encoding="utf-8")
    monkeypatch.setattr(filesystem.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")))
    locator.register(SessionServices(session_id="atomic", workspace_dir=tmp_path))
    try:
        result = await edit_file().invoke(ToolContext(tenant_id="t", run_id="r", session_id="atomic", metadata={"tool_call_id": "c1"}), {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"})
        assert result.is_error and "could not write" in result.content
        assert (tmp_path / "app.py").read_text(encoding="utf-8").startswith("x = 1\n")
        assert _leftovers(tmp_path) == []
    finally:
        locator.unregister("atomic")


@posix_only
def test_the_file_keeps_its_mode_and_a_new_one_gets_the_usual_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret.env"
    target.write_text("A=1\n")
    target.chmod(0o640)
    inode = target.stat().st_ino
    write_untranslated(target, "A=2\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640 and target.read_text() == "A=2\n"
    assert target.stat().st_ino != inode, "replaced by a rename, not rewritten in place"
    umask = os.umask(0)
    os.umask(umask)
    fresh = tmp_path / "deep" / "new.txt"
    write_untranslated(fresh, "hi")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o666 & ~umask and fresh.read_text() == "hi"


@posix_only
def test_a_link_stays_a_link_and_hard_links_stay_shared(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("one\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    write_untranslated(link, "two\n")
    assert link.is_symlink() and real.read_text() == "two\n"
    other = tmp_path / "other-name.txt"
    os.link(real, other)
    write_untranslated(real, "three\n")
    assert other.read_text() == "three\n", "a second name of the file sees the change, as before"


@posix_only
async def test_a_pipe_is_not_written_into(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    try:
        with pytest.raises(NotRegularFile):
            await asyncio.wait_for(LocalFS().write_text(fifo, "data"), timeout=5)
    finally:
        try:
            os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass


class DropsAfter:
    """A shell that runs the first commands and then loses the connection: a write cut off part way."""

    def __init__(self, root: Path, *, pieces_before_drop: int) -> None:
        self.inner = LocalShellBackend(root)
        self.left = pieces_before_drop

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float) -> ExecOutcome:
        if "base64 -d" in command:
            if self.left == 0:
                return ExecOutcome(exit_code=255, output="Connection to the host closed.")
            self.left -= 1
        return await self.inner.run(command, cwd=cwd, env=env, timeout=timeout)


async def test_a_shell_write_cut_off_part_way_leaves_the_old_file(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text(OLD, encoding="utf-8")
    fs = ShellFS(DropsAfter(tmp_path, pieces_before_drop=1))  # type: ignore[arg-type]
    with pytest.raises(OSError, match="Connection"):
        await fs.write_text(target, "y" * 200_000)  # four pieces; the second never arrives
    assert target.read_text(encoding="utf-8") == OLD
    assert _leftovers(tmp_path) == []


@posix_only
async def test_a_shell_write_replaces_the_file_and_keeps_its_mode_and_links(tmp_path: Path) -> None:
    backend = LocalShellBackend(tmp_path)
    fs = ShellFS(backend)
    target = tmp_path / "run.sh"
    target.write_text("echo old\n")
    target.chmod(0o750)
    inode = target.stat().st_ino
    await fs.write_text(target, "echo new\n" + "z" * 130_000)
    assert target.read_text().startswith("echo new\n") and len(target.read_text()) == 9 + 130_000
    assert stat.S_IMODE(target.stat().st_mode) == 0o750 and target.stat().st_ino != inode
    assert any("mv -f" in c for c in backend.commands) and _leftovers(tmp_path) == []
    link = tmp_path / "current.sh"
    link.symlink_to(target)
    await fs.write_text(link, "echo through the link\n")
    assert link.is_symlink() and target.read_text() == "echo through the link\n"
    await fs.write_text(tmp_path / "fresh" / "new.txt", "")
    assert (tmp_path / "fresh" / "new.txt").read_text() == ""
