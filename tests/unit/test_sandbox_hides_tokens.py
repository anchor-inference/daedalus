"""A command in the sandbox cannot read the launcher's private files, however it spells the path.

The policy refuses a command that names them, by matching its words, and the sandbox binds ``/`` read-only
rather than hiding it, so a command that built the path read the tunnel's tokens and the launcher's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.tools import shell
from daedalus.tools.shell import hidden_installation, sandbox_argv

KEY = "sk-the-provider-key"
KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"


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
    monkeypatch.delenv("DAEDALUS_SECRETS", raising=False)
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_the_sandbox_covers_both(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_native", lambda: True)
    argv = hidden_installation()
    assert argv[:2] == ["--tmpfs", str(installation / "local")]
    assert argv[2:5] == ["--ro-bind", "/dev/null", str(installation / "launcher.json")]
    assert argv[5:] == ["--tmpfs", str(installation / "state")]


def test_nothing_is_asked_for_a_path_that_is_not_there(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bwrap refuses a whole command over a bind it cannot make, which would be a broken sandbox for all of them."""
    monkeypatch.setattr(shell, "_native", lambda: True)
    (installation / "launcher.json").unlink()
    (installation / "state").rmdir()
    monkeypatch.setenv("DAEDALUS_ENV_FILE", str(installation / ".env"))
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


