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
    (workspace / "inbox").mkdir()
    secret = tmp_path / "operator-home"
    (secret / "project").mkdir(parents=True)
    (workspace / "link").symlink_to(secret)
    for path, reason in (
        ("", "root"), (".", "root"), ("/etc", "root"), ("../ws2", "outside"), ("keep/../..", "outside"),
        ("link", "symlink"), ("link/project", "symlink"), ("inbox", "protected"),
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
    (workspace / "inbox").mkdir()
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
        assert entries["inbox"]["protected"]
        assert [c["path"] for c in entries["repo"]["children"]][:1] in (["repo/src"], ["repo/.git"])
        project = (await client.get("/api/disk/project/proj")).json()
        assert project["outside"] == [] and project["workspaces"] == []  # its folder was never created
        response = await client.post("/api/disk/cleanup", json={
            "workspace": "sess1", "paths": ["_scratch", "escape", "inbox", "repo/src", "", "../sess1"],
        })
        assert response.status_code == 200
        result = response.json()
        assert result["removed"] == ["_scratch"] and result["freed_bytes"] >= 3 * MB
        assert {r["path"]: r["reason"] for r in result["refused"]} == {
            "escape": "symlink", "inbox": "protected", "repo/src": "tracked", "": "root", "../sess1": "outside",
        }
        assert (workspace / "repo" / "src" / "main.py").exists() and root.parent.exists()
        forced = (await client.post("/api/disk/cleanup", json={"workspace": "sess1", "paths": ["repo/src"], "include_tracked": True})).json()
        assert forced["removed"] == ["repo/src"]
        assert (await client.post("/api/disk/cleanup", json={"workspace": "..", "paths": ["x"]})).status_code == 404
        assert (await client.post("/api/disk/cleanup", json={"workspace": "escape", "paths": ["x"]})).status_code == 404
        overview = (await client.get("/api/disk")).json()
        assert overview["workspaces"][0]["owner"] == {"kind": "session", "id": "sess1", "title": "Experiments"}


def _api(app: Any, auth: Any = None) -> FastAPI:
    api = FastAPI()
    api_disk.register(api, app, auth or (lambda: {"via": "token", "user_id": 1}))
    return api


def _client(api: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


async def test_the_limit_is_the_workspaces_own_or_the_default(host) -> None:
    app, root = host
    (root / "sess1").mkdir()
    (root / "other").mkdir()
    guard = DiskGuard(app)
    await guard.load()
    default = guard.default_limit
    assert default == 2 * MB and guard.limit_for("sess1") == default and not guard.has_own_limit("sess1")
    await guard.set_limit("sess1", 7 * MB)
    assert guard.limit_for("sess1") == 7 * MB and guard.has_own_limit("sess1")
    assert guard.limit_for("other") == default
    # "No limit" is a limit of its own, not a missing one: the default no longer reaches the workspace.
    await guard.set_limit("sess1", 0)
    assert guard.limit_for("sess1") == 0 and guard.has_own_limit("sess1")
    # Changing the default moves only the workspaces that follow it.
    app.config.disk.workspace_soft_limit_gb = 50 * MB / 1e9
    assert guard.limit_for("other") == 50 * MB and guard.limit_for("sess1") == 0
    await guard.set_limit("sess1", None)
    assert guard.limit_for("sess1") == 50 * MB and not guard.has_own_limit("sess1")
    # The default turned off means no limit for everyone who follows it.
    app.config.disk.workspace_soft_limit_gb = 0
    assert guard.limit_for("other") == 0
    with pytest.raises(KeyError):
        await guard.set_limit("nowhere", MB)
    with pytest.raises(KeyError):
        await guard.set_limit("../sess1", MB)


async def test_a_limit_outlives_a_restart(host) -> None:
    app, root = host
    (root / "sess1").mkdir()
    await DiskGuard(app).set_limit("sess1", 9 * MB)
    again = DiskGuard(app)
    await again.load()
    assert again.limit_for("sess1") == 9 * MB
    rows = await app.db.fetchall("SELECT name, limit_bytes FROM workspace_limits")
    assert [(row["name"], row["limit_bytes"]) for row in rows] == [("sess1", 9 * MB)]


async def test_the_workspaces_own_limit_decides_the_warning(host) -> None:
    app, root = host
    _fill(root / "sess1" / "_scratch" / "big.bin", 3 * MB)
    _fill(root / "orphan" / "big.bin", 3 * MB)
    guard = DiskGuard(app)
    await guard.load()
    # 3 MB is past the 2 MB default for both; the operator lifts one and removes the limit of the other.
    await guard.set_limit("sess1", 50 * MB)
    await guard.set_limit("orphan", 0)
    await guard.check()
    assert app.manager.submitted == [] and app.notifications.posted == []
    # A limit under the size warns although the default would not.
    app.config.disk.workspace_soft_limit_gb = 500 * MB / 1e9
    await guard.set_limit("sess1", MB)
    await guard.check()
    assert [s[0] for s in app.manager.submitted] == ["sess1"]
    assert "past the soft limit of 1.0 MB" in app.manager.submitted[0][2]
    assert "twice the limit" in app.manager.submitted[0][2]  # 3 MB is past two limits of 1 MB


async def test_raising_the_limit_rearms_the_warning_levels(host) -> None:
    app, root = host
    _fill(root / "sess1" / "_scratch" / "big.bin", 3 * MB)
    guard = DiskGuard(app)
    await guard.check()
    assert guard.state["levels"] == {"sess1": 1}
    assert len(app.manager.submitted) == 1
    # Raised only a little: 3 MB is still above 0.8 of 3.5 MB, so the level stays told, and lowering the
    # limit again does not repeat a warning that was given for this very crossing.
    await guard.set_limit("sess1", int(3.5 * MB))
    assert guard.state["levels"] == {"sess1": 1}
    await guard.check(now=time.time() + 60)
    await guard.set_limit("sess1", 2 * MB)
    await guard.check(now=time.time() + 120)
    assert len(app.manager.submitted) == 1
    # Raised well past the size: nothing is said, and the told level is forgotten at once.
    await guard.set_limit("sess1", 40 * MB)
    assert "sess1" not in guard.state["levels"]
    await guard.check(now=time.time() + 180)
    assert len(app.manager.submitted) == 1
    # Down to the original limit after having been far above it: it is news again, told at the next pass.
    await guard.set_limit("sess1", 2 * MB)
    assert "sess1" not in guard.state["levels"]
    await guard.check(now=time.time() + 240)
    assert len(app.manager.submitted) == 2 and guard.state["levels"] == {"sess1": 1}
    # What a restart reads back is the reconciled state, not the one from before the raise.
    await guard.set_limit("sess1", 40 * MB)
    assert (await app.db.kv_get("disk_guard"))["levels"] == {}
    # Twice the size is the second level of the limit in force, not of the one that was told about.
    await guard.set_limit("sess1", MB)
    await guard.check(now=time.time() + 300)
    assert len(app.manager.submitted) == 3 and "twice the limit" in app.manager.submitted[2][2]


async def test_a_changed_limit_is_judged_at_the_next_pass_whatever_the_cadence(host) -> None:
    app, root = host
    _fill(root / "sess1" / "small.bin", MB)
    guard = DiskGuard(app)
    now = time.time()
    await guard.check(now=now)
    owner = (await guard.owners())["sess1"]
    assert not guard.due("sess1", owner, now + 60)  # under half the limit, nobody running: left alone
    await guard.set_limit("sess1", MB // 4)
    assert guard.due("sess1", owner, now + 60)
    await guard.check(now=now + 60)
    assert len(app.manager.submitted) == 1 and not guard.rejudge


async def test_limits_of_removed_workspaces_go_but_an_unreadable_root_costs_nothing(host) -> None:
    app, root = host
    (root / "sess1").mkdir()
    (root / "gone").mkdir()
    guard = DiskGuard(app)
    await guard.set_limit("sess1", MB)
    await guard.set_limit("gone", MB)
    (root / "gone").rmdir()
    await guard.forget_limits_of_gone([])  # the root listed nothing: that is no evidence of anything
    assert set(guard.limits) == {"sess1", "gone"}
    await guard.forget_limits_of_gone(["sess1"])
    assert guard.limits == {"sess1": MB}
    assert [row["name"] for row in await app.db.fetchall("SELECT name FROM workspace_limits")] == ["sess1"]


async def test_the_limit_route_sets_shows_and_clears(host) -> None:
    app, root = host
    _fill(root / "sess1" / "big.bin", 3 * MB)
    async with _client(_api(app)) as client:
        before = (await client.get("/api/disk/session/sess1")).json()
        assert before["default_limit_bytes"] == 2 * MB
        assert before["workspaces"][0]["limit_bytes"] == 2 * MB and not before["workspaces"][0]["own_limit"]
        assert before["workspaces"][0]["over"] == 1
        # 5 MB is written as the decimal gigabytes the setting uses.
        raised = await client.put("/api/disk/limit/sess1", json={"limit_gb": 5 * MB / 1e9})
        assert raised.status_code == 200
        assert raised.json() == {"name": "sess1", "limit_bytes": 5 * MB, "own_limit": True, "default_limit_bytes": 2 * MB, "over": 0}
        after = (await client.get("/api/disk/session/sess1")).json()["workspaces"][0]
        assert (after["limit_bytes"], after["own_limit"], after["over"]) == (5 * MB, True, 0)
        row = (await client.get("/api/disk")).json()["workspaces"][0]
        assert (row["name"], row["limit_bytes"], row["own_limit"], row["over"]) == ("sess1", 5 * MB, True, 0)
        # Past twice the limit.
        tight = (await client.put("/api/disk/limit/sess1", json={"limit_gb": 1.2 * MB / 1e9})).json()
        assert tight["over"] == 2
        # No limit, and back to following the default.
        none = (await client.put("/api/disk/limit/sess1", json={"limit_gb": 0})).json()
        assert (none["limit_bytes"], none["own_limit"], none["over"]) == (0, True, 0)
        default = (await client.put("/api/disk/limit/sess1", json={"limit_gb": None})).json()
        assert (default["limit_bytes"], default["own_limit"], default["over"]) == (2 * MB, False, 1)
        assert await app.db.fetchall("SELECT 1 FROM workspace_limits") == []


async def test_the_limit_route_refuses_what_is_not_a_limit_of_a_workspace(host) -> None:
    app, root = host
    (root / "sess1").mkdir()
    (root / "escape").symlink_to(root.parent)
    async with _client(_api(app)) as client:
        assert (await client.put("/api/disk/limit/nowhere", json={"limit_gb": 1})).status_code == 404
        assert (await client.put("/api/disk/limit/escape", json={"limit_gb": 1})).status_code == 404
        assert (await client.put("/api/disk/limit/sess1", json={"limit_gb": -1})).status_code == 422
        assert (await client.put("/api/disk/limit/sess1", json={"limit_gb": 10**9})).status_code == 422
        assert (await client.put("/api/disk/limit/sess1", json={})).status_code == 422
        assert (await client.put("/api/disk/limit/sess1", json={"limit_gb": 1, "extra": 1})).status_code == 422
        assert (await client.put("/api/disk/limit/sess1", json={"limit_gb": "lots"})).status_code == 422
    assert await app.db.fetchall("SELECT 1 FROM workspace_limits") == []


async def test_only_the_operator_reaches_the_limit_route(host) -> None:
    app, root = host
    (root / "sess1").mkdir()

    def auth() -> dict[str, Any]:
        from fastapi import HTTPException

        raise HTTPException(401, "not signed in")

    async with _client(_api(app, auth)) as client:
        assert (await client.put("/api/disk/limit/sess1", json={"limit_gb": 1})).status_code == 401
        assert (await client.get("/api/disk")).status_code == 401
    assert await app.db.fetchall("SELECT 1 FROM workspace_limits") == []


async def test_the_limits_table_exists_and_refuses_a_negative_limit(host) -> None:
    app, _root = host
    from daedalus.stores.database import MIGRATIONS

    assert int((await app.db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
    with pytest.raises(Exception, match="CHECK"):
        await app.db.execute("INSERT INTO workspace_limits(name, limit_bytes, updated_at) VALUES ('x', -1, 'now')")


async def test_the_views_know_the_limits_before_the_guard_has_made_its_first_pass(host) -> None:
    app, root = host
    _fill(root / "sess1" / "big.bin", 3 * MB)
    await app.db.execute("INSERT INTO workspace_limits(name, limit_bytes, updated_at) VALUES ('sess1', ?, 'now')", (9 * MB,))
    # A freshly started host: nothing has loaded the table, and the routes must not answer with the default.
    async with _client(_api(app)) as client:
        workspace = (await client.get("/api/disk/session/sess1")).json()["workspaces"][0]
        assert (workspace["limit_bytes"], workspace["own_limit"], workspace["over"]) == (9 * MB, True, 0)
    app.extensions.clear()
    async with _client(_api(app)) as client:
        await client.get("/api/disk/session/sess1")  # measures it, so the overview lists it
        row = (await client.get("/api/disk")).json()["workspaces"][0]
        assert (row["limit_bytes"], row["own_limit"]) == (9 * MB, True)
