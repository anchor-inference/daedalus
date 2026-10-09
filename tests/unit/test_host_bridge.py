"""The host bridge on a server: the unit and its installer, the compose mount, what the host says
about a daemon it cannot use, and the bridge the rest of the application reaches the host through.

The installer is exercised only as far as it can be without a user manager: syntax, the unit it
renders, the run directory it picks, and the directory it prepares. Nothing here talks to systemd.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from daedalus.config import RuntimeConfig, Settings, TerminalsConfig
from daedalus.doctor import DoctorContext, _terminals
from daedalus.extensions import api_projects
from daedalus.stores.database import Database
from daedalus.terminals.bridge import HostBridge, HostDaemonOutdated
from daedalus.terminals.client import PtydClient, Unavailable
from daedalus.terminals.model import EnvUnavailable
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd
from tests.unit.test_terminals_sidechannels import FreeOwners, eventually

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "host-terminal.sh"
TEMPLATE = REPO / "deploy" / "ptyd" / "daedalus-ptyd.service"
COMPOSE = REPO / "deploy" / "compose.yaml"


def _checkout(tmp_path: Path, env_text: str | None = None) -> Path:
    """A copy of the installer and its template in a checkout of its own, so the run directory it
    derives is a sibling of that checkout, not of this one."""
    root = tmp_path / "srv" / "daedalus"
    (root / "deploy" / "ptyd").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "deploy" / "host-terminal.sh")
    shutil.copy(TEMPLATE, root / "deploy" / "ptyd" / "daedalus-ptyd.service")
    if env_text is not None:
        (root / ".env").write_text(env_text)
    return root


def _script(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(root / "deploy" / "host-terminal.sh"), *args], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603, S607


# -- the installer and the unit ------------------------------------------------------------------


def test_the_scripts_parse() -> None:
    for script in (REPO / "deploy" / "setup.sh", SCRIPT):
        assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0, script  # noqa: S603, S607


def test_the_unit_renders_with_its_run_directory(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    done = _script(root, "render", "/srv/host terminals/50%")
    assert done.returncode == 0, done.stderr
    unit = done.stdout
    assert "@RUN_DIR@" not in unit
    # Quoted, because a path may hold a space; % doubled, because systemd reads it as a specifier.
    assert '--run-dir "/srv/host terminals/50%%"' in unit
    [exec_start] = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert exec_start.startswith("ExecStart=%h/.local/lib/daedalus/ptyd serve --env host ")
    assert "--state-dir %h/.local/state/daedalus-ptyd" in exec_start
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    # ptyd owns its PTYs, and the terminals must end with it rather than outlive it as orphans.
    assert "KillMode=" not in "".join(line for line in unit.splitlines() if not line.startswith("#"))
    # The paths the unit names are the ones the installer writes.
    text = SCRIPT.read_text()
    assert 'BIN_DIR="$HOME/.local/lib/daedalus"' in text and 'STATE_DIR="$HOME/.local/state/daedalus-ptyd"' in text
    assert "EnvironmentFile=-%h/.config/daedalus/ptyd.env" in unit and 'ENV_FILE_OUT="$HOME/.config/daedalus/ptyd.env"' in text


@pytest.mark.parametrize("bad", ["relative/dir", '/srv/"quoted"', "/srv/back\\slash"])
def test_a_run_directory_a_unit_cannot_carry_is_refused(tmp_path: Path, bad: str) -> None:
    done = _script(_checkout(tmp_path), "render", bad)
    assert done.returncode == 1 and not done.stdout and "host terminal:" in done.stderr


def test_the_run_directory_is_a_sibling_of_the_checkout_unless_env_says_otherwise(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    assert _script(root, "run-dir").stdout.strip() == str(tmp_path / "srv" / "daedalus-host-terminals")
    root = _checkout(tmp_path / "b", "DAEDALUS_HOST_TERMINALS_DIR=/data/host-terminals\n")
    assert _script(root, "run-dir").stdout.strip() == "/data/host-terminals"
    # Relative, as compose reads it: against deploy/, where the compose file is.
    root = _checkout(tmp_path / "c", "DAEDALUS_HOST_TERMINALS_DIR=../../elsewhere\n")
    assert _script(root, "run-dir").stdout.strip() == str(tmp_path / "c" / "srv" / "elsewhere")


def test_preparing_the_directory_makes_it_the_operators_and_private(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    done = _script(root, "prepare-dir")
    assert done.returncode == 0, done.stdout + done.stderr
    made = tmp_path / "srv" / "daedalus-host-terminals"
    assert made.is_dir() and stat.S_IMODE(made.stat().st_mode) == 0o700 and made.stat().st_uid == os.getuid()
    made.chmod(0o755)
    assert _script(root, "prepare-dir").returncode == 0
    assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_the_compose_file_always_mounts_the_host_terminals_directory() -> None:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    agent = services["daedalus"]
    mount = "${DAEDALUS_HOST_TERMINALS_DIR:-../../daedalus-host-terminals}:/run/daedalus-host-terminals"
    assert mount in agent["volumes"]
    assert agent["environment"]["TERMINALS_HOST_DIR"] == "/run/daedalus-host-terminals"
    # The default is relative to deploy/, which puts it beside the checkout — where the installer
    # puts the daemon's run directory.
    assert (COMPOSE.parent / "../../daedalus-host-terminals").resolve() == REPO.parent / "daedalus-host-terminals"
    # The container terminals have their own daemon; the host's socket is the agent's alone.
    assert not any("daedalus-host-terminals" in str(v) for v in services["terminals"].get("volumes", []))
    assert "DAEDALUS_HOST_TERMINALS_DIR" in (REPO / "deploy" / "env.example").read_text()


def test_setup_prepares_the_directory_before_compose_can_create_it_as_root() -> None:
    text = (REPO / "deploy" / "setup.sh").read_text()
    prepared = text.index("host-terminal.sh prepare-dir")
    started = text.index("up -d --build")
    assert prepared < started
    assert text.index("host-terminal.sh install") > started, "the binary comes out of the image the stack just built"
    assert "systemctl --user disable --now daedalus-ptyd" in text


# -- what the host says about a daemon it cannot use -------------------------------------------------


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))  # a unix socket path is at most 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _reason(run_dir: Path) -> str:
    client = PtydClient("host", run_dir)
    try:
        await client.connect()
    except Unavailable as exc:
        return exc.reason
    finally:
        await client.close()
    return ""


async def test_the_reasons_a_host_daemon_is_not_there(short_dir: Path) -> None:
    run_dir = short_dir / "run"
    run_dir.mkdir()
    assert await _reason(run_dir) == "not_installed"
    fake = await FakePtyd(run_dir, env="host").start()
    assert await _reason(run_dir) == ""
    # Killed without its shutdown: the endpoint stays behind and nothing answers at it.
    assert fake._server is not None
    fake._server.close()
    (run_dir / "ptyd.sock").unlink()
    assert await _reason(run_dir) == "not_running"
    await fake.stop()
    old = await FakePtyd(run_dir, env="host", protocol=2).start()
    assert await _reason(run_dir) == "protocol_mismatch"
    await old.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
async def test_a_directory_this_process_may_not_read_is_permission_denied(short_dir: Path) -> None:
    """What rootless Docker or userns-remap looks like from inside: the files are there, and root in
    the container is an ordinary user on the host."""
    run_dir = short_dir / "run"
    fake = await FakePtyd(run_dir, env="host").start()
    try:
        (run_dir / "endpoint").chmod(0)
        assert await _reason(run_dir) == "permission_denied"
        (run_dir / "endpoint").chmod(0o600)
        (run_dir / "token").chmod(0)
        client = PtydClient("host", run_dir)
        with pytest.raises(Unavailable) as caught:
            await client.connect()
        assert caught.value.reason == "permission_denied" and "rootless Docker or userns-remap" in caught.value.detail
    finally:
        (run_dir / "token").chmod(0o600)
        await fake.stop()


async def test_an_empty_directory_docker_made_as_root_says_so(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.terminals import endpoint  # noqa: PLC0415

    real_stat = Path.stat

    def as_root(self: Path, **kwargs: Any) -> Any:
        result = real_stat(self, **kwargs)
        if self == short_dir:
            return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink, 0, 0, result.st_size, 0, 0, 0))
        return result

    monkeypatch.setattr(Path, "stat", as_root)
    with pytest.raises(endpoint.EndpointMissing) as caught:
        endpoint.read_endpoint(short_dir)
    assert caught.value.reason == "not_installed" and "sudo chown" in caught.value.detail


async def _host_check(context: DoctorContext) -> Any:
    [check] = [c for c in await _terminals(context) if c.name == "terminals (host)"]
    return check


async def test_the_doctor_gives_host_fixes_to_run_on_the_server(settings: Settings, config: RuntimeConfig, short_dir: Path) -> None:
    run_dir = short_dir / "run"
    run_dir.mkdir()
    configured = settings.model_copy(update={"terminals_host_dir": run_dir})
    context = DoctorContext(settings=configured, config=config)
    check = await _host_check(context)
    assert check.severity == "info" and "Host terminal" in check.fix_hint and "as yourself" in check.fix_hint
    (run_dir / "ptyd.lock").write_text("")
    check = await _host_check(context)
    assert check.message.startswith("not running") and check.severity == "warn"
    assert "systemctl --user restart daedalus-ptyd" in check.fix_hint and "enable-linger" in check.fix_hint
    old = await FakePtyd(run_dir, env="host", protocol=2).start()
    try:
        check = await _host_check(context)
        assert check.message.startswith("protocol mismatch") and "setup.sh" in check.fix_hint
    finally:
        await old.stop()


# -- the bridge ------------------------------------------------------------------------------


@pytest.fixture
async def daemon(short_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(short_dir / "run", env="host").start()
    fake.home = str(short_dir / "home")
    yield fake
    await fake.stop()


@pytest.fixture
async def service(db: Database, short_dir: Path, daemon: FakePtyd) -> AsyncIterator[Terminals]:
    made = Terminals(db, run_dirs={"container": None, "host": short_dir / "run"}, config=lambda: TerminalsConfig(), owners=FreeOwners())  # type: ignore[arg-type]
    await made.start()
    assert await made.wait_available("host")
    yield made
    await made.close()


async def test_a_folder_is_checked_and_made_on_the_host(service: Terminals, daemon: FakePtyd, db: Database, short_dir: Path) -> None:
    bridge = HostBridge(lambda: service)
    assert bridge.available()
    target = short_dir / "code" / "site"
    missing = await bridge.check_folder(str(target))
    assert not missing.exists and missing.problem == "the folder does not exist on the host" and not target.exists()
    assert daemon.calls[-1] == ("fs.stat", {"path": str(target), "as_root": True})

    daemon.exec_results["git"] = {"exit_code": 128, "stderr": "fatal: not a git repository"}
    made = await bridge.check_folder(str(target), create_missing=True, actor="operator")
    assert made.created and made.exists and made.is_dir and made.writable is True and made.is_git is False and made.problem == ""
    assert target.is_dir()
    daemon.exec_results["git"] = {"stdout": "true\n"}
    again = await bridge.check_folder(str(target), create_missing=True)
    assert not again.created and again.is_git is True
    rows = await db.fetchall("SELECT actor, detail_json FROM terminal_audit WHERE action = 'mkdir' ORDER BY seq")
    assert [r["actor"] for r in rows] == ["operator", "system"]

    # The daemon's rule for a root: never the home directory itself or what holds it.
    home = await bridge.check_folder(daemon.home, create_missing=True)
    assert not home.exists and "cannot be a folder of a project" in home.problem
    a_file = short_dir / "code" / "notes.txt"
    a_file.write_text("x")
    assert (await bridge.check_folder(str(a_file))).problem == "the path on the host is not a folder"
    assert "not a directory" in (await bridge.check_folder(str(a_file), create_missing=True)).problem


async def test_the_folder_picker_browses_the_host_outside_the_roots(service: Terminals, daemon: FakePtyd, db: Database, short_dir: Path) -> None:
    bridge = HostBridge(lambda: service)
    home = Path(daemon.home)
    for folder in (home / "Repo" / ".git", home / "notes", home / ".config", home / ".ssh"):
        folder.mkdir(parents=True)
    (home / "file.txt").write_text("not a folder")
    # No root is set: a folder for a new project is under none yet, and the picker still sees it.
    listing = await bridge.browse("")
    assert listing["path"] == str(home) and listing["home"] == str(home)
    assert [entry["name"] for entry in listing["entries"]] == ["notes", "Repo"]
    repo = next(entry for entry in listing["entries"] if entry["name"] == "Repo")
    assert repo["is_git"] and repo["writable"] and repo["readable"] and repo["path"] == str(home / "Repo")
    assert listing["places"][0] == {"name": "home", "path": str(home), "kind": "home"}
    assert daemon.calls[-1] == ("fs.browse", {"path": "", "limit": 500})
    hidden = await bridge.browse(str(home), hidden=True)
    assert [entry["name"] for entry in hidden["entries"]] == [".config", "notes", "Repo"]
    limited = await bridge.browse("~", limit=1)
    assert len(limited["entries"]) == 1 and limited["truncated"]
    # What the deny list or the daemon's own folders hold is refused, and a missing folder is missing.
    with pytest.raises(OSError, match="denied") as refused:
        await bridge.browse(str(home / ".ssh"))
    assert not isinstance(refused.value, (HostDaemonOutdated, FileNotFoundError, ConnectionError))
    with pytest.raises(OSError, match="denied"):
        await bridge.browse(str(short_dir / "run"))
    with pytest.raises(FileNotFoundError):
        await bridge.browse(str(home / "missing"))
    # The picker's check and its "new folder" are the project-folder calls, and the folder is audited.
    made = await bridge.mkdir(str(home / "notes" / "fresh"))
    assert made["created"] and (home / "notes" / "fresh").is_dir()
    assert (await bridge.stat_folder(str(home / "notes" / "fresh")))["writable"] is True
    rows = await db.fetchall("SELECT actor FROM terminal_audit WHERE action = 'mkdir'")
    assert [r["actor"] for r in rows] == ["operator"]
    # A daemon from before the call says what to do about it rather than looking broken.
    daemon.browse_supported = False
    with pytest.raises(HostDaemonOutdated, match="update it"):
        await bridge.browse("")


async def test_other_programs_sessions_are_read_with_the_calls_the_daemon_knows(service: Terminals, daemon: FakePtyd) -> None:
    bridge = HostBridge(lambda: service)
    with pytest.raises(HostDaemonOutdated, match="other programs' sessions"):
        await bridge.sessions_harnesses()
    daemon.session_answers = {"sessions.harnesses": {"harnesses": [{"id": "claude", "found": True}]},
                              "sessions.scan": {"path": "/home/someone/p", "here": [], "children": [], "folders": [], "truncated": False, "cursor": ""},
                              "sessions.read": {"header": {"id": "s1"}, "turns": [], "next": 0, "done": True, "live": False, "masked": 0}}
    assert (await bridge.sessions_harnesses())["harnesses"][0]["id"] == "claude"
    assert daemon.calls[-1] == ("sessions.harnesses", {})
    await bridge.sessions_scan("claude", "/home/someone/p", query="door", deep=True)
    assert daemon.calls[-1] == ("sessions.scan", {"harness": "claude", "path": "/home/someone/p", "depth": 1, "query": "door", "limit": 200, "cursor": "", "deep": True})
    await bridge.sessions_read("claude", "s1", start=12)
    assert daemon.calls[-1] == ("sessions.read", {"harness": "claude", "id": "s1", "from": 12, "max_bytes": 512 << 10, "sidechains": True, "raw": False})
    await bridge.sessions_read("claude", "s1", raw=True)
    assert daemon.calls[-1][1]["raw"] is True and daemon.calls[-1][1]["from"] == 0


async def test_browsing_without_a_host_bridge_is_a_connection_error() -> None:
    with pytest.raises(ConnectionError):
        await HostBridge(lambda: None).browse("")


async def test_git_goes_to_the_host_and_a_missing_bridge_is_an_os_error(service: Terminals, daemon: FakePtyd, short_dir: Path) -> None:
    bridge = HostBridge(lambda: service)
    daemon.exec_results["git"] = {"stdout": "abc\n"}
    result = await bridge.exec_run("host", ["git", "rev-parse", "HEAD"], cwd=str(short_dir), timeout=5)
    assert result.stdout == "abc\n" and daemon.calls[-1][1]["argv"] == ["git", "rev-parse", "HEAD"]
    # Refused programs and a daemon that is gone are the call failing, which the worktrees tell
    # apart from git failing by the exception's kind.
    with pytest.raises(OSError, match="sh"):
        await bridge.exec_run("host", ["sh", "-c", "true"], cwd=str(short_dir), timeout=5)
    await daemon.stop()
    await eventually(lambda: _gone(service))
    assert not bridge.available()
    with pytest.raises(ConnectionError, match="not available"):
        await bridge.exec_run("host", ["git", "status"], cwd=str(short_dir), timeout=5)


async def _gone(service: Terminals) -> bool:
    return not service.available("host")


async def test_without_a_host_daemon_the_bridge_says_so() -> None:
    assert not HostBridge(lambda: None).available()
    with pytest.raises(EnvUnavailable, match="setup.sh"):
        await HostBridge(lambda: None).check_folder("/srv/code")


async def test_project_folders_trust_the_live_connection_over_the_endpoint_file(service: Terminals, daemon: FakePtyd, short_dir: Path) -> None:
    settings = SimpleNamespace(terminals_host_dir=short_dir / "run")
    assert api_projects.host_bridge(settings, service) is True
    assert api_projects.environments(settings, "container", service)["available"] == ["container", "host"]
    # A daemon killed without its shutdown leaves the endpoint behind; the file alone would say yes.
    assert daemon._server is not None
    daemon._server.close()
    for writer in list(daemon._writers):
        writer.close()
    await eventually(lambda: _gone(service))
    assert (short_dir / "run" / "endpoint").is_file()
    assert api_projects.host_bridge(settings, service) is False
    assert api_projects.environments(settings, "container", service)["available"] == ["container"]
