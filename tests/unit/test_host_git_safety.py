"""Git the host runs in a repository the agent can write never runs the repository's programs.

A session workspace, a staff worktree and the checkpoint store are all places the agent writes, and
a repository's configuration can name a program git runs on its behalf: a hook on commit, an
``fsmonitor`` on status and add. These plant one of each and check that the host's snapshot,
restore and Verify leave them alone, and that whatever git does start sees none of the host's keys.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from daedalus.host.checkpoints import Checkpoints
from daedalus.host.gitrun import child_environment, run_git
from daedalus.tools.verify import _content_digests, _tree_state

SECRETS = {
    "TELEGRAM_BOT_TOKEN": "telegram-secret",
    "KEYPROXY_SECRET": "keyproxy-secret",
    "ANTHROPIC_API_KEY": "provider-secret",
    "DAEDALUS_SUPERVISOR_TOKEN": "supervisor-secret",
}


def _plant(git_dir: Path, marker: Path) -> None:
    """A pre-commit hook and an fsmonitor in ``git_dir``, each of which leaves ``marker`` behind."""
    script = git_dir / "planted.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    hooks = git_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "post-commit", "post-checkout"):
        (hooks / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n", encoding="utf-8")
        (hooks / name).chmod(0o755)
    subprocess.run(["git", "--git-dir", str(git_dir), "config", "core.fsmonitor", str(script)], check=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "core.hooksPath=/dev/null", "commit", "-qm", "first"], check=True)
    return path


async def test_snapshot_and_restore_never_run_a_planted_hook_or_fsmonitor(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("one")
    checkpoints = Checkpoints(workspace)
    first = await checkpoints.snapshot("first")
    marker = tmp_path / "ran"
    _plant(checkpoints.git_dir, marker)
    (workspace / "a.txt").write_text("two")
    await checkpoints.snapshot("second")
    await checkpoints.restore(first)
    assert (workspace / "a.txt").read_text() == "one"
    assert not marker.exists(), marker.read_text()


async def test_verify_reads_the_tree_without_running_the_repository_s_programs(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    marker = tmp_path / "ran"
    _plant(repo / ".git", marker)
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    state = await _tree_state(repo)
    digests = await _content_digests(repo)
    assert state.endswith("+worktree")
    assert {"a.txt", "new.txt"} <= set(digests)
    assert not marker.exists(), marker.read_text()


async def test_host_git_runs_never_run_a_planted_hook(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    marker = tmp_path / "ran"
    _plant(repo / ".git", marker)
    (repo / "a.txt").write_text("changed\n")
    await run_git(["status", "--porcelain"], cwd=repo)
    await run_git(["commit", "-qam", "second"], cwd=repo, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    assert not marker.exists(), marker.read_text()


def test_the_child_environment_carries_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GIT_TRACE_SETUP", "0")
    env = child_environment({"GH_TOKEN": "asked-for"})
    assert not set(SECRETS) & set(env)
    assert env["PATH"] and env["GIT_TRACE_SETUP"] == "0" and env["GH_TOKEN"] == "asked-for"


async def test_a_program_git_does_start_sees_none_of_the_host_s_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean filter is the one program a repository can still name (by a name of its own choosing),
    so it is the witness: it runs during the snapshot and writes down the environment it was given."""
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    checkpoints = Checkpoints(workspace)
    await checkpoints.snapshot("first")
    seen = tmp_path / "env"
    subprocess.run(["git", "--git-dir", str(checkpoints.git_dir), "config", "filter.witness.clean", f"env > {seen}; cat"], check=True)
    (workspace / ".gitattributes").write_text("*.txt filter=witness\n")
    (workspace / "b.txt").write_text("two")
    await checkpoints.snapshot("second")
    text = await asyncio.to_thread(seen.read_text)
    assert "PATH=" in text
    for value in SECRETS.values():
        assert value not in text
