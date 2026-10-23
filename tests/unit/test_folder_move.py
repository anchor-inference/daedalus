"""Moving a folder between the container and the host: the stream, its checks, and the switch."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.folder_move import (
    FolderMover,
    MoveFailed,
    MoveRefused,
    _Progress,
    count_tree,
    extract_tar,
    scan_tree,
    write_tar,
)
from daedalus.host.session_runner import FolderMoving, SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from daedalus.terminals.bridge import OUTDATED_TRANSFER, FolderCheck
from daedalus.terminals.model import ExecResult

HEADERS = {"X-Daedalus-Token": "tok"}


class _Buffer:
    """A sink for ``write_tar`` that keeps the whole stream."""

    def __init__(self) -> None:
        self.data = io.BytesIO()

    def write(self, data: bytes) -> int:
        return self.data.write(data)

    def flush(self) -> None:
        pass


def stream_of(root: Path) -> tuple[bytes, Any]:
    scan = scan_tree(root)
    sink = _Buffer()
    write_tar(root, scan, sink, _Progress())  # type: ignore[arg-type]
    return sink.data.getvalue(), scan


def make_tree(root: Path) -> None:
    (root / "bin" / "deep").mkdir(parents=True)
    (root / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (root / "bin" / "run.sh").chmod(0o755)
    (root / "bin" / "deep" / "big.bin").write_bytes(os.urandom(1_200_000))
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "run").symlink_to("bin/run.sh")
    (root / "escape").symlink_to("/etc/passwd")
    os.mkfifo(root / "pipe")


class FakeBridge:
    """The host daemon's folder moves over a directory standing in for the operator's machine, held to
    the daemon's rules where a test needs them: a destination inside the home, new or empty."""

    def __init__(self, home: Path, *, version: int = 1) -> None:
        self.home = home
        self.version = version
        self.unpacks: dict[str, dict[str, Any]] = {}
        self.packs: dict[str, dict[str, Any]] = {}
        self.aborted: list[str] = []
        self.removed: list[str] = []
        self.gate: asyncio.Event | None = None
        self.corrupt = False
        self.git: list[list[str]] = []

    def available(self) -> bool:
        return True

    def daemon_info(self) -> dict[str, Any]:
        return {"home": str(self.home), "os": "linux", "capabilities": {"fs_transfer": self.version}}

    def transfer_problem(self) -> str:
        return "" if self.version >= 1 else OUTDATED_TRANSFER

    async def stat_folder(self, path: str) -> dict[str, Any]:
        return {"exists": Path(path).exists(), "type": "dir" if Path(path).is_dir() else ""}

    async def check_folder(self, path: str, *, create_missing: bool = False, actor: str = "system") -> FolderCheck:
        return FolderCheck(path=path, exists=True, is_dir=True, writable=True, is_git=(Path(path) / ".git").exists())

    async def exec_run(self, env: str, argv: list[str], **_: Any) -> ExecResult:
        self.git.append(argv)
        return ExecResult(exit_code=0, signal="", stdout="", stderr="", truncated=False, timed_out=False, duration_ms=1, path="git")

    async def unpack_begin(self, path: str, *, total_bytes: int, entries: int, actor: str = "operator") -> dict[str, Any]:
        target = Path(path)
        if self.home not in target.parents:
            raise OSError(f"forbidden: {path} is not inside the home")
        if target.exists() and any(target.iterdir()):
            raise OSError(f"forbidden: {path} is not empty")
        created = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        transfer = f"u{len(self.unpacks)}"
        self.unpacks[transfer] = {"path": target, "data": bytearray(), "created": created, "total": total_bytes, "entries": entries}
        return {"transfer": transfer, "path": path, "created": created}

    async def unpack_write(self, transfer: str, data: bytes) -> int:
        if self.gate is not None:
            await self.gate.wait()
        self.unpacks[transfer]["data"] += data
        return len(self.unpacks[transfer]["data"])

    async def unpack_finish(self, transfer: str, *, actor: str = "operator") -> dict[str, Any]:
        item = self.unpacks.pop(transfer)
        counts = extract_tar(io.BytesIO(bytes(item["data"])), item["path"], total_bytes=item["total"], entries=item["entries"], progress=_Progress())
        digest = hashlib.sha256(bytes(item["data"]) + (b"x" if self.corrupt else b"")).hexdigest()
        return {**counts.view(), "sha256": digest, "path": str(item["path"])}

    async def unpack_abort(self, transfer: str, *, actor: str = "operator") -> None:
        item = self.unpacks.pop(transfer)
        self.aborted.append(transfer)
        if item["created"]:
            shutil.rmtree(item["path"], ignore_errors=True)

    async def pack_begin(self, path: str, *, actor: str = "operator") -> dict[str, Any]:
        data, scan = stream_of(Path(path))
        transfer = f"p{len(self.packs)}"
        self.packs[transfer] = {"path": Path(path), "data": data, "at": 0}
        return {"transfer": transfer, **scan.counts.view(), "skipped": scan.skipped, "skipped_count": scan.skipped_count}

    async def pack_read(self, transfer: str, *, max_bytes: int = 640 << 10) -> tuple[bytes, bool, str]:
        item = self.packs[transfer]
        chunk = item["data"][item["at"]:item["at"] + max_bytes]
        item["at"] += len(chunk)
        eof = item["at"] >= len(item["data"])
        return chunk, eof, hashlib.sha256(item["data"]).hexdigest() if eof else ""

    async def pack_close(self, transfer: str) -> None:
        self.packs.pop(transfer, None)

    async def pack_remove(self, transfer: str, *, actor: str = "operator") -> None:
        item = self.packs.pop(transfer)
        shutil.rmtree(item["path"])
        self.removed.append(str(item["path"]))


