"""Staff worktrees, against real git in temporary repositories.

The behaviour under test is git's as much as ours — whether ``worktree add`` accepts a path, whether a
branch is merged, what ``git status`` shows the operator — so a fake repository would pass where a real
one refused. Nothing here touches the network: every repository is local and has no remote.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.host.containment import walls_for
from daedalus.host.worktrees import (
    EXCLUDE_MARKER,
    LocalGit,
    StaffWorktrees,
    WorktreeRefused,
    WorktreeUnavailable,
    append_exclude,
    branch_name,
    exclude_lines,
    slug,
    staff_slug,
)
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.tools import shell


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's own git config out: a global ``commit.gpgsign`` or hook path would change
    what these repositories do, and the operator identity the tests check must be the one set here."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(root: Path, name: str = "project") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "config", "user.email", "operator@localhost")
    (repo / "readme.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the first commit")
    return repo


def _folder(path: Path, *, env: str = "container", readonly: bool = False) -> ProjectFolder:
    return ProjectFolder(
        id="f-1", project_id="p-1", path=path, label="", env=env, is_git=True, readonly=readonly, position=0, created_at=datetime.now(UTC)
    )


async def test_prepare_cuts_the_branch_from_the_folders_current_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "develop")
    (repo / "develop.txt").write_text("d\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "on develop")
    trees = StaffWorktrees("container")

    tree = await trees.prepare(_folder(repo), "Anna", "42", "Fix the login form")

    assert tree.path == repo / ".agents" / "worktrees" / "anna"
    assert tree.branch == "agent/anna/42-fix-the-login-form"
    assert tree.base_ref == "develop" and tree.cwd == tree.path
    assert _git(tree.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == tree.branch
    assert _git(tree.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "develop")
    assert (tree.path / "develop.txt").exists()
    # The staff identity is the worktree's own; the operator's repository config still says Operator.
    assert _git(tree.path, "config", "user.email").strip() == "daedalus@localhost"
    assert _git(repo, "config", "user.email").strip() == "operator@localhost"
    # The operator's checkout does not see the worktree, and nothing was written into its .gitignore.
    assert _git(repo, "status", "--porcelain") == ""
    assert "/.agents/" in (repo / ".git" / "info" / "exclude").read_text()
    assert not (repo / ".gitignore").exists()


async def test_two_staff_in_one_repository_at_once_both_get_a_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)

    first, second = await asyncio.gather(
        trees.prepare(folder, "Anna", "1", "one"),
        trees.prepare(folder, "Boris", "2", "two"),
    )

    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(first.path) in listed and str(second.path) in listed
    assert first.branch == "agent/anna/1-one" and second.branch == "agent/boris/2-two"
    assert _git(repo, "status", "--porcelain") == ""
    # Both went through the one lock of that repository.
    assert len(trees._locks) == 1


@pytest.mark.parametrize("untracked", [False, True])
async def test_dirty_source_is_refused_before_worktree_metadata_is_written(tmp_path: Path, untracked: bool) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    if untracked:
        (repo / "new.txt").write_text("unfinished\n")
    else:
        (repo / "readme.txt").write_text("unfinished\n")
    exclude = repo / ".git" / "info" / "exclude"
    before = exclude.read_bytes()

    with pytest.raises(WorktreeRefused, match="uncommitted changes"):
        await trees.prepare(_folder(repo), "Anna", "1", "one")

    assert exclude.read_bytes() == before
    assert not (repo / ".agents" / "worktrees" / "anna").exists()
    assert not _git(repo, "branch", "--list", "agent/anna/1-one").strip()


async def test_source_commit_moved_while_queued_is_refused_before_write(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    assigned_head = await trees.check(folder)
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "operator moved the source")
    exclude = repo / ".git" / "info" / "exclude"
    before = exclude.read_bytes()

    with pytest.raises(WorktreeRefused, match="moved since assignment"):
        await trees.prepare(folder, "Anna", "1", "one", expected_head=assigned_head)

    assert exclude.read_bytes() == before
    assert not _git(repo, "branch", "--list", "agent/anna/1-one").strip()
    assert not (repo / ".agents" / "worktrees" / "anna").exists()


async def test_generated_untracked_files_do_not_make_the_source_dirty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".agents").mkdir()
    (repo / ".agents" / "inbox.txt").write_text("generated\n")
    (repo / "inbox").mkdir()
    (repo / "inbox" / "message.txt").write_text("generated\n")
    (repo / "PLAN.md").write_text("# Generated board plan\n")

    tree = await StaffWorktrees("container").prepare(_folder(repo), "Anna", "1", "one")

    assert tree.path.exists()
    assert _git(repo, "status", "--porcelain") == ""


async def test_subfolder_source_checks_the_whole_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "module").mkdir()
    (repo / "module" / "entry.txt").write_text("entry\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add module")
    (repo / "outside.txt").write_text("unfinished\n")

    with pytest.raises(WorktreeRefused, match="uncommitted changes"):
        await StaffWorktrees("container").prepare(_folder(repo / "module"), "Anna", "1", "one")

    assert not (repo / "module" / ".agents" / "worktrees" / "anna").exists()


async def test_source_move_during_preparation_is_refused_before_branch_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    original = LocalGit.ensure_excluded

    async def move_source(git: LocalGit, folder: Path) -> None:
        await original(git, folder)
        (folder / "later.txt").write_text("later\n")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-qm", "operator moved the source")

    monkeypatch.setattr(LocalGit, "ensure_excluded", move_source)

    with pytest.raises(WorktreeRefused, match="moved since assignment"):
        await StaffWorktrees("container").prepare(_folder(repo), "Anna", "1", "one")

    assert not _git(repo, "branch", "--list", "agent/anna/1-one").strip()
    assert not (repo / ".agents" / "worktrees" / "anna").exists()


async def test_the_next_task_reuses_the_worktree_and_keeps_what_was_installed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ignore dependencies")
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    first = await trees.prepare(folder, "Anna", "1", "one")
    (first.path / "node_modules").mkdir()
    (first.path / "node_modules" / "left-pad.js").write_text("installed once\n")
    (first.path / "work.txt").write_text("done\n")
    _git(first.path, "add", "-A")
    _git(first.path, "commit", "-qm", "the first task")

    second = await trees.prepare(folder, "Anna", "2", "two")

    assert second.path == first.path and second.branch == "agent/anna/2-two"
    assert (second.path / "node_modules" / "left-pad.js").exists()
    # The new branch starts from the folder's branch, not from the previous task's work.
    assert not (second.path / "work.txt").exists()
    assert _git(repo, "branch", "--list", first.branch).strip()
    # Preparing the same task again is a no-op.
    again = await trees.prepare(folder, "Anna", "2", "two")
    assert again == second


async def test_a_dirty_worktree_is_refused_rather_than_switched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    first = await trees.prepare(folder, "Anna", "1", "one")
    (first.path / "readme.txt").write_text("half done\n")

    with pytest.raises(WorktreeRefused, match="uncommitted changes"):
        await trees.prepare(folder, "Anna", "2", "two")

    assert _git(first.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == first.branch
    assert (first.path / "readme.txt").read_text() == "half done\n"


async def test_commit_wip_commits_everything_on_the_staff_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    tree = await trees.prepare(_folder(repo), "Anna", "1", "one")
    assert await trees.commit_wip(tree, "wip: one (paused)") is None

    (tree.path / "readme.txt").write_text("changed\n")
    (tree.path / "new.txt").write_text("new\n")
    # A hook that refuses every commit does not stop a pause.
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    sha = await trees.commit_wip(tree, "wip: one (paused)")

    assert sha == _git(tree.path, "rev-parse", "refs/heads/" + tree.branch).strip()
    assert _git(tree.path, "log", "-1", "--format=%an <%ae>|%s").strip() == "daedalus <daedalus@localhost>|wip: one (paused)"
    assert _git(tree.path, "status", "--porcelain") == ""
    status = await trees.status(tree)
    assert (status.dirty, status.ahead, status.behind) == (False, 1, 0)


async def test_status_counts_both_sides_of_the_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    tree = await trees.prepare(_folder(repo), "Anna", "1", "one")
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the operator moved on")
    (tree.path / "mine.txt").write_text("mine\n")

    status = await trees.status(tree)

    assert (status.dirty, status.ahead, status.behind) == (True, 0, 1)


async def test_a_staff_branch_merges_as_a_merge_commit_even_when_it_could_fast_forward(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    tree = await trees.prepare(folder, "Anna", "1", "one")
    (tree.path / "feature.txt").write_text("feature\n")
    await trees.commit_wip(tree, "add the feature")
    assert await trees.unmerged(folder) == [tree.branch]
    assert not await trees.merged(folder, tree.branch)

    sha = await trees.merge(folder, tree.branch)

    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    assert len(parents) == 3  # the commit and its two parents
    assert (repo / "feature.txt").exists()
    assert await trees.merged(folder, tree.branch)
    assert await trees.unmerged(folder) == []


async def test_a_conflicting_merge_is_undone_and_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    tree = await trees.prepare(folder, "Anna", "1", "one")
    (tree.path / "readme.txt").write_text("staff\n")
    await trees.commit_wip(tree, "staff edit")
    (repo / "readme.txt").write_text("operator\n")
    _git(repo, "commit", "-qam", "operator edit")
    head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(WorktreeRefused, match="did not go through"):
        await trees.merge(folder, tree.branch)

    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "readme.txt").read_text() == "operator\n"


async def test_a_merge_into_a_folder_with_uncommitted_changes_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    tree = await trees.prepare(folder, "Anna", "1", "one")
    (tree.path / "feature.txt").write_text("feature\n")
    await trees.commit_wip(tree, "add the feature")
    (repo / "readme.txt").write_text("the operator is editing\n")

    with pytest.raises(WorktreeRefused, match="uncommitted changes"):
        await trees.merge(folder, tree.branch)

    assert (repo / "readme.txt").read_text() == "the operator is editing\n"


async def test_remove_keeps_an_unmerged_branch_and_deletes_a_merged_one(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    tree = await trees.prepare(folder, "Anna", "1", "one")
    (tree.path / "feature.txt").write_text("feature\n")
    await trees.commit_wip(tree, "add the feature")

    assert await trees.remove(tree, delete_branch_if_merged=True) is False
    assert not tree.path.exists()
    assert _git(repo, "branch", "--list", tree.branch).strip()

    again = await trees.prepare(folder, "Anna", "1", "one")
    assert (again.path / "feature.txt").exists()  # the kept branch carries the work back
    await trees.merge(folder, again.branch)
    assert await trees.remove(again, delete_branch_if_merged=True) is True
    assert not _git(repo, "branch", "--list", again.branch).strip()
    assert _git(repo, "status", "--porcelain") == ""


async def test_remove_refuses_a_dirty_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    tree = await trees.prepare(_folder(repo), "Anna", "1", "one")
    (tree.path / "unsaved.txt").write_text("unsaved\n")

    with pytest.raises(WorktreeRefused, match="uncommitted changes"):
        await trees.remove(tree, delete_branch_if_merged=True)

    assert (tree.path / "unsaved.txt").exists()


@pytest.mark.parametrize("same_task", [False, True])
async def test_a_worktree_deleted_behind_gits_back_is_recovered_by_prune_and_retry(tmp_path: Path, same_task: bool, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="daedalus.host.worktrees")
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    folder = _folder(repo)
    first = await trees.prepare(folder, "Anna", "1", "one")
    shutil.rmtree(first.path)

    tree = await trees.prepare(folder, "Anna", "1" if same_task else "2", "one" if same_task else "two")

    assert tree.path == first.path and tree.path.is_dir()
    assert _git(tree.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == tree.branch
    assert "prunable" not in _git(repo, "worktree", "list", "--porcelain")
    assert "pruning and retrying" in caplog.text


async def test_folders_that_cannot_have_a_worktree_are_refused_with_the_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    trees = StaffWorktrees("container")

    with pytest.raises(WorktreeRefused, match="read-only"):
        await trees.prepare(_folder(repo, readonly=True), "Anna", "1", "one")
    with pytest.raises(WorktreeRefused, match="not a git repository"):
        await trees.prepare(_folder(plain), "Anna", "1", "one")
    with pytest.raises(WorktreeRefused, match="no commit yet"):
        await trees.prepare(_folder(empty), "Anna", "1", "one")
    with pytest.raises(WorktreeUnavailable, match="host terminal bridge"):
        await trees.prepare(_folder(repo, env="host"), "Anna", "1", "one")
    assert not (repo / ".agents").exists()


async def test_a_folder_inside_a_repository_works_in_the_same_place_of_its_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "web").mkdir()
    (repo / "web" / "index.html").write_text("<p>hi</p>\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a web folder")
    trees = StaffWorktrees("container")

    tree = await trees.prepare(_folder(repo / "web"), "Anna", "1", "one")

    assert tree.path == repo / "web" / ".agents" / "worktrees" / "anna"
    assert tree.subdir == "web" and tree.cwd == tree.path / "web"
    assert (tree.cwd / "index.html").exists()
    assert "/web/.agents/" in (repo / ".git" / "info" / "exclude").read_text()
    assert _git(repo, "status", "--porcelain") == ""


async def test_an_exclude_note_from_before_gains_only_the_missing_lines(tmp_path: Path) -> None:
    exclude = tmp_path / "exclude"
    exclude.write_text("*.log\n" + EXCLUDE_MARKER + "\n/inbox/\n/.exec/")

    append_exclude(exclude, exclude_lines())
    once = exclude.read_text()
    append_exclude(exclude, exclude_lines())

    assert exclude.read_text() == once
    assert once.startswith("*.log\n")
    assert once.count(EXCLUDE_MARKER) == 1 and once.count("/inbox/") == 1
    assert all(line in once.splitlines() for line in exclude_lines())


def test_slugs_are_safe_ref_components() -> None:
    assert staff_slug("Анна") == "anna"
    assert staff_slug("Щука Жёлтая") == "shchuka-zheltaya"
    assert staff_slug("Zoë O'Brien") == "zoe-o-brien"
    assert staff_slug("???") == "staff"
    assert slug("..hidden..name.lock", limit=32) == "hidden.name"
    assert slug("a" * 40, limit=32) == "a" * 32
    title = branch_name("Anna", "T-7", "Переписать форму входа, чтобы она не падала на пустом пароле")
    assert title.startswith("agent/anna/t-7-perepisat-formu")
    assert len(title.rsplit("/", 1)[1]) <= len("t-7-") + 32
    assert branch_name("Anna", "7", "!!!") == "agent/anna/7"
    for name in (title, branch_name("x", "1", "a.lock"), branch_name("-", "..", "--")):
        subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"], check=True)


@dataclass
class _Result:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


class _FakeBridge:
    """The host terminal bridge's ``exec_run``, running the argv here: what the bridge would do on the host."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str]] = []

    async def exec_run(self, env: str, argv: list[str], *, cwd: str, env_vars: dict[str, str] | None = None, timeout: float, stdin: bytes | None = None) -> _Result:
        self.calls.append((env, argv, cwd))
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env={**os.environ, **(env_vars or {})}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return _Result(proc.returncode or 0, out, err)


