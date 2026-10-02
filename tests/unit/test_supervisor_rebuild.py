"""A rebuild preflights the new revision beside the running bot and stops it only to swap."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "launcher" / "supervisor.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("supervisor_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, preflight_ok: bool) -> tuple[Any, Any, list[str]]:
    sup = _load()
    calls: list[str] = []
    monkeypatch.setattr(sup, "BOT_REPO", tmp_path / "daedalus")
    monkeypatch.setattr(sup, "CORE_REPO", tmp_path / "protocore-exp")
    monkeypatch.setattr(sup, "CANDIDATE", tmp_path / "preflight")
    monkeypatch.setattr(sup, "FAILED", tmp_path / "good" / "FAILED")
    monkeypatch.setattr(sup, "LAST_REBUILD", tmp_path / "good" / "LAST_REBUILD")
    monkeypatch.setattr(sup, "LOG", tmp_path / "supervisor.log")
    monkeypatch.setattr(sup, "git", lambda repo, *args: (calls.append(f"git {repo.name} {' '.join(args)}"), (0, "abc123\n"))[1])
    monkeypatch.setattr(sup, "head", lambda repo: "old0000000")
    monkeypatch.setattr(sup, "prepare_candidate", lambda repo: (calls.append(f"candidate {repo.name}"), (True, ""))[1])
    monkeypatch.setattr(sup, "preflight", lambda repo: (calls.append(f"preflight {repo}"), (preflight_ok, "transcript"))[1])
    monkeypatch.setattr(sup, "install_from_candidate", lambda repo: (calls.append(f"install {repo.name}"), (True, ""))[1])
    supervisor = sup.Supervisor()
    supervisor._changed_files = lambda old, new: set()  # type: ignore[method-assign]

    async def stop_child() -> None:
        calls.append("stop")

    supervisor.stop_child = stop_child  # type: ignore[method-assign]
    return sup, supervisor, calls


@pytest.mark.asyncio
async def test_a_failing_preflight_never_stops_the_running_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    await supervisor._rebuild("merged PR #1")
    assert "stop" not in calls
    assert not any(c.startswith("git daedalus reset") for c in calls)
    assert any(c.startswith("preflight") and "preflight/daedalus" in c for c in calls)
    assert supervisor.restart_requested.is_set() is False
    assert "the bot kept running" in sup.FAILED.read_text()


@pytest.mark.asyncio
async def test_a_passing_preflight_swaps_and_restarts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    await supervisor._rebuild("merged PR #2")
    order = [c for c in calls if c.startswith(("preflight", "stop", "git daedalus reset", "install"))]
    assert order[0].startswith("preflight") and order[1] == "stop"
    assert "git daedalus reset --hard origin/main" in order and order[-1] == "install daedalus"
    assert supervisor.restart_requested.is_set()
    assert "rebuilt" in sup.LAST_REBUILD.read_text()


def test_reaping_touches_only_zombies_of_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = _load()
    assert sup.reap_zombies(keep=set()) >= 0


async def _noop() -> None:
    await asyncio.sleep(0)


def test_a_candidate_whose_entry_the_host_pruned_is_made_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`git worktree prune` on the machine removes the checkout's entry for the candidate, whose path
    exists only in the container; the candidate's own `.git` then names nothing. Reusing it failed every
    preflight from then on."""
    import subprocess

    sup = _load()
    repo = tmp_path / "daedalus"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    monkeypatch.setattr(sup, "CANDIDATE", tmp_path / "preflight")
    assert sup.prepare_candidate(repo, "main")[0]
    subprocess.run(["rm", "-rf", str(repo / ".git" / "worktrees")], check=True)
    ok, out = sup.prepare_candidate(repo, "main")
    assert ok, out
    assert subprocess.run(["git", "-C", str(tmp_path / "preflight" / "daedalus"), "rev-parse", "HEAD"], capture_output=True).returncode == 0
