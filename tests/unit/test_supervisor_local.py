"""A restart that applies what the checkout already holds, and undoes it when it cannot boot.

Where a change lives only in the checkout there is no merged revision to pull and no reviewer who
saw it: the commit the agent left behind is preflighted on a detached copy of itself, and the bot is
stopped only once that passes. Three starts that die young put the last known-good commit back,
because at that point the app the operator would have used to undo it is the thing not coming up.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "launcher" / "supervisor.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("supervisor_local_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_local_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# -- the decisions, as pure functions ------------------------------------------------------


def test_the_mode_is_read_from_what_is_there_and_from_what_was_asked_for(tmp_path: Path) -> None:
    sup = _load()
    repo = tmp_path / "daedalus"
    (repo / ".git").mkdir(parents=True)
    assert sup.resolve_mode("auto", repo=repo, token="") == "local", "no remote and no token is a local install"

    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = https://example.invalid/x.git\n')
    assert sup.resolve_mode("auto", repo=repo, token="t") == "server"
    assert sup.resolve_mode("auto", repo=repo, token="  ") == "local", "a remote with nothing to authenticate with is not a server"
    assert sup.resolve_mode("auto", repo=tmp_path / "nothing", token="t") == "off"
    # The operator's word wins: the bot obeys it too, and the two must not disagree about the preflight.
    assert sup.resolve_mode("local", repo=repo, token="t") == "local"
    assert sup.resolve_mode("off", repo=repo, token="t") == "off"


def test_the_mode_the_bot_published_is_the_mode_the_supervisor_uses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rule sets for one decision is a change restarting with no preflight: the bot's answer wins.

    The supervisor's own probe is what a first boot has and nothing else — before the bot has ever
    run there is no published answer to read.
    """
    sup = _load()
    monkeypatch.setattr(sup, "PUBLISHED_CAPABILITIES", tmp_path / "capabilities.json")
    repo = tmp_path / "daedalus"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = https://example.invalid/x.git\n')

    assert sup.published_mode() == "", "nothing has been published yet"
    assert sup.resolve_mode("auto", repo=repo, token="t", published=sup.published_mode()) == "server"

    # The bot looked at the same installation and found no rebuild channel and no core remote.
    (tmp_path / "capabilities.json").write_text(json.dumps({"selfdev": {"mode": "local", "configured": "auto"}}))
    assert sup.published_mode() == "local"
    assert sup.resolve_mode("auto", repo=repo, token="t", published=sup.published_mode()) == "local"
    # The operator's word still wins over both, because the bot obeys it too.
    assert sup.resolve_mode("server", repo=repo, token="t", published="local") == "server"
    # Nonsense in the file is not a mode; the probe answers instead of the process refusing to start.
    (tmp_path / "capabilities.json").write_text("{not json")
    assert sup.published_mode() == ""
    (tmp_path / "capabilities.json").write_text(json.dumps({"selfdev": {"mode": "whatever"}}))
    assert sup.published_mode() == ""


def test_a_linked_worktree_is_not_read_as_a_checkout_without_a_remote(tmp_path: Path) -> None:
    """A worktree's .git is a file naming the main repository's; its remotes are the main one's."""
    sup = _load()
    main = tmp_path / "daedalus"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "config").write_text('[remote "origin"]\n\turl = https://example.invalid/x.git\n')
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    assert sup.has_origin(worktree) is True
    assert sup.has_origin(tmp_path / "nothing") is False


def test_only_the_image_files_ask_for_a_new_image() -> None:
    sup = _load()
    assert sup.needs_new_image({"deploy/Dockerfile"})
    assert sup.needs_new_image({"deploy/apt-packages.txt"})
    assert sup.needs_new_image({"launcher/supervisor.py"})
    assert not sup.needs_new_image({"daedalus/host/greeting.py", "tests/unit/test_greeting.py"})
    assert not sup.needs_new_image(set())


def test_the_virtualenv_is_synced_only_when_a_dependency_really_moved(tmp_path: Path) -> None:
    sup = _load()
    assert not sup.needs_dependency_sync({"daedalus/host/greeting.py"})
    assert sup.needs_dependency_sync({"uv.lock"})
    assert sup.needs_dependency_sync({"pyproject.toml"})
    # The core is a path dependency: what it declares is declared into the same environment, and a
    # requirement it adds never moves the bot's own lock.
    assert sup.needs_dependency_sync(set(), {"pyproject.toml"})
    assert not sup.needs_dependency_sync(set(), {"protocore/engine.py"})
    # And nothing else is a reason. The preflight has a virtualenv of its own and never borrows the
    # running bot's, so the state of that one can no longer ask for a sync of it.
    assert not sup.needs_dependency_sync(set())
    assert not sup.needs_dependency_sync(set(), set())