async def test_host_folders_go_through_the_bridge_and_never_write_a_file_themselves(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bridge = _FakeBridge()
    trees = StaffWorktrees("container", host=bridge)
    folder = _folder(repo, env="host")

    # The bridge cannot write the exclude file, so a repository without the line is refused with it.
    with pytest.raises(WorktreeUnavailable, match=r"/\.agents/"):
        await trees.prepare(folder, "Anna", "1", "one")
    assert not (repo / ".agents").exists()

    (repo / ".git" / "info" / "exclude").write_text("/.agents/\n")
    tree = await trees.prepare(folder, "Anna", "1", "one")
    (tree.path / "feature.txt").write_text("feature\n")
    await trees.commit_wip(tree, "add the feature")
    await trees.merge(folder, tree.branch)
    assert await trees.remove(tree, delete_branch_if_merged=True) is True

    assert bridge.calls and all(env == "host" and argv[0] == "git" for env, argv, _ in bridge.calls)
    assert (repo / ".git" / "info" / "exclude").read_text() == "/.agents/\n"
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "feature.txt").exists()


async def test_a_failing_host_command_is_reported_with_its_exit_code(tmp_path: Path) -> None:
    class Refusing:
        async def exec_run(self, env: str, argv: list[str], **_: object) -> _Result:
            return _Result(128, b"", b"fatal: not a git repository")

    trees = StaffWorktrees("container", host=Refusing())

    with pytest.raises(WorktreeRefused, match="not a git repository"):
        await trees.prepare(_folder(tmp_path, env="host"), "Anna", "1", "one")


