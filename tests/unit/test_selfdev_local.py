"""Applying a change to the checkout the app runs from.

A local installation has no remote and no reviewer: the change goes from a worktree onto the
checkout's own branch, and the only thing between it and running is a restart. The tests here work
against a real git checkout in a temporary directory — the behaviour under test is git's as much as
ours, and a fake repository would pass while the real one refused.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.selfdev import GitError, SelfDevelopment
from daedalus.host import capabilities
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _checkout(root: Path) -> Path:
    """A small git repository shaped like the host: a package, a test directory, one commit on main."""
    repo = root / "daedalus"
    (repo / "daedalus" / "host").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "daedalus" / "__init__.py").write_text("")
    (repo / "daedalus" / "host" / "__init__.py").write_text("")
    (repo / "tests" / "unit" / "__init__.py").write_text("")  # git carries files, not directories
    (repo / "daedalus" / "host" / "greeting.py").write_text("def greet() -> str:\n    return 'hello'\n")
    (repo / "daedalus" / "app.py").write_text("from daedalus.host import greeting\n\nHELLO = greeting.greet\n")
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@localhost")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the first commit")
    return repo


def _settings(tmp_path: Path, repo: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        state_dir=tmp_path / "state",
        workspaces_dir=tmp_path / "workspaces",
        bot_repo_dir=repo,
        core_repo_dir=repo,
        telegram_bot_token="",
        owner_user_id=0,
        api_port=0,
    )


def _app(settings: Settings, db: Database) -> SimpleNamespace:
    return SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=None, front=None, extensions={}, notifications=RecordingNotifications(), guard=None)


async def _receipt(db: Database, session_id: str, command: str, *, tests_run: int = 4) -> None:
    """A passing Verify receipt for a command, recorded now — what the evidence gate reads."""
    await db.execute(
        "INSERT INTO verifications(session_id, criterion, command, exit_code, passed, output_digest, at, sandboxed, tests_run)"
        " VALUES (?, ?, ?, 0, 1, 'digest', ?, 1, ?)",
        (session_id, "the change works", command, datetime.now(UTC).isoformat(), tests_run),
    )


async def _worktree_with_a_change(selfdev: SelfDevelopment, repo: Path) -> Path:
    """The agent's worktree, one commit ahead, changing a host module and its test."""
    worktree = await selfdev.workspace("bot", "greeting")
    (worktree / "daedalus" / "host" / "greeting.py").write_text("def greet() -> str:\n    return 'hello there'\n")
    (worktree / "tests" / "unit" / "test_greeting.py").write_text("from daedalus.host import greeting\n\n\ndef test_greet():\n    assert greeting.greet()\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "A warmer greeting\n\nCo-authored-by: Daedalus <daedalus@localhost>")
    return worktree


@pytest.fixture
def local(tmp_path: Path, db: Database) -> tuple[SelfDevelopment, Path]:
    repo = _checkout(tmp_path)
    return SelfDevelopment(_app(_settings(tmp_path, repo), db), "local"), repo  # type: ignore[arg-type]


# -- the worktree -------------------------------------------------------------------------


async def test_the_worktree_is_cut_from_the_running_branch_not_a_remote(local: tuple[SelfDevelopment, Path]) -> None:
    """There is no origin to fetch here; asking for one is the failure this mode exists to avoid."""
    selfdev, repo = local
    worktree = await selfdev.workspace("bot", "greeting")
    assert (worktree / "daedalus" / "app.py").is_file()
    # The running branch as the agent's own repository last fetched it from the checkout.
    assert await selfdev.base_ref(selfdev.repo("bot")) == "checkout/main"
    assert _git(worktree, "rev-parse", "HEAD").strip() == _git(repo, "rev-parse", "main").strip()
    # And the worktree belongs to that repository: the checkout's .git did not gain an entry for it.
    assert not (repo / ".git" / "worktrees").exists()
    assert selfdev.repo("bot").repository in Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip()).parents


# -- the gates ----------------------------------------------------------------------------


