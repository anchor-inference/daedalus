"""The host bridge against the real daemon, run as the host environment is on a server.

A daemon built from ``ptyd/`` serves ``--env host`` with its own home, as the systemd user unit
runs it. Through it: a terminal opened in that home, a project folder checked and made, git run in
it, a staff worktree cut, committed, merged and removed from the container's side; then the daemon
stopped and started again as the unit's restarts do, and what the host says meanwhile.

Needs a built daemon: ``DAEDALUS_INTEGRATION=1 DAEDALUS_PTYD_BIN=<path to ptyd>``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings, TerminalsConfig
from daedalus.host.folder_move import FolderMover
from daedalus.host.session_runner import SessionManager
from daedalus.host.worktrees import StaffWorktrees
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectFolder
from daedalus.terminals.bridge import HostBridge
from daedalus.terminals.model import Origin, Owner, TerminalSpec
from daedalus.terminals.service import Terminals

BINARY = os.environ.get("DAEDALUS_PTYD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_PTYD_BIN to a built ptyd"),
]


class FreeOnly:
    async def exists(self, owner: Owner) -> bool:
        return owner.kind == "free"

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        return {}

    async def project_of(self, owner: Owner) -> str | None:
        return None

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        return None

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        return [cwd]


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))  # short: a unix socket path is at most 108 bytes
    (path / "home").mkdir()
    # The operator's git identity, and nothing of this machine's own configuration.
    (path / "gitconfig").write_text("[user]\n\tname = Operator\n\temail = operator@localhost\n[init]\n\tdefaultBranch = main\n")
    yield path
    shutil.rmtree(path, ignore_errors=True)


class Daemon:
    """The host daemon as its unit runs it; ``start`` again is the unit restarting it."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.process: subprocess.Popen[bytes] | None = None

    async def start(self) -> None:
        env = {**os.environ, "HOME": str(self.base / "home"), "GIT_CONFIG_GLOBAL": str(self.base / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        self.process = subprocess.Popen(  # noqa: S603 — the binary the caller named, with fixed arguments
            [BINARY, "serve", "--env", "host", "--run-dir", str(self.base / "run"), "--state-dir", str(self.base / "state"), "--home", str(self.base / "home"), "--shell", "/bin/sh"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        async with asyncio.timeout(15):
            while not (self.base / "run" / "endpoint").exists():
                await asyncio.sleep(0.05)

    async def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(sig)
            await asyncio.to_thread(self.process.wait, 10)


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[Daemon]:
    made = Daemon(base)
    await made.start()
    yield made
    await made.stop()


@pytest.fixture
async def service(db: Database, base: Path, daemon: Daemon) -> AsyncIterator[Terminals]:
    cfg = TerminalsConfig()
    made = Terminals(db, run_dirs={"container": None, "host": base / "run"}, config=lambda: cfg, owners=FreeOnly())
    await made.start()
    assert await made.wait_available("host", timeout=15)
    yield made
    await made.close()


async def _until(check: Any, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.05)


def _host(service: Terminals) -> Any:
    return next(e for e in service.environments() if e.env == "host")


async def test_a_terminal_a_folder_and_a_worktree_on_the_host(service: Terminals, base: Path, db: Database) -> None:
    bridge = HostBridge(lambda: service)
    assert bridge.available() and _host(service).home == str(base / "home")

    # A shell opens in the operator's home, as the unit's does.
    view = await service.create(TerminalSpec(env="host", owner=Owner("free")))
    await service.write(view["id"], text="echo bridge-$((6*7)) $PWD\r", origin=Origin("test"), wait_keyboard=False)

    async def printed() -> bool:
        return f"bridge-42 {base / 'home'}" in str((await service.read_output(view["id"], since_seq=0)).data)

    await _until(printed)
    await service.kill(view["id"])

    # The folder of a new project: missing, then made with its parents, then a repository.
    folder = base / "home" / "code" / "site"
    assert (await bridge.check_folder(str(folder))).problem == "the folder does not exist on the host"
    made = await bridge.check_folder(str(folder), create_missing=True, actor="operator")
    assert made.created and made.is_dir and made.writable is True and made.is_git is False and made.problem == ""
    assert folder.is_dir()
    for argv in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "the first commit"]):
        result = await bridge.exec_run("host", argv, cwd=str(folder), timeout=30)
        assert result.exit_code == 0, result.stderr
    assert (await bridge.check_folder(str(folder))).is_git is True
    # The home itself cannot be a project's folder: every credential in it would be under a root.
    assert "holds the home directory" in (await bridge.check_folder(str(base / "home"))).problem

    # A staff worktree, from the container's side of the bridge. The operator adds the exclude line
    # on the host; the bridge only checks it.
    (folder / ".git" / "info" / "exclude").write_text("/.agents/\n")
    trees = StaffWorktrees("container", host=bridge)
    host_folder = ProjectFolder(id="f-1", project_id="p-1", path=folder, label="", env="host", is_git=True, readonly=False, position=0, created_at=datetime.now(UTC))
    tree = await trees.prepare(host_folder, "Anna", "1", "the first task")
    assert tree.path.is_relative_to(folder / ".agents") and tree.path.is_dir()
    (tree.path / "feature.txt").write_text("feature\n")
    assert await trees.commit_wip(tree, "add the feature")
    await trees.merge(host_folder, tree.branch)
    assert await trees.remove(tree, delete_branch_if_merged=True) is True
    assert (folder / "feature.txt").read_text() == "feature\n"

    rows = await db.fetchall("SELECT action, actor FROM terminal_audit WHERE env = 'host' ORDER BY seq")
    actions = [r["action"] for r in rows]
    assert actions[0] == "create" and "mkdir" in actions and actions.count("exec") >= 6


async def test_the_daemon_stopped_and_started_as_its_unit_does(service: Terminals, daemon: Daemon, db: Database) -> None:
    async def status(terminal_id: str) -> str:
        row = await db.fetchone("SELECT status FROM terminals WHERE id = ?", (terminal_id,))
        return str(row["status"])

    # systemctl --user stop: SIGTERM. The daemon hangs its terminals up on the way out; whether their
    # ends reach the host before the connection closes is a race, so the row is ended either as
    # exited (they did) or as lost once the next daemon is seen (they did not). It never stays running.
    polite = await service.create(TerminalSpec(env="host", owner=Owner("free")))
    await daemon.stop(signal.SIGTERM)

    async def not_running() -> bool:
        return _host(service).reason == "not_running"

    await _until(not_running)
    assert not HostBridge(lambda: service).available()

    # Started again: a new instance, and new terminals can be made.
    await daemon.start()
    await _until(lambda: _available(service))

    async def ended() -> bool:
        return await status(polite["id"]) in ("exited", "lost")

    await _until(ended)
    crashed = await service.create(TerminalSpec(env="host", owner=Owner("free")))
    # A crash (or a kill -9 by the out-of-memory killer): nothing is reported, the endpoint stays
    # behind, and nothing answers at it until the unit restarts the daemon.
    await daemon.stop(signal.SIGKILL)
    await _until(not_running)
    assert await status(crashed["id"]) == "running", "the host cannot know yet"
    await daemon.start()
    await _until(lambda: _available(service))

    async def lost() -> bool:
        return await status(crashed["id"]) == "lost"

    await _until(lost)
    fresh = await service.create(TerminalSpec(env="host", owner=Owner("free")))
    assert await status(fresh["id"]) == "running"


async def _available(service: Terminals) -> bool:
    return service.available("host")


async def test_a_folder_moves_to_the_host_and_back(service: Terminals, base: Path, db: Database, settings: Settings, config: RuntimeConfig) -> None:
    """A chat's folder carried out of the container into the host's home by the real daemon, and back
    again with the host copy removed: the Python stream and the daemon's tar are one format."""
    bridge = HostBridge(lambda: service)
    assert bridge.transfer_problem() == ""
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        mover = FolderMover(manager, lambda: bridge, workspaces_dir=settings.workspaces_dir)
        project = await manager.projects.create("Weather station")
        source = project.primary.path
        (source / "src" / "deep").mkdir(parents=True)
        (source / "src" / "main.py").write_text("print('hi')\n")
        (source / "run.sh").write_text("#!/bin/sh\n")
        (source / "run.sh").chmod(0o755)
        (source / "src" / "deep" / ("long-name-" * 20)).write_bytes(os.urandom(3 << 20))
        (source / "latest").symlink_to("src/main.py")
        plan = await mover.plan(project.id, project.primary.id)
        assert plan["problem"] == "" and plan["destination"] == str(base / "home" / "Daedalus" / "projects" / "weather-station")

        async def finished(job_id: str) -> dict[str, Any]:
            async with asyncio.timeout(60):
                while mover.get(job_id).active:
                    await asyncio.sleep(0.05)
            return mover.get(job_id).view()

        out = await finished((await mover.start(project.id, project.primary.id)).id)
        assert out["state"] == "done", out
        dest = Path(plan["destination"])
        assert not source.exists() and (dest / "run.sh").stat().st_mode & 0o111
        assert os.readlink(dest / "latest") == "src/main.py"
        assert (dest / "src" / "deep" / ("long-name-" * 20)).stat().st_size == 3 << 20
        moved = (await manager.projects.get(project.id)).primary
        assert moved.env == "host" and moved.path == dest

        # The roots follow the table on the next housekeeping; pushed now rather than waited for.
        link = service.links["host"]
        await service._push_roots("host", link.client, force=True)  # type: ignore[arg-type]
        back = await finished((await mover.start(project.id, project.primary.id, remove_original=True)).id)
        assert back["state"] == "done" and back["original_removed"], back
        home = (await manager.projects.get(project.id)).primary
        assert home.env == "container" and (home.path / "src" / "main.py").read_text() == "print('hi')\n"
        assert not dest.exists()
    finally:
        await manager.close()
