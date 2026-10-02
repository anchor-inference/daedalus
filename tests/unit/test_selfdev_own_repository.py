"""Self-development works in a repository of the agent's own, and the operator's checkout stays the operator's.

Worktrees were once cut from the operator's checkout, so their metadata lived in its ``.git`` and the
session that opened one was handed parts of that ``.git`` as writable. A session rewrote a worktree's
``commondir`` there to a directory of its own; on the machine that path did not exist and every
``git fetch`` in the checkout failed. These tests hold the separation from both sides: the agent's
whole cycle — worktree, commit, push — runs with the checkout's ``.git`` read-only, an agent's own
attempt to cut a worktree in the checkout fails in the sandbox and is refused by the policy, and the
worktrees made the old way are moved over with their work intact.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.selfdev import SelfDevelopment
from daedalus.host.containment import worktree_writable_paths
from daedalus.host.policy import ALLOW, DENY, Policy
from daedalus.stores.database import Database
from daedalus.tools import shell


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _checkout(root: Path) -> tuple[Path, Path]:
    """The operator's checkout with an origin it pushed main to, as a server installation has it."""
    repo = root / "daedalus"
    (repo / "daedalus").mkdir(parents=True)
    (repo / "daedalus" / "app.py").write_text("HELLO = 'hello'\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@localhost")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the first commit")
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo, origin


def _selfdev(root: Path, repo: Path, db: Database, mode: str = "server") -> SelfDevelopment:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, state_dir=root / "state", workspaces_dir=root / "workspaces", bot_repo_dir=repo, core_repo_dir=repo,
        telegram_bot_token="", owner_user_id=0, api_port=0,
    )
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=None, front=None, extensions={}, notifications=None, guard=None)
    return SelfDevelopment(app, mode)  # type: ignore[arg-type]


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(directory)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(directory.rglob("*")) if p.is_file()}


def _read_only(directory: Path) -> None:
    for path in [directory, *directory.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _writable(directory: Path) -> None:
    for path in [directory, *directory.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through permission bits, so a read-only directory proves nothing")
async def test_the_whole_cycle_runs_with_the_checkouts_git_read_only(tmp_path: Path, db: Database) -> None:
    repo, origin = _checkout(tmp_path)
    selfdev = _selfdev(tmp_path, repo, db)
    before = _snapshot(repo / ".git")
    _read_only(repo / ".git")
    try:
        # What a session did by hand: a worktree cut in the checkout. With its .git read-only, git cannot.
        refused = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "agent/mine", str(tmp_path / "mine")], capture_output=True, text=True)
        assert refused.returncode != 0

        worktree = await selfdev.workspace("bot", "greeting")
        spec = selfdev.repo("bot")
        assert worktree == spec.worktrees / "greeting"
        assert spec.repository in Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip()).parents
        (worktree / "daedalus" / "app.py").write_text("HELLO = 'hello there'\n", encoding="utf-8")
        _git(worktree, "commit", "-qam", "A warmer greeting")
        # The push a proposal makes, from the worktree, to the checkout's own remote.
        await selfdev.git(spec, "push", "-u", "origin", "agent/greeting", "--force-with-lease", cwd=worktree)
        assert _git(origin, "rev-parse", "agent/greeting").strip() == _git(worktree, "rev-parse", "HEAD").strip()
        # A second worktree reuses the repository and sees the remote's new branch.
        await selfdev.workspace("bot", "another")
        assert _git(spec.repository, "rev-parse", "origin/agent/greeting").strip() == _git(worktree, "rev-parse", "HEAD").strip()
    finally:
        _writable(repo / ".git")
    assert _snapshot(repo / ".git") == before


async def test_a_session_is_given_nothing_of_the_checkout_to_write(tmp_path: Path, db: Database) -> None:
    repo, _origin = _checkout(tmp_path)
    selfdev = _selfdev(tmp_path, repo, db)
    worktree = await selfdev.workspace("bot", "greeting")
    paths = worktree_writable_paths(worktree)
    assert worktree in paths
    assert not [p for p in paths if p == repo or repo in p.parents]
    # What a commit there writes is in the agent's own repository.
    assert selfdev.repo("bot").repository / "objects" in paths