@pytest.fixture
async def manager(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        yield manager
    finally:
        await manager.close()


async def wait_job(mover: FolderMover, job_id: str) -> dict[str, Any]:
    for _ in range(500):
        job = mover.get(job_id)
        if not job.active:
            return job.view()
        await asyncio.sleep(0.01)
    raise AssertionError("the move never ended")


def test_a_tree_streams_and_unpacks_the_same(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    make_tree(source)
    data, scan = stream_of(source)
    assert {s["path"]: s["reason"] for s in scan.skipped} == {"escape": "link outside the folder", "pipe": "special file"}
    dest = tmp_path / "dest"
    dest.mkdir()
    counts = extract_tar(io.BytesIO(data), dest, total_bytes=scan.counts.bytes, entries=len(scan.entries), progress=_Progress())
    assert counts == scan.counts == count_tree(dest)
    assert (dest / "bin" / "deep" / "big.bin").read_bytes() == (source / "bin" / "deep" / "big.bin").read_bytes()
    assert stat.S_IMODE((dest / "bin" / "run.sh").stat().st_mode) & 0o111
    assert os.readlink(dest / "run") == "bin/run.sh"
    assert (dest / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert not (dest / "escape").exists() and not (dest / "pipe").exists()


def hostile(*members: tuple[str, str, str]) -> bytes:
    """A stream of ``(name, kind, body or link)`` entries, made by hand as a hostile sender would."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, value in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, value
                tar.addfile(info)
            else:
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value.encode()))
    return buffer.getvalue()


@pytest.mark.parametrize("members", [
    [("../evil", "file", "x")],
    [("a/../../evil", "file", "x")],
    [("/tmp/evil", "file", "x")],
    [("l", "link", "../../outside")],
    [("l", "link", "/etc")],
    [("sub", "dir", ""), ("l", "link", "sub"), ("l/f", "file", "x")],
    [("f", "file", "x" * 200)],
    [("a", "file", "1"), ("b", "file", "2"), ("c", "file", "3"), ("d", "file", "4")],
])
def test_a_hostile_stream_writes_nothing_outside(tmp_path: Path, members: list[tuple[str, str, str]]) -> None:
    dest = tmp_path / "box" / "dest"
    dest.mkdir(parents=True)
    with pytest.raises((MoveFailed, OSError)):
        extract_tar(io.BytesIO(hostile(*members)), dest, total_bytes=100, entries=3, progress=_Progress())
    assert not (tmp_path / "box" / "evil").exists() and not (tmp_path / "evil").exists() and not Path("/tmp/evil").exists()


async def test_a_container_folder_moves_to_the_host_and_everything_follows(settings: Settings, manager: SessionManager, db: Database, tmp_path: Path) -> None:
    home = tmp_path / "hosthome"
    home.mkdir()
    bridge = FakeBridge(home)
    mover = FolderMover(manager, lambda: bridge, workspaces_dir=settings.workspaces_dir)  # type: ignore[arg-type, return-value]
    project = await manager.projects.create("Умный дом")
    folder = project.primary
    make_tree(folder.path)
    session = (await manager.create_session("chat", project_id=project.id)).session
    worktree = folder.path / ".agents" / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    (folder.path / ".git" / "worktrees" / "w1").mkdir(parents=True)
    (folder.path / ".git" / "worktrees" / "w1" / "gitdir").write_text(f"{worktree}/.git\n")
    await manager.sessions.update_metadata(session.id, {**session.metadata, "worktree_cwd": str(worktree)})
    await db.execute("INSERT INTO terminals(id, env, project_id, owner_kind, cwd, status, created_at) VALUES ('t1', 'container', ?, 'project', ?, 'exited', '2026-10-23')",
                     (project.id, str(folder.path / "bin")))

    plan = await mover.plan(project.id, folder.id)
    assert plan["direction"] == "to_host" and plan["problem"] == ""
    assert plan["destination"] == str(home / "Daedalus" / "projects" / "умный-дом")
    (home / "Daedalus" / "projects" / "умный-дом").mkdir(parents=True)
    (home / "Daedalus" / "projects" / "умный-дом" / "taken").write_text("x")
    plan = await mover.plan(project.id, folder.id)
    assert plan["destination"].endswith("умный-дом-2"), "an existing folder of that name is never proposed"

    job = await mover.start(project.id, folder.id, destination=plan["destination"])
    assert folder.id in manager.moving_folders
    view = await wait_job(mover, job.id)
    assert view["state"] == "done", view
    assert view["original_removed"] is True and not folder.path.exists()
    assert "2 entries were not carried" in " ".join(view["notes"])
    dest = Path(plan["destination"])
    assert (dest / "bin" / "deep" / "big.bin").stat().st_size == 1_200_000
    moved = (await manager.projects.get(project.id)).primary
    assert moved.env == "host" and moved.path == dest and moved.is_git
    assert (await manager.projects.get(project.id)).settings.default_env == "host"
    row = await db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (session.id,))
    assert json.loads(row["metadata"])["worktree_cwd"] == str(dest / ".agents" / "worktrees" / "w1")
    terminal = await db.fetchone("SELECT env, cwd FROM terminals WHERE id = 't1'")
    assert (terminal["env"], terminal["cwd"]) == ("host", str(dest / "bin"))
    assert bridge.git == [["git", "worktree", "repair", str(dest / ".agents" / "worktrees" / "w1")]]
    assert folder.id not in manager.moving_folders
    state = manager.live_state(session.id)
    assert state is not None and state.metadata["worktree_cwd"] == str(dest / ".agents" / "worktrees" / "w1")


async def test_a_host_folder_comes_back_and_is_kept_unless_asked(settings: Settings, manager: SessionManager, tmp_path: Path) -> None:
    home = tmp_path / "hosthome"
    for remove in (False, True):
        source = home / "work" / f"site-{remove}"
        source.mkdir(parents=True)
        (source / "index.html").write_text("<h1>hi</h1>")
        bridge = FakeBridge(home)
        mover = FolderMover(manager, lambda bridge=bridge: bridge, workspaces_dir=settings.workspaces_dir)  # type: ignore[misc]
        project = await manager.projects.create(f"Site {remove}", [FolderSpec(str(source), env="host")])
        plan = await mover.plan(project.id, project.primary.id)
        assert plan["direction"] == "to_container" and not plan["destination_editable"]
        assert Path(plan["destination"]).parent == settings.workspaces_dir
        job = await mover.start(project.id, project.primary.id, remove_original=remove)
        view = await wait_job(mover, job.id)
        assert view["state"] == "done", view
        moved = (await manager.projects.get(project.id)).primary
        assert moved.env == "container" and moved.managed and (moved.path / "index.html").read_text() == "<h1>hi</h1>"
        assert source.exists() is (not remove)
        assert view["original_removed"] is remove


async def test_nothing_moves_under_a_live_agent_or_another_project(settings: Settings, manager: SessionManager, db: Database, tmp_path: Path) -> None:
    home = tmp_path / "hosthome"
    home.mkdir()
    bridge = FakeBridge(home)
    mover = FolderMover(manager, lambda: bridge, workspaces_dir=settings.workspaces_dir)  # type: ignore[arg-type, return-value]
    project = await manager.projects.create("Busy")
    folder = project.primary
    await db.execute("INSERT INTO terminals(id, env, project_id, owner_kind, cwd, status, created_at) VALUES ('t2', 'container', ?, 'project', ?, 'running', '2026-10-23')",
                     (project.id, str(folder.path)))
    plan = await mover.plan(project.id, folder.id)
    assert plan["problem"] == "busy" and "terminal" in plan["detail"]
    with pytest.raises(MoveRefused, match="terminal"):
        await mover.start(project.id, folder.id)
    assert folder.id not in manager.moving_folders
    await db.execute("UPDATE terminals SET status = 'exited' WHERE id = 't2'")

    other = await manager.projects.create("Other")
    await manager.projects.add_folder(other.id, str(folder.path), env="container")
    with pytest.raises(MoveRefused, match="also belongs to Other"):
        await mover.start(project.id, folder.id)


async def test_a_run_waits_for_the_move_and_a_session_at_work_stops_it(settings: Settings, manager: SessionManager, tmp_path: Path) -> None:
    home = tmp_path / "hosthome"
    home.mkdir()
    bridge = FakeBridge(home)
    bridge.gate = asyncio.Event()
    mover = FolderMover(manager, lambda: bridge, workspaces_dir=settings.workspaces_dir)  # type: ignore[arg-type, return-value]
    project = await manager.projects.create("Gate")
    (project.primary.path / "a.txt").write_text("a")
    session = (await manager.create_session("chat", project_id=project.id)).session
    job = await mover.start(project.id, project.primary.id, destination=str(home / "gate"))
    with pytest.raises(FolderMoving):
        await manager.submit(session.id, "hello")
    # Cancelled while copying: the original stays, the partial copy goes, and runs may start again.
    for _ in range(100):
        if mover.get(job.id).state == "copying":
            break
        await asyncio.sleep(0.01)
    mover.cancel(job.id)
    view = await wait_job(mover, job.id)
    assert view["state"] == "cancelled"
    assert (project.primary.path / "a.txt").read_text() == "a"
    assert bridge.aborted and not (home / "gate").exists()
    assert (await manager.projects.get(project.id)).primary.env == "container"
    assert project.primary.id not in manager.moving_folders


async def test_a_copy_that_does_not_match_is_thrown_away(settings: Settings, manager: SessionManager, tmp_path: Path) -> None:
    home = tmp_path / "hosthome"
    home.mkdir()
    bridge = FakeBridge(home)
    bridge.corrupt = True
    mover = FolderMover(manager, lambda: bridge, workspaces_dir=settings.workspaces_dir)  # type: ignore[arg-type, return-value]
    project = await manager.projects.create("Corrupt")
    (project.primary.path / "a.txt").write_text("a")
    job = await mover.start(project.id, project.primary.id, destination=str(home / "corrupt"))
    view = await wait_job(mover, job.id)
    assert view["state"] == "failed" and "does not match" in view["error"]
    assert (project.primary.path / "a.txt").exists()
    assert (await manager.projects.get(project.id)).primary.env == "container"


async def test_an_old_daemon_or_a_native_install_is_told_before_anything_starts(settings: Settings, manager: SessionManager, tmp_path: Path) -> None:
    bridge = FakeBridge(tmp_path, version=0)
    mover = FolderMover(manager, lambda: bridge, workspaces_dir=settings.workspaces_dir)  # type: ignore[arg-type, return-value]
    project = await manager.projects.create("Old")
    plan = await mover.plan(project.id, project.primary.id)
    assert plan["problem"] == "host_outdated" and "update it" in plan["detail"]
    with pytest.raises(MoveRefused, match="older than this version"):
        await mover.start(project.id, project.primary.id, destination=str(tmp_path / "x"))
    manager.projects.local_env = "host"
    try:
        plan = await mover.plan(project.id, project.primary.id)
        assert plan["problem"] == "not_movable"
    finally:
        manager.projects.local_env = "container"


async def test_the_routes_plan_start_follow_and_refuse(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, tmp_path: Path) -> None:
    home = tmp_path / "hosthome"
    home.mkdir()
    bridge = FakeBridge(home)

    async def create_session(title: str, **kwargs: Any) -> Any:
        return await manager.create_session(title, **kwargs)

    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={"host_bridge": bridge}, guard=None, create_session=create_session)
    project = await manager.projects.create("Routes")
    (project.primary.path / "a.txt").write_text("a")
    base = f"/api/projects/{project.id}/folders/{project.primary.id}/move"
    transport = httpx.ASGITransport(app=build_app(app, "tok"))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        plan = (await client.get(base, headers=HEADERS)).json()
        assert plan["direction"] == "to_host" and plan["destination"].startswith(str(home))
        outside = await client.post(base, headers=HEADERS, json={"destination": "/srv/elsewhere"})
        assert outside.status_code == 409 and "inside the home folder" in outside.json()["detail"]
        started = (await client.post(base, headers=HEADERS, json={})).json()
        for _ in range(500):
            view = (await client.get(f"/api/folder-moves/{started['id']}", headers=HEADERS)).json()
            if not view["active"]:
                break
            await asyncio.sleep(0.01)
        assert view["state"] == "done", view
        listed = (await client.get("/api/folder-moves", headers=HEADERS, params={"folder_id": project.primary.id})).json()
        assert [job["id"] for job in listed["jobs"]] == [started["id"]]
        late = await client.post(f"/api/folder-moves/{started['id']}/cancel", headers=HEADERS)
        assert late.status_code == 409
        assert (await client.get("/api/folder-moves/nope", headers=HEADERS)).status_code == 404