def test_the_preflight_never_writes_to_the_bots_own_virtualenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv`` writes to whatever UV_PROJECT_ENVIRONMENT names, and the image names the running bot's.

    Every command the preflight runs is given an environment that names the preflight's own instead:
    a check of a change that fails must leave the installation it checked exactly as it was.
    """
    sup = _load()
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/srv/venv")
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/venv")
    monkeypatch.setattr(sup, "PREFLIGHT_VENV", tmp_path / "preflight-venv")
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(sup, "run", lambda cmd, cwd=None, timeout=0, env=None: (seen.append(dict(env or {})), (0, ""))[1])
    monkeypatch.setattr(sup, "restore_owner", lambda repo: None)
    monkeypatch.setattr(sup.shutil, "which", lambda name: None)

    sup.preflight(tmp_path, sync=True)
    assert seen, "the preflight ran nothing"
    for env in seen:
        assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "preflight-venv")
        assert "VIRTUAL_ENV" not in env
    assert sup.bot_env()["UV_PROJECT_ENVIRONMENT"] == "/srv/venv", "the bot keeps the environment it runs from"


def test_the_environment_is_synced_when_the_checkout_asks_for_something_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The virtualenv outlives the image that seeded it, so it is checked against the checkout, not assumed."""
    sup = _load()
    repo = tmp_path / "daedalus"
    repo.mkdir()
    (repo / "uv.lock").write_text("version = 1\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'daedalus'\n")
    venv = tmp_path / "venv"
    venv.mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 1800, env: dict[str, str] | None = None) -> tuple[int, str]:
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(sup, "run", fake_run)

    # An environment with no stamp is one nothing vouches for: sync it and write down what it now holds.
    assert sup.sync_venv_if_stale(repo, venv=venv) is True
    assert calls == [["uv", "sync", "--frozen", "--inexact"]], "--inexact: what the image installed beside the lock is not this sync's to remove"
    assert (venv / ".daedalus-dependencies").read_text() == sup.dependency_digest(repo)

    # Stamped and unchanged: this is the released install's first start, and it runs no uv at all.
    calls.clear()
    assert sup.sync_venv_if_stale(repo, venv=venv) is False
    assert calls == []

    # A checkout that declares something else — a hand update, or a change the agent landed here.
    (repo / "uv.lock").write_text("version = 1\n# one more package\n")
    assert sup.sync_venv_if_stale(repo, venv=venv) is True
    assert calls == [["uv", "sync", "--frozen", "--inexact"]]

    # A sync that failed leaves no stamp behind: the next start tries again rather than believing it.
    calls.clear()
    (repo / "pyproject.toml").write_text("[project]\nname = 'daedalus'\nversion = '2'\n")
    monkeypatch.setattr(sup, "run", lambda cmd, **kw: (1, "resolution failed"))
    assert sup.sync_venv_if_stale(repo, venv=venv) is True
    assert (venv / ".daedalus-dependencies").read_text() != sup.dependency_digest(repo)


def test_the_boot_window_forgets_old_failures_and_ignores_impossible_ones() -> None:
    sup = _load()
    now = 10_000.0
    inside = [now - 100, now - 10, now]
    assert sup.unhealthy_boots([now - 5000, *inside], now) == inside
    # A record from the future is a clock step back, not a failed boot, and would never age out.
    assert sup.unhealthy_boots([now + 5000, now], now) == [now]