async def test_local_mode_lands_the_commit_on_the_checkouts_branch_and_nothing_else(tmp_path: Path, db: Database) -> None:
    repo, _origin = _checkout(tmp_path)
    _git(repo, "remote", "remove", "origin")
    selfdev = _selfdev(tmp_path, repo, db, mode="local")
    spec = selfdev.repo("bot")
    worktree = await selfdev.workspace("bot", "greeting")
    (worktree / "daedalus" / "app.py").write_text("HELLO = 'hello there'\n", encoding="utf-8")
    _git(worktree, "commit", "-qam", "A warmer greeting")
    # The operator committed on the running branch meanwhile: the agent's branch is rebased onto it.
    (repo / "NOTES").write_text("mine\n", encoding="utf-8")
    _git(repo, "add", "NOTES")
    _git(repo, "commit", "-qm", "the operator's own commit")
    await selfdev._fast_forward(spec, "agent/greeting", await selfdev.base_ref(spec))
    assert _git(repo, "show", "HEAD:daedalus/app.py") == "HELLO = 'hello there'\n"
    assert _git(repo, "log", "--format=%s", "-2").split("\n")[:2] == ["A warmer greeting", "the operator's own commit"]
    assert not (repo / ".git" / "worktrees").exists() and not _git(repo, "branch", "--list", "agent/*").strip()


async def test_worktrees_cut_from_the_checkout_are_adopted_with_their_work(tmp_path: Path, db: Database) -> None:
    repo, _origin = _checkout(tmp_path)
    selfdev = _selfdev(tmp_path, repo, db)
    spec = selfdev.repo("bot")
    spec.worktrees.mkdir(parents=True)
    # The old shape: committed work on agent/old, and an edit never committed.
    old = spec.worktrees / "old"
    _git(repo, "worktree", "add", "-q", "-b", "agent/old", str(old), "main")
    (old / "daedalus" / "app.py").write_text("HELLO = 'committed'\n", encoding="utf-8")
    _git(old, "commit", "-qam", "committed work")
    committed = _git(old, "rev-parse", "HEAD").strip()
    (old / "daedalus" / "app.py").write_text("HELLO = 'not yet committed'\n", encoding="utf-8")
    # And one whose entry in the checkout is already gone, as the host's cleanup left them.
    orphan = spec.worktrees / "orphan"
    _git(repo, "worktree", "add", "-q", "-b", "agent/orphan", str(orphan), "main")
    (orphan / "NEW.md").write_text("an untracked file\n", encoding="utf-8")
    subprocess.run(["rm", "-rf", str(repo / ".git" / "worktrees" / "orphan")], check=True)

    assert sorted(await selfdev.adopt_worktrees()) == ["bot/old", "bot/orphan"]
    for worktree in (old, orphan):
        assert spec.repository in Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip()).parents
    assert _git(old, "rev-parse", "--abbrev-ref", "HEAD").strip() == "agent/old"
    assert _git(old, "rev-parse", "HEAD").strip() == committed
    assert (old / "daedalus" / "app.py").read_text(encoding="utf-8") == "HELLO = 'not yet committed'\n"
    assert _git(old, "status", "--porcelain").strip() == "M daedalus/app.py"
    assert _git(orphan, "rev-parse", "--abbrev-ref", "HEAD").strip() == "agent/orphan"
    assert _git(orphan, "status", "--porcelain").strip() == "?? NEW.md"
    # The work goes on from there, and a second start finds nothing left to adopt.
    _git(old, "commit", "-qam", "the rest")
    assert await selfdev.adopt_worktrees() == []
    assert spec.repository / "objects" in worktree_writable_paths(old)


def _sandboxed(command: str, *, writable: list[Path], sealed: list[Path]) -> subprocess.CompletedProcess[str]:
    argv, sandboxed = asyncio.run(shell.sandbox_argv(command, SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[]), writable=writable, sealed=sealed))
    assert sandboxed
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shell.bwrap_status() != "ok", reason="bubblewrap cannot create namespaces here")
def test_the_sandbox_binds_the_checkouts_git_read_only_after_every_writable_path(tmp_path: Path) -> None:
    repo = tmp_path / "daedalus"
    (repo / ".git").mkdir(parents=True)
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    argv, _ = asyncio.run(shell.sandbox_argv("true", config, writable=[repo, repo / ".git"], sealed=[repo / ".git"]))
    sealed = max(i for i, word in enumerate(argv) if word == "--ro-bind")
    assert argv[sealed + 1 : sealed + 3] == [str(repo / ".git")] * 2
    # bubblewrap applies binds in order: the read-only one has to come after every writable one.
    assert all(i < sealed for i, word in enumerate(argv) if word == "--bind")


