"""``.worktreeinclude``: the ignored files a new worktree needs, copied in from the folder it was cut from.

Against real git in temporary repositories, because what counts as ignored is git's answer. Nothing here
touches the network: every repository is local and has no remote.
"""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daedalus.host import worktrees
from daedalus.host.worktrees import StaffWorktrees, copy_included
from daedalus.stores.projects import ProjectFolder


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(root: Path, *, ignore: str = ".env\nsecrets/\nnode_modules/\n*.log\n", include: str | None = ".env\nsecrets/\n") -> Path:
    repo = root / "project"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "config", "user.email", "operator@localhost")
    (repo / ".gitignore").write_text(ignore)
    (repo / "readme.txt").write_text("one\n")
    if include is not None:
        (repo / ".worktreeinclude").write_text(include)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the first commit")
    return repo


def _folder(path: Path) -> ProjectFolder:
    return ProjectFolder(id="f-1", project_id="p-1", path=path, label="", env="container", is_git=True, readonly=False, position=0, created_at=datetime.now(UTC))


async def test_a_new_staff_worktree_gets_the_ignored_files_the_include_names(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".env").write_text("TOKEN=abc\n")
    os.chmod(repo / ".env", 0o600)
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key.pem").write_text("pem\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "big.js").write_text("x\n")
    (repo / "debug.log").write_text("noise\n")

    tree = await StaffWorktrees("container").prepare(_folder(repo), "Anna", "1", "Task")

    assert (tree.path / ".env").read_text() == "TOKEN=abc\n"
    assert stat.S_IMODE((tree.path / ".env").stat().st_mode) == 0o600
    assert (tree.path / "secrets" / "key.pem").read_text() == "pem\n"
    # Ignored but not named: a dependency folder is the setup command's to build, not ours to copy.
    assert not (tree.path / "node_modules").exists()
    assert not (tree.path / "debug.log").exists()
    assert _git(tree.path, "status", "--porcelain") == ""


async def test_without_an_include_file_nothing_is_copied(tmp_path: Path) -> None:
    repo = _repo(tmp_path, include=None)
    (repo / ".env").write_text("TOKEN=abc\n")

    tree = await StaffWorktrees("container").prepare(_folder(repo), "Anna", "1", "Task")

    assert not (tree.path / ".env").exists()


async def test_a_named_file_git_does_not_ignore_is_left_alone(tmp_path: Path) -> None:
    # An untracked file that is not ignored would show up in the member's git status and be swept into
    # its commit; only what the branch ignores too is copied.
    repo = _repo(tmp_path, include=".env\nnotes.txt\n")
    (repo / "notes.txt").write_text("draft\n")

    copied = await copy_included(repo, _worktree(repo, tmp_path))

    assert copied == []


def _worktree(repo: Path, tmp_path: Path) -> Path:
    target = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(target))
    return target


async def test_an_existing_file_in_the_worktree_is_never_overwritten(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".env").write_text("from the folder\n")
    target = _worktree(repo, tmp_path)
    (target / ".env").write_text("the member's own\n")

    copied = await copy_included(repo, target)

    assert copied == []
    assert (target / ".env").read_text() == "the member's own\n"


async def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not the project's\n")
    repo = _repo(tmp_path)
    (repo / ".env").symlink_to(outside / "private.txt")
    (repo / "secrets").symlink_to(outside, target_is_directory=True)

    target = _worktree(repo, tmp_path)
    copied = await copy_included(repo, target)

    assert copied == []
    assert not os.path.lexists(target / ".env")
    assert not os.path.lexists(target / "secrets")


async def test_an_include_file_that_is_a_symlink_is_not_read(tmp_path: Path) -> None:
    repo = _repo(tmp_path, include=None)
    (tmp_path / "elsewhere").write_text(".env\n")
    (repo / ".worktreeinclude").symlink_to(tmp_path / "elsewhere")
    (repo / ".env").write_text("TOKEN=abc\n")

    assert await copy_included(repo, _worktree(repo, tmp_path)) == []


async def test_a_tracked_symlinked_directory_in_the_worktree_is_not_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = _repo(tmp_path, ignore="secrets/key.pem\n", include="secrets/key.pem\n")
    # The folder has a real directory; the branch the worktree is on has a link there instead.
    target = _worktree(repo, tmp_path)
    (target / "secrets").symlink_to(outside, target_is_directory=True)
    _git(target, "add", "secrets")
    _git(target, "commit", "-qm", "link")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key.pem").write_text("pem\n")

    assert await copy_included(repo, target) == []
    assert list(outside.iterdir()) == []


async def test_the_size_cap_stops_the_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees, "INCLUDE_MAX_BYTES", 10)
    repo = _repo(tmp_path)
    (repo / ".env").write_text("small\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "huge.bin").write_bytes(b"x" * 100)

    target = _worktree(repo, tmp_path)
    copied = await copy_included(repo, target)

    assert copied == [".env"]
    assert not (target / "secrets" / "huge.bin").exists()


async def test_other_worktrees_inside_the_folder_are_not_a_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".env").write_text("TOKEN=abc\n")
    trees = StaffWorktrees("container")
    first = await trees.prepare(_folder(repo), "Anna", "1", "Task")
    (first.path / "secrets").mkdir()
    (first.path / "secrets" / "anna.pem").write_text("anna's\n")

    second = await trees.prepare(_folder(repo), "Boris", "2", "Task")

    assert (second.path / ".env").exists()
    assert not (second.path / ".agents").exists()
    assert not (second.path / "secrets").exists()