def test_the_preflight_skips_the_sync_when_it_is_not_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one sync a local change may cost happens inside the preflight, or not at all."""
    sup = _load()
    ran: list[list[str]] = []
    seeded = tmp_path / "preflight-venv"
    (seeded / "bin").mkdir(parents=True)
    (seeded / "bin" / "pytest").write_text("")  # seeded by an earlier preflight and kept between them
    monkeypatch.setattr(sup, "PREFLIGHT_VENV", seeded)
    monkeypatch.setattr(sup, "run", lambda cmd, cwd=None, timeout=0, env=None: (ran.append(cmd), (0, ""))[1])
    monkeypatch.setattr(sup, "restore_owner", lambda repo: None)
    monkeypatch.setattr(sup.shutil, "which", lambda name: None)

    sup.preflight(tmp_path, sync=False)
    assert not any("sync" in cmd for cmd in ran)
    assert ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "daedalus"] in ran
    ran.clear()
    sup.preflight(tmp_path, sync=True)
    assert [cmd for cmd in ran if "sync" in cmd] == [["uv", "sync", "--frozen", "--extra", "dev"]]

    # An environment that has never been seeded is filled whatever the caller asked for: there is
    # nothing there to run the checks in.
    ran.clear()
    monkeypatch.setattr(sup, "PREFLIGHT_VENV", tmp_path / "never-seeded")
    sup.preflight(tmp_path, sync=False)
    assert [cmd for cmd in ran if "sync" in cmd] == [["uv", "sync", "--frozen", "--extra", "dev"]]


# -- the restart ----------------------------------------------------------------------------


def _harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, preflight_ok: bool, changed: set[str] | None = None) -> tuple[Any, Any, list[str]]:
    sup = _load()
    calls: list[str] = []
    monkeypatch.setattr(sup, "BOT_REPO", tmp_path / "daedalus")
    monkeypatch.setattr(sup, "CORE_REPO", tmp_path / "protocore-exp")
    monkeypatch.setattr(sup, "CANDIDATE", tmp_path / "preflight")
    monkeypatch.setattr(sup, "FAILED", tmp_path / "good" / "FAILED")
    monkeypatch.setattr(sup, "SELFDEV_DIR", tmp_path / "selfdev")
    monkeypatch.setattr(sup, "APPLY_RESULT", tmp_path / "selfdev" / "result.json")
    monkeypatch.setattr(sup, "LOG", tmp_path / "supervisor.log")
    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setattr(sup, "VENV", venv)
    preflight_venv = tmp_path / "preflight-venv"
    (preflight_venv / "bin").mkdir(parents=True)
    (preflight_venv / "bin" / "pytest").write_text("")  # seeded by an earlier preflight, so only a dependency change syncs
    monkeypatch.setattr(sup, "PREFLIGHT_VENV", preflight_venv)
    monkeypatch.setattr(sup, "run", lambda cmd, cwd=None, timeout=0, env=None: (calls.append(f"run {' '.join(cmd)}"), (0, ""))[1])
    monkeypatch.setattr(sup, "load_history", lambda: [{"bot": "good000000", "core": "core000000", "at": "2026-09-17T00:00:00+00:00"}])
    monkeypatch.setattr(sup, "git", lambda repo, *args: (calls.append(f"git {repo.name} {' '.join(args)}"), (0, "new1111111\n"))[1])
    monkeypatch.setattr(sup, "head", lambda repo: "new1111111")
    monkeypatch.setattr(sup, "prepare_candidate", lambda repo, ref="origin/main": (calls.append(f"candidate {repo.name} {ref}"), (True, ""))[1])
    monkeypatch.setattr(sup, "preflight", lambda repo, sync=True: (calls.append(f"preflight {repo} sync={sync}"), (preflight_ok, "transcript"))[1])
    supervisor = sup.Supervisor()
    supervisor.configured = "local"
    supervisor.running_revision = "run0000000"
    supervisor._changed_files = lambda old, new, repo=sup.BOT_REPO: set(changed or {"daedalus/host/greeting.py"})  # type: ignore[method-assign]

    async def stop_child() -> None:
        calls.append("stop")

    supervisor.stop_child = stop_child  # type: ignore[method-assign]
    async def committed(bot: str, core: str, transcript: str) -> dict[str, str]:
        calls.append("drain committed")
        return {"state": "committed", "receipt_id": "saved"}

    supervisor._drain_before_stop = committed  # type: ignore[method-assign]
    return sup, supervisor, calls


async def test_a_passing_change_is_preflighted_on_a_copy_of_itself_and_then_applied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    await supervisor._apply_local("the operator asked the app to apply the change")
    assert "candidate daedalus new1111111" in calls, "the preflight must run on the local commit, not on a remote"
    order = [c for c in calls if c.startswith(("preflight", "stop"))]
    assert order[0].startswith("preflight") and order[1] == "stop"
    assert "sync=False" in order[0], "nothing declared a dependency, so nothing is synced"
    assert supervisor.restart_requested.is_set()
    result = sup.last_change()
    assert result["status"] == "applied" and result["commit"] == "new1111111"


async def test_a_failing_change_never_stops_the_bot_and_is_taken_back_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    await supervisor._apply_local("the operator asked the app to apply the change")
    assert "stop" not in calls
    assert not supervisor.restart_requested.is_set()
    assert "git daedalus reset --hard run0000000" in calls, "the refused commit was left in the running checkout"
    result = sup.last_change()
    assert result["status"] == "preflight_failed" and "not applied" in result["detail"]
    assert "the bot kept running" in sup.FAILED.read_text()


async def test_a_refusal_goes_back_to_what_is_running_not_to_the_last_known_good(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A revision becomes known-good only after two healthy minutes. A change refused before then must
    not take a change that is already running out with it."""
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    supervisor.running_revision = "applied000"  # applied a minute ago; good000000 is older
    await supervisor._apply_local("another change")
    assert "git daedalus reset --hard applied000" in calls
    assert not any("reset --hard good000000" in c for c in calls)
    assert "applied000" in sup.last_change()["detail"]