async def test_a_setup_command_that_hangs_is_killed_with_its_children_at_the_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trees = StaffWorktrees("container")
    tree = await trees.prepare(_folder(repo), "Anna", "1", "one")
    assert tree.created
    pid_file = tmp_path / "child.pid"

    result = await trees.run_setup(tree, f"echo installing; sleep 30 & echo $! > {pid_file}; wait", timeout=1.0)

    assert not result.ok and result.exit_code is None and "installing" in result.tail
    assert "timed out" in result.reason("npm ci")
    child = int(pid_file.read_text())
    # The shell's child is in the killed group too: an orphaned install would keep writing into the
    # worktree after the launch was refused.
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the setup command's child outlived the timeout")


async def test_a_host_setup_command_runs_through_the_bridge_in_the_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text("/.agents/\n")
    bridge = _FakeBridge()
    trees = StaffWorktrees("container", host=bridge)
    tree = await trees.prepare(_folder(repo, env="host"), "Anna", "1", "one")

    result = await trees.run_setup(tree, "echo ready; echo 'warn' >&2; exit 4")

    assert bridge.calls[-1] == ("host", ["bash", "-lc", "echo ready; echo 'warn' >&2; exit 4"], str(tree.cwd))
    assert not result.ok and result.exit_code == 4 and "ready" in result.tail and "warn" in result.tail
    again = await trees.prepare(_folder(repo, env="host"), "Anna", "1", "one")
    assert not again.created