async def test_a_change_with_no_receipt_is_refused_and_the_checkout_does_not_move(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    selfdev, repo = local
    before = _git(repo, "rev-parse", "HEAD").strip()
    await _worktree_with_a_change(selfdev, repo)
    with pytest.raises(GitError, match="no passing Verify receipt"):
        await selfdev.apply(repo="bot", summary="A warmer greeting", session_id="s1", execution_path="daedalus.app")
    assert _git(repo, "rev-parse", "HEAD").strip() == before
    assert selfdev.pending_change() is None


async def test_a_change_that_names_no_execution_path_is_refused(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    selfdev, repo = local
    await _worktree_with_a_change(selfdev, repo)
    await _receipt(db, "s1", "pytest tests/unit/test_greeting.py")
    with pytest.raises(GitError, match="names no execution_path"):
        await selfdev.apply(repo="bot", summary="A warmer greeting", session_id="s1")


async def test_an_uncommitted_worktree_is_refused(local: tuple[SelfDevelopment, Path]) -> None:
    selfdev, repo = local
    worktree = await selfdev.workspace("bot", "greeting")
    (worktree / "daedalus" / "host" / "greeting.py").write_text("broken\n")
    with pytest.raises(GitError, match="uncommitted changes"):
        await selfdev.apply(repo="bot", summary="x", session_id="s1", execution_path="daedalus.app")


# -- what a good change does --------------------------------------------------------------


async def test_a_checked_change_lands_on_the_branch_and_asks_for_a_restart(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    selfdev, repo = local
    worktree = await _worktree_with_a_change(selfdev, repo)
    await _receipt(db, "s1", "pytest tests/unit/test_greeting.py daedalus/host/greeting.py")
    answer = await selfdev.apply(repo="bot", summary="A warmer greeting for the operator", session_id="s1", execution_path="daedalus.app")

    commit = _git(worktree, "rev-parse", "HEAD").strip()
    assert _git(repo, "rev-parse", "main").strip() == commit, "the checkout's branch was not moved onto the change"
    assert "restart" in answer and commit[:10] in answer
    # The history stays one line, and the agent's own trailer survives: nothing here is published.
    assert len(_git(repo, "log", "--format=%H", "main").split()) == 2
    assert "Co-authored-by: Daedalus" in _git(repo, "log", "-1", "--format=%B", "main")

    pending = selfdev.pending_change()
    assert pending is not None
    assert pending["commit"] == commit and pending["summary"] == "A warmer greeting for the operator"
    assert pending["needs_image"] is False and pending["session_id"] == "s1"
    # The worktree and the branch stay: the agent's local git is its own record of the work.
    assert worktree.is_dir() and "agent/greeting" in _git(selfdev.repo("bot").repository, "branch", "--list", "agent/greeting")
    # The record is the agent's repository; the checkout received the commits and nothing else.
    assert not _git(repo, "branch", "--list", "agent/*").strip()


async def test_a_change_to_the_image_says_a_restart_cannot_deliver_it(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    selfdev, repo = local
    worktree = await selfdev.workspace("bot", "packages")
    (worktree / "deploy").mkdir()
    (worktree / "deploy" / "apt-packages.txt").write_text("ripgrep\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "One more package")
    await _receipt(db, "s1", "pytest tests/unit")
    answer = await selfdev.apply(repo="bot", summary="One more package", session_id="s1")
    assert "update" in answer and "image" in answer
    assert selfdev.pending_change()["needs_image"] is True  # type: ignore[index]


# -- what the restart leaves behind -------------------------------------------------------


async def test_reconcile_calls_the_change_applied_when_the_checkout_is_at_it(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    selfdev, repo = local
    worktree = await _worktree_with_a_change(selfdev, repo)
    await _receipt(db, "s1", "pytest tests/unit/test_greeting.py daedalus/host/greeting.py")
    await selfdev.apply(repo="bot", summary="A warmer greeting", session_id="s1", execution_path="daedalus.app")
    await selfdev.reconcile()
    assert selfdev.pending_change() is None
    result = selfdev.last_change()
    assert result is not None and result["status"] == "applied"
    assert result["commit"] == _git(worktree, "rev-parse", "HEAD").strip()


async def test_reconcile_repeats_the_supervisors_reason_when_the_change_was_refused(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    """The commit is not in the checkout and the supervisor left a note about it: that note is the answer."""
    selfdev, repo = local
    worktree = await _worktree_with_a_change(selfdev, repo)
    await _receipt(db, "s1", "pytest tests/unit/test_greeting.py daedalus/host/greeting.py")
    await selfdev.apply(repo="bot", summary="A warmer greeting", session_id="s1", execution_path="daedalus.app")
    commit = _git(worktree, "rev-parse", "HEAD").strip()
    _git(repo, "reset", "--hard", "HEAD~1")  # what the supervisor does when the preflight says no
    (selfdev.selfdev_dir / "result.json").write_text(json.dumps({"status": "preflight_failed", "commit": commit, "detail": "the checks did not pass"}))

    await selfdev.reconcile()
    result = selfdev.last_change()
    assert result is not None and result["status"] == "preflight_failed"
    assert result["detail"] == "the checks did not pass" and result["summary"] == "A warmer greeting"
    assert selfdev.pending_change() is None


async def test_a_change_still_waiting_is_left_alone(local: tuple[SelfDevelopment, Path], db: Database) -> None:
    """A restart that has not happened yet must not be reported as anything at all."""
    selfdev, repo = local
    await _worktree_with_a_change(selfdev, repo)
    await _receipt(db, "s1", "pytest tests/unit/test_greeting.py daedalus/host/greeting.py")
    await selfdev.apply(repo="bot", summary="A warmer greeting", session_id="s1", execution_path="daedalus.app")
    _git(repo, "reset", "--hard", "HEAD~1")  # the process restarted before the supervisor got to it
    await selfdev.reconcile()
    assert selfdev.pending_change() is not None and selfdev.last_change() is None


# -- the surface the app reads ------------------------------------------------------------


async def test_the_capabilities_carry_the_restart_banner_and_the_restart_route(tmp_path: Path, db: Database) -> None:
    repo = _checkout(tmp_path)
    settings = _settings(tmp_path, repo)
    config = RuntimeConfig()
    config.self_change.mode = "local"  # type: ignore[assignment]
    app = _app(settings, db)
    app.config = config
    selfdev = SelfDevelopment(app, "local")  # type: ignore[arg-type]
    app.extensions["selfdev"] = selfdev
    # busy_sessions: a restart refuses while anything is running or still writing its last turn
    # down, so the stand-in manager has to be able to answer that too. Nothing is running here.
    app.manager = SimpleNamespace(capabilities=capabilities.resolve(settings, config), providers=SimpleNamespace(available=lambda: []), busy_sessions=set)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    headers = {"X-Daedalus-Token": "tok"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        quiet = (await client.get("/api/capabilities", headers=headers)).json()
        assert quiet["restart_required"] is None and quiet["last_change"] is None

        selfdev._write_pending({"repo": "bot", "commit": "abc1234567", "summary": "A warmer greeting", "needs_image": False})
        loud = (await client.get("/api/capabilities", headers=headers)).json()
        assert loud["restart_required"]["commit"] == "abc1234567"
        assert loud["selfdev"]["mode"] == "local"

        assert (await client.post("/api/self/restart")).status_code == 401  # it restarts the installation; not for anyone
        answer = await client.post("/api/self/restart", headers=headers)
    assert answer.status_code == 200 and "restart" in answer.json()["result"]  # no supervisor here: it says so rather than pretending


def test_the_launcher_applies_a_change_through_the_supervisors_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """``daedalus self restart`` is what the desktop launcher's Apply runs inside the container.

    The button used to stop the containers and start them again, which comes back on whatever the
    checkout holds with nothing having checked it; this is the op the preflight is on.
    """
    from daedalus import __main__ as cli
    from daedalus import supervisor_client

    asked: list[tuple[str, dict[str, object]]] = []

    async def fake_call(socket_path: Path, op: str, **params: object) -> str:
        asked.append((op, params))
        return "checking the change and restarting"

    monkeypatch.setattr(supervisor_client, "call", fake_call)
    argv = ["--state-dir", str(tmp_path / "state"), "--workspaces-dir", str(tmp_path / "ws"), "self", "restart", "--reason", "the launcher's Apply"]
    assert cli.main(argv) == 0
    assert asked == [("restart", {"token_path": tmp_path / "state" / "supervisor.token", "reason": "the launcher's Apply"})]
    assert "checking the change and restarting" in capsys.readouterr().out

    # No supervisor to ask: it says so and fails, so the launcher knows to say what it is falling back to.
    async def unavailable(socket_path: Path, op: str, **params: object) -> str:
        raise supervisor_client.SupervisorUnavailable("no socket")

    monkeypatch.setattr(supervisor_client, "call", unavailable)
    assert cli.main(argv) == 2