def test_operator_git_dirs_follow_a_checkout_that_is_itself_a_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    _git(tmp_path, "init", "-q", "-b", "main", str(main))
    _git(main, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "--allow-empty", "-m", "x")
    _git(main, "worktree", "add", "-q", "--detach", str(tmp_path / "linked"))
    found = shell.operator_git_dirs([main, tmp_path / "linked", tmp_path / "missing"])
    assert found == [main / ".git", main / ".git" / "worktrees" / "linked"]


@pytest.mark.skipif(shell.bwrap_status() != "ok", reason="bubblewrap cannot create namespaces here")
async def test_in_the_sandbox_a_worktree_cannot_be_cut_in_the_checkout_and_self_development_still_commits(tmp_path: Path, db: Database) -> None:
    repo, _origin = _checkout(tmp_path)
    selfdev = _selfdev(tmp_path, repo, db)
    worktree = await selfdev.workspace("bot", "greeting")
    # The control: without the seal, a session whose walls include the checkout does write its .git.
    probe = await asyncio.to_thread(_sandboxed, f"touch {repo}/.git/probe", writable=[repo], sealed=[])
    assert probe.returncode == 0 and (repo / ".git" / "probe").is_file()
    (repo / ".git" / "probe").unlink()
    before = _snapshot(repo / ".git")
    # Even a session whose walls include the whole checkout cannot write its .git.
    refused = await asyncio.to_thread(_sandboxed, f"git -C {repo} worktree add -b agent/mine {tmp_path}/workspaces/mine", writable=[repo, *worktree_writable_paths(worktree)], sealed=[repo / ".git"])
    assert refused.returncode != 0, refused.stdout + refused.stderr
    redirected = await asyncio.to_thread(_sandboxed, f"echo /elsewhere > {repo}/.git/commondir", writable=[repo], sealed=[repo / ".git"])
    assert redirected.returncode != 0
    assert _snapshot(repo / ".git") == before
    (worktree / "daedalus" / "app.py").write_text("HELLO = 'hello there'\n", encoding="utf-8")
    committed = await asyncio.to_thread(
        _sandboxed,
        f"git -C {worktree} -c user.name=daedalus -c user.email=daedalus@localhost commit -qam 'A warmer greeting'",
        writable=worktree_writable_paths(worktree),
        sealed=[repo / ".git"],
    )
    assert committed.returncode == 0, committed.stderr
    assert _git(worktree, "log", "-1", "--format=%s").strip() == "A warmer greeting"


def test_the_policy_refuses_git_that_would_change_the_operators_checkout(tmp_path: Path) -> None:
    checkout = "/srv/daedalus"
    linked = tmp_path / "old-worktree"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {checkout}/.git/worktrees/old-worktree\n", encoding="utf-8")
    policy = Policy(operator_checkouts=[Path(checkout)], workspace_roots=[Path("/srv/workspaces")], base_dir="/srv/workspaces/s1")
    refused = [
        f"git -C {checkout} worktree add /srv/worktrees/bot/x",
        f"cd {checkout} && git worktree add ../x",
        f"git --git-dir={checkout}/.git branch agent/x",
        f"git --git-dir {checkout}/.git update-ref refs/heads/x HEAD",
        f"GIT_DIR={checkout}/.git git fetch",
        f"git -C {checkout} config core.bare false",
        f"git -C {checkout} stash",
        f"git -C {checkout}/daedalus commit -am x",
        f"git -C {linked} commit -am x",
        f"echo /elsewhere > {checkout}/.git/worktrees/x/commondir",
    ]
    for command in refused:
        assert policy.evaluate("Exec", {"command": command}).action == DENY, command
    assert policy.evaluate("Exec", {"command": f"git -C {checkout} worktree add x"}).rule == "git.operator_repo"
    # Pushing keeps its own refusal and its own reason.
    assert policy.evaluate("Exec", {"command": f"git -C {checkout} push origin x"}).rule == "git.operator_push"
    allowed = [
        f"git -C {checkout} log --oneline -3",
        f"git -C {checkout} status",
        f"git -C {checkout} diff HEAD~1",
        f"git -C {checkout} branch",
        f"git -C {checkout} branch --list 'agent/*'",
        f"git -C {checkout} worktree list",
        f"git -C {checkout} remote -v",
        f"git -C {checkout} config --get remote.origin.url",
        f"git clone {checkout} /srv/workspaces/s1/copy",
        "git -C /srv/worktrees/bot/x commit -am x",
        "git -C /srv/worktrees/bot/x worktree add ../y",
    ]
    for command in allowed:
        assert policy.evaluate("Exec", {"command": command}).action == ALLOW, command