async def test_a_change_to_the_image_is_applied_without_asking_for_a_rebuild(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True, changed={"deploy/Dockerfile"})
    triggered: list[str] = []
    supervisor._request_image_rebuild = lambda: (triggered.append("asked"), True)[1]  # type: ignore[method-assign]
    await supervisor._apply_local("a new package")
    assert triggered == [], "a local installation has no rebuilder to ask"
    assert "needs a new image" not in sup.last_change()["detail"]
    assert "run an update" in sup.last_change()["detail"].lower()
    assert supervisor.restart_requested.is_set(), "the code part of the change still applies"


async def test_a_dependency_change_buys_exactly_one_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True, changed={"uv.lock", "pyproject.toml"})
    await supervisor._apply_local("a new dependency")
    assert [c for c in calls if c.startswith("preflight")] == [f"preflight {sup.candidate_dir(sup.BOT_REPO)} sync=True"]


async def test_the_rebuilder_is_never_asked_outside_server_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, _ = _harness(monkeypatch, tmp_path, preflight_ok=True)
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setattr(sup, "REBUILD_TRIGGER_DIR", trigger)
    (trigger / sup.REBUILDER_HEARTBEAT).write_text("")
    assert supervisor._request_image_rebuild() is False
    assert not (trigger / "rebuild").exists()
    supervisor.configured = "server"
    assert supervisor._request_image_rebuild() is True


async def test_the_trigger_is_not_written_where_no_rebuilder_collects_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The trigger directory is a volume mounted whether or not the sidecar behind it was started, so
    its existence is not the question; the heartbeat the sidecar keeps fresh is."""
    sup, supervisor, _ = _harness(monkeypatch, tmp_path, preflight_ok=True)
    supervisor.configured = "server"
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setattr(sup, "REBUILD_TRIGGER_DIR", trigger)
    assert supervisor._request_image_rebuild() is False, "an empty volume is not a rebuilder"
    assert not (trigger / "rebuild").exists()

    heartbeat = trigger / sup.REBUILDER_HEARTBEAT
    heartbeat.write_text("")
    stale = time.time() - sup.REBUILDER_HEARTBEAT_SECONDS - 60
    os.utime(heartbeat, (stale, stale))
    assert supervisor._request_image_rebuild() is False, "a heartbeat this old is from a sidecar that has stopped"

    heartbeat.write_text("")
    assert supervisor._request_image_rebuild() is True
    assert (trigger / "rebuild").exists()


async def test_a_restart_is_not_started_twice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, _ = _harness(monkeypatch, tmp_path, preflight_ok=True)
    async with supervisor.lock:
        assert "already running" in await supervisor.restart("again")


async def test_a_server_restart_just_goes_round_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With a remote the running revision was preflighted when it was merged; there is nothing to check."""
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    supervisor.configured = "server"
    assert await supervisor.restart("after a merge") == "checking the running work before restart"
    assert supervisor.restart_task is not None
    await supervisor.restart_task
    assert supervisor.restart_requested.is_set()
    assert not any(c.startswith("preflight") for c in calls)


async def test_known_good_records_the_revision_that_ran_not_what_is_in_the_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A local change lands in the checkout while the old process is still serving. Reading HEAD when the
    health timer fires would mark a revision known-good that has never started — and the rollback trusts
    this list."""
    sup = _load()
    monkeypatch.setattr(sup, "GOOD_DIR", tmp_path / "good")
    monkeypatch.setattr(sup, "HISTORY", tmp_path / "good" / "history.json")
    monkeypatch.setattr(sup, "LOG", tmp_path / "supervisor.log")
    monkeypatch.setattr(sup, "head", lambda repo: "checkout00")  # the agent's change, committed a moment ago
    sup.record_good("running000", "core000000")
    assert sup.load_history() == [{"bot": "running000", "core": "core000000", "at": sup.load_history()[0]["at"]}]


# -- the automatic rollback ------------------------------------------------------------------


async def test_three_boots_that_die_young_put_the_last_known_good_commit_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    assert await supervisor._note_failed_boot() is False
    assert await supervisor._note_failed_boot() is False
    assert not any("reset" in c for c in calls)
    assert await supervisor._note_failed_boot() is True

    assert "git daedalus reset --hard good000000" in calls
    assert "git protocore-exp reset --hard core000000" in calls
    result = sup.last_change()
    assert result["status"] == "rolled_back" and result["commit"] == "new1111111"
    assert "still in the checkout's history" in result["detail"]
    assert supervisor.failed_boots == [], "the count starts again from the revision that is running now"


async def test_a_refused_core_change_is_taken_out_of_the_core_checkout_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A change the agent applied to the core alone leaves the bot's HEAD where it was.

    Reverting only the bot would leave the refused core commit in the checkout for the next start —
    any start, for any reason — to pick up, with nothing having checked it.
    """
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=False)
    supervisor.running_revision = "new1111111"  # the bot is already on what is in its checkout
    supervisor.running_core = "core000000"  # the core is not: the agent moved it a moment ago
    await supervisor._apply_local("a change to the core")
    assert "git protocore-exp reset --hard core000000" in calls
    assert not any("git daedalus reset" in c for c in calls), "the bot checkout is where the running process is"
    assert "protocore-exp back to core000000" in sup.last_change()["detail"]