async def test_a_sandboxed_commit_in_a_worktree_needs_only_the_paths_the_walls_open(request: pytest.FixtureRequest) -> None:
    """The opt-in sandbox proof: a commit inside bubblewrap, with only the writable walls of a staff
    session in its worktree bound, lands on the staff branch, and a write into the folder the worktree
    was made from does not. Skipped where unprivileged namespaces are not allowed."""

    if await asyncio.to_thread(shell.bwrap_status) != "ok":
        pytest.skip("bubblewrap cannot create namespaces here")
    # Not under /tmp: the sandbox mounts a private /tmp, which would hide the repository's own config
    # and leave git inside unable to read it. A project folder is never under /tmp.
    outside = Path(tempfile.mkdtemp(prefix="staff-worktree-", dir="/var/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(outside, ignore_errors=True))
    repo = _repo(outside)
    trees = StaffWorktrees("container")
    tree = await trees.prepare(_folder(repo), "Anna", "1", "one")
    before = _git(repo, "rev-parse", tree.branch).strip()
    project = Project(id="p-1", name="Atlas", created_at=datetime.now(UTC), folders=(_folder(repo),))
    walls = walls_for(project, folder_id=None, directory=None, local_env="container", isolation="worktree", worktree=tree.path)
    assert repo not in walls.writable and walls.writable[0] == tree.path
    writable = list(walls.writable)
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    command = "echo sandboxed > sandboxed.txt && git add -A && git commit -qm 'from the sandbox' && touch ../../../outside.txt"
    argv, sandboxed = await shell.sandbox_argv(command, config, writable=writable)
    assert sandboxed

    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(tree.path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()

    # The commit landed; the write outside the walls, into the operator's folder, did not.
    assert _git(repo, "log", "-1", "--format=%s", tree.branch).strip() == "from the sandbox", err.decode()
    assert _git(repo, "rev-parse", f"{tree.branch}~1").strip() == before
    assert proc.returncode != 0 and not (repo / "outside.txt").exists()
