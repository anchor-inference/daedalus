"""The disk guard: sizes, the told levels, the throwaway sweep and the clean-up's refusals."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.config import RuntimeConfig
from daedalus.extensions import api_disk
from daedalus.extensions.disk_guard import DiskGuard, level_for, rearmed
from daedalus.host import disk_usage
from daedalus.host.disk_usage import Refused
from daedalus.stores.database import Database

MB = 1 << 20


def _fill(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Real bytes, not a sparse file: the guard counts blocks the way ``du`` does.
    path.write_bytes(os.urandom(size))


def _age(path: Path, days: float) -> None:
    when = time.time() - days * 86400
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            os.utime(os.path.join(root, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when))


@pytest.fixture
def mtime_ages(monkeypatch):
    """Age by mtime alone. A test can set an mtime back but not a ctime, which ``touch`` always moves
    to now; the rule that a ctime counts too has a test of its own."""
    monkeypatch.setattr(disk_usage, "changed", lambda info: info.st_mtime)


class FakeManager:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, str]] = []
        self.busy: set[str] = set()
        self.shutting_down = False
        self.recovering = False

    def busy_sessions(self) -> set[str]:
        return set(self.busy)

    async def submit(self, session_id: str, text: str, *, steer: bool, as_answer: bool, origin: str) -> str:
        self.submitted.append((session_id, origin, text))
        return "run"


class FakeNotifications:
    def __init__(self) -> None:
        self.posted: list[Any] = []

    def language(self) -> str:
        return "en"

    async def post(self, draft: Any) -> None:
        self.posted.append(draft)


@pytest.fixture
async def host(tmp_path: Path):
    root = tmp_path / "workspaces"
    root.mkdir()
    db = Database(tmp_path / "state.sqlite", workspaces_dir=root)
    await db.open()
    config = RuntimeConfig()
    # Limits in megabytes, so a test tree of a few files crosses them.
    config.disk.workspace_soft_limit_gb = 2 * MB / 1e9
    config.disk.min_free_gb = 0
    config.disk.min_free_percent = 0
    app = SimpleNamespace(
        db=db, config=config, settings=SimpleNamespace(workspaces_dir=root), manager=FakeManager(),
        notifications=FakeNotifications(), extensions={},
    )
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES (?,?,?,?)", ("proj", "Sample", "2026-01-01", "{}"))
    await db.execute(
        "INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,metadata,project_id) VALUES (?,?,?,?,?,?,?)",
        ("sess1", "t", "Experiments", "2026-01-01", "2026-01-01T00:00:00+00:00", "{}", "proj"),
    )
    await db.execute(
        "INSERT INTO project_folders(id,project_id,path,label,env,created_at) VALUES (?,?,?,?,?,?)",
        ("folder", "proj", str(root / "proj"), "Work", db.local_env, "2026-01-01"),
    )
    try:
        yield app, root
    finally:
        await db.close()


def test_levels_and_hysteresis() -> None:
    limit = 100
    assert [level_for(size, limit) for size in (0, 99, 100, 199, 200)] == [0, 0, 1, 1, 2]
    assert level_for(10**12, 0) == 0
    # Told about the limit, a workspace hovering just under it is still counted as told...
    assert rearmed(1, 95, limit) == 1
    # ...and only shrinking well below it makes the next crossing news.
    assert rearmed(1, 79, limit) == 0
    assert rearmed(2, 170, limit) == 2
    assert rearmed(2, 150, limit) == 1
    assert rearmed(2, 10, limit) == 0


def test_measure_counts_blocks_once_per_hard_link_and_skips_links(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _fill(root / "a" / "b" / "one.bin", MB)
    os.link(root / "a" / "b" / "one.bin", root / "a" / "twin.bin")
    outside = tmp_path / "outside"
    _fill(outside / "big.bin", 4 * MB)
    (root / "escape").symlink_to(outside)
    measured = disk_usage.measure(root, max_entries=10_000)
    assert MB <= measured.bytes < 2 * MB  # the hard link is counted once, the link not followed
    assert measured.dirs["a"].bytes >= MB and measured.dirs["a/b"].files == 1
    assert "escape" not in measured.dirs
    assert [path for path, _ in measured.children("")] == ["a"]


def test_measure_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    for index in range(50):
        _fill(root / f"f{index}", 10)
    measured = disk_usage.measure(root, max_entries=10)
    assert measured.truncated and measured.entries == 10


def test_resolve_target_refuses_escapes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "keep" / "inner").mkdir(parents=True)
    (workspace / ".checkpoints").mkdir()
    secret = tmp_path / "operator-home"
    (secret / "project").mkdir(parents=True)
    (workspace / "link").symlink_to(secret)
    for path, reason in (
        ("", "root"), (".", "root"), ("/etc", "root"), ("../ws2", "outside"), ("keep/../..", "outside"),
        ("link", "symlink"), ("link/project", "symlink"), (".checkpoints", "protected"),
        ("inbox/x", "protected"), ("keep/.git", "protected"), ("nothing", "missing"),
    ):
        with pytest.raises(Refused) as caught:
            disk_usage.resolve_target(workspace, path)
        assert caught.value.reason == reason, path
    assert disk_usage.resolve_target(workspace, "keep/inner") == workspace / "keep" / "inner"


def test_find_throwaway_and_stale(tmp_path: Path, mtime_ages) -> None:
    workspace = tmp_path / "ws"
    patterns = RuntimeConfig().disk.throwaway_patterns
    _fill(workspace / "_scratch" / "tree" / "case1" / "f", 10)
    _fill(workspace / "scratch-critic2" / "f", 10)
    _fill(workspace / "project" / ".uv-cache" / "x", 10)
    _fill(workspace / "project" / "node_modules" / "tmp" / "index.js", 10)
    _fill(workspace / "src" / "main.py", 10)
    found = {path.relative_to(workspace).as_posix() for path in disk_usage.find_throwaway(workspace, patterns)}
    assert found == {"_scratch", "scratch-critic2", "project/.uv-cache"}
    old = workspace / "_scratch"
    _age(old, 10)
    cutoff = time.time() - 7 * 86400
    assert disk_usage.stale(old, older_than=cutoff, max_entries=1000)[0]
    # One recent file deep inside keeps the whole tree.
    _fill(old / "tree" / "case1" / "new", 10)
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    assert not disk_usage.stale(old, older_than=cutoff, max_entries=1000)[0]
    # Too big to finish walking is never called stale.
    _age(old, 10)
    assert not disk_usage.stale(old, older_than=cutoff, max_entries=1)[0]


def test_a_copy_with_old_mtimes_is_not_stale(tmp_path: Path) -> None:
    # What ``cp -a`` of an old repository leaves: every mtime weeks back, every inode made just now.
    copy = tmp_path / "ws" / "_scratch"
    _fill(copy / "case" / "f", 10)
    _age(copy, 30)
    assert not disk_usage.stale(copy, older_than=time.time() - 7 * 86400, max_entries=1000)[0]
    assert disk_usage.stale(copy, older_than=time.time() + 60, max_entries=1000)[0]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "someone", "GIT_AUTHOR_EMAIL": "someone@example.invalid",
                        "GIT_COMMITTER_NAME": "someone", "GIT_COMMITTER_EMAIL": "someone@example.invalid"})


def test_git_tracked_looks_at_the_enclosing_checkout_only(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    repo = workspace / "repo"
    _fill(repo / "tmp" / "fixture.txt", 10)
    _fill(repo / "scratch" / "junk", 10)
    _git(repo, "init", "-q")
    _git(repo, "add", "tmp/fixture.txt")
    _git(repo, "commit", "-q", "-m", "fixture")
    assert disk_usage.git_tracked(repo / "tmp", workspace)
    assert not disk_usage.git_tracked(repo / "scratch", workspace)
    # A repository copy inside a scratch directory is the copy to remove, not a checkout to protect.
    copy = workspace / "_scratch" / "case1"
    _fill(copy / "f", 10)
    _git(copy, "init", "-q")
    assert not disk_usage.git_tracked(workspace / "_scratch", workspace)


async def test_quota_crossing_is_told_once_per_level(host) -> None:
    app, root = host
    guard = DiskGuard(app)
    _fill(root / "sess1" / "_scratch" / "copy" / "big.bin", 3 * MB)
    await guard.check()
    assert len(app.manager.submitted) == 1
    session_id, origin, text = app.manager.submitted[0]
    assert (session_id, origin) == ("sess1", "disk")
    assert "_scratch/" in text and "_scratch/copy/" in text
    assert [d.kind for d in app.notifications.posted] == ["disk_quota"]
    # A second pass at the same size says nothing.
    await guard.check(now=time.time() + 3600)
    assert len(app.manager.submitted) == 1
    # The level survives a restart: a fresh guard reads it back from the kv table.
    again = DiskGuard(app)
    await again.check()
    assert len(app.manager.submitted) == 1
    # Twice the limit is news again.
    _fill(root / "sess1" / "_scratch" / "copy2" / "big.bin", 2 * MB)
    await again.check(now=time.time() + 7 * 3600)
    assert len(app.manager.submitted) == 2 and "twice the limit" in app.manager.submitted[1][2]
    assert app.notifications.posted[-1].dedupe_key == "disk-quota:sess1:2"


async def test_project_folder_tells_the_latest_session(host) -> None:
    app, root = host
    guard = DiskGuard(app)
    _fill(root / "proj" / ".uv-cache" / "blob", 3 * MB)
    _fill(root / "orphan" / "blob", 3 * MB)
    await guard.check()
    assert [s[0] for s in app.manager.submitted] == ["sess1"]
    owners = {d.title.split(" uses ")[0]: d for d in app.notifications.posted}
    assert owners["Sample"].project_id == "proj" and owners["Sample"].link == "/app/project/proj"
    # Nobody owns the orphan, so only the operator hears of it.
    assert "No session owns it" in owners["orphan"].body


async def test_low_free_disk_tells_the_operator_once(host, monkeypatch) -> None:
    app, root = host
    app.config.disk.min_free_gb = 10.0
    guard = DiskGuard(app)
    free = {"value": int(5e9)}
    monkeypatch.setattr(disk_usage, "free_space", lambda _path: (free["value"], int(100e9)))
    await guard.check_free()
    await guard.check_free()
    assert [d.kind for d in app.notifications.posted] == ["disk_low"]
    free["value"] = int(11e9)  # above the floor, but not by the margin that re-arms it
    await guard.check_free()
    free["value"] = int(5e9)
    await guard.check_free()
    assert len(app.notifications.posted) == 1
    free["value"] = int(13e9)
    await guard.check_free()
    free["value"] = int(5e9)
    await guard.check_free()
    assert len(app.notifications.posted) == 2


async def test_sweep_removes_stale_throwaway_and_leaves_tracked(host, mtime_ages) -> None:
    app, root = host
    app.config.disk.workspace_soft_limit_gb = 0
    workspace = root / "sess1"
    _fill(workspace / "_scratch" / "case" / "f", MB)
    _fill(workspace / "fresh-scratch" / "f", 10)
    _fill(workspace / "repo" / "tmp" / "fixture", 10)
    _git(workspace / "repo", "init", "-q")
    _git(workspace / "repo", "add", "tmp/fixture")
    _git(workspace / "repo", "commit", "-q", "-m", "fixture")
    _age(workspace / "_scratch", 9)
    _age(workspace / "repo" / "tmp", 9)
    guard = DiskGuard(app)
    await guard.check()
    assert not (workspace / "_scratch").exists()
    assert (workspace / "fresh-scratch").exists()
    assert (workspace / "repo" / "tmp" / "fixture").exists()
    cleaned = [d for d in app.notifications.posted if d.kind == "disk_cleaned"]
    assert len(cleaned) == 1 and "_scratch/" in cleaned[0].body
    # The note to the agent waits for its next turn rather than starting one.
    assert app.manager.submitted == []
    await guard.deliver_notes()
    assert app.manager.submitted == []
    app.manager.busy.add("sess1")
    await guard.deliver_notes()
    assert len(app.manager.submitted) == 1 and "_scratch/" in app.manager.submitted[0][2]
    await guard.deliver_notes()
    assert len(app.manager.submitted) == 1


async def test_sweep_never_leaves_the_managed_root(host, tmp_path: Path, mtime_ages) -> None:
    app, root = host
    elsewhere = tmp_path / "operator-folder"
    _fill(elsewhere / "scratch" / "f", 10)
    _age(elsewhere / "scratch", 30)
    (root / "linked").symlink_to(elsewhere)
    guard = DiskGuard(app)
    await guard.check()
    assert (elsewhere / "scratch" / "f").exists()
    assert "linked" not in guard.workspaces()


async def test_routes_show_and_clean(host) -> None:
    app, root = host
    workspace = root / "sess1"
    _fill(workspace / "_scratch" / "big.bin", 3 * MB)
    _fill(workspace / "notes" / "a.md", 10)
    _fill(workspace / "repo" / "src" / "main.py", 10)
    _git(workspace / "repo", "init", "-q")
    _git(workspace / "repo", "add", "src/main.py")
    _git(workspace / "repo", "commit", "-q", "-m", "src")
    (workspace / ".checkpoints").mkdir()
    (workspace / "escape").symlink_to(root.parent)
    api = FastAPI()
    api_disk.register(api, app, lambda: {"via": "token", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.get("/api/disk/session/missing")).status_code == 404
        view = (await client.get("/api/disk/session/sess1")).json()
        assert [w["name"] for w in view["workspaces"]] == ["sess1"]
        detail = view["workspaces"][0]
        assert detail["over"] == 1
        entries = {e["path"]: e for e in detail["entries"]}
        assert entries["_scratch"]["throwaway"] and not entries["_scratch"]["tracked"]
        assert entries[".checkpoints"]["protected"]
        assert [c["path"] for c in entries["repo"]["children"]][:1] in (["repo/src"], ["repo/.git"])
        project = (await client.get("/api/disk/project/proj")).json()
        assert project["outside"] == [] and project["workspaces"] == []  # its folder was never created
        response = await client.post("/api/disk/cleanup", json={
            "workspace": "sess1", "paths": ["_scratch", "escape", ".checkpoints", "repo/src", "", "../sess1"],
        })
        assert response.status_code == 200
        result = response.json()
        assert result["removed"] == ["_scratch"] and result["freed_bytes"] >= 3 * MB
        assert {r["path"]: r["reason"] for r in result["refused"]} == {
            "escape": "symlink", ".checkpoints": "protected", "repo/src": "tracked", "": "root", "../sess1": "outside",
        }
        assert (workspace / "repo" / "src" / "main.py").exists() and root.parent.exists()
        forced = (await client.post("/api/disk/cleanup", json={"workspace": "sess1", "paths": ["repo/src"], "include_tracked": True})).json()
        assert forced["removed"] == ["repo/src"]
        assert (await client.post("/api/disk/cleanup", json={"workspace": "..", "paths": ["x"]})).status_code == 404
        assert (await client.post("/api/disk/cleanup", json={"workspace": "escape", "paths": ["x"]})).status_code == 404
        overview = (await client.get("/api/disk")).json()
        assert overview["workspaces"][0]["owner"] == {"kind": "session", "id": "sess1", "title": "Experiments"}