async def test_a_server_install_is_not_rolled_back_behind_the_operators_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    supervisor.configured = "server"
    for _ in range(5):
        assert await supervisor._note_failed_boot() is False
    assert not any("reset" in c for c in calls)


async def test_the_rollback_does_not_swap_between_two_revisions_that_both_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With A and B known good and a broken C: back to B, and when B cannot boot either, back to A —
    never to B again. Choosing "the newest that is not the one that just died" alone is a loop of
    three failed boots per swap, with no sleep between them."""
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    monkeypatch.setattr(
        sup,
        "load_history",
        lambda: [
            {"bot": "aaaaaaaaaa", "core": "core000000", "at": "2026-09-17T00:00:00+00:00"},
            {"bot": "bbbbbbbbbb", "core": "core000000", "at": "2026-09-17T01:00:00+00:00"},
        ],
    )
    heads = iter(["cccccccccc", "bbbbbbbbbb", "aaaaaaaaaa"])
    current = {"bot": "cccccccccc"}
    monkeypatch.setattr(sup, "head", lambda repo: current["bot"] if repo == sup.BOT_REPO else "core000000")

    for expected in ("bbbbbbbbbb", "aaaaaaaaaa"):
        calls.clear()
        current["bot"] = next(heads)
        for _ in range(sup.BOOT_THRESHOLD):
            await supervisor._note_failed_boot()
        assert f"git daedalus reset --hard {expected}" in calls

    # Both have now been tried and neither boots: it says so instead of going round again.
    calls.clear()
    current["bot"] = next(heads)
    for _ in range(sup.BOOT_THRESHOLD):
        await supervisor._note_failed_boot()
    assert not any("reset" in c for c in calls)
    assert sup.last_change()["status"] == "no_rollback"

    # A start that lives long enough clears the record: the next failure may go back to either again.
    supervisor.tried_revisions.clear()
    calls.clear()
    current["bot"] = "cccccccccc"
    for _ in range(sup.BOOT_THRESHOLD):
        await supervisor._note_failed_boot()
    assert "git daedalus reset --hard bbbbbbbbbb" in calls


async def test_a_first_revision_that_cannot_boot_says_so_rather_than_guessing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup, supervisor, calls = _harness(monkeypatch, tmp_path, preflight_ok=True)
    monkeypatch.setattr(sup, "load_history", lambda: [{"bot": "new1111111", "core": "core000000", "at": "2026-09-17T00:00:00+00:00"}])
    for _ in range(3):
        await supervisor._note_failed_boot()
    assert not any("reset" in c for c in calls)
    assert sup.last_change()["status"] == "no_rollback"


def test_the_sync_holds_the_environment_to_the_image_extras(monkeypatch) -> None:
    sup = _load()
    monkeypatch.setenv("DAEDALUS_VENV_EXTRAS", "speech,browser")
    assert sup.sync_argv() == ["uv", "sync", "--frozen", "--inexact", "--extra", "speech", "--extra", "browser"]
    monkeypatch.setenv("DAEDALUS_VENV_EXTRAS", "")
    assert sup.sync_argv() == ["uv", "sync", "--frozen", "--inexact"]


def test_a_new_extra_on_the_image_stales_the_stamp(tmp_path: Path, monkeypatch) -> None:
    sup = _load()
    (tmp_path / "pyproject.toml").write_text("x")
    monkeypatch.setenv("DAEDALUS_VENV_EXTRAS", "")
    before = sup.dependency_digest(tmp_path)
    monkeypatch.setenv("DAEDALUS_VENV_EXTRAS", "speech")
    assert sup.dependency_digest(tmp_path) != before
