"""A command in the sandbox cannot read the launcher's private files, however it spells the path.

The policy refuses a command that names them, by matching its words, and the sandbox binds ``/`` read-only
rather than hiding it, so a command that built the path read the tunnel's tokens and the launcher's.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.tools import shell
from daedalus.tools.shell import hidden_installation, sandbox_argv


@pytest.fixture
def installation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Not under /tmp: the sandbox has a /tmp of its own, which would hide these for a reason that proves nothing.
    tmp_path = Path(tempfile.mkdtemp(prefix="daedalus-test-", dir="/var/tmp"))  # noqa: S108
    local = tmp_path / "local"
    (local / "tunnel").mkdir(parents=True)
    (local / "tunnel" / "secrets.json").write_text('{"ngrok_token": "the-token"}')
    (tmp_path / "state").mkdir()
    (tmp_path / "launcher.json").write_text('{"token": "the-launcher-token"}')
    monkeypatch.setenv("DAEDALUS_LOCAL", str(local))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DAEDALUS_NATIVE", "1")
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_the_sandbox_covers_both(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_native", lambda: True)
    argv = hidden_installation()
    assert argv[:2] == ["--tmpfs", str(installation / "local")]
    assert argv[2:] == ["--ro-bind", "/dev/null", str(installation / "launcher.json")]


def test_nothing_is_asked_for_a_path_that_is_not_there(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bwrap refuses a whole command over a bind it cannot make, which would be a broken sandbox for all of them."""
    monkeypatch.setattr(shell, "_native", lambda: True)
    (installation / "launcher.json").unlink()
    assert hidden_installation() == ["--tmpfs", str(installation / "local")]
    monkeypatch.setenv("DAEDALUS_LOCAL", str(installation / "not-there"))
    assert hidden_installation() == []


def test_a_container_has_none_of_this(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAEDALUS_LOCAL", raising=False)
    monkeypatch.setattr(shell, "_native", lambda: False)
    assert hidden_installation() == []


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_real_command_that_builds_the_path_reads_nothing(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_native", lambda: True)
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    built = f'd="{installation}"; cat "$d/lo""cal/tunnel/secrets.json"; cat "$d/launcher"".json"; echo done'
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])

    def run() -> str:
        argv, sandboxed = asyncio.run(sandbox_argv(built, config, writable=[], sealed=[]))
        assert sandboxed
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        assert "done" in done.stdout, done.stderr
        return done.stdout

    output = run()
    assert "the-token" not in output and "the-launcher-token" not in output
    # The control: without the two covers the same command reads both, which is what this test is about.
    monkeypatch.setattr(shell, "hidden_installation", list)
    output = run()
    assert "the-token" in output and "the-launcher-token" in output
