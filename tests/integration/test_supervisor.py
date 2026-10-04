"""The supervisor against real git repositories: rebuild, failing preflight, rollback.

Slow (runs ``uv sync`` and the smoke tests in a scratch clone); excluded from the
smoke gate and run on demand: ``uv run pytest tests/integration -q``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT.parent / "protocore-exp"

pytestmark = pytest.mark.skipif(
    os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _load_supervisor(env: dict[str, str]):  # type: ignore[no-untyped-def]
    for key, value in env.items():
        os.environ[key] = value
    spec = importlib.util.spec_from_file_location("supervisor_under_test", REPO_ROOT / "launcher" / "supervisor.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def scratch(tmp_path: Path) -> dict[str, Path]:
    """A bare origin per repo plus a clone the supervisor owns."""
    layout: dict[str, Path] = {}
    for name, source, dirname in (("bot", REPO_ROOT, "daedalus"), ("core", CORE_ROOT, "protocore-exp")):
        bare = tmp_path / f"{name}.git"
        _git("clone", "--bare", "-q", str(source), str(bare), cwd=tmp_path)
        # A worktree may run this test from a branch behind main. The scratch
        # origin's main must start at the revision under test for later pushes.
        source_head = _git("rev-parse", "HEAD", cwd=source).strip()
        _git("--git-dir", str(bare), "update-ref", "refs/heads/main", source_head, cwd=tmp_path)
        _git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main", cwd=tmp_path)
        clone = tmp_path / dirname  # the bot resolves the core from its sibling directory
        _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
        _git("config", "user.email", "t@example.com", cwd=clone)
        _git("config", "user.name", "t", cwd=clone)
        layout[name] = clone
        layout[f"{name}_bare"] = bare
    (tmp_path / "state").mkdir()
    # The bot clone needs a virtualenv that resolves the core from its sibling path.
    subprocess.run(["uv", "sync", "--frozen", "--extra", "dev"], cwd=layout["bot"], check=True, capture_output=True)
    return layout


async def test_rebuild_applies_good_commit_and_rolls_back_bad_one(
    scratch: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _load_supervisor(
        {
            "DAEDALUS_BOT_REPO": str(scratch["bot"]),
            "DAEDALUS_CORE_REPO": str(scratch["core"]),
            "DAEDALUS_STATE": str(tmp_path / "state"),
            "DAEDALUS_WORKSPACES": str(tmp_path / "workspaces"),
            "DAEDALUS_SUPERVISOR_SOCKET": str(tmp_path / "state" / "sup.sock"),
            "DAEDALUS_BOT_CMD": "sleep 3600",
        }
    )
    supervisor = sup.Supervisor()
    # The git/preflight integration runs without an HTTP host. Drain protocol
    # failure cases have their own tests; this boundary models its commitment.
    class CommittedDrain:
        def __init__(self, state: Path) -> None:
            pass

        async def commit(self, *args: object, **kwargs: object) -> dict[str, str]:
            return {"commit_receipt_id": "scratch-receipt"}

    monkeypatch.setattr(sup.update_drain, "UpdateDrainClient", CommittedDrain)
    supervisor.child = SimpleNamespace(returncode=None)
    async def stop_scratch_child() -> None:
        supervisor.child = None

    monkeypatch.setattr(supervisor, "stop_child", stop_scratch_child)
    sup.record_good()
    base = sup.head(scratch["bot"])

    # A good change lands on origin/main.
    work = tmp_path / "work"
    _git("clone", "-q", str(scratch["bot_bare"]), str(work), cwd=tmp_path)
    (work / "docs" / "NOTE.md").write_text("good change\n")
    _git("add", "-A", cwd=work)
    _git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "good change", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    await supervisor._rebuild("good change")
    result = sup.LAST_REBUILD.read_text()
    assert "rebuilt:" in result, result
    assert supervisor.restart_requested.is_set()
    assert sup.head(scratch["bot"]) != base
    good_sha = sup.head(scratch["bot"])
    supervisor.restart_requested.clear()
    sup.record_good()

    # A broken change (syntax error) must be rolled back by preflight.
    (work / "daedalus" / "__init__.py").write_text("this is not python (\n")
    _git("add", "-A", cwd=work)
    _git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "broken", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    await supervisor._rebuild("broken change")
    result = sup.LAST_REBUILD.read_text()
    assert "preflight failed" in result, result
    assert sup.head(scratch["bot"]) == good_sha
    assert sup.FAILED.exists() and "compileall" in sup.FAILED.read_text()

    # Rollback returns to the earlier known-good revision.
    supervisor.child = SimpleNamespace(returncode=None)
    result = await supervisor.rollback(0)
    assert result.startswith("rolled back"), result
    assert sup.head(scratch["bot"]) == base
    await asyncio.sleep(0)


async def test_a_preflight_leaves_the_running_environment_exactly_as_it_was(scratch: dict[str, Path], tmp_path: Path) -> None:
    """The virtualenv the bot imports from is not the preflight's to write to.

    ``uv`` writes to whatever UV_PROJECT_ENVIRONMENT names, and the image names the running bot's —
    so a preflight used to reconcile the live environment with the candidate's lock, take out
    whatever the image had installed beside it, and leave editable pointers into a detached
    candidate checkout behind, whether the change passed or was refused.
    """
    live_venv = scratch["bot"] / ".venv"
    os.environ["UV_PROJECT_ENVIRONMENT"] = str(live_venv)
    sup = _load_supervisor(
        {
            "DAEDALUS_BOT_REPO": str(scratch["bot"]),
            "DAEDALUS_CORE_REPO": str(scratch["core"]),
            "DAEDALUS_STATE": str(tmp_path / "state"),
            "DAEDALUS_WORKSPACES": str(tmp_path / "workspaces"),
            "DAEDALUS_SUPERVISOR_SOCKET": str(tmp_path / "state" / "sup.sock"),
            "DAEDALUS_BOT_CMD": "sleep 3600",
        }
    )
    # Something the image installed beside the lock: an exact sync of this environment removes it.
    beside_the_lock = live_venv / "lib" / "python3.12" / "site-packages" / "_installed_by_the_image.pth"
    beside_the_lock.write_text("")

    before = _fingerprint(live_venv)
    ok, transcript = sup.preflight(scratch["bot"], sync=True)
    assert ok, transcript[-3000:]
    assert _fingerprint(live_venv) == before, "the preflight wrote to the virtualenv the bot runs from"
    assert beside_the_lock.exists(), "the preflight pruned a package the image installed"
    assert sup.venv_ready(sup.PREFLIGHT_VENV), "the preflight's own environment was not built"


def _fingerprint(venv: Path) -> list[tuple[str, int, str]]:
    """Every file in the virtualenv, by path, size and digest."""
    out = []
    for path in sorted(p for p in venv.rglob("*") if p.is_file()):
        raw = path.read_bytes()
        out.append((str(path.relative_to(venv)), len(raw), hashlib.sha256(raw).hexdigest()))
    return out
