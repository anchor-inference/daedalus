"""A rebuild asked for during another one is queued, not dropped."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(tmp_path: Path):  # type: ignore[no-untyped-def]
    for key in ("DAEDALUS_BOT_REPO", "DAEDALUS_CORE_REPO", "DAEDALUS_STATE", "DAEDALUS_WORKSPACES", "DAEDALUS_SUPERVISOR_SOCKET", "DAEDALUS_REBUILD_TRIGGER_DIR"):
        os.environ[key] = str(tmp_path / key.lower())
    spec = importlib.util.spec_from_file_location("supervisor_queue_under_test", REPO_ROOT / "launcher" / "supervisor.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_queue_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


async def test_a_rebuild_requested_during_one_runs_right_after(tmp_path: Path) -> None:
    sup = _load(tmp_path)
    supervisor = sup.Supervisor()
    ran: list[str] = []
    release = asyncio.Event()

    async def fake_rebuild(reason: str) -> None:
        async with supervisor.lock:
            ran.append(reason)
            await release.wait()
        supervisor._run_queued_rebuild()

    supervisor._rebuild = fake_rebuild  # type: ignore[method-assign]
    first = await supervisor.rebuild("first merge")
    await asyncio.sleep(0)
    assert first.startswith("rebuild started") and ran == ["first merge"]
    second = await supervisor.rebuild("second merge")
    third = await supervisor.rebuild("third merge")  # one slot: the latest reason wins, the target is origin/main anyway
    assert "queued" in second and "queued" in third
    assert supervisor.queued_rebuild == "third merge"
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert ran == ["first merge", "third merge"]
    assert supervisor.queued_rebuild is None


async def test_a_rollback_drops_what_was_queued_behind_it(tmp_path: Path) -> None:
    sup = _load(tmp_path)
    supervisor = sup.Supervisor()
    ran: list[str] = []

    async def fake_rollback(steps_back: int) -> str:
        async with supervisor.lock:
            queued = await supervisor.rebuild("merge during rollback")
            assert "queued" in queued
            return "rolled back"

    async def fake_rebuild(reason: str) -> None:
        ran.append(reason)

    supervisor._rollback = fake_rollback  # type: ignore[method-assign]
    supervisor._rebuild = fake_rebuild  # type: ignore[method-assign]
    assert await supervisor.rollback(0) == "rolled back"
    await asyncio.sleep(0)
    assert ran == [] and supervisor.queued_rebuild is None


def test_a_checkout_without_a_built_app_gets_one_before_the_first_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundle is not in git: a fresh clone must not serve 404 at /app until the first rebuild."""
    sup = _load(tmp_path)
    repo = tmp_path / "bot"
    (repo / "miniapp").mkdir(parents=True)
    (repo / "miniapp" / "package.json").write_text("{}")
    ran: list[list[str]] = []

    def fake_run(step: list[str], cwd: Path, timeout: int = 0) -> tuple[int, str]:
        ran.append(step)
        if step[:2] == ["/usr/bin/npm", "run"]:
            (cwd / "dist").mkdir(exist_ok=True)
            (cwd / "dist" / "index.html").write_text("<!doctype html>")
        return 0, ""

    sup.run = fake_run
    # Through monkeypatch: ``sup.shutil`` is the one ``shutil`` module of the whole test run, and a
    # bare assignment left every later test with a ``which`` that takes no ``path`` (the staff tests
    # that spawn real programs then failed, depending only on the order the files ran in).
    monkeypatch.setattr(sup.shutil, "which", lambda name, *args, **kwargs: "/usr/bin/npm")
    sup.restore_owner = lambda repo: None
    monkeypatch.setattr(sup, "BAKED_APP", tmp_path / "no-copy-here")
    sup.build_app_if_missing(repo)
    # npm by the path `which` found: on Windows that is npm.cmd, which a bare "npm" never reaches.
    assert [s[:2] for s in ran] == [["/usr/bin/npm", "ci"], ["/usr/bin/npm", "run"]]
    sup.build_app_if_missing(repo)  # already built: nothing runs
    assert len(ran) == 2


def test_the_app_that_came_with_the_installation_is_used_before_building_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The release's own build is the same app in a second; npm ci on a first run is minutes."""
    sup = _load(tmp_path)
    repo = tmp_path / "bot"
    (repo / "miniapp").mkdir(parents=True)
    (repo / "miniapp" / "package.json").write_text("{}")
    baked = tmp_path / "miniapp-dist"
    baked.mkdir()
    (baked / "index.html").write_text("<!doctype html>")
    ran: list[list[str]] = []
    sup.run = lambda step, cwd, timeout=0: (ran.append(step), (0, ""))[1]
    monkeypatch.setattr(sup.shutil, "which", lambda name, *args, **kwargs: "/usr/bin/npm")
    monkeypatch.setattr(sup, "BAKED_APP", baked)
    sup.restore_owner = lambda repo: None
    sup.build_app_if_missing(repo)
    assert ran == []
    assert (repo / "miniapp" / "dist" / "index.html").read_text() == "<!doctype html>"


def test_an_npm_that_cannot_be_started_does_not_stop_the_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What Windows did with a bare "npm": FileNotFoundError, every start, and no app ever served."""
    sup = _load(tmp_path)
    repo = tmp_path / "bot"
    (repo / "miniapp").mkdir(parents=True)
    (repo / "miniapp" / "package.json").write_text("{}")

    def missing(step: list[str], cwd: Path, timeout: int = 0) -> tuple[int, str]:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    sup.run = missing
    monkeypatch.setattr(sup.shutil, "which", lambda name, *args, **kwargs: "C:/node/npm.cmd")
    monkeypatch.setattr(sup, "BAKED_APP", tmp_path / "no-copy-here")
    sup.restore_owner = lambda repo: None
    sup.build_app_if_missing(repo)  # logged, not raised
