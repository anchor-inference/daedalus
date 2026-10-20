"""A file whose name is not ASCII is listed under its own name, and can be opened from the list.

git quotes such a name by default (``"\\320\\276\\321\\202..."``) and a listing read line by line also
breaks on a rename (``dir/{old => new}``). A worker's ``отчёт.md`` used to appear in the changes view
under a name no file has, so opening it failed.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daedalus.extensions.api_staff import _numstat
from daedalus.host.gitrun import git_argv, nul_names, numstat_entries, run_command
from daedalus.host.worktrees import StaffWorktrees
from daedalus.stores.projects import ProjectFolder

REPORT = "отчёт.md"


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(root: Path) -> tuple[Path, str]:
    """A repository with a base commit, then a branch that adds the report and renames a file."""
    repo = root / "project"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "config", "user.email", "operator@localhost")
    (repo / "docs").mkdir()
    (repo / "docs" / "old name.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "switch", "-q", "-c", "agent/work")
    (repo / REPORT).write_text("готово\n")
    _git(repo, "mv", "docs/old name.txt", "docs/новое имя.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the report")
    return repo, base


async def test_the_changes_view_names_the_report_as_it_is_on_disk(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "заметки.txt").write_text("draft\n")
    numstat = await run_command(git_argv("-C", str(repo), "diff", "--numstat", "-z", base), env={})
    untracked = await run_command(git_argv("-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"), env={})
    view = _numstat(numstat)
    paths = [f["path"] for f in view["files"]]
    assert REPORT in paths and "docs/новое имя.txt" in paths, paths
    assert nul_names(untracked) == ["заметки.txt"]
    for path in [*paths, *nul_names(untracked)]:
        assert (repo / path).is_file(), f"{path} does not open"


async def test_a_staff_branch_comparison_lists_files_that_exist(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    _git(repo, "switch", "-q", "main")
    folder = ProjectFolder(id="f-1", project_id="p-1", path=repo, label="", env="container", is_git=True, readonly=False, position=0, created_at=datetime.now(UTC))
    comparison = await StaffWorktrees("container").compare(folder, "agent/work")
    _git(repo, "switch", "-q", "agent/work")
    assert {f.path for f in comparison.files} == {REPORT, "docs/новое имя.txt"}
    assert REPORT in comparison.patch and "готово" in comparison.patch


def test_numstat_reads_renames_and_binaries() -> None:
    text = "3\t0\tотчёт.md\0-\t-\timage.png\0" + "1\t1\t\0docs/a b.txt\0docs/new\tname.txt\0"
    assert numstat_entries(text) == [(3, 0, "отчёт.md"), (None, None, "image.png"), (1, 1, "docs/new\tname.txt")]
