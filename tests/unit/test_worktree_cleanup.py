"""The worker worktree list and its safe removal, against real git in temporary repositories.

What decides whether a removal is safe is git's own view — what ``git status`` counts, whether a
branch is an ancestor of the folder's branch, what ``git worktree remove`` refuses — so these run
real git in a temporary directory with no remote. Nothing outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.host.worktrees import StaffWorktrees, WorktreeRefused
from daedalus.stores.database import Database

HEADERS = {"X-Daedalus-Token": "tok"}


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine's own git config stays out: a global hook or signing rule would change what git does here."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(root: Path) -> Path:
    repo = root / "shop"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "config", "user.email", "operator@localhost")
    (repo / "readme.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the first commit")
    return repo


def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


async def test_list_and_remove_worktrees_safely(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    repo = _repo(tmp_path)
    try:
        async with _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Shop", "folders": [{"path": str(repo)}]})).json()
            pid = project["id"]
            folder = (await manager.projects.get(pid)).primary  # type: ignore[union-attr]
            ada = (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada"})).json()
            bo = (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Bo"})).json()
            cy = (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Cy"})).json()
            trees = StaffWorktrees("container")
            ada_tree = await trees.prepare(folder, "Ada", "1", "Menu page")
            bo_tree = await trees.prepare(folder, "Bo", "2", "Prices")
            cy_tree = await trees.prepare(folder, "Cy", "3", "Footer")

            # Ada works in hers right now; Bo left a file uncommitted; Cy's branch is merged into main.
            await manager.staff.claim_session(ada["id"], kind="daedalus", worktree_path=str(ada_tree.path), branch=ada_tree.branch)
            (bo_tree.path / "draft.txt").write_text("half done\n")
            (cy_tree.path / "footer.txt").write_text("footer\n")
            _git(cy_tree.path, "add", "-A")
            _git(cy_tree.path, "commit", "-qm", "the footer")
            _git(repo, "merge", "-q", "--no-ff", "--no-edit", cy_tree.branch)

            assert (await client.get(f"/api/projects/{pid}/worktrees")).status_code == 401
            listing = (await client.get(f"/api/projects/{pid}/worktrees", headers=HEADERS)).json()
            assert listing["problems"] == []
            rows = {row["name"]: row for row in listing["worktrees"]}
            assert set(rows) == {"ada", "bo", "cy"}
            assert rows["ada"]["member"]["name"] == "Ada" and rows["ada"]["live"]["staff_id"] == ada["id"]
            assert rows["ada"]["blocked"] == "live" and rows["ada"]["branch"] == "agent/ada/1-menu-page"
            assert rows["bo"]["changes"] == 1 and rows["bo"]["blocked"] == "changes" and rows["bo"]["live"] is None
            assert rows["cy"]["blocked"] == "" and rows["cy"]["merged"] is True and rows["cy"]["base"] == "main"
            assert rows["cy"]["size_bytes"] > 0 and rows["cy"]["last_commit_at"]
            # A branch with no commit of its own is in main already: deleting it would lose nothing.
            assert rows["bo"]["merged"] is True and rows["bo"]["member"]["id"] == bo["id"] and rows["cy"]["member"]["id"] == cy["id"]

            # The refusals are checked again on the server, whatever the app drew.
            refused = await client.post(f"/api/projects/{pid}/worktrees/remove", headers=HEADERS,
                                        json={"folder_id": folder.id, "path": str(ada_tree.path)})
            assert refused.status_code == 409 and "Ada is working" in refused.json()["detail"]
            dirty = await client.post(f"/api/projects/{pid}/worktrees/remove", headers=HEADERS,
                                      json={"folder_id": folder.id, "path": str(bo_tree.path)})
            assert dirty.status_code == 409 and "uncommitted" in dirty.json()["detail"]
            assert (bo_tree.path / "draft.txt").exists()
            stray = await client.post(f"/api/projects/{pid}/worktrees/remove", headers=HEADERS,
                                      json={"folder_id": folder.id, "path": str(repo)})
            assert stray.status_code == 409 and "not a staff worktree" in stray.json()["detail"]
            assert (repo / "readme.txt").exists()

            # A merged branch goes too when asked; the worktree directory is gone and git forgets it.
            gone = await client.post(f"/api/projects/{pid}/worktrees/remove", headers=HEADERS,
                                     json={"folder_id": folder.id, "path": str(cy_tree.path), "delete_branch": True})
            assert gone.status_code == 200 and gone.json() == {"removed": True, "branch_deleted": True}
            assert not cy_tree.path.exists()
            assert cy_tree.branch not in _git(repo, "branch", "--list")
            after = (await client.get(f"/api/projects/{pid}/worktrees", headers=HEADERS)).json()
            assert {row["name"] for row in after["worktrees"]} == {"ada", "bo"}
    finally:
        await manager.close()


async def test_removal_keeps_an_unmerged_branch_and_prunes_a_vanished_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    folder = SimpleNamespace(id="f-1", project_id="p-1", path=repo, env="container", readonly=False)
    trees = StaffWorktrees("container")
    tree = await trees.prepare(folder, "Dee", "4", "Search")  # type: ignore[arg-type]
    (tree.path / "search.txt").write_text("search\n")
    _git(tree.path, "add", "-A")
    _git(tree.path, "commit", "-qm", "search")

    # Asked to delete the branch, an unmerged one is still kept: it is the only copy of the work.
    assert await trees.remove_listed(folder, tree.path, delete_branch=True) is False  # type: ignore[arg-type]
    assert not tree.path.exists() and tree.branch in _git(repo, "branch", "--list")

    other = await trees.prepare(folder, "Eve", "5", "Cart")  # type: ignore[arg-type]
    shutil.rmtree(other.path)
    entries: list[Any] = await trees.inventory(folder)  # type: ignore[arg-type]
    assert [(e.path.name, e.prunable) for e in entries] == [("eve", True)]
    assert await trees.remove_listed(folder, other.path, delete_branch=False) is False  # type: ignore[arg-type]
    assert await trees.inventory(folder) == []  # type: ignore[arg-type]


async def test_a_read_only_folder_or_plain_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    writable = SimpleNamespace(id="f-1", project_id="p-1", path=repo, env="container", readonly=False)
    tree = await trees.prepare(writable, "Fay", "6", "Docs")  # type: ignore[arg-type]
    locked = SimpleNamespace(id="f-1", project_id="p-1", path=repo, env="container", readonly=True)
    with pytest.raises(WorktreeRefused, match="read-only"):
        await trees.remove_listed(locked, tree.path, delete_branch=False)  # type: ignore[arg-type]
    assert tree.path.exists()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await trees.inventory(SimpleNamespace(id="f-2", project_id="p-1", path=plain, env="container", readonly=False)) == []  # type: ignore[arg-type]