@pytest.fixture
def keys(installation: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The launcher's keys file where it keeps it, in a sandbox that can really be made."""
    secrets = installation / "daedalus-secrets"
    (secrets / "ssh").mkdir(parents=True)
    file = secrets / "keyproxy.env"
    file.write_text(f"OPENROUTER_API_KEY={KEY}\n")
    monkeypatch.setenv("DAEDALUS_SECRETS", str(secrets))
    monkeypatch.setattr(shell, "_native", lambda: True)
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    return file


def sandboxed(command: str) -> list[str]:
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    argv, is_sandboxed = asyncio.run(sandbox_argv(command, config, writable=[], sealed=[]))
    assert is_sandboxed
    return argv


def test_the_keys_folder_is_covered_and_the_file_has_no_cover_of_its_own(keys: Path) -> None:
    """A cover on the file falls off at the next save: see the test below that saves one."""
    argv = hidden_installation()
    assert argv[argv.index(str(keys.parent)) - 1] == "--tmpfs" and str(keys) not in argv


def test_a_container_keeps_nothing_of_this_though_the_names_are_set(keys: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_native", lambda: False)
    monkeypatch.delenv("DAEDALUS_LOCAL")
    assert hidden_installation() == []


def test_the_default_place_of_the_keys_is_covered_too(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_native", lambda: True)
    (installation / "state" / "secrets").mkdir()
    assert str(installation / "state" / "secrets") in hidden_installation()


@pytest.fixture
def copies(keys: Path, installation: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every folder the launcher keeps a copy of the keys in, one level down so that the update's folders, which
    lie beside the data folder, stay inside this test's own and out of a directory other runs share."""
    data = installation / "data"
    (data / "state").mkdir(parents=True)
    monkeypatch.setenv("STATE_DIR", str(data / "state"))
    folders = [data / "backups", data / "upgrade", installation / ".daedalus-update", installation / ".data-slot-feed"]
    for folder in folders:
        folder.mkdir()
    return folders


def test_every_copy_the_sealed_set_names_is_covered(copies: list[Path]) -> None:
    """Named twice, because the configuration imports the shell module: this keeps the two lists one list."""
    sealed = Settings(_env_file=None).sealed_paths  # type: ignore[call-arg]
    argv = hidden_installation()
    assert all(folder in sealed and str(folder) in argv for folder in copies)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_folder_that_is_a_link_is_covered_where_it_really_is(keys: Path, copies: list[Path], installation: Path) -> None:
    """bubblewrap cannot mount over a link and refuses the whole command, which would end every command at once."""
    backups = copies[0]
    elsewhere = installation / "other-disk"
    elsewhere.mkdir()
    (elsewhere / "copy.env").write_text(KEY)
    backups.rmdir()
    backups.symlink_to(elsewhere)
    done = subprocess.run(sandboxed(f'cat "{backups}/copy.env"; echo done'), capture_output=True, text=True, timeout=20, check=False)
    assert "done" in done.stdout and KEY not in done.stdout, done.stderr


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_real_command_that_builds_the_path_reads_no_keys_and_no_copy(keys: Path, copies: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Every place the key is, by a path the policy's reading of the words cannot see, with the control below."""
    backup = copies[0] / "2026-11-07-1"
    backup.mkdir()
    with tarfile.open(backup / "data.tar.gz", "w:gz") as archive:
        archive.add(keys, arcname="daedalus-secrets/keyproxy.env")
    for folder in copies[1:]:
        (folder / "keyproxy.env").write_text(KEY)
    built = f'd="{keys.parent.parent}"; cat "$d/daedalus-""secrets/keyproxy"".env"; tar -xzOf "$d/data/back""ups/2026-11-07-1/data.tar.gz"'
    built += "".join(f'; cat "{folder}/keypr""oxy.env"' for folder in copies[1:]) + "; echo done"

    def run() -> str:
        done = subprocess.run(sandboxed(built), capture_output=True, text=True, timeout=20, check=False)
        assert "done" in done.stdout, done.stderr
        return done.stdout

    assert KEY not in run()
    # The control: with the folders left uncovered the same command reads all five.
    monkeypatch.setattr(shell, "folders_with_keys", lambda _state: [])
    assert run().count(KEY) == 5


def load_proxy() -> Any:
    sys.path.insert(0, str(KEYPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location("sandbox_keyproxy_under_test", KEYPROXY_DIR / "proxy.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        sys.path.remove(str(KEYPROXY_DIR))
    return module


def read_after_a_save(argv: list[str], flag: Path, keys: Path, value: str) -> str:
    """What a command already running reads of the keys file once the proxy has saved a key under it."""
    proxy = load_proxy()
    running = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Past bwrap's own setup, or the save would land before the sandbox exists and prove nothing.
        assert running.stdout is not None and running.stdout.readline().strip() == "ready"
        proxy.write_env_value(str(keys), "OPENROUTER_API_KEY", value)
        flag.touch()
        output, errors = running.communicate(timeout=20)
    finally:
        running.kill()
    assert "done" in output, errors
    return output


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_key_saved_under_a_running_command_is_written_and_stays_hidden(keys: Path, installation: Path) -> None:
    """What decided the folder over the file. The proxy saves by a rename, and the kernel drops a mount from a name
    renamed over, in every namespace: the control shows a file's cover going, and the new key with it."""
    flag = installation / "saved"
    wait = f'd="{installation}"; echo ready; while [ ! -e "{flag}" ]; do sleep 0.05; done; cat "$d/daedalus-secrets/keyproxy.env"; echo done'
    argv = sandboxed(wait)
    assert "sk-the-next-key" not in read_after_a_save(argv, flag, keys, "sk-the-next-key")
    assert "OPENROUTER_API_KEY=sk-the-next-key" in keys.read_text(), "the save itself went through"

    flag.unlink()
    at = argv.index(str(keys.parent))
    file_cover = [*argv[:at - 1], "--ro-bind", "/dev/null", str(keys), *argv[at + 1:]]
    assert "sk-the-key-after" in read_after_a_save(file_cover, flag, keys, "sk-the-key-after"), "the kernel kept the cover: the folder is no longer needed"


# -- the state folder and the launcher's environment file ---------------------------------------------------

TELEGRAM = "123456:the-telegram-bot-token"
API_TOKEN = "the-apps-api-token"


@pytest.fixture
def state(installation: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A native installation's state folder with its database, and the launcher's environment file beside it."""
    folder = installation / "state"
    (folder / "daedalus.sqlite").write_text(f"api_token={API_TOKEN}")
    (folder / "config.toml").write_text("[api]\n")
    env = installation / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TELEGRAM}\nAPI_PORT=8765\n")
    monkeypatch.setenv("DAEDALUS_ENV_FILE", str(env))
    monkeypatch.setattr(shell, "_native", lambda: True)
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    return folder


def test_the_state_folder_and_the_environment_file_are_what_the_policy_seals(state: Path, installation: Path) -> None:
    """The sandbox and the policy name one set: what the policy refuses by name a command cannot reach by building it."""
    folders, files = shell.hidden_installation_paths(str(state))
    sealed = set(Settings(_env_file=None, state_dir=state).sealed_paths)  # type: ignore[call-arg]
    assert state in folders and state in sealed
    assert files == [installation / ".env"] and installation / ".env" in sealed


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_real_command_that_builds_the_path_reads_neither_the_database_nor_the_telegram_token(state: Path, installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built = f'd="{installation}"; cat "$d/sta""te/daedalus.sqlite" "$d/state/config"".toml"; cat "$d/.e""nv"; ls "$d/state"; echo done'

    def run() -> str:
        done = subprocess.run(sandboxed(built), capture_output=True, text=True, timeout=20, check=False)
        assert "done" in done.stdout, done.stderr
        return done.stdout

    output = run()
    assert API_TOKEN not in output and TELEGRAM not in output and "daedalus.sqlite" not in output
    # The control: uncovered, the same command reads both.
    monkeypatch.setattr(shell, "hidden_installation_paths", lambda _state: ([], []))
    output = run()
    assert API_TOKEN in output and TELEGRAM in output


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_the_environment_file_written_under_a_running_command_stays_hidden(state: Path, installation: Path) -> None:
    """As the launcher writes it, in place: the cover on the file holds through the write, and the write lands."""
    env = installation / ".env"
    flag = installation / "written"
    wait = f'echo ready; while [ ! -e "{flag}" ]; do sleep 0.05; done; cat "{installation}/.e""nv"; echo done'
    running = subprocess.Popen(sandboxed(wait), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert running.stdout is not None and running.stdout.readline().strip() == "ready"
        with env.open("w", encoding="utf-8") as handle:  # what os.WriteFile does: truncate and write the same file
            handle.write("TELEGRAM_BOT_TOKEN=999:the-next-token\n")
        flag.touch()
        output, errors = running.communicate(timeout=20)
    finally:
        running.kill()
    assert "done" in output, errors
    assert "the-next-token" not in output and TELEGRAM not in output
    assert "the-next-token" in env.read_text(), "the write itself went through"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_state_folder_that_is_a_link_is_covered_where_it_really_is(state: Path, installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = installation / "other-disk-state"
    state.rename(elsewhere)
    state.symlink_to(elsewhere)
    done = subprocess.run(sandboxed(f'cat "{state}/daedalus.sqlite" "{elsewhere}/daedalus.sqlite"; echo done'), capture_output=True, text=True, timeout=20, check=False)
    assert "done" in done.stdout and API_TOKEN not in done.stdout, done.stderr


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_folder_the_host_opened_inside_a_state_folder_is_still_the_commands(state: Path) -> None:
    """A benchmark's state folder holds its own workspaces: a cover over the folder must not take the work away."""
    work = state / "workspaces" / "task"
    work.mkdir(parents=True)
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    argv, _ = asyncio.run(sandbox_argv(f'cd "{work}" && echo made > out.txt && cat "{state}/daedalus.sqlite"; echo done', config, writable=[work], sealed=[]))
    done = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    assert "done" in done.stdout, done.stderr
    assert (work / "out.txt").read_text().strip() == "made" and API_TOKEN not in done.stdout


def test_the_launcher_writes_its_environment_file_in_place() -> None:
    """The cover on the file holds only while the file is written where it is: a rename over it would take the
    cover off in every running sandbox, as it did for ``keyproxy.env``. The launcher is Go and this is the one
    place both sides meet, so its sources are read for every write of that file."""
    desktop = Path(__file__).resolve().parents[2] / "desktop"
    writes = []
    for source in sorted(desktop.glob("*.go")):
        if source.name.endswith("_test.go"):
            continue
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b(?:p|paths|s\.app\.paths|a\.paths|n\.paths|t\.paths)\.Env\b", line) and re.search(r"Rename|Create|OpenFile|WriteFile|atomic", line):
                writes.append((source.name, number, line.strip()))
    assert writes, "the launcher writes the file somewhere"
    assert all("os.WriteFile(" in line and "Rename" not in line for _, _, line in writes), writes
