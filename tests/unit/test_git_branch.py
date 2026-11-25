"""The branch a session's folder is on, and whether it is in a repository at all, are read from
``HEAD``, never by running git."""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.git_branch import UNKNOWN, Head, branch_of, head_of
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

H = {"X-Daedalus-Token": "tok"}


def _repository(path: Path, head: str = "ref: refs/heads/main\n") -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text(head, encoding="utf-8")
    return path


def test_a_checkout_reports_the_branch_it_is_on(tmp_path: Path) -> None:
    assert branch_of(_repository(tmp_path / "repo")) == "main"
    assert branch_of(_repository(tmp_path / "slashed", "ref: refs/heads/feature/small-things\n")) == "feature/small-things"


def test_what_git_itself_says_is_what_is_read(tmp_path: Path) -> None:
    repo = tmp_path / "real"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    assert branch_of(repo) == "trunk"
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side/branch"], check=True)
    assert branch_of(repo) == "side/branch"


def test_a_subfolder_belongs_to_the_repository_around_it(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    inner = repo / "src" / "deep"
    inner.mkdir(parents=True)
    assert branch_of(inner) == "main"


def test_a_folder_outside_any_repository_has_no_branch(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert branch_of(tmp_path / "plain") is None
    assert branch_of(tmp_path / "missing") is None


def test_a_detached_head_is_on_no_branch(tmp_path: Path) -> None:
    assert branch_of(_repository(tmp_path / "repo", "0123456789abcdef0123456789abcdef01234567\n")) is None


def test_a_linked_worktree_reads_the_head_its_git_file_points_to(tmp_path: Path) -> None:
    main = _repository(tmp_path / "main")
    linked_git = main / ".git" / "worktrees" / "wt"
    linked_git.mkdir(parents=True)
    (linked_git / "HEAD").write_text("ref: refs/heads/topic\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {linked_git}\n", encoding="utf-8")
    assert branch_of(tree) == "topic"
    # A relative gitdir (a submodule's) is relative to the folder that holds the file.
    relative = tmp_path / "relative"
    relative.mkdir()
    (relative / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8")
    assert branch_of(relative) == "topic"


def test_the_search_never_leaves_the_folder_it_was_told_to_stay_in(tmp_path: Path) -> None:
    _repository(tmp_path / "outer")
    workspace = tmp_path / "outer" / "workspaces" / "session"
    workspace.mkdir(parents=True)
    assert branch_of(workspace) == "main"
    assert branch_of(workspace, stop=tmp_path / "outer" / "workspaces") is None


@pytest.mark.parametrize("head", ["", "ref: refs/tags/v1\n", "ref: refs/heads/\n", "ref: refs/heads/a\x00b\n", "\xff\xfe garbage"])
def test_a_head_that_is_not_a_branch_reads_as_none(tmp_path: Path, head: str) -> None:
    assert branch_of(_repository(tmp_path / "repo", head)) is None


def test_a_broken_git_marker_reads_as_none_and_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("not a pointer\n", encoding="utf-8")
    assert branch_of(repo) is None
    (repo / ".git").unlink()
    (repo / ".git").mkdir()  # a repository whose HEAD is missing
    assert branch_of(repo) is None


def test_the_head_says_whether_the_folder_is_in_a_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert head_of(_repository(tmp_path / "repo")) == Head(True, "main")
    assert head_of(_repository(tmp_path / "detached", "0123456789abcdef0123456789abcdef01234567\n")) == Head(True, None)
    (tmp_path / "plain").mkdir()
    assert head_of(tmp_path / "plain") == Head(False, None)
    # A .git with no HEAD in it is no repository to git; a .git file that points nowhere neither.
    (tmp_path / "headless" / ".git").mkdir(parents=True)
    assert head_of(tmp_path / "headless") == Head(False, None)
    (tmp_path / "pointer").mkdir()
    (tmp_path / "pointer" / ".git").write_text("gitdir: ../nowhere\n", encoding="utf-8")
    assert head_of(tmp_path / "pointer") == Head(False, None)
    # A HEAD that cannot be read for another reason is not known, which is not "no".
    unreadable = _repository(tmp_path / "unreadable")
    real_open = Path.open

    def refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == unreadable / ".git" / "HEAD":
            raise PermissionError(13, "denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)
    assert head_of(unreadable) == UNKNOWN


def test_reading_the_branch_runs_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the branch is read from a file, not by a subprocess")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    assert branch_of(_repository(tmp_path / "repo")) == "main"
    assert head_of(tmp_path / "repo") == Head(True, "main")


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    made = SessionManager(settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def client(settings: Settings, db: Database, manager: SessionManager) -> Any:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


async def test_an_idle_session_outside_a_repository_has_neither(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("plain")
    body = (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()
    assert body["run_started_at"] is None and body["branch"] is None and body["status"] == "idle"
    assert body["repository"] is False


async def test_the_detail_names_the_branch_of_the_working_folder(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("repo")
    _repository(state.workspace)
    body = (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()
    assert body["branch"] == "main" and body["repository"] is True
    (state.workspace / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()["branch"] == "other"  # read afresh, nothing cached
    (state.workspace / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    detached = (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()
    assert detached["branch"] is None and detached["repository"] is True


async def test_a_folder_in_another_environment_is_not_known_here(client: httpx.AsyncClient, manager: SessionManager,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # A remote conversation's folder is on the operator's machine: whatever this process could read
    # at the same path is not it.
    state = await manager.create_session("remote")
    _repository(state.workspace)
    monkeypatch.setattr(manager, "env_of", lambda *_: "device:laptop")
    body = (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()
    assert body["repository"] is None and body["branch"] is None


async def test_a_running_session_says_when_its_run_began_and_an_idle_one_says_nothing(client: httpx.AsyncClient, manager: SessionManager) -> None:
    state = await manager.create_session("busy")
    sid = state.session.id
    release = asyncio.Event()
    state.task = asyncio.create_task(release.wait())
    state.run_active_since = time.monotonic() - 90
    try:
        body = (await client.get(f"/api/sessions/{sid}", headers=H)).json()
        assert body["status"] == "running"
        began = datetime.fromisoformat(body["run_started_at"])
        assert 85 < (datetime.now(UTC) - began).total_seconds() < 120
    finally:
        release.set()
        await state.task
    assert (await client.get(f"/api/sessions/{sid}", headers=H)).json()["run_started_at"] is None
